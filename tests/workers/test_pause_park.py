"""``ctx.defer`` / ``park_job_until`` — a window-scoped LLM pause parks a
non-coordinator (``claude_inproc``) job instead of failing it (gr482938).

Covers the whole deferral loop end to end on a real DB: park writes
``STATUS:waiting_time`` + ``meta.wake_when``; the executor's happy-path
finalize leaves the parked row alone; the wake_runner's ``at_time``
selector re-queues it when the instant passes (executor-agnostic — the
re-queued row is claimable by ``claude_inproc`` again); and the
``agent_jobs_completing`` watchdog never counts a parked job as a
failure.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from precis.store.store import Store
from precis.store.types import Tag
from precis.workers import wake_runner
from precis.workers.executors import claude_inproc as ci
from precis.workers.executors._common import (
    MAX_PAUSE_PARKS,
    claim_executor_jobs,
    current_status,
    park_job_until,
)
from precis.workers.health_digest import _check_agent_jobs_completing

pytestmark = pytest.mark.db


def _mk_job(store: Store, *, executor: str = "claude_inproc") -> int:
    """A claimed-shape job row: STATUS:running + executor meta."""
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="draft_refresh (nt:dc1)",
        meta={
            "executor": executor,
            "job_type": "draft_refresh",
            "params": {"draft": "nt", "scope": "dc1"},
        },
    )
    store.add_tag(
        job.id,
        Tag.closed("STATUS", "running"),
        set_by="system",
        replace_prefix=True,
    )
    return int(job.id)


def _status(store: Store, ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        return current_status(conn, ref_id)


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return dict(row[0] or {})


def test_ctx_defer_parks_and_wake_requeues(store: Store) -> None:
    """The full loop: ctx.defer parks; finalize leaves it; the wake
    re-queues it claimable (lease cleared) when the instant passes."""
    job_id = _mk_job(store)
    # A live lease like the claim stamps — the re-queue must drop it so
    # the woken job is a clean fresh-work claim.
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || "
            "jsonb_build_object('lease_until', "
            " (now() + interval '90 minutes')::text) WHERE ref_id = %s",
            (job_id,),
        )
        conn.commit()

    meta = _meta(store, job_id)
    ctx = ci._build_dispatch_context(store, job_id, "draft_refresh", meta)
    wake_at = datetime.now(UTC) + timedelta(hours=5)
    assert ctx.defer is not None
    assert ctx.defer(wake_at, "taper pause — parked") is True

    assert _status(store, job_id) == "waiting_time"
    meta = _meta(store, job_id)
    assert meta["wake_when"]["kind"] == "at_time"
    assert int(meta["wake_when"]["payload"]["ts"]) == int(wake_at.timestamp())
    assert meta["pause_parks"] == 1

    # The happy-path finalize must NOT stamp succeeded over a parked job
    # (gr482938's deferral would otherwise be stranded as a fake success).
    ci._finalize_plugin_dispatch(store, job_id)
    assert _status(store, job_id) == "waiting_time"

    # Pull the wake instant into the past and run a wake pass: the job
    # goes back to queued with its stale lease cleared.
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = jsonb_set(meta, "
            "'{wake_when,payload,ts}', '1') WHERE ref_id = %s",
            (job_id,),
        )
        conn.commit()
    out = wake_runner.run_wake_pass(store)
    assert out["ok"] >= 1
    assert _status(store, job_id) == "queued"
    assert "lease_until" not in _meta(store, job_id)

    # …and the claude_inproc lane can claim it again as fresh work.
    with store.pool.connection() as conn:
        rows = claim_executor_jobs(conn, executor="claude_inproc", limit=4)
        conn.commit()
    assert [r[0] for r in rows] == [job_id]


def test_park_cap_fails_open(store: Store) -> None:
    """A job parked MAX_PAUSE_PARKS times stops deferring — the caller
    fails the pause normally rather than sleeping forever."""
    job_id = _mk_job(store)
    with store.pool.connection() as conn:
        for i in range(MAX_PAUSE_PARKS):
            assert park_job_until(store, conn, job_id, None, f"park {i + 1}"), (
                f"park {i + 1} should succeed"
            )
        assert park_job_until(store, conn, job_id, None, "park cap+1") is False
        conn.commit()
    assert _meta(store, job_id)["pause_parks"] == MAX_PAUSE_PARKS


def test_defer_none_wake_at_uses_default_backoff(store: Store) -> None:
    """``wake_at=None`` parks for PAUSE_BACKOFF (~2h), the quota
    wording's own fallback horizon."""
    job_id = _mk_job(store)
    before = datetime.now(UTC)
    with store.pool.connection() as conn:
        assert park_job_until(store, conn, job_id, None, "pause") is True
        conn.commit()
    ts = int(_meta(store, job_id)["wake_when"]["payload"]["ts"])
    assert (
        before + timedelta(hours=1, minutes=59)
        <= datetime.fromtimestamp(ts, UTC)
        <= datetime.now(UTC) + timedelta(hours=2, seconds=30)
    )


def test_watchdog_ignores_parked_jobs(store: Store) -> None:
    """``_check_agent_jobs_completing`` counts only terminal STATUS
    writes — a parked (waiting_time) job is a deliberate pause, not a
    failure: it contributes nothing to the all-failing tally."""
    parked_id = _mk_job(store)
    with store.pool.connection() as conn:
        assert park_job_until(store, conn, parked_id, None, "pause") is True
        conn.commit()

    with store.pool.connection() as conn:
        result = _check_agent_jobs_completing(conn)
        conn.commit()
    assert result.status == "ok"
    assert "none completed" in result.detail

    # A real failure next to the park still counts — and counts as ONE,
    # not two (the parked job is invisible to the tally).
    failed_id = _mk_job(store)
    store.add_tag(
        failed_id,
        Tag.closed("STATUS", "failed"),
        set_by="system",
        replace_prefix=True,
    )
    with store.pool.connection() as conn:
        result = _check_agent_jobs_completing(conn)
        conn.commit()
    assert result.status == "stale"
    assert "1 completed" in result.detail


def test_watchdog_counts_succeeded_normally(store: Store) -> None:
    """Baseline: a succeeded + parked mix is a healthy tally."""
    parked_id = _mk_job(store)
    ok_id = _mk_job(store)
    with store.pool.connection() as conn:
        assert park_job_until(store, conn, parked_id, None, "pause") is True
        conn.commit()
    store.add_tag(
        ok_id, Tag.closed("STATUS", "succeeded"), set_by="system", replace_prefix=True
    )
    with store.pool.connection() as conn:
        result = _check_agent_jobs_completing(conn)
        conn.commit()
    assert result.status == "ok"
    assert "1 ok / 0 failed" in result.detail
