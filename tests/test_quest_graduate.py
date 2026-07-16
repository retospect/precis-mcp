"""Tests for quest graduation — the in-silico ceiling (slice 4e).

Covers the graduation rule (from ``meta.graduation``), graduating a frontier
candidate that crosses the ceiling (tag + `milestone` deed, idempotent), the
no-rule no-op, and the `needs-experiment` gap the slice-3 queue then surfaces.
Runs against real PG (the ``store`` fixture).
"""

from __future__ import annotations

import re
from typing import Any

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.quest import compute as compute_mod
from precis.quest import graduate as grad
from precis.quest.gaps import quest_gaps

_SPEC = {
    "cell": {"a": 8.4, "b": 8.4, "c": 24.0},
    "ops": [{"op": "add_atom", "element": "Fe", "frac": [0.0, 0.0, 0.5]}],
}


def _mk_quest(store: Any, text: str) -> int:
    resp = QuestHandler(hub=Hub(store=store)).put(text=text)
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, resp.body
    return int(m.group(1))


def _candidate_with_energy(
    store: Any, qid: int, spec: dict[str, Any], energy: float
) -> int:
    sid = compute_mod.ensure_candidate(store, qid, {"name": "cand", "structure": spec})
    assert sid is not None
    store.structure_record_run(
        sid,
        fidelity="ml",
        on_version=1,
        converged=True,
        n_steps=10,
        max_disp=0.0,
        energy=energy,
    )
    return sid


def _set_rule(store: Any, qid: int, **rule: Any) -> None:
    from precis.quest.cascade import _merge_meta

    _merge_meta(store, qid, {"graduation": rule})


class TestGraduation:
    def test_no_rule_is_noop(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        _candidate_with_energy(store, qid, _SPEC, -20.0)
        assert grad.graduate_frontier(store, qid) == []

    def test_crossing_the_ceiling_graduates(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        sid = _candidate_with_energy(store, qid, _SPEC, -20.0)
        _set_rule(store, qid, key="energy", sense="min", threshold=-15.0)
        graduated = grad.graduate_frontier(store, qid)
        assert graduated == [sid]
        assert any(str(t) == "needs-experiment" for t in store.tags_for(sid))
        logs = [
            b for b in store.list_blocks_for_ref(qid) if b.chunk_kind == "quest_log"
        ]
        assert any(
            (b.meta or {}).get("entry_type") == "milestone" and "graduated" in b.text
            for b in logs
        )
        # idempotent — a second call does not re-graduate
        assert grad.graduate_frontier(store, qid) == []

    def test_below_the_ceiling_does_not_graduate(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        _candidate_with_energy(store, qid, _SPEC, -10.0)  # not < -15
        _set_rule(store, qid, key="energy", sense="min", threshold=-15.0)
        assert grad.graduate_frontier(store, qid) == []

    def test_graduated_candidate_surfaces_as_gap(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        sid = _candidate_with_energy(store, qid, _SPEC, -20.0)
        _set_rule(store, qid, key="energy", sense="min", threshold=-15.0)
        grad.graduate_frontier(store, qid)
        kinds = [g.kind for g in quest_gaps(store, qid)]
        assert "needs-experiment" in kinds
        exp = next(g for g in quest_gaps(store, qid) if g.kind == "needs-experiment")
        assert exp.handle == f"st{sid}"
