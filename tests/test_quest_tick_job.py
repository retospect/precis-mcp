"""``quest_tick`` coordinator job_type — phase-machine unit tests.

Stubs ``run_quest_tick`` and the two SQL helpers (``_pending_sim_ids`` /
``_queued_sim_count``) so the coordinator's scheduling logic is tested without a
DB: the tick→await→tick cycle, the Yield/Done shapes + wake payloads, the
per-quest backpressure, and the node-load starvation gate.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.workers.executors._yield import Done, Yield
from precis.workers.job_types import quest_tick as qt


class _Outcome:
    def __init__(self, status: str = "succeeded", note: str = "ok") -> None:
        self.status = status
        self.note = note
        self.candidates_created = 0
        self.sims_dispatched = 0
        self.results_harvested = 0
        self.graduated = 0
        self.searches_run = 0
        self.papers_linked = 0


class FakeCtx:
    def __init__(self, meta: dict[str, Any], *, cancel: bool = False) -> None:
        self.store = object()  # helpers are stubbed, so a real store isn't needed
        self.ref_id = 700
        self.title = "quest_tick"
        self.meta = meta
        self.chunks: list[tuple[str, str]] = []
        self._cancel = cancel

    def append_chunk(self, kind: str, text: str) -> None:
        self.chunks.append((kind, text))

    def set_status(self, value: str) -> None:  # pragma: no cover - unused here
        pass

    def set_meta(self, **fields: Any) -> None:  # pragma: no cover - unused here
        pass

    def record_failure(self, reason: str) -> None:  # pragma: no cover
        pass

    def is_cancel_requested(self) -> bool:
        return self._cancel


def _meta(
    state: dict[str, Any] | None = None, *, tier: str = "local-big"
) -> dict[str, Any]:
    m: dict[str, Any] = {
        "job_type": "quest_tick",
        "executor": "coordinator",
        "params": {"quest_id": 164903, "tier": tier},
    }
    if state is not None:
        m["coordinator_state"] = state
    return m


def _stub_tick(
    monkeypatch: pytest.MonkeyPatch, outcome: _Outcome
) -> list[dict[str, Any]]:
    """Patch run_quest_tick; return a list capturing each call's kwargs."""
    calls: list[dict[str, Any]] = []

    def _fake(store: Any, quest_id: int, **kw: Any) -> _Outcome:
        calls.append({"quest_id": quest_id, **kw})
        return outcome

    monkeypatch.setattr("precis.quest.tick.run_quest_tick", _fake)
    return calls


def _stub_pending(monkeypatch: pytest.MonkeyPatch, values: list[list[int]]) -> None:
    """Patch _pending_sim_ids to return successive values (last repeats)."""
    seq = list(values)

    def _fake(store: Any, quest_id: int) -> list[int]:
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(qt, "_pending_sim_ids", _fake)


def _stub_queued(monkeypatch: pytest.MonkeyPatch, n: int) -> None:
    monkeypatch.setattr(qt, "_queued_sim_count", lambda store: n)


class TestPhaseTick:
    def test_first_tick_dispatches_then_yields_await(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _stub_tick(monkeypatch, _Outcome())
        _stub_queued(monkeypatch, 0)
        # idle before the tick, two sims in flight after it
        _stub_pending(monkeypatch, [[], [811, 812]])
        out = qt._dispatch(FakeCtx(_meta()), qt.SPEC)
        assert isinstance(out, Yield)
        assert out.state["phase"] == "await"
        assert out.state["child_job_ids"] == [811, 812]
        assert out.wake_when.kind == "at_time"
        assert "ts" in out.wake_when.payload
        assert len(calls) == 1 and calls[0]["compute"] is True
        assert calls[0]["tier"] == "local-big"

    def test_nothing_dispatched_is_done(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_tick(monkeypatch, _Outcome(status="paused", note="graduated"))
        _stub_queued(monkeypatch, 0)
        _stub_pending(monkeypatch, [[]])  # idle before AND after → nothing dispatched
        out = qt._dispatch(FakeCtx(_meta()), qt.SPEC)
        assert isinstance(out, Done)
        assert out.success is True
        assert out.summary_meta.get("last_status") == "paused"

    def test_starvation_gate_defers_without_ticking(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _stub_tick(monkeypatch, _Outcome())
        _stub_pending(monkeypatch, [[]])  # this quest idle...
        _stub_queued(monkeypatch, qt._max_queued_sims())  # ...but node queue full
        out = qt._dispatch(FakeCtx(_meta()), qt.SPEC)
        assert isinstance(out, Yield)
        assert out.state["phase"] == "await"
        assert out.wake_when.kind == "at_time"
        assert calls == []  # did NOT run a tick / dispatch a batch

    def test_backpressure_waits_when_sims_in_flight(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _stub_tick(monkeypatch, _Outcome())
        _stub_queued(monkeypatch, 0)
        _stub_pending(monkeypatch, [[901]])  # already in flight → don't propose more
        out = qt._dispatch(FakeCtx(_meta()), qt.SPEC)
        assert isinstance(out, Yield)
        assert out.state["child_job_ids"] == [901]
        assert calls == []


class TestPhaseAwait:
    def test_still_pending_reyields_heartbeat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_pending(monkeypatch, [[811, 812]])
        state = {"phase": "await", "child_job_ids": [811, 812], "slice_count": 1}
        out = qt._dispatch(FakeCtx(_meta(state)), qt.SPEC)
        assert isinstance(out, Yield)
        assert out.state["phase"] == "await"
        assert out.wake_when.kind == "at_time"

    def test_all_done_ticks_again_and_dispatches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _stub_tick(monkeypatch, _Outcome())
        _stub_queued(monkeypatch, 0)
        # await sees empty → tick → (backpressure recheck empty) → tick runs →
        # new sims in flight
        _stub_pending(monkeypatch, [[], [], [821, 822]])
        state = {"phase": "await", "child_job_ids": [811], "slice_count": 2}
        out = qt._dispatch(FakeCtx(_meta(state)), qt.SPEC)
        assert isinstance(out, Yield)
        assert out.state["child_job_ids"] == [821, 822]
        assert out.state["slice_count"] == 3  # incremented from prior
        assert len(calls) == 1


class TestCancel:
    def test_cancel_is_terminal(self) -> None:
        out = qt._dispatch(FakeCtx(_meta(), cancel=True), qt.SPEC)
        assert isinstance(out, Done)
        assert out.success is False
        assert out.summary_meta.get("cancelled") is True


class TestRegistration:
    def test_registered_and_coordinator_only(self) -> None:
        from precis.workers.job_types import get_job_type, known_job_types

        assert "quest_tick" in known_job_types()
        spec = get_job_type("quest_tick")
        assert spec is not None
        assert spec.compatible_executors == frozenset({"coordinator"})
        assert spec.dispatch is not None
        with pytest.raises(NotImplementedError):
            spec.run()
