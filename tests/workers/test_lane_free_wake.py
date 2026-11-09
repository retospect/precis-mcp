"""A finishing ``run_slice`` job wakes the oldest job parked for its lane
(gr611299): the park's 10-min ``LANE_FULL_BACKOFF`` timer is only the
fallback, so a freed slot is re-claimed within a wake/claim cycle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from precis.plans import lanes
from precis.store.store import Store
from precis.store.types import Tag
from precis.workers import wake_runner
from precis.workers.executors._common import current_status, set_status

pytestmark = pytest.mark.db


def _job(
    store: Store,
    *,
    status: str,
    lane: str | None = None,
    lane_wait: list[str] | None = None,
    wake_in: timedelta | None = None,
    why: str = f"{lanes.LANE_PARK_WHY}run_slice: lane full",
    job_type: str = "run_slice",
) -> int:
    meta: dict[str, Any] = {"executor": "claude_inproc", "job_type": job_type}
    if lane is not None:
        meta["lane"] = lane
    if lane_wait is not None:
        meta["lane_wait"] = lane_wait
    if wake_in is not None:
        ts = int((datetime.now(UTC) + wake_in).timestamp())
        meta["wake_when"] = {"kind": "at_time", "payload": {"ts": ts, "why": why}}
    job = store.insert_ref(kind="job", slug=None, title="run_slice", meta=meta)
    store.add_tag(
        job.id, Tag.closed("STATUS", status), set_by="system", replace_prefix=True
    )
    return int(job.id)


def _parked(store: Store, lane: str, *, wake_in: timedelta | None = None) -> int:
    return _job(
        store,
        status="waiting_time",
        lane_wait=[lane],
        wake_in=wake_in if wake_in is not None else timedelta(minutes=10),
    )


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return dict(row[0] or {})


def _ts(store: Store, ref_id: int) -> int:
    return int(_meta(store, ref_id)["wake_when"]["payload"]["ts"])


def _status(store: Store, ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        return current_status(conn, ref_id)


def _is_due(store: Store, ref_id: int) -> bool:
    return _ts(store, ref_id) <= int(datetime.now(UTC).timestamp())


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "cancelled"])
def test_finish_wakes_only_the_oldest_parked_job(store: Store, terminal: str) -> None:
    running = _job(store, status="running", lane="devin")
    oldest = _parked(store, "devin")
    newer = _parked(store, "devin")
    newer_ts = _ts(store, newer)

    set_status(store, running, terminal)

    assert _is_due(store, oldest)
    assert _meta(store, oldest)["lane_woken"] is True
    assert _ts(store, newer) == newer_ts
    assert "lane_woken" not in _meta(store, newer)
    # Only the wake instant moved: still parked until the wake_runner pass.
    assert _status(store, oldest) == "waiting_time"

    # The wake_runner re-queues the woken job and leaves the other parked.
    wake_runner.run_wake_pass(store)
    assert _status(store, oldest) == "queued"
    assert _status(store, newer) == "waiting_time"


def test_each_finish_wakes_the_next_oldest(store: Store) -> None:
    first = _job(store, status="running", lane="devin")
    second = _job(store, status="running", lane="devin")
    a = _parked(store, "devin")
    b = _parked(store, "devin")
    c = _parked(store, "devin")

    set_status(store, first, "succeeded")
    set_status(store, second, "failed")

    assert _is_due(store, a) and _is_due(store, b)
    assert not _is_due(store, c)


def test_second_terminal_write_on_a_finished_job_wakes_nothing(store: Store) -> None:
    """Idempotent: a finished job holds no slot, so a repeat terminal
    write (failed after succeeded, a reaper racing a finalize) frees
    nothing and wakes no second waiter."""
    running = _job(store, status="running", lane="devin")
    a = _parked(store, "devin")
    b = _parked(store, "devin")

    set_status(store, running, "succeeded")
    set_status(store, running, "failed")

    assert _is_due(store, a)
    assert not _is_due(store, b)


def test_no_parked_job_is_a_no_op(store: Store) -> None:
    running = _job(store, status="running", lane="devin")
    bystander = _job(store, status="queued")
    set_status(store, running, "succeeded")
    assert _status(store, running) == "succeeded"
    assert _status(store, bystander) == "queued"
    with store.pool.connection() as conn:
        assert lanes.wake_lane_waiter(conn, "devin") is None


def test_other_lanes_stay_parked(store: Store) -> None:
    running = _job(store, status="running", lane="devin")
    other = _parked(store, "codex")
    both = _job(
        store,
        status="waiting_time",
        lane_wait=["codex", "devin"],
        wake_in=timedelta(minutes=10),
    )
    other_ts = _ts(store, other)

    set_status(store, running, "succeeded")

    assert _ts(store, other) == other_ts
    assert "lane_woken" not in _meta(store, other)
    # A job waiting on several lanes wakes on any one of them.
    assert _is_due(store, both)


def test_only_waiting_run_slice_jobs_with_a_future_wake_are_picked(
    store: Store,
) -> None:
    running = _job(store, status="running", lane="devin")
    # Same lane_wait but queued (already re-queued), or a different job type.
    queued = _job(store, status="queued", lane_wait=["devin"])
    foreign = _job(
        store,
        status="waiting_time",
        lane_wait=["devin"],
        wake_in=timedelta(minutes=10),
        job_type="draft_refresh",
    )
    foreign_ts = _ts(store, foreign)

    set_status(store, running, "succeeded")

    assert _status(store, queued) == "queued"
    assert _ts(store, foreign) == foreign_ts


def test_non_run_slice_terminal_write_wakes_nothing(store: Store) -> None:
    other = _job(store, status="running", lane="devin", job_type="draft_refresh")
    parked = _parked(store, "devin")
    parked_ts = _ts(store, parked)
    set_status(store, other, "succeeded")
    assert _ts(store, parked) == parked_ts


def test_quota_parked_job_with_stale_lane_wait_is_not_woken(store: Store) -> None:
    """A job that left its lane park, ran, then parked on a quota window
    keeps no claim on a lane's freed slot: the wake requires the lane-park
    marker in ``wake_when.payload.why``, not just a stale ``meta.lane_wait``."""
    running = _job(store, status="running", lane="devin")
    quota = _job(
        store,
        status="waiting_time",
        lane="devin",
        lane_wait=["devin"],
        wake_in=timedelta(hours=2),
        why="claude quota window — parked",
    )
    quota_ts = _ts(store, quota)

    set_status(store, running, "succeeded")

    assert _ts(store, quota) == quota_ts
    assert "lane_woken" not in _meta(store, quota)
    with store.pool.connection() as conn:
        assert lanes.wake_lane_waiter(conn, "devin") is None


def test_wake_lane_waiter_returns_the_woken_id(store: Store) -> None:
    a = _parked(store, "devin")
    _parked(store, "devin")
    with store.pool.connection() as conn:
        assert lanes.wake_lane_waiter(conn, "devin") == a
        conn.commit()
    # Idempotent: the woken job is due, so it is not picked again.
    with store.pool.connection() as conn:
        assert lanes.wake_lane_waiter(conn, "devin") != a
