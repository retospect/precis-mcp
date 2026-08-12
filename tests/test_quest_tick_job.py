"""``quest_tick`` coordinator job_type — phase-machine unit tests.

Stubs ``run_quest_tick`` and the two SQL helpers (``_pending_sim_ids`` /
``_queued_sim_count``) so the coordinator's scheduling logic is tested without a
DB: the tick→await→tick cycle, the Yield/Done shapes + wake payloads, the
per-quest backpressure, the node-load starvation gate, RC2's active-quest
self-rest gate, and the punt-vs-genuine-dry budget split.

The autouse ``_default_active_quest`` fixture stubs ``active_quest_ids`` to
report quest 164903 (the default ``_meta()`` quest id) active, so every test
that doesn't care about RC2 keeps exercising the tick/await phase machine as
before; ``TestRC2SelfRest`` overrides it to drive the self-rest gate itself.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from precis.workers.executors._yield import Done, Yield
from precis.workers.job_types import quest_tick as qt

_QUEST_ID = 164903


@pytest.fixture(autouse=True)
def _default_active_quest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qt, "active_quest_ids", lambda store: [_QUEST_ID])


class _Outcome:
    def __init__(
        self,
        status: str = "succeeded",
        note: str = "ok",
        *,
        searches_run: int = 0,
        logbook_added: int = 0,
        dossier_rewritten: bool = False,
        proposals: int = 0,
        ledger_added: int = 0,
    ) -> None:
        self.status = status
        self.note = note
        self.candidates_created = 0
        self.sims_dispatched = 0
        self.results_harvested = 0
        self.graduated = 0
        self.searches_run = searches_run
        self.papers_linked = 0
        self.logbook_added = logbook_added
        self.dossier_rewritten = dossier_rewritten
        self.proposals = proposals
        self.ledger_added = ledger_added


class FakeCtx:
    def __init__(self, meta: dict[str, Any], *, cancel: bool = False) -> None:
        # helpers are stubbed, so a real store isn't needed — but Hub(store=...)
        # (built for the acquiring search_fn) sets `store.hint_bus`, so a bare
        # `object()` (no __dict__) would blow up; SimpleNamespace accepts it.
        self.store = SimpleNamespace()
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


def _meta(state: dict[str, Any] | None = None, *, tier: str = "big") -> dict[str, Any]:
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
        assert calls[0]["tier"] == "big"

    def test_empty_punt_backs_off_and_retries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A successful tick that produced NOTHING substantive (no logbook,
        # no dossier rewrite, no proposals, no ledger entries) and dispatched
        # nothing is a *punt* — not evidence the quest is out of ideas. It
        # backs off and retries, bumping punt_ticks (not dry_ticks).
        _stub_tick(monkeypatch, _Outcome(status="succeeded", note="graduated"))
        _stub_queued(monkeypatch, 0)
        _stub_pending(monkeypatch, [[]])  # idle before AND after → nothing dispatched
        out = qt._dispatch(FakeCtx(_meta()), qt.SPEC)
        assert isinstance(out, Yield)
        assert out.state["phase"] == "await"
        assert out.state["punt_ticks"] == 1
        assert "dry_ticks" not in out.state
        assert out.state["child_job_ids"] == []
        assert out.wake_when.kind == "at_time"

    def test_punt_tick_rests_after_max_punt_ticks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_tick(monkeypatch, _Outcome(status="succeeded", note="graduated"))
        _stub_queued(monkeypatch, 0)
        _stub_pending(monkeypatch, [[]])
        state = {
            "phase": "tick",
            "slice_count": 9,
            "punt_ticks": qt._max_punt_ticks() - 1,
        }
        out = qt._dispatch(FakeCtx(_meta(state)), qt.SPEC)
        assert isinstance(out, Done)
        assert out.success is True
        assert out.summary_meta.get("punt_ticks") == qt._max_punt_ticks()
        assert out.summary_meta.get("last_status") == "succeeded"

    def test_engaged_but_nothing_new_backs_off_as_genuine_dry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The model DID engage (wrote a logbook entry) but dispatched no new
        # sims — real evidence of exhaustion, so it bumps dry_ticks (the
        # small budget), not punt_ticks.
        _stub_tick(
            monkeypatch,
            _Outcome(status="succeeded", note="graduated", logbook_added=1),
        )
        _stub_queued(monkeypatch, 0)
        _stub_pending(monkeypatch, [[]])
        out = qt._dispatch(FakeCtx(_meta()), qt.SPEC)
        assert isinstance(out, Yield)
        assert out.state["phase"] == "await"
        assert out.state["dry_ticks"] == 1
        assert "punt_ticks" not in out.state
        assert out.state["child_job_ids"] == []
        assert out.wake_when.kind == "at_time"

    def test_dry_tick_rests_after_max_dry_ticks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_tick(
            monkeypatch,
            _Outcome(status="succeeded", note="graduated", dossier_rewritten=True),
        )
        _stub_queued(monkeypatch, 0)
        _stub_pending(monkeypatch, [[]])
        state = {
            "phase": "tick",
            "slice_count": 9,
            "dry_ticks": qt._max_dry_ticks() - 1,
        }
        out = qt._dispatch(FakeCtx(_meta(state)), qt.SPEC)
        assert isinstance(out, Done)
        assert out.success is True
        assert out.summary_meta.get("dry_ticks") == qt._max_dry_ticks()
        assert out.summary_meta.get("last_status") == "succeeded"
        # gr170252: the rest is stamped with WHY, so reconcile_quest_loops can
        # apply the cooldown-symmetry fix instead of re-minting immediately.
        # FakeCtx's store is a bare SimpleNamespace (no get_ref/update_ref), so
        # ``_register_dry_rest`` degrades to 0 rather than crashing the tick.
        assert out.summary_meta.get("rest_reason") == "dry"
        assert out.summary_meta.get("consecutive_dry_rests") == 0

    def test_productive_tick_after_dry_resets_counter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A tick that DOES dispatch sims rebuilds state fresh (no dry_ticks /
        # punt_ticks carried), so the await hop resets both counters to 0.
        _stub_tick(monkeypatch, _Outcome())
        _stub_queued(monkeypatch, 0)
        _stub_pending(monkeypatch, [[], [901]])
        state = {"phase": "tick", "slice_count": 4, "dry_ticks": 2, "punt_ticks": 3}
        out = qt._dispatch(FakeCtx(_meta(state)), qt.SPEC)
        assert isinstance(out, Yield)
        assert "punt_ticks" not in out.state
        assert out.state["child_job_ids"] == [901]
        assert "dry_ticks" not in out.state


class TestPhaseAwaitDryTicks:
    def test_await_hop_carries_dry_ticks_into_next_tick(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Mirrors how tick_failures is carried across the await→tick hop
        # (_phase_await): a dry_ticks=1 in the await state should surface as
        # dry_ticks=2 if the next (engaged, nothing-new) tick is dry again
        # (still below the default budget of 3, so it yields rather than
        # resting).
        _stub_tick(
            monkeypatch,
            _Outcome(status="succeeded", note="graduated", logbook_added=1),
        )
        _stub_queued(monkeypatch, 0)
        _stub_pending(monkeypatch, [[]])
        state = {
            "phase": "await",
            "child_job_ids": [],
            "slice_count": 4,
            "dry_ticks": 1,
        }
        out = qt._dispatch(FakeCtx(_meta(state)), qt.SPEC)
        assert isinstance(out, Yield)
        assert out.state["dry_ticks"] == 2

    def test_await_hop_carries_punt_ticks_into_next_tick(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same cross-hop carry, on the punt_ticks budget: a punt_ticks=1 in
        # the await state surfaces as punt_ticks=2 if the next tick is an
        # empty punt again.
        _stub_tick(monkeypatch, _Outcome(status="succeeded", note="graduated"))
        _stub_queued(monkeypatch, 0)
        _stub_pending(monkeypatch, [[]])
        state = {
            "phase": "await",
            "child_job_ids": [],
            "slice_count": 4,
            "punt_ticks": 1,
        }
        out = qt._dispatch(FakeCtx(_meta(state)), qt.SPEC)
        assert isinstance(out, Yield)
        assert out.state["punt_ticks"] == 2

    def test_failed_tick_backs_off_and_retries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A transient LLM error (e.g. endpoint 400) must NOT end the loop — it
        # re-yields on a heartbeat and bumps the consecutive-failure counter.
        _stub_tick(monkeypatch, _Outcome(status="failed", note="llm error: 400"))
        _stub_queued(monkeypatch, 0)
        _stub_pending(monkeypatch, [[]])
        out = qt._dispatch(FakeCtx(_meta()), qt.SPEC)
        assert isinstance(out, Yield)
        assert out.state["phase"] == "await"
        assert out.state["tick_failures"] == 1
        assert out.state["child_job_ids"] == []
        assert out.wake_when.kind == "at_time"

    def test_paused_tick_retries_without_counting_a_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A breaker/quota pause is a wait-for-window, not a failure: it retries
        # but does not consume the give-up budget.
        _stub_tick(monkeypatch, _Outcome(status="paused", note="paused: cap"))
        _stub_queued(monkeypatch, 0)
        _stub_pending(monkeypatch, [[]])
        state = {"phase": "tick", "slice_count": 3, "tick_failures": 2}
        out = qt._dispatch(FakeCtx(_meta(state)), qt.SPEC)
        assert isinstance(out, Yield)
        assert out.state["tick_failures"] == 2  # unchanged by a pause

    def test_failed_tick_rests_after_max_failures(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_tick(monkeypatch, _Outcome(status="failed", note="llm error: 400"))
        _stub_queued(monkeypatch, 0)
        _stub_pending(monkeypatch, [[]])
        state = {
            "phase": "tick",
            "slice_count": 9,
            "tick_failures": qt._max_tick_failures() - 1,
        }
        out = qt._dispatch(FakeCtx(_meta(state)), qt.SPEC)
        assert isinstance(out, Done)
        assert out.success is False
        assert out.summary_meta.get("tick_failures") == qt._max_tick_failures()
        assert out.summary_meta.get("last_status") == "failed"

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

    def test_starvation_defer_preserves_giveup_budgets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A defer (node queue full) is NOT a tick outcome, so it must not reset
        # the consecutive-dry / consecutive-punt / consecutive-failed budgets
        # — else recurring defers under multi-quest load would zero the
        # streak and an out-of-ideas quest could never reach its rest
        # condition.
        calls = _stub_tick(monkeypatch, _Outcome())
        _stub_pending(monkeypatch, [[]])  # this quest idle...
        _stub_queued(monkeypatch, qt._max_queued_sims())  # ...but node queue full
        state = {
            "phase": "tick",
            "slice_count": 3,
            "dry_ticks": 2,
            "punt_ticks": 4,
            "tick_failures": 1,
        }
        out = qt._dispatch(FakeCtx(_meta(state)), qt.SPEC)
        assert isinstance(out, Yield)
        assert calls == []  # deferred, no tick ran
        assert out.state["dry_ticks"] == 2  # preserved, not reset to 0
        assert out.state["punt_ticks"] == 4  # preserved, not reset to 0
        assert out.state["tick_failures"] == 1  # preserved, not reset to 0

    def test_backpressure_defer_preserves_giveup_budgets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same invariant on the defensive in-flight-sims backpressure path.
        calls = _stub_tick(monkeypatch, _Outcome())
        _stub_queued(monkeypatch, 0)
        _stub_pending(monkeypatch, [[901]])  # sims already in flight → defer
        state = {
            "phase": "tick",
            "slice_count": 3,
            "dry_ticks": 2,
            "punt_ticks": 4,
            "tick_failures": 1,
        }
        out = qt._dispatch(FakeCtx(_meta(state)), qt.SPEC)
        assert isinstance(out, Yield)
        assert calls == []
        assert out.state["dry_ticks"] == 2
        assert out.state["punt_ticks"] == 4
        assert out.state["tick_failures"] == 1


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


class TestRC2SelfRest:
    """RC2: a loop whose quest is no longer active self-rests at the top of
    ``_dispatch`` — before routing to await/tick, on any phase — via the
    same ``active_quest_ids`` notion the reconciler uses."""

    def test_active_quest_routes_to_a_tick_as_before(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The autouse fixture already reports _QUEST_ID active; a normal
        # tick still runs (RC2 didn't change the happy path).
        calls = _stub_tick(monkeypatch, _Outcome())
        _stub_queued(monkeypatch, 0)
        _stub_pending(monkeypatch, [[], [811]])
        out = qt._dispatch(FakeCtx(_meta()), qt.SPEC)
        assert isinstance(out, Yield)
        assert len(calls) == 1

    def test_inactive_quest_self_rests_from_tick_phase(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(qt, "active_quest_ids", lambda store: [])
        calls = _stub_tick(monkeypatch, _Outcome())
        ctx = FakeCtx(_meta())
        ctx.store.get_ref = lambda *, kind, id: SimpleNamespace(
            deleted_at=None, meta={}
        )
        ctx.store.tags_for = lambda ref_id: [
            SimpleNamespace(namespace="STATUS", value="dormant")
        ]
        out = qt._dispatch(ctx, qt.SPEC)
        assert isinstance(out, Done)
        assert out.success is True
        assert out.summary_meta.get("self_rested") is True
        assert out.summary_meta.get("quest_status") == "dormant"
        assert calls == []  # did NOT run a tick

    def test_inactive_quest_self_rests_from_await_phase(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An *awaiting* loop also rests on the next heartbeat, not only once
        # its dry-tick budget winds down — this is the whole point of RC2.
        monkeypatch.setattr(qt, "active_quest_ids", lambda store: [])
        pending_calls: list[Any] = []

        def _fake_pending(store: Any, quest_id: int) -> list[int]:
            pending_calls.append(quest_id)
            return [811]

        monkeypatch.setattr(qt, "_pending_sim_ids", _fake_pending)
        ctx = FakeCtx(_meta({"phase": "await", "child_job_ids": [811]}))
        ctx.store.get_ref = lambda *, kind, id: SimpleNamespace(
            deleted_at=None, meta={}
        )
        ctx.store.tags_for = lambda ref_id: [
            SimpleNamespace(namespace="STATUS", value="abandoned")
        ]
        out = qt._dispatch(ctx, qt.SPEC)
        assert isinstance(out, Done)
        assert out.summary_meta.get("self_rested") is True
        assert out.summary_meta.get("quest_status") == "abandoned"
        assert pending_calls == []  # never even re-checked the sim wait set

    def test_deleted_quest_reports_deleted_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(qt, "active_quest_ids", lambda store: [])
        ctx = FakeCtx(_meta())
        ctx.store.get_ref = lambda *, kind, id: None
        out = qt._dispatch(ctx, qt.SPEC)
        assert isinstance(out, Done)
        assert out.summary_meta.get("quest_status") == "deleted"


class TestFallbackLitSearch:
    """Guaranteed-acquisition fallback: a tick that ran zero `searches` of its
    own still fires one directly, so the loop asks the literature for
    something new every slice — not only when the model happens to."""

    def _fake_quest_ref(self) -> SimpleNamespace:
        return SimpleNamespace(
            title="NO to NH3 on Pd catalyst quest",
            meta={"reaction_config": {"substrate": "NO", "target": "NH3"}},
        )

    def test_zero_searches_run_triggers_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_tick(monkeypatch, _Outcome(searches_run=0))
        _stub_queued(monkeypatch, 0)
        _stub_pending(monkeypatch, [[]])  # idle before and after -> dry Yield

        ctx = FakeCtx(_meta())
        ctx.store.get_ref = lambda *, kind, id: self._fake_quest_ref()

        calls: list[dict[str, Any]] = []

        def _fake_run_search_step(
            store: Any, quest_id: int, queries: list[str], **kw: Any
        ) -> Any:
            calls.append({"quest_id": quest_id, "queries": queries, **kw})

        monkeypatch.setattr(
            "precis.quest.search.run_search_step", _fake_run_search_step
        )

        out = qt._dispatch(ctx, qt.SPEC)

        # A single dry tick backs off and retries rather than resting — the
        # fallback fires regardless (it runs before the dry-budget check).
        assert isinstance(out, Yield)
        assert len(calls) == 1
        assert calls[0]["quest_id"] == 164903
        assert calls[0]["queries"]  # non-empty query list
        assert any("fallback lit-search" in text for _kind, text in ctx.chunks)

    def test_nonzero_searches_run_skips_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_tick(monkeypatch, _Outcome(searches_run=2))
        _stub_queued(monkeypatch, 0)
        _stub_pending(monkeypatch, [[]])

        ctx = FakeCtx(_meta())
        ctx.store.get_ref = lambda *, kind, id: self._fake_quest_ref()

        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "precis.quest.search.run_search_step",
            lambda *a, **kw: calls.append(kw),
        )

        out = qt._dispatch(ctx, qt.SPEC)

        assert isinstance(out, Yield)
        assert calls == []
        assert not any("fallback lit-search" in text for _kind, text in ctx.chunks)

    def test_fallback_failure_never_fails_the_slice(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even if the fallback machinery blows up, the slice still completes."""
        _stub_tick(monkeypatch, _Outcome(searches_run=0))
        _stub_queued(monkeypatch, 0)
        _stub_pending(monkeypatch, [[]])

        ctx = FakeCtx(_meta())
        ctx.store.get_ref = lambda *, kind, id: self._fake_quest_ref()

        def _boom(*a: Any, **kw: Any) -> Any:
            raise RuntimeError("acquire pipeline down")

        monkeypatch.setattr("precis.quest.search.run_search_step", _boom)

        out = qt._dispatch(ctx, qt.SPEC)
        assert isinstance(out, Yield)  # did not raise

    def test_force_acquire_false_skips_fallback_even_when_quiet(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PRECIS_QUEST_FORCE_ACQUIRE", "false")
        _stub_tick(monkeypatch, _Outcome(searches_run=0))
        _stub_queued(monkeypatch, 0)
        _stub_pending(monkeypatch, [[]])

        ctx = FakeCtx(_meta())
        ctx.store.get_ref = lambda *, kind, id: self._fake_quest_ref()

        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "precis.quest.search.run_search_step",
            lambda *a, **kw: calls.append(kw),
        )

        out = qt._dispatch(ctx, qt.SPEC)

        assert isinstance(out, Yield)
        assert calls == []
        assert not any("fallback lit-search" in text for _kind, text in ctx.chunks)

    def test_fallback_query_rotates_by_slice_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Consecutive quiet slices should explore different facets, not
        repeat the same query — the rotation is keyed on `slice_count`."""
        ctx = FakeCtx(_meta())
        ctx.store.get_ref = lambda *, kind, id: self._fake_quest_ref()

        q1 = qt._fallback_queries(ctx.store, 164903, 1)
        q2 = qt._fallback_queries(ctx.store, 164903, 2)
        q3 = qt._fallback_queries(ctx.store, 164903, 3)

        assert len(q1) == 1 and len(q2) == 1 and len(q3) == 1
        assert len({q1[0], q2[0], q3[0]}) == 3  # all distinct facets
        # same slice_count -> same facet (deterministic, not random)
        assert qt._fallback_queries(ctx.store, 164903, 1) == q1

    def test_fallback_queries_empty_when_no_topic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = FakeCtx(_meta())
        ctx.store.get_ref = lambda *, kind, id: SimpleNamespace(title="", meta={})
        assert qt._fallback_queries(ctx.store, 164903, 1) == []


class TestRegistration:
    def test_registered_and_coordinator_only(self) -> None:
        from precis.workers.job_types import get_job_type, known_job_types

        assert "quest_tick" in known_job_types()
        spec = get_job_type("quest_tick")
        assert spec is not None
        assert spec.compatible_executors == frozenset({"coordinator"})
        assert spec.dispatch is not None
        assert spec.run is None


class TestSimWaitSetReachesBarrierFanout:
    """Regression (the 238-seed runaway): the barrier fan-out parents
    ``autocatpath_seed`` three levels below the candidate
    (seed_job → seed_todo → agg_todo → candidate), so the per-quest
    backpressure query must walk the whole subtree, not a single hop — and the
    job type must be in the wait set at all. Before the fix the wait set listed
    only ``autocatpath_explore``/``struct_relax`` and joined a single parent
    hop, so it was blind to the queued seeds and the loop proposed a fresh batch
    every slice.

    Unlike the phase-machine tests above (which STUB ``_pending_sim_ids`` /
    ``_queued_sim_count`` — the very reason this disconnect slipped through),
    these exercise the REAL SQL helpers against the ``store`` fixture.
    """

    def _serving_candidate(self, store: Any) -> tuple[int, int]:
        """A quest and a candidate ``structure`` that ``serves`` it."""
        q = store.insert_ref(kind="quest", slug=None, title="cat quest", meta={})
        cand = store.insert_ref(
            kind="structure", slug=f"cand-{q.id}", title="cand", meta={}
        )
        store.add_link(src_ref_id=cand.id, dst_ref_id=q.id, relation="serves")
        return int(q.id), int(cand.id)

    def _nested_seed_job(
        self, store: Any, cand_id: int, *, status: str | None = None
    ) -> int:
        """The real 3-hop shape ``dispatch_autocatpath`` mints:
        candidate → agg_todo → seed_todo → ``autocatpath_seed`` job. An untagged
        job COALESCEs to ``queued`` (non-terminal)."""
        agg = store.insert_ref(
            kind="todo",
            slug=None,
            title="autocatpath aggregate",
            meta={"executor": "ssh_node", "job_type": "autocatpath_aggregate"},
            parent_id=cand_id,
        )
        seed_todo = store.insert_ref(
            kind="todo",
            slug=None,
            title="autocatpath seed",
            meta={"auto_check": {"type": "child_job_succeeded"}},
            parent_id=agg.id,
        )
        job = store.insert_ref(
            kind="job",
            slug=None,
            title="autocatpath_seed",
            meta={"job_type": "autocatpath_seed"},
            parent_id=seed_todo.id,
        )
        if status is not None:
            from precis.store import Tag

            store.add_tag(job.id, Tag.closed("STATUS", status), set_by="system")
        return int(job.id)

    def test_queued_nested_seed_is_in_wait_set(self, store: Any) -> None:
        qid, cand = self._serving_candidate(store)
        seed = self._nested_seed_job(store, cand)  # untagged → queued
        assert seed in qt._pending_sim_ids(store, qid)

    def test_directly_parented_relax_still_counted(self, store: Any) -> None:
        """The 1-hop stability-lane shape must keep registering after the
        subtree rewrite (no regression for ``struct_relax``)."""
        qid, cand = self._serving_candidate(store)
        relax = store.insert_ref(
            kind="job",
            slug=None,
            title="struct_relax",
            meta={"job_type": "struct_relax"},
            parent_id=cand,
        )
        assert int(relax.id) in qt._pending_sim_ids(store, qid)

    def test_terminal_seed_clears_backpressure(self, store: Any) -> None:
        qid, cand = self._serving_candidate(store)
        cancelled = self._nested_seed_job(store, cand, status="cancelled")
        succeeded = self._nested_seed_job(store, cand, status="succeeded")
        pending = qt._pending_sim_ids(store, qid)
        assert cancelled not in pending
        assert succeeded not in pending

    def test_seed_counts_toward_global_starvation_gate(self, store: Any) -> None:
        _qid, cand = self._serving_candidate(store)
        self._nested_seed_job(store, cand)  # non-terminal
        assert qt._queued_sim_count(store) >= 1


class TestDryRestEscalation:
    """gr170252: the quest-side ``consecutive_dry_rests`` counter, its
    operator alert, and the frontier-improvement reset — exercised against
    the REAL ``store`` fixture (``_register_dry_rest`` writes real
    ``refs.meta`` and raises a real ``kind='alert'`` ref)."""

    def _mk_quest(self, store: Any) -> int:
        ref = store.insert_ref(
            kind="quest", slug=None, title="A blocked striving", meta={}
        )
        return int(ref.id)

    def _mint_dry_rested_job(self, store: Any, quest_id: int, *, age: str) -> int:
        """Insert a terminal ``quest_tick:<id>`` coordinator job rested dry —
        the recency source :func:`loop_mod._dry_rest_escalation_active` reads
        (gr170252 review finding #3), mirroring the shape a real dry rest
        leaves on the job."""
        from precis.store import Tag

        job = store.insert_ref(
            kind="job",
            slug=None,
            title="quest_tick",
            meta={
                "idem_key": f"quest_tick:{quest_id}",
                "executor": "coordinator",
                "job_type": "quest_tick",
                "rest_reason": "dry",
            },
            parent_id=quest_id,
        )
        store.add_tag(job.id, Tag.closed("STATUS", "succeeded"), set_by="system")
        with store.pool.connection() as conn:
            conn.execute(
                """
                UPDATE ref_tags rt SET created_at = (now() - (%s)::interval)
                  FROM tags t
                 WHERE rt.tag_id = t.tag_id AND t.namespace = 'STATUS'
                   AND rt.ref_id = %s
                """,
                (age, job.id),
            )
            conn.commit()
        return int(job.id)

    def test_register_dry_rest_increments(self, store: Any) -> None:
        q = self._mk_quest(store)
        assert qt._register_dry_rest(store, q) == 1
        assert qt._register_dry_rest(store, q) == 2
        assert qt._register_dry_rest(store, q) == 3
        ref = store.get_ref(kind="quest", id=q)
        assert (ref.meta or {}).get("consecutive_dry_rests") == 3

    def test_alert_fires_only_at_threshold(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.alerts import open_alert_severity

        monkeypatch.setenv("PRECIS_QUEST_DRY_REST_ESCALATE", "3")
        q = self._mk_quest(store)

        qt._register_dry_rest(store, q)
        qt._register_dry_rest(store, q)
        assert (
            open_alert_severity(
                store, source="quest_tick", fingerprint=f"quest:dry-rest/{q}"
            )
            is None
        )

        qt._register_dry_rest(store, q)
        assert (
            open_alert_severity(
                store, source="quest_tick", fingerprint=f"quest:dry-rest/{q}"
            )
            == "warn"
        )

    def test_escalation_gate_reads_the_same_counter(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.quest import loop as loop_mod

        monkeypatch.setenv("PRECIS_QUEST_DRY_REST_ESCALATE", "3")
        q = self._mk_quest(store)
        qt._register_dry_rest(store, q)
        qt._register_dry_rest(store, q)
        assert (
            loop_mod._dry_rest_escalation_active(
                store, q, threshold=3, cooldown_s=86400
            )
            is False
        )
        qt._register_dry_rest(store, q)
        # gr170252 review finding #3: the gate is now a cooldown, keyed off
        # the most recent dry-rested job's recency, not the bare counter —
        # mint that job so the (still-active) cooldown has something to read.
        self._mint_dry_rested_job(store, q, age="1 minute")
        assert (
            loop_mod._dry_rest_escalation_active(
                store, q, threshold=3, cooldown_s=86400
            )
            is True
        )
        # And once the escalated cooldown elapses, the gate opens again even
        # though the counter itself is untouched — a running tick is the only
        # thing that can reset it, so it must remain reachable.
        self._mint_dry_rested_job(store, q, age="25 hours")
        assert (
            loop_mod._dry_rest_escalation_active(
                store, q, threshold=3, cooldown_s=86400
            )
            is False
        )

    def test_non_dry_reset_clears_the_counter(self, store: Any) -> None:
        q = self._mk_quest(store)
        qt._register_dry_rest(store, q)
        qt._register_dry_rest(store, q)
        qt._reset_dry_rest_counter(store, q)
        ref = store.get_ref(kind="quest", id=q)
        assert (ref.meta or {}).get("consecutive_dry_rests", 0) == 0

    def test_frontier_improvement_is_detected_and_resets(self, store: Any) -> None:
        q = self._mk_quest(store)
        qt._register_dry_rest(store, q)
        qt._register_dry_rest(store, q)
        # A real tick's cascade update resets this to 0 exactly when it made
        # progress — simulate that without touching cascade.py itself.
        store.update_ref(q, meta_patch={"ticks_since_frontier_improve": 0})
        assert qt._frontier_improved_this_tick(store, q) is True
        qt._reset_dry_rest_counter(store, q)
        ref = store.get_ref(kind="quest", id=q)
        assert (ref.meta or {}).get("consecutive_dry_rests", 0) == 0

    def test_no_progress_is_not_mistaken_for_improvement(self, store: Any) -> None:
        q = self._mk_quest(store)
        store.update_ref(q, meta_patch={"ticks_since_frontier_improve": 2})
        assert qt._frontier_improved_this_tick(store, q) is False


class TestJobRefIdThreadedForTranscriptPersistence:
    """gr170252: a quest_tick slice's job-ref transcript persistence
    (:func:`precis.quest.tick._persist_job_transcript`) only fires because
    the coordinator threads its OWN ``kind='job'`` ref id through to
    ``run_quest_tick`` on every tick — slices don't get their own job ref,
    so this coordinator ref is the only place the conclusion/raw-transcript
    can land. Regression guard for that wiring: the coordinator tests
    elsewhere stub ``run_quest_tick`` entirely and would never notice it
    silently dropped from the call."""

    def test_phase_tick_passes_its_own_ref_id_as_job_ref_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _stub_tick(monkeypatch, _Outcome())
        _stub_queued(monkeypatch, 0)
        _stub_pending(monkeypatch, [[], [811]])
        ctx = FakeCtx(_meta())
        out = qt._dispatch(ctx, qt.SPEC)
        assert isinstance(out, Yield)
        assert len(calls) == 1
        assert calls[0]["job_ref_id"] == ctx.ref_id
