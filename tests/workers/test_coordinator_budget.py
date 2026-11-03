"""Coordinator pass budgets and just-in-time claims on the test DB (gr551639)."""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any

import pytest

from precis.store import Store, Tag
from precis.workers.executors import coordinator
from precis.workers.executors._context import DispatchContext
from precis.workers.executors._yield import Done, WakeWhen, Yield
from precis.workers.job_types import JobTypeSpec
from precis.workers.wake_runner import run_wake_pass


def _job(store: Store, *, steps: int = 1) -> int:
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="budget test",
        meta={
            "executor": "coordinator",
            "job_type": "coordinator_budget_test",
            "params": {"steps": steps},
        },
    )
    store.add_tag(ref.id, Tag.closed("STATUS", "queued"), set_by="system")
    return ref.id


def _snapshot(store: Store, ids: list[int]) -> dict[int, tuple[str, dict[str, Any]]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id, t.value, r.meta FROM refs r "
            "JOIN ref_tags rt USING (ref_id) JOIN tags t USING (tag_id) "
            "WHERE r.ref_id = ANY(%s) AND t.namespace = 'STATUS'",
            (ids,),
        ).fetchall()
    return {rid: (status, dict(meta)) for rid, status, meta in rows}


@pytest.fixture
def pass_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [0.0]
    monkeypatch.setenv("PRECIS_COORDINATOR_PASS_BUDGET_S", "5")
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(coordinator, "_LeaseKeepalive", lambda *a: nullcontext())
    return now


def _dispatch_stub(
    monkeypatch: pytest.MonkeyPatch,
    dispatch: Callable[[DispatchContext, JobTypeSpec], Done | Yield],
) -> None:
    spec = JobTypeSpec(
        name="coordinator_budget_test",
        params_schema={"type": "object"},
        compatible_executors=frozenset({"coordinator"}),
        requires=frozenset(),
        description="Test coordinator slices",
        run=lambda **kw: None,
        dispatch=dispatch,
    )
    monkeypatch.setattr(coordinator, "get_job_type", lambda name: spec)


def test_budget_stops_after_crossing_slice_without_claiming_waiters(
    store: Store, monkeypatch: pytest.MonkeyPatch, pass_clock: list[float]
) -> None:
    """A 5s pass finishes three 2s slices, never pre-starts the next three."""
    ids = [_job(store) for _ in range(6)]
    ran: list[int] = []
    deadlines: list[float | None] = []

    def dispatch(ctx: DispatchContext, spec: JobTypeSpec) -> Done:
        snapshot = _snapshot(store, ids)
        assert [rid for rid, (s, _) in snapshot.items() if s == "running"] == [
            ctx.ref_id
        ]
        for rid in ids:
            if rid not in ran and rid != ctx.ref_id:
                status, meta = snapshot[rid]
                assert status == "queued"
                assert "lease_until" not in meta
                assert "started_at" not in meta
                assert "lease_host" not in meta
        ran.append(ctx.ref_id)
        deadlines.append(ctx.pass_deadline)
        pass_clock[0] += 2
        return Done(summary="slice finished")

    _dispatch_stub(monkeypatch, dispatch)
    out = coordinator.run_coordinator_pass(store, limit=6)

    assert out == {"claimed": 3, "ok": 3, "failed": 0}
    assert ran == ids[:3]
    assert deadlines == [5.0] * 3
    assert pass_clock[0] == 6.0
    snapshot = _snapshot(store, ids)
    assert [snapshot[rid][0] for rid in ids] == ["succeeded"] * 3 + ["queued"] * 3
    for rid in ids[3:]:
        assert "lease_until" not in snapshot[rid][1]
        assert "started_at" not in snapshot[rid][1]

    out = coordinator.run_coordinator_pass(store, limit=6)

    assert out == {"claimed": 3, "ok": 3, "failed": 0}
    assert ran == ids
    assert deadlines == [5.0] * 3 + [11.0] * 3
    assert all(s == "succeeded" for s, _ in _snapshot(store, ids).values())


def test_budget_checkpoint_resumes_after_wake_without_repeating_units(
    store: Store, monkeypatch: pytest.MonkeyPatch, pass_clock: list[float]
) -> None:
    """Complete each unit's writes before yielding; resume the persisted cursor."""
    long_job = _job(store, steps=5)
    next_job = _job(store)
    ids = [long_job, next_job]
    starts: list[tuple[int, int]] = []
    units: list[tuple[int, int]] = []

    def dispatch(ctx: DispatchContext, spec: JobTypeSpec) -> Done | Yield:
        snapshot = _snapshot(store, ids)
        assert sum(s == "running" for s, _ in snapshot.values()) == 1
        idx = ctx.meta.get("coordinator_state", {}).get("idx", 0)
        starts.append((ctx.ref_id, idx))
        while idx < ctx.meta["params"]["steps"]:
            if ctx.budget_exhausted():
                return Yield(
                    state={"idx": idx},
                    wake_when=WakeWhen("at_time", {"ts": 0}),
                )
            pass_clock[0] += 2
            units.append((ctx.ref_id, idx))
            ctx.append_chunk("job_event", f"finished unit {idx}")
            idx += 1
        return Done(summary="all units finished", summary_meta={"finished": idx})

    _dispatch_stub(monkeypatch, dispatch)
    out = coordinator.run_coordinator_pass(store)

    assert out == {"claimed": 1, "ok": 1, "failed": 0}
    assert pass_clock[0] == 6.0
    snapshot = _snapshot(store, ids)
    assert snapshot[long_job][0] == "waiting_time"
    assert snapshot[long_job][1]["coordinator_state"] == {"idx": 3}
    assert "lease_until" not in snapshot[long_job][1]
    assert snapshot[next_job][0] == "queued"
    assert "started_at" not in snapshot[next_job][1]
    assert "lease_until" not in snapshot[next_job][1]

    assert run_wake_pass(store)["ok"] == 1
    assert _snapshot(store, ids)[long_job][0] == "queued"
    out = coordinator.run_coordinator_pass(store)

    assert out == {"claimed": 2, "ok": 2, "failed": 0}
    assert starts == [(long_job, 0), (next_job, 0), (long_job, 3)]
    assert [idx for rid, idx in units if rid == long_job] == list(range(5))
    snapshot = _snapshot(store, ids)
    assert all(s == "succeeded" for s, _ in snapshot.values())
    assert snapshot[long_job][1]["finished"] == 5
    events = store.chunks.list_chunks_for_ref(long_job)
    assert [
        c.text
        for c in events
        if c.chunk_kind == "job_event" and c.text.startswith("finished unit ")
    ] == [f"finished unit {idx}" for idx in range(5)]
