"""Rung 6e-2 — wiring the weave tick into the ``quest_tick`` coordinator.

Unit tests (no DB), mirroring ``tests/test_quest_tick_job.py``'s harness:
stubs ``run_quest_tick`` / ``weave_tick`` / the SQL helpers so the routing
decision (``meta.quest_body == "weave"`` → the weave body, else the default
catalyst body) is exercised without a real store. ``mark_weave_quest``'s own
round-trip is tested against a tiny in-memory fake store.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from precis.quest.weave_tick import (
    QUEST_BODY_META_KEY,
    QUEST_BODY_WEAVE,
    mark_weave_quest,
)
from precis.workers.executors._yield import Done, Yield
from precis.workers.job_types import quest_tick as qt

_QUEST_ID = 164903


@pytest.fixture(autouse=True)
def _default_active_quest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qt, "active_quest_ids", lambda store: [_QUEST_ID])


class _Outcome:
    """Minimal catalyst-tick stand-in — only used to prove it was NOT called
    in the weave-routing tests, or that it WAS in the catalyst-routing ones."""

    def __init__(self) -> None:
        self.status = "succeeded"
        self.note = "ok"
        self.candidates_created = 0
        self.sims_dispatched = 0
        self.results_harvested = 0
        self.graduated = 0
        self.searches_run = 0
        self.papers_linked = 0
        self.logbook_added = 0
        self.dossier_rewritten = False
        self.proposals = 0
        self.ledger_added = 0


class FakeCtx:
    def __init__(self, meta: dict[str, Any], *, cancel: bool = False) -> None:
        self.store = SimpleNamespace()
        self.ref_id = 700
        self.title = "quest_tick"
        self.meta = meta
        self.chunks: list[tuple[str, str]] = []
        self._cancel = cancel

    def append_chunk(self, kind: str, text: str) -> None:
        self.chunks.append((kind, text))

    def is_cancel_requested(self) -> bool:
        return self._cancel


def _meta(state: dict[str, Any] | None = None, *, tier: str = "big") -> dict[str, Any]:
    m: dict[str, Any] = {
        "job_type": "quest_tick",
        "executor": "coordinator",
        "params": {"quest_id": _QUEST_ID, "tier": tier},
    }
    if state is not None:
        m["coordinator_state"] = state
    return m


def _stub_catalyst_tick(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _fake(store: Any, quest_id: int, **kw: Any) -> _Outcome:
        calls.append({"quest_id": quest_id, **kw})
        return _Outcome()

    monkeypatch.setattr("precis.quest.tick.run_quest_tick", _fake)
    return calls


def _stub_weave_tick(
    monkeypatch: pytest.MonkeyPatch, result: dict[str, Any]
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _fake(store: Any, client: Any, quest_id: int, **kw: Any) -> dict[str, Any]:
        calls.append({"quest_id": quest_id, "client": client, **kw})
        return result

    monkeypatch.setattr("precis.quest.weave_tick.weave_tick", _fake)
    return calls


def _stub_pending(monkeypatch: pytest.MonkeyPatch, values: list[list[int]]) -> None:
    seq = list(values)

    def _fake(store: Any, quest_id: int) -> list[int]:
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(qt, "_pending_sim_ids", _fake)


def _stub_queued(monkeypatch: pytest.MonkeyPatch, n: int) -> None:
    monkeypatch.setattr(qt, "_queued_sim_count", lambda store: n)


def _weave_quest_ref(meta: dict[str, Any] | None = None) -> SimpleNamespace:
    m = {QUEST_BODY_META_KEY: QUEST_BODY_WEAVE}
    if meta:
        m.update(meta)
    return SimpleNamespace(deleted_at=None, meta=m)


class TestWeaveRouting:
    def test_marked_quest_routes_to_weave_tick_not_catalyst(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        catalyst_calls = _stub_catalyst_tick(monkeypatch)
        weave_calls = _stub_weave_tick(
            monkeypatch,
            {"ok": True, "woven": [{"ok": True}], "new_sections": [], "batch_size": 1},
        )
        _stub_queued(monkeypatch, 0)
        _stub_pending(monkeypatch, [[]])

        ctx = FakeCtx(_meta())
        ctx.store.get_ref = lambda *, kind, id: _weave_quest_ref()

        out = qt._dispatch(ctx, qt.SPEC)

        assert isinstance(out, Yield)
        assert len(weave_calls) == 1
        assert weave_calls[0]["quest_id"] == _QUEST_ID
        assert catalyst_calls == []

    def test_unmarked_quest_routes_to_catalyst_not_weave(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        catalyst_calls = _stub_catalyst_tick(monkeypatch)
        weave_calls = _stub_weave_tick(monkeypatch, {"ok": True, "woven": []})
        _stub_queued(monkeypatch, 0)
        _stub_pending(monkeypatch, [[], [811]])

        # No get_ref stubbed at all — mirrors most existing quest_tick tests;
        # `_quest_body` must degrade to "not weave" rather than raising.
        out = qt._dispatch(FakeCtx(_meta()), qt.SPEC)

        assert isinstance(out, Yield)
        assert len(catalyst_calls) == 1
        assert weave_calls == []

    def test_quest_body_other_value_also_routes_to_catalyst(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        catalyst_calls = _stub_catalyst_tick(monkeypatch)
        weave_calls = _stub_weave_tick(monkeypatch, {"ok": True, "woven": []})
        _stub_queued(monkeypatch, 0)
        _stub_pending(monkeypatch, [[], [811]])

        ctx = FakeCtx(_meta())
        ctx.store.get_ref = lambda *, kind, id: SimpleNamespace(
            deleted_at=None, meta={QUEST_BODY_META_KEY: "something-else"}
        )

        out = qt._dispatch(ctx, qt.SPEC)

        assert isinstance(out, Yield)
        assert len(catalyst_calls) == 1
        assert weave_calls == []


class TestWeaveTickOutcomes:
    def test_productive_weave_tick_yields_await_heartbeat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_weave_tick(
            monkeypatch,
            {
                "ok": True,
                "woven": [{"ok": True}],
                "new_sections": [{"title": "x"}],
                "batch_size": 2,
            },
        )
        _stub_queued(monkeypatch, 0)
        _stub_pending(monkeypatch, [[]])

        ctx = FakeCtx(_meta())
        ctx.store.get_ref = lambda *, kind, id: _weave_quest_ref()

        out = qt._dispatch(ctx, qt.SPEC)

        assert isinstance(out, Yield)
        assert out.state["phase"] == "await"
        assert out.state["child_job_ids"] == []
        assert "punt_ticks" not in out.state
        assert "tick_failures" not in out.state
        assert out.wake_when.kind == "at_time"

    def test_empty_weave_tick_backs_off_as_punt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_weave_tick(
            monkeypatch,
            {
                "ok": True,
                "woven": [],
                "new_sections": [],
                "note": "nothing unintegrated",
            },
        )
        _stub_queued(monkeypatch, 0)
        _stub_pending(monkeypatch, [[]])

        ctx = FakeCtx(_meta())
        ctx.store.get_ref = lambda *, kind, id: _weave_quest_ref()

        out = qt._dispatch(ctx, qt.SPEC)

        assert isinstance(out, Yield)
        assert out.state["phase"] == "await"
        assert out.state["punt_ticks"] == 1
        assert out.wake_when.kind == "at_time"

    def test_weave_punt_rests_after_max_punt_ticks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_weave_tick(monkeypatch, {"ok": True, "woven": [], "new_sections": []})
        _stub_queued(monkeypatch, 0)
        _stub_pending(monkeypatch, [[]])

        ctx = FakeCtx(
            _meta(
                {
                    "phase": "tick",
                    "slice_count": 9,
                    "punt_ticks": qt._max_punt_ticks() - 1,
                }
            )
        )
        ctx.store.get_ref = lambda *, kind, id: _weave_quest_ref()

        out = qt._dispatch(ctx, qt.SPEC)

        assert isinstance(out, Done)
        assert out.success is True
        assert out.summary_meta.get("punt_ticks") == qt._max_punt_ticks()

    def test_no_dossier_error_backs_off_without_crashing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_weave_tick(monkeypatch, {"ok": False, "error": "no_dossier"})
        _stub_queued(monkeypatch, 0)
        _stub_pending(monkeypatch, [[]])

        ctx = FakeCtx(_meta())
        ctx.store.get_ref = lambda *, kind, id: _weave_quest_ref()

        out = qt._dispatch(ctx, qt.SPEC)

        assert isinstance(out, Yield)
        assert out.state["phase"] == "await"
        assert out.state["tick_failures"] == 1
        assert out.wake_when.kind == "at_time"
        assert any("weave error" in text for _kind, text in ctx.chunks)

    def test_no_topics_error_backs_off_without_crashing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_weave_tick(monkeypatch, {"ok": False, "error": "no_topics", "did": 42})
        _stub_queued(monkeypatch, 0)
        _stub_pending(monkeypatch, [[]])

        ctx = FakeCtx(_meta())
        ctx.store.get_ref = lambda *, kind, id: _weave_quest_ref()

        out = qt._dispatch(ctx, qt.SPEC)

        assert isinstance(out, Yield)
        assert out.state["tick_failures"] == 1

    def test_weave_error_rests_after_max_tick_failures(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_weave_tick(monkeypatch, {"ok": False, "error": "no_dossier"})
        _stub_queued(monkeypatch, 0)
        _stub_pending(monkeypatch, [[]])

        ctx = FakeCtx(
            _meta(
                {
                    "phase": "tick",
                    "slice_count": 9,
                    "tick_failures": qt._max_tick_failures() - 1,
                }
            )
        )
        ctx.store.get_ref = lambda *, kind, id: _weave_quest_ref()

        out = qt._dispatch(ctx, qt.SPEC)

        assert isinstance(out, Done)
        assert out.success is False
        assert out.summary_meta.get("tick_failures") == qt._max_tick_failures()

    def test_weave_tick_raising_is_treated_as_a_failed_tick(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(store: Any, client: Any, quest_id: int, **kw: Any) -> Any:
            raise RuntimeError("dossier query blew up")

        monkeypatch.setattr("precis.quest.weave_tick.weave_tick", _boom)
        _stub_queued(monkeypatch, 0)
        _stub_pending(monkeypatch, [[]])

        ctx = FakeCtx(_meta())
        ctx.store.get_ref = lambda *, kind, id: _weave_quest_ref()

        out = qt._dispatch(ctx, qt.SPEC)

        assert isinstance(out, Yield)  # did not raise / crash the coordinator
        assert out.state["tick_failures"] == 1


class TestMarkWeaveQuest:
    class _FakeStore:
        def __init__(self, ref_id: int, meta: dict[str, Any]) -> None:
            self._refs = {ref_id: dict(meta)}

        def stamp_ref_meta(self, ref_id: int, updates: dict[str, Any]) -> None:
            self._refs.setdefault(ref_id, {}).update(updates)

        def get_ref(self, *, kind: str, id: int) -> SimpleNamespace:
            return SimpleNamespace(meta=self._refs.get(id, {}))

    def test_mark_weave_quest_stamps_meta_key(self) -> None:
        store = self._FakeStore(164903, {"seed_key": "some_dossier_quest"})

        mark_weave_quest(store, 164903)

        ref = store.get_ref(kind="quest", id=164903)
        assert ref.meta[QUEST_BODY_META_KEY] == QUEST_BODY_WEAVE
        # Existing meta is preserved (shallow-merge, not overwrite).
        assert ref.meta["seed_key"] == "some_dossier_quest"
