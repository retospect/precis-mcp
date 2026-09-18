"""Tests for the quest results table — docs/backlog/pathway-conditions-
effects-report.md decision 4 (Phase 1 gains a results-table context block).

Covers :func:`precis.quest.results_table.build_results_rows`/
:func:`render_results_table` (all four Pareto bands, dopant/site/co-adsorbate
derivation from a candidate's own atoms, lineage ordering, budget
truncation), the tick prompt's new ``## Results so far`` section, and the
quest handler's ``view='results'``. Runs against real PG (the ``store``
fixture), reusing :func:`precis.quest.compute.ensure_candidate` +
:meth:`Store.structure_record_run`/``stamp_ref_meta`` (read-only reuse, same
fixture idiom as ``tests/test_quest_compute.py``) to mint realistic candidate
structures without a live relax/autocatpath dispatch.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import Unsupported
from precis.handlers.quest import QuestHandler
from precis.quest import compute as compute_mod
from precis.quest.frontier import quest_frontier
from precis.quest.results_table import (
    RESULTS_COLUMNS,
    build_results_rows,
    render_results_table,
)
from precis.quest.tick import build_tick_prompt

_CELL = {"a": 10.0, "b": 10.0, "c": 20.0, "pbc": [True, True, False]}

#: A minimal 4-atom Pd "slab" (all at the same fractional z) — enough atoms
#: to establish a top-layer z for the site (adatom/subst) heuristic, without
#: needing ``ase.build``'s real slab generator.
_PD_SLAB_OPS: list[dict[str, Any]] = [
    {"op": "add_atom", "element": "Pd", "frac": [0.0, 0.0, 0.3]},
    {"op": "add_atom", "element": "Pd", "frac": [0.5, 0.0, 0.3]},
    {"op": "add_atom", "element": "Pd", "frac": [0.0, 0.5, 0.3]},
    {"op": "add_atom", "element": "Pd", "frac": [0.5, 0.5, 0.3]},
]


def _mk_quest(store: Any, text: str = "A NO→NH₃ catalyst") -> int:
    resp = QuestHandler(hub=Hub(store=store)).put(text=text)
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, resp.body
    qid = int(m.group(1))
    store.stamp_ref_meta(
        qid,
        {
            "reaction_config": {"slab": {"element": "Pd"}},
            "rubric_objectives": [{"key": "barrier", "sense": "min"}],
        },
    )
    return qid


def _mk_candidate(
    store: Any, qid: int, name: str, extra_ops: list[dict[str, Any]]
) -> int:
    spec = {"cell": _CELL, "ops": [*_PD_SLAB_OPS, *extra_ops]}
    sid = compute_mod.ensure_candidate(store, qid, {"name": name, "structure": spec})
    assert sid is not None
    return sid


def _converge(store: Any, sid: int, *, energy: float = -1.0) -> None:
    store.structure_record_run(
        sid,
        fidelity="ml",
        on_version=1,
        converged=True,
        n_steps=1,
        max_disp=0.0,
        energy=energy,
    )


def _dopant_ops(element: str, n: int, *, h: int = 0) -> list[dict[str, Any]]:
    """``n`` atoms of ``element`` above the slab (adatom-height) + ``h``
    co-adsorbed H atoms, at distinct positions so none collide."""
    ops: list[dict[str, Any]] = []
    for i in range(n):
        ops.append(
            {
                "op": "add_atom",
                "element": element,
                "frac": [0.1 + 0.1 * i, 0.1 + 0.1 * i, 0.45],
            }
        )
    for i in range(h):
        ops.append(
            {"op": "add_atom", "element": "H", "frac": [0.7 + 0.05 * i, 0.7, 0.5]}
        )
    return ops


# ── build_results_rows — bands ──────────────────────────────────────────


class TestBands:
    def test_row_per_band(self, store: Any) -> None:
        qid = _mk_quest(store)

        frontier_sid = _mk_candidate(store, qid, "frontier-cand", _dopant_ops("Ag", 1))
        _converge(store, frontier_sid)
        store.stamp_ref_meta(frontier_sid, {"barrier": 0.3, "barrier_trusted": True})

        beaten_sid = _mk_candidate(store, qid, "beaten-cand", _dopant_ops("Ag", 2))
        _converge(store, beaten_sid)
        store.stamp_ref_meta(beaten_sid, {"barrier": 0.9, "barrier_trusted": True})

        provisional_sid = _mk_candidate(
            store, qid, "provisional-cand", _dopant_ops("Cu", 1)
        )
        _converge(store, provisional_sid)
        store.stamp_ref_meta(
            provisional_sid, {"barrier": 0.5, "barrier_trusted": False}
        )

        awaiting_sid = _mk_candidate(store, qid, "awaiting-cand", _dopant_ops("Zn", 1))

        rows = build_results_rows(store, qid)
        by_sid = {r["ref_id"]: r for r in rows}
        assert len(rows) == 4
        assert by_sid[frontier_sid]["band"] == "frontier"
        assert by_sid[beaten_sid]["band"] == "beaten"
        assert by_sid[provisional_sid]["band"] == "provisional"
        assert by_sid[awaiting_sid]["band"] == "awaiting"
        # trusted column reads the barrier_trusted flag
        assert by_sid[frontier_sid]["trusted"] == "yes"
        assert by_sid[provisional_sid]["trusted"] == "no"
        assert by_sid[awaiting_sid]["trusted"] == "-"
        # the provisional row's barrier survives (untrusted-value backfill),
        # marked with the same ≈ convention as the leaderboard/frontier text.
        assert by_sid[provisional_sid]["barrier"] == "≈0.5"
        assert by_sid[frontier_sid]["barrier"] == "0.3"
        assert by_sid[awaiting_sid]["barrier"] == "-"


# ── dopant / site / co-adsorbate derivation ─────────────────────────────


class TestDopantDerivation:
    def test_dopant_n_dopant_site_and_coads_from_atoms(self, store: Any) -> None:
        qid = _mk_quest(store)
        sid = _mk_candidate(store, qid, "Ag+2H", _dopant_ops("Ag", 1, h=2))
        rows = build_results_rows(store, qid)
        assert len(rows) == 1
        row = rows[0]
        assert row["dopant"] == "Ag"
        assert row["n_dopant"] == 1
        # Ag sits well above the 4-atom Pd layer (frac z 0.3 -> 0.45, a 3 Å
        # rise over a c=20 Å cell) — reads as an adatom, not a substitution.
        assert row["site"] == "adatom"
        assert row["coads"] == "H2"

    def test_no_dopant_no_coads_renders_dashes(self, store: Any) -> None:
        qid = _mk_quest(store)
        sid = _mk_candidate(store, qid, "bare-slab", [])
        rows = build_results_rows(store, qid)
        row = rows[0]
        assert row["dopant"] == "-"
        assert row["n_dopant"] == 0
        assert row["coads"] == "-"

    def test_params_stamp_wins_over_atom_derivation(self, store: Any) -> None:
        """A proposer-stamped ``meta.params`` (§7.8) is authoritative over
        the atom-derived reading — even when it disagrees with the geometry."""
        qid = _mk_quest(store)
        sid = _mk_candidate(store, qid, "Ag-explicit", _dopant_ops("Ag", 1))
        store.stamp_ref_meta(
            sid, {"params": {"dopant": "Ag", "n_dopant": 1, "site": "subst"}}
        )
        rows = build_results_rows(store, qid)
        row = rows[0]
        assert row["dopant"] == "Ag"
        assert row["n_dopant"] == 1
        assert row["site"] == "subst"  # the stamped value, not the geometry read


# ── lineage ordering ─────────────────────────────────────────────────────


class TestLineageOrdering:
    def test_grouped_by_dopant_then_n_then_coads(self, store: Any) -> None:
        qid = _mk_quest(store)
        # Minted out of lineage order on purpose — the row order must come
        # from composition, not creation/ref_id order.
        cu1 = _mk_candidate(store, qid, "Cu n=1", _dopant_ops("Cu", 1))
        ag2 = _mk_candidate(store, qid, "Ag n=2", _dopant_ops("Ag", 2))
        ag1h2 = _mk_candidate(store, qid, "Ag n=1 H2", _dopant_ops("Ag", 1, h=2))
        ag1h0 = _mk_candidate(store, qid, "Ag n=1 H0", _dopant_ops("Ag", 1))

        rows = build_results_rows(store, qid)
        order = [r["ref_id"] for r in rows]
        assert order == [ag1h0, ag1h2, ag2, cu1]


# ── budget truncation ────────────────────────────────────────────────────


class TestBudgetTruncation:
    def test_truncation_keeps_newest_ten(self, store: Any) -> None:
        qid = _mk_quest(store)
        sids = [
            _mk_candidate(store, qid, f"Zn n={i}", _dopant_ops("Zn", i + 1))
            for i in range(15)
        ]
        rows = build_results_rows(store, qid)
        assert len(rows) == 15
        newest_ten = set(sorted(sids, reverse=True)[:10])
        text = render_results_table(rows, token_budget=1)
        assert "(+5 rows omitted)" in text
        for sid in newest_ten:
            handle_row = next(r for r in rows if r["ref_id"] == sid)
            assert handle_row["handle"] in text

    def test_empty_rows_render_placeholder(self) -> None:
        assert render_results_table([]) == "(no candidates yet)"


# ── handler views honour the budget (gr345353) ───────────────────────────


class TestViewBudget:
    def _fifteen(self, store: Any) -> tuple[int, list[int]]:
        qid = _mk_quest(store)
        sids = [
            _mk_candidate(store, qid, f"Zn n={i}", _dopant_ops("Zn", i + 1))
            for i in range(15)
        ]
        return qid, sids

    def test_results_view_defaults_to_the_tick_budget_and_reports_drops(
        self, store: Any
    ) -> None:
        qid, sids = self._fifteen(store)
        h = QuestHandler(hub=Hub(store=store))
        body = h.get(id=qid, view="results", args={"budget": 1}).body
        assert "of 15 rows omitted for the 1-token budget" in body
        rows = build_results_rows(store, qid)
        newest_ten = set(sorted(sids, reverse=True)[:10])
        for r in rows:
            present = r["handle"] in body
            assert present == (r["ref_id"] in newest_ten), r["handle"]
        wide = h.get(id=qid, view="results", args={"budget": 100_000}).body
        assert "rows omitted" not in wide
        assert all(r["handle"] in wide for r in rows)

    def test_frontier_view_trims_tail_band_first(self, store: Any) -> None:
        qid, _sids = self._fifteen(store)
        h = QuestHandler(hub=Hub(store=store))
        full = h.get(id=qid, view="frontier").body
        assert "omitted" not in full
        tight = h.get(id=qid, view="frontier", args={"budget": 30}).body
        assert "omitted — args={'budget': N} widens" in tight
        assert tight.startswith("# frontier — quest")
        assert len(tight) < len(full)

    def test_bad_budget_is_bad_input(self, store: Any) -> None:
        from precis.errors import BadInput

        qid, _ = self._fifteen(store)
        h = QuestHandler(hub=Hub(store=store))
        for bad in ("x", 0, -5, True):
            with pytest.raises(BadInput):
                h.get(id=qid, view="results", args={"budget": bad})


# ── tick prompt section ──────────────────────────────────────────────────


class TestTickPromptSection:
    def test_section_present_when_candidates_exist(self, store: Any) -> None:
        qid = _mk_quest(store)
        sid = _mk_candidate(store, qid, "Ag n=1", _dopant_ops("Ag", 1))
        _converge(store, sid)
        store.stamp_ref_meta(sid, {"barrier": 0.4, "barrier_trusted": True})
        quest = store.get_ref(kind="quest", id=qid)
        prompt = build_tick_prompt(store, quest)
        assert (
            "## Results so far (one row per evaluated candidate — read this "
            "BEFORE proposing)" in prompt
        )
        assert "band: frontier|beaten|provisional|awaiting" in prompt
        assert "Ag" in prompt

    def test_section_absent_when_no_candidates(self, store: Any) -> None:
        qid = _mk_quest(store, "A lonely striving")
        quest = store.get_ref(kind="quest", id=qid)
        prompt = build_tick_prompt(store, quest)
        assert "## Results so far" not in prompt


# ── view='results' ────────────────────────────────────────────────────────


class TestViewResults:
    def test_view_results_renders_toon_rows_and_text_table(self, store: Any) -> None:
        qid = _mk_quest(store)
        sid = _mk_candidate(store, qid, "Ag n=1", _dopant_ops("Ag", 1))
        _converge(store, sid)
        store.stamp_ref_meta(sid, {"barrier": 0.4, "barrier_trusted": True})

        h = QuestHandler(hub=Hub(store=store))
        body = h.get(id=qid, view="results").body
        assert "results — quest" in body
        # TOON header carries every declared column.
        for col in RESULTS_COLUMNS:
            assert col in body
        assert "text table (as embedded in the tick prompt)" in body
        from precis.utils import handle_registry

        handle = handle_registry.try_format("structure", sid)
        assert handle is not None and handle in body

    def test_view_results_empty_quest(self, store: Any) -> None:
        qid = _mk_quest(store, "A lonely striving")
        h = QuestHandler(hub=Hub(store=store))
        body = h.get(id=qid, view="results").body
        assert "no candidate structures serve this quest yet." in body

    def test_view_results_listed_in_unknown_view_error(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        h = QuestHandler(hub=Hub(store=store))
        with pytest.raises(Unsupported) as exc_info:
            h.get(id=qid, view="bogus")
        assert "results" in list(exc_info.value.options or [])


def test_quest_frontier_still_agrees_with_results_bands(store: Any) -> None:
    """Sanity cross-check: :func:`build_results_rows`'s band assignment must
    partition the SAME :class:`FrontierResult` the frontier/leaderboard views
    already render — no second ranking to drift."""
    qid = _mk_quest(store)
    sid = _mk_candidate(store, qid, "Ag n=1", _dopant_ops("Ag", 1))
    _converge(store, sid)
    store.stamp_ref_meta(sid, {"barrier": 0.4, "barrier_trusted": True})
    fr = quest_frontier(store, qid)
    rows = build_results_rows(store, qid, fr=fr)
    assert len(rows) == len(fr.frontier) + len(fr.dominated) + len(
        fr.provisional
    ) + len(fr.unevaluated)
