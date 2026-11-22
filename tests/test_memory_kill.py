"""The memory-kill requeue policy (gr595538): a job killed for memory is
requeued once with its footprint estimate bumped + a ``killed:memory``
flag; a second kill parks it (``STATUS:parked``, reason memory) and mints
one ``memory_triage`` job. Nothing here counts toward poison_guard's
attempt cap and a stale outcome stamp never retriggers.

DB-backed: the policy reads and rewrites ``refs.meta``/tags atomically.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from precis.dispatch import Hub
from precis.handlers.job import JobHandler
from precis.store import Store
from precis.store.types import Tag
from precis.utils.container_limits import KILLED_FOR_MEMORY
from precis.workers import wake_runner
from precis.workers.executors import _common


def _mk_job(store: Store, *, status: str = "running", **meta: Any) -> int:
    started = datetime.now(UTC) - timedelta(seconds=60)
    base: dict[str, Any] = {
        "executor": "ssh_node",
        "job_type": "struct_relax",
        "started_at": started.isoformat(),
        "lease_host": "node-a",
        "lease_until": (datetime.now(UTC) + timedelta(seconds=120)).isoformat(),
    }
    base.update(meta)
    ref = store.insert_ref(kind="job", slug=None, title="mk test", meta=base)
    store.add_tag(ref.id, Tag.parse_strict(f"STATUS:{status}"), set_by="agent")
    return int(ref.id)


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return dict(row[0] or {})


def _status(store: Store, ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        return _common.current_status(conn, ref_id)


def _has_open_tag(store: Store, ref_id: int, value: str) -> bool:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = %s AND t.namespace = 'OPEN' AND t.value = %s",
            (ref_id, value),
        ).fetchone()
    return row is not None


def _triage_job(store: Store, ref_id: int) -> int | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'job' AND retired_at IS NULL "
            "AND meta->>'idem_key' = %s",
            (f"memory_triage:{ref_id}",),
        ).fetchone()
    return int(row[0]) if row else None


def test_no_outcome_stamp_is_a_no_op(store: Store) -> None:
    ref_id = _mk_job(store)
    assert _common.handle_memory_kill(store, ref_id) is False
    assert _status(store, ref_id) == "running"


def test_stale_outcome_after_success_is_a_no_op(store: Store) -> None:
    # The outcome stamp persists in meta across a requeue; a job that
    # later failed for an unrelated reason must not retrigger the policy.
    ref_id = _mk_job(store, status="failed", outcome=KILLED_FOR_MEMORY)
    assert _common.handle_memory_kill(store, ref_id) is False
    assert _status(store, ref_id) == "failed"


def test_first_kill_requeues_with_bumped_estimate(store: Store) -> None:
    ref_id = _mk_job(store, outcome=KILLED_FOR_MEMORY, footprint_req=2048)
    assert _common.handle_memory_kill(store, ref_id) is True

    assert _status(store, ref_id) == "queued"
    assert _has_open_tag(store, ref_id, "killed:memory")
    meta = _meta(store, ref_id)
    assert meta["footprint_req"] == 3072  # ceil(2048 × 1.5)
    assert "lease_until" not in meta
    assert "outcome" not in meta  # popped — cannot retrigger
    history = meta["killed_memory"]
    assert len(history) == 1
    assert history[0]["bumped_req_mb"] == 3072


def test_first_kill_without_footprint_uses_attempt_cap(store: Store) -> None:
    ref_id = _mk_job(store, outcome=KILLED_FOR_MEMORY)
    assert _common.handle_memory_kill(store, ref_id, cap_mb=1024) is True
    assert _status(store, ref_id) == "queued"
    assert _has_open_tag(store, ref_id, "killed:memory")
    meta = _meta(store, ref_id)
    assert meta["footprint_req"] == 1536
    assert len(meta["killed_memory"]) == 1


def test_second_kill_parks_and_mints_one_triage_job(store: Store) -> None:
    ref_id = _mk_job(store, outcome=KILLED_FOR_MEMORY, footprint_req=2048)
    assert _common.handle_memory_kill(store, ref_id) is True
    # The retry dies again: the kill site re-stamps the outcome.
    parent = store.insert_ref(kind="todo", slug=None, title="parent", meta={})
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET parent_id = %s WHERE ref_id = %s", (parent.id, ref_id)
        )
        _common.set_meta(conn, ref_id, outcome=KILLED_FOR_MEMORY)
        conn.commit()
    assert _common.handle_memory_kill(store, ref_id) is True

    assert _status(store, ref_id) == "parked"
    meta = _meta(store, ref_id)
    assert len(meta["killed_memory"]) == 2
    assert "outcome" not in meta

    triage = _triage_job(store, ref_id)
    assert triage is not None
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta, parent_id FROM refs WHERE ref_id = %s", (triage,)
        ).fetchone()
    assert row is not None
    assert row[1] is None
    tmeta = dict(row[0])
    assert tmeta["job_type"] == "memory_triage"
    assert tmeta["params"]["job_id"] == ref_id
    assert _status(store, triage) == "queued"

    # A third kill re-parks but reuses the idem — never two triage jobs.
    with store.pool.connection() as conn:
        _common.set_meta(conn, ref_id, outcome=KILLED_FOR_MEMORY)
        conn.commit()
    assert _common.handle_memory_kill(store, ref_id) is True
    assert _status(store, ref_id) == "parked"
    assert _triage_job(store, ref_id) == triage


def test_requeue_does_not_count_as_an_attempt(store: Store) -> None:
    ref_id = _mk_job(store, outcome=KILLED_FOR_MEMORY, footprint_req=1024)
    assert _common.handle_memory_kill(store, ref_id) is True
    meta = _meta(store, ref_id)
    assert "attempts" not in meta or meta["attempts"] == 0


def test_parked_is_in_waiting_not_terminal(store: Store) -> None:
    assert _common.PARKED in _common.WAITING
    assert _common.PARKED not in _common.TERMINAL
    assert _common.PARKED in _common.LEAVE_RUNNING
    # The closed vocabulary admits the new status value.
    Tag.parse_strict("STATUS:parked")


def test_memory_park_honors_operator_cancel(store: Store) -> None:
    ref_id = _mk_job(
        store, status="parked", park_reason="memory", killed_memory=[{}, {}]
    )
    JobHandler(hub=Hub(store=store)).tag(id=ref_id, add=["STATUS:cancel_requested"])
    wake_runner.run_wake_pass(store)
    assert _status(store, ref_id) == "cancelled"
