"""Tests for the quest cascade — local grind vs. frontier-review (slice 4c).

Covers the escalation signal (first-review / new-evidence / stalled), the
per-tick cascade state + the `promise` proxy (frontier-improvement rate), and
the tick's review mode (senior tier + directions logged as a `decision`).
Runs against real PG (the ``store`` fixture); the model call is injected.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.quest import cascade as cascade_mod
from precis.quest import compute as compute_mod
from precis.quest.logbook import append_entry
from precis.quest.tick import run_quest_tick

_SPEC = {
    "cell": {"a": 8.4, "b": 8.4, "c": 24.0},
    "ops": [{"op": "add_atom", "element": "Fe", "frac": [0.0, 0.0, 0.5]}],
}


def _mk_quest(store: Any, text: str) -> int:
    resp = QuestHandler(hub=Hub(store=store)).put(text=text)
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, resp.body
    return int(m.group(1))


def _fake_dispatch(payload: dict[str, Any]) -> Any:
    def _d(_req: Any) -> Any:
        return SimpleNamespace(data=payload, text="", error=None, cost_usd=0.01)

    return _d


def _add_candidate(store: Any, qid: int, spec: dict[str, Any] | None = None) -> int:
    sid = compute_mod.ensure_candidate(
        store, qid, {"name": "c", "structure": spec or _SPEC}
    )
    assert sid is not None
    return sid


# ── the escalation signal ─────────────────────────────────────────────


class TestEscalationSignal:
    def test_no_candidates_no_escalation(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        assert cascade_mod.escalation_signal(store, qid).escalate is False

    def test_first_review_when_candidates_exist(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        _add_candidate(store, qid)
        sig = cascade_mod.escalation_signal(store, qid)
        assert sig.escalate is True and sig.reason == "first-review"

    def test_new_evidence_after_reviews(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        cascade_mod._merge_meta(store, qid, {"frontier_reviews": 1})
        for _ in range(cascade_mod.FRONTIER_REVIEW_EVERY):
            append_entry(store, qid, text="r", entry_type="result", by="agent")
        sig = cascade_mod.escalation_signal(store, qid)
        assert sig.escalate is True and sig.reason == "new-evidence"

    def test_stalled_after_many_ticks(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        _add_candidate(store, qid)
        cascade_mod._merge_meta(
            store,
            qid,
            {
                "frontier_reviews": 1,
                "frontier_review_results": 0,
                "tick_count": cascade_mod.STALL_TICKS + 2,
                "frontier_review_tick": 1,
            },
        )
        sig = cascade_mod.escalation_signal(store, qid)
        assert sig.escalate is True and sig.reason == "stalled"


# ── cascade state + promise ───────────────────────────────────────────


class TestCascadeState:
    def test_tick_count_and_review_stamp(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        cascade_mod.update_cascade_state(store, qid, reviewed=True)
        meta = store.get_ref(kind="quest", id=qid).meta
        assert meta["tick_count"] == 1
        assert meta["frontier_reviews"] == 1
        assert meta["frontier_review_tick"] == 1

    def test_promise_is_improvement_over_cost(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        c1 = _add_candidate(store, qid, _SPEC)
        store.structure_record_run(
            c1,
            fidelity="ml",
            on_version=1,
            converged=True,
            n_steps=10,
            max_disp=0.0,
            energy=-10.0,
        )
        cascade_mod.update_cascade_state(store, qid, reviewed=False)  # best=-10
        # a better candidate lands + a recorded compute cost of 5
        append_entry(store, qid, text="cost", entry_type="result", by="agent", cost=5.0)
        c2 = _add_candidate(
            store,
            qid,
            {
                "cell": {"a": 8.4, "b": 8.4, "c": 24.0},
                "ops": [{"op": "add_atom", "element": "Co", "frac": [0.0, 0.0, 0.5]}],
            },
        )
        store.structure_record_run(
            c2,
            fidelity="ml",
            on_version=1,
            converged=True,
            n_steps=10,
            max_disp=0.0,
            energy=-20.0,
        )
        promise = cascade_mod.update_cascade_state(store, qid, reviewed=False)
        assert abs(promise - 2.0) < 1e-9  # (20-10 improvement) / 5 cost
        assert abs(cascade_mod.quest_promise(store, qid) - 2.0) < 1e-9

    def test_reasoning_quest_stall_clock_climbs(self, store: Any) -> None:
        # A quest with no compute frontier used to leave `ticks_since_
        # frontier_improve` untouched, so `cool_stalled` could never catch a
        # spin. Now each frontier-less tick advances it.
        qid = _mk_quest(store, "A reasoning striving")
        for _ in range(3):
            cascade_mod.update_cascade_state(store, qid, reviewed=False)
        meta = store.get_ref(kind="quest", id=qid).meta
        assert meta["ticks_since_frontier_improve"] == 3

    def test_milestone_resets_reasoning_stall_clock(self, store: Any) -> None:
        qid = _mk_quest(store, "A reasoning striving")
        cascade_mod.update_cascade_state(store, qid, reviewed=False)
        cascade_mod.update_cascade_state(store, qid, reviewed=False)
        assert (
            store.get_ref(kind="quest", id=qid).meta["ticks_since_frontier_improve"]
            == 2
        )
        # a deed lands → the next tick resets the stall clock to zero
        append_entry(store, qid, text="shipped", entry_type="milestone", by="agent")
        cascade_mod.update_cascade_state(store, qid, reviewed=False)
        assert (
            store.get_ref(kind="quest", id=qid).meta["ticks_since_frontier_improve"]
            == 0
        )


# ── tick integration ──────────────────────────────────────────────────


class TestTickCascade:
    def test_forced_review_logs_directions_as_decision(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        payload = {
            "logbook": [],
            "dossier_markdown": "# D",
            "directions": ["push Fe–N₄ coordination", "abandon oxide route"],
        }
        out = run_quest_tick(
            store, qid, dispatch_fn=_fake_dispatch(payload), review=True
        )
        assert out.escalated is True and out.mode == "frontier-review"
        logs = [
            b for b in store.list_blocks_for_ref(qid) if b.chunk_kind == "quest_log"
        ]
        assert any(
            (b.meta or {}).get("entry_type") == "decision" and "directions" in b.text
            for b in logs
        )

    def test_local_by_default_without_candidates(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        out = run_quest_tick(
            store,
            qid,
            dispatch_fn=_fake_dispatch({"logbook": [], "dossier_markdown": "# D"}),
        )
        assert out.escalated is False and out.mode == "local"

    def test_auto_escalates_first_review_with_candidate(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        _add_candidate(store, qid)
        out = run_quest_tick(
            store,
            qid,
            dispatch_fn=_fake_dispatch(
                {"logbook": [], "dossier_markdown": "# D", "directions": ["go"]}
            ),
        )
        assert out.escalated is True and out.mode == "frontier-review"
