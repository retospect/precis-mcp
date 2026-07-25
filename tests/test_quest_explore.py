"""Tests for :mod:`precis.quest.explore` — ``tried_set_summary``, a pure
DB-fact read of what a quest's candidates have already measured. No
chemistry is enumerated or chosen here (that's the discovery agent's job,
prompted via :mod:`precis.quest.tick`'s explorer's creed + commit
re-prompt) — this module only reports back what already happened.

Runs against real PG (the ``store`` fixture).
"""

from __future__ import annotations

import re
from typing import Any

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.quest import compute as compute_mod
from precis.quest import explore as explore_mod
from precis.store import Tag


def _mk_quest(store: Any, text: str) -> int:
    resp = QuestHandler(hub=Hub(store=store)).put(text=text)
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, resp.body
    return int(m.group(1))


def _flat_spec(element: str) -> dict[str, Any]:
    """A simple, non-slab structure spec — ``tried_set_summary`` doesn't care
    about real fcc111 geometry, so no ``ase.build`` dependency is needed."""
    return {
        "cell": {"a": 8.4, "b": 8.4, "c": 24.0, "pbc": [True, True, False]},
        "ops": [{"op": "add_atom", "element": element, "frac": [0.0, 0.0, 0.5]}],
    }


def _mk_candidate(store: Any, qid: int, name: str, element: str) -> int:
    sid = compute_mod.ensure_candidate(
        store, qid, {"name": name, "structure": _flat_spec(element)}
    )
    assert sid is not None
    return sid


class TestTriedSetSummary:
    def test_empty_when_no_candidates(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        assert explore_mod.tried_set_summary(store, qid) == ""

    def test_lists_measured_candidates_best_first(self, store: Any) -> None:
        qid = _mk_quest(store, "A Pd catalyst striving")
        store.stamp_ref_meta(
            qid, {"rubric_objectives": [{"key": "barrier", "sense": "min"}]}
        )
        cu = _mk_candidate(store, qid, "Cu adatom", "Cu")
        store.stamp_ref_meta(cu, {"barrier": 0.96})
        ag = _mk_candidate(store, qid, "Ag adatom", "Ag")
        store.stamp_ref_meta(ag, {"barrier": 0.74})

        summary = explore_mod.tried_set_summary(store, qid)
        assert summary.startswith("Tried:")
        # best (lowest barrier) sorts first and carries the BEST marker
        assert summary.index("Ag adatom 0.74 (BEST)") < summary.index("Cu adatom 0.96")

    def test_lists_ruled_out_separately(self, store: Any) -> None:
        qid = _mk_quest(store, "A Pd catalyst striving")
        store.stamp_ref_meta(
            qid, {"rubric_objectives": [{"key": "barrier", "sense": "min"}]}
        )
        dead = _mk_candidate(store, qid, "PdCuNi alloy", "Ni")
        store.stamp_ref_meta(dead, {"barrier": 2.84})
        store.add_tag(dead, Tag.open("ruled-out:relax-failed"), set_by="system")

        summary = explore_mod.tried_set_summary(store, qid)
        assert "ruled out:" in summary
        assert "PdCuNi alloy 2.84" in summary
        # a ruled-out candidate never appears as a live "tried" measurement
        assert "PdCuNi alloy 2.84 (BEST)" not in summary

    def test_unmeasured_candidate_reads_as_awaiting(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        _mk_candidate(store, qid, "Fe adatom", "Fe")  # no barrier/energy stamped
        summary = explore_mod.tried_set_summary(store, qid)
        assert "Fe adatom (awaiting)" in summary

    def test_no_chemistry_is_chosen_here(self, store: Any) -> None:
        # Structural guarantee this module never picks an element/site: the
        # public surface is exactly one pure-read function.
        assert explore_mod.__all__ == ["tried_set_summary"]
        assert not hasattr(explore_mod, "next_untried_candidate")
        assert not hasattr(explore_mod, "exploration_coverage")
