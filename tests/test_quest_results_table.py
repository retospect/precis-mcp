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


def _hollow_h(*, height: float = 1.6) -> dict[str, Any]:
    """An H placed on a NAMED hollow site over three of the Pd fixture's
    atoms — the ``site`` an ``add_atom_site`` op takes is the object
    ``{'type', 'anchors'}``, not a bare name. ``height`` distinguishes two
    otherwise-identical placements so the second doesn't collide."""
    return {
        "op": "add_atom_site",
        "element": "H",
        "site": {"type": "hollow", "anchors": ["aPd1", "aPd2", "aPd3"]},
        "height": height,
    }


def _top_h() -> dict[str, Any]:
    return {
        "op": "add_atom_site",
        "element": "H",
        "site": {"type": "top", "anchors": ["aPd1"]},
        "height": 1.6,
    }


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
        # the objective measure rides its own column (gr573266: an
        # energy-rubric quest read every beaten row as "no energy value"
        # because only the barrier-family measures had columns).
        assert by_sid[frontier_sid]["energy"] == "-1"
        assert by_sid[beaten_sid]["energy"] == "-1"
        assert by_sid[awaiting_sid]["energy"] == "-"


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


# ── meta.params writer (Phase 1 item 2, closes gr345342) ─────────────────


class TestParamsStamp:
    def test_ops_stamp_params_with_coadsorbate_site(self, store: Any) -> None:
        """``ensure_candidate`` stamps ``meta.params`` from the spec's own
        ops — including the NAMED site a co-adsorbate was placed on, which
        atoms alone can't report (gr345342)."""
        qid = _mk_quest(store)
        sid = _mk_candidate(
            store,
            qid,
            "Ag subst + 2H hollow",
            [
                {"op": "set_element", "atom": "aPd4", "element": "Ag"},
                _hollow_h(),
                _hollow_h(height=2.4),
            ],
        )
        ref = store.fetch_refs_by_ids({sid})[sid]
        params = (ref.meta or {}).get("params")
        assert params == {
            "dopant": "Ag",
            "n_dopant": 1,
            "site": "subst",
            "coads": {"H": 2},
            "coads_site": {"H": "hollow"},
        }
        row = build_results_rows(store, qid)[0]
        assert row["coads"] == "H2@hollow"  # the site rides into the table
        assert row["site"] == "subst"

    def test_host_atoms_are_not_dopants_and_raw_frac_has_no_site_suffix(
        self, store: Any
    ) -> None:
        qid = _mk_quest(store)  # reaction_config.slab.element = Pd
        sid = _mk_candidate(store, qid, "Ag+2H frac", _dopant_ops("Ag", 1, h=2))
        ref = store.fetch_refs_by_ids({sid})[sid]
        params: dict[str, Any] = (ref.meta or {})["params"]
        # the four bare `add_atom` Pd host atoms are not self-doping
        assert params["dopant"] == "Ag" and params["n_dopant"] == 1
        assert params["site"] == "adatom"
        assert params["coads_site"] == {"H": "frac"}
        # 'frac' = "placed by coordinates, site not named" — no @suffix
        assert build_results_rows(store, qid)[0]["coads"] == "H2"

    def test_proposer_params_override_the_derived_ones(self, store: Any) -> None:
        qid = _mk_quest(store)
        spec = {
            "cell": _CELL,
            "ops": [*_PD_SLAB_OPS, _top_h()],
        }
        sid = compute_mod.ensure_candidate(
            store,
            qid,
            {
                "name": "explicit",
                "structure": spec,
                "params": {"coads": {"H": 1}, "coads_site": {"H": "bridge"}},
            },
        )
        assert sid is not None
        ref = store.fetch_refs_by_ids({sid})[sid]
        assert (ref.meta or {})["params"]["coads_site"] == {"H": "bridge"}

    def test_add_adsorbate_group_counts_as_its_species(self, store: Any) -> None:
        """A whole-group ``add_adsorbate`` (Phase 1 item 3) carries its own
        species name into the param space — OH is one OH, not one O + one H."""
        qid = _mk_quest(store)
        sid = _mk_candidate(
            store,
            qid,
            "OH on top",
            [
                {
                    "op": "add_adsorbate",
                    "species": "OH",
                    "site": {"type": "top", "anchors": ["aPd1"]},
                    "height": 2.0,
                }
            ],
        )
        ref = store.fetch_refs_by_ids({sid})[sid]
        params: dict[str, Any] = (ref.meta or {})["params"]
        assert params["coads"] == {"OH": 1}
        assert params["coads_site"] == {"OH": "top"}
        assert build_results_rows(store, qid)[0]["coads"] == "OH1@top"

    def test_retransmuting_one_atom_counts_once(self) -> None:
        """Labels are stable, so a spec may ``set_element`` the same atom
        twice (a corrected choice). Only the atom's FINAL element counts —
        counting per op would report two dopants for one doped site, and
        would report a dopant for an atom put back to the host element."""
        from precis.quest.compute import params_from_spec

        corrected = params_from_spec(
            {
                "ops": [
                    {"op": "slab", "element": "Pd", "size": [2, 2, 3]},
                    {"op": "set_element", "atom": "aPd4", "element": "Ag"},
                    {"op": "set_element", "atom": "aPd4", "element": "Cu"},
                ]
            }
        )
        assert corrected["dopant"] == "Cu" and corrected["n_dopant"] == 1

        reverted = params_from_spec(
            {
                "ops": [
                    {"op": "slab", "element": "Pd", "size": [2, 2, 3]},
                    {"op": "set_element", "atom": "aPd4", "element": "Ag"},
                    {"op": "set_element", "atom": "aPd4", "element": "Pd"},
                ]
            }
        )
        assert reverted == {}  # back to a clean slab: no doping at all

        two_atoms = params_from_spec(
            {
                "ops": [
                    {"op": "slab", "element": "Pd", "size": [2, 2, 3]},
                    {"op": "set_element", "atom": "aPd3", "element": "Ag"},
                    {"op": "set_element", "atom": "aPd4", "element": "Ag"},
                ]
            }
        )
        assert two_atoms["n_dopant"] == 2

    def test_bare_slab_stamps_no_params(self, store: Any) -> None:
        qid = _mk_quest(store)
        sid = _mk_candidate(store, qid, "bare", [])
        ref = store.fetch_refs_by_ids({sid})[sid]
        assert "params" not in (ref.meta or {})


# ── controlled series view (Phase 1 item 2) ──────────────────────────────


class TestSeriesView:
    def _series_quest(self, store: Any) -> int:
        qid = _mk_quest(store)
        # a θ_H series on an Ag-doped base: 0, 1 and 2 co-adsorbed H
        for h in (0, 1, 2):
            sid = _mk_candidate(store, qid, f"Ag H{h}", _dopant_ops("Ag", 1, h=h))
            _converge(store, sid)
            store.stamp_ref_meta(
                sid, {"barrier": 0.3 + 0.1 * h, "barrier_trusted": True}
            )
        # one lone Cu candidate differing in dopant AND n_dopant from every
        # Ag row — two axes at once, so it belongs to no controlled series.
        _mk_candidate(store, qid, "Cu2", _dopant_ops("Cu", 2))
        return qid

    def test_build_series_groups_one_varied_axis(self, store: Any) -> None:
        from precis.quest.results_table import build_series

        qid = self._series_quest(store)
        rows = build_results_rows(store, qid)
        series = build_series(rows)
        coads_series = [s for s in series if s["axis"] == "coads"]
        assert coads_series, series
        widest = coads_series[0]
        assert [lv["coads"] for lv in widest["levels"]] == ["-", "H1", "H2"]
        assert "dopant=Ag" in widest["base"]
        # the Cu candidate differs in dopant AND coads from every Ag row, so
        # it is in no series.
        handles = {lv["handle"] for s in series for lv in s["levels"]}
        cu_row = next(r for r in rows if r["dopant"] == "Cu")
        assert cu_row["handle"] not in handles

    def test_view_series_renders_blocks_and_empty_case(self, store: Any) -> None:
        h = QuestHandler(hub=Hub(store=store))
        qid = self._series_quest(store)
        body = h.get(id=qid, view="series").body
        assert "controlled series" in body
        assert "── coads varied (3 levels)" in body
        assert "dopant=Ag" in body
        assert "span_at_Uopt" in body and "trusted" in body

        lonely = _mk_quest(store, "a quest with one candidate")
        _mk_candidate(store, lonely, "only", _dopant_ops("Ag", 1))
        empty = h.get(id=lonely, view="series").body
        assert "no controlled series yet" in empty

    def test_series_view_budget_drops_blocks(self, store: Any) -> None:
        """Two series (a θ_H one and a dopant one) under a 1-token budget:
        the first block is always rendered — an empty view would be worse
        than an over-budget one — and the rest are named as omitted."""
        handler = QuestHandler(hub=Hub(store=store))
        qid = _mk_quest(store, "two-series quest")
        for ops in (
            _dopant_ops("Ag", 1),
            _dopant_ops("Ag", 1, h=1),
            _dopant_ops("Cu", 1),
        ):
            _mk_candidate(store, qid, "cand", ops)
        body = handler.get(id=qid, view="series").body
        assert "(2 controlled series" in body
        tight = handler.get(id=qid, view="series", args={"budget": 1}).body
        assert "(+1 series omitted — args={'budget': N} widens)" in tight

    def test_numeric_axis_levels_sort_numerically(self) -> None:
        """``n_dopant`` is an int column — 10 must not sort before 2."""
        from precis.quest.results_table import build_series

        rows = [
            {
                "handle": f"st{n}",
                "dopant": "Ag",
                "n_dopant": n,
                "site": "subst",
                "coads": "-",
            }
            for n in (2, 10, 1)
        ]
        series = build_series(rows)
        assert [lv["n_dopant"] for lv in series[0]["levels"]] == [1, 2, 10]

    def test_series_block_header_counts_distinct_levels(self, store: Any) -> None:
        """Two candidates at the SAME level are one level measured twice —
        the header must not read them as two levels."""
        from precis.quest.results_table import build_series, render_series

        qid = _mk_quest(store, "repeat-level quest")
        for h_count, height in ((0, None), (1, 1.6), (1, 2.4)):
            ops = _dopant_ops("Ag", 1)
            if h_count:
                ops = [*ops, _hollow_h(height=height or 1.6)]
            _mk_candidate(store, qid, f"Ag H{h_count}", ops)
        rows = build_results_rows(store, qid)
        text = render_series(build_series(rows))
        assert "── coads varied (2 levels over 3 candidates)" in text


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


class TestFrontierEmptyHeadline:
    """gr345354: an empty confirmed frontier names the missing required
    objective instead of claiming nothing converged."""

    def test_names_missing_required_axis_and_optional_ones(self, store: Any) -> None:
        qid = _mk_quest(store)
        store.stamp_ref_meta(
            qid,
            {
                "rubric_objectives": [
                    {"key": "span_at_Uopt", "sense": "min"},
                    {"key": "energy", "sense": "min"},
                    {"key": "P_side", "sense": "min", "optional": True},
                ]
            },
        )
        a = _mk_candidate(store, qid, "Zn n=1", _dopant_ops("Zn", 1))
        b = _mk_candidate(store, qid, "Zn n=2", _dopant_ops("Zn", 2))
        _converge(store, a, energy=-1.0)
        _converge(store, b, energy=-1.2)
        body = QuestHandler(hub=Hub(store=store)).get(id=qid, view="frontier").body
        assert "(none converged yet)" not in body
        assert (
            "2 converged candidate(s) lack a required objective: span_at_Uopt ×2"
            in body
        )
        assert "optional (not required): P_side" in body

    def test_none_converged_stays_honest(self, store: Any) -> None:
        qid = _mk_quest(store)
        _mk_candidate(store, qid, "Zn n=1", _dopant_ops("Zn", 1))
        body = QuestHandler(hub=Hub(store=store)).get(id=qid, view="frontier").body
        assert "(none converged yet)" in body


class TestObjectiveLineWithoutRubric:
    """gr503440: a quest with no ``meta.rubric_objectives`` must not
    assert the ``energy (min)`` fallback as if somebody set it — the
    objective line says ``(none set …)`` when there are no candidates
    and flags the fallback ``[default]`` when there are."""

    def _no_rubric_quest(self, store: Any) -> int:
        resp = QuestHandler(hub=Hub(store=store)).put(text="A non-materials quest")
        m = re.search(r"\bqu(\d+)\b", resp.body)
        assert m is not None, resp.body
        return int(m.group(1))

    def test_no_rubric_no_candidates_says_none_set(self, store: Any) -> None:
        qid = self._no_rubric_quest(store)
        h = QuestHandler(hub=Hub(store=store))
        for view in ("frontier", "leaderboard"):
            body = h.get(id=qid, view=view).body
            assert "objective: (none set — meta.rubric_objectives)" in body
            assert "energy (min)" not in body

    def test_no_rubric_with_candidates_flags_default(self, store: Any) -> None:
        qid = self._no_rubric_quest(store)
        sid = _mk_candidate(store, qid, "Ag n=1", _dopant_ops("Ag", 1))
        _converge(store, sid)
        h = QuestHandler(hub=Hub(store=store))
        for view in ("frontier", "leaderboard"):
            body = h.get(id=qid, view=view).body
            assert "objective: energy (min) [default]" in body

    def test_rubric_quest_line_is_unmarked(self, store: Any) -> None:
        qid = _mk_quest(store)  # rubric_objectives = barrier min
        body = QuestHandler(hub=Hub(store=store)).get(id=qid, view="frontier").body
        assert "objective: barrier (min)" in body
        assert "[default]" not in body


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
        assert "energy" in prompt  # the objective measure has its own column
        assert "Ag" in prompt

    def test_section_absent_when_no_candidates(self, store: Any) -> None:
        qid = _mk_quest(store, "A lonely striving")
        quest = store.get_ref(kind="quest", id=qid)
        prompt = build_tick_prompt(store, quest)
        assert "## Results so far" not in prompt


# ── view='results' ────────────────────────────────────────────────────────


class TestViewResults:
    def test_view_results_renders_toon_rows_only_by_default(self, store: Any) -> None:
        """gr592661: the default agent view carries ONE copy of the rows —
        the tick prompt's fixed-width text table is opt-in."""
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
        assert "text table (as embedded in the tick prompt)" not in body
        from precis.utils import handle_registry

        handle = handle_registry.try_format("structure", sid)
        assert handle is not None and handle in body

    def test_view_results_format_text_renders_tick_table(self, store: Any) -> None:
        qid = _mk_quest(store)
        sid = _mk_candidate(store, qid, "Ag n=1", _dopant_ops("Ag", 1))
        _converge(store, sid)
        store.stamp_ref_meta(sid, {"barrier": 0.4, "barrier_trusted": True})

        h = QuestHandler(hub=Hub(store=store))
        body = h.get(id=qid, view="results", args={"format": "text"}).body
        assert "results — quest" in body
        assert "text table (as embedded in the tick prompt)" in body
        from precis.utils import handle_registry

        handle = handle_registry.try_format("structure", sid)
        assert handle is not None and handle in body

    def test_view_results_format_toon_matches_default(self, store: Any) -> None:
        qid = _mk_quest(store)
        _mk_candidate(store, qid, "Ag n=1", _dopant_ops("Ag", 1))
        h = QuestHandler(hub=Hub(store=store))
        assert (
            h.get(id=qid, view="results", args={"format": "toon"}).body
            == h.get(id=qid, view="results").body
        )

    def test_view_results_bad_format_is_bad_input(self, store: Any) -> None:
        from precis.errors import BadInput

        qid = _mk_quest(store)
        h = QuestHandler(hub=Hub(store=store))
        for bad in ("xml", "", 5, None):
            with pytest.raises(BadInput):
                h.get(id=qid, view="results", args={"format": bad})

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


# ── declared-objective columns (gr567692) ─────────────────────────────────
#
# The fixed pathway columns never showed the quest's own objective measure:
# a no-rubric dopant-stability quest ranks on ``energy``, and a completed
# structure relax (a converged energy, no pathway yet) rendered ``beaten``
# with every measure cell ``-`` — the tick could not confirm a frontier
# position from the table at all and filed a needs-tool gripe.


class TestObjectiveColumns:
    def _energy_quest(
        self, store: Any, text: str = "A dopant-stability striving"
    ) -> int:
        """A quest with NO ``meta.rubric_objectives`` — the default
        ``energy (min)`` objective applies."""
        resp = QuestHandler(hub=Hub(store=store)).put(text=text)
        m = re.search(r"\bqu(\d+)\b", resp.body)
        assert m is not None, resp.body
        qid = int(m.group(1))
        store.stamp_ref_meta(qid, {"reaction_config": {"slab": {"element": "Pd"}}})
        return qid

    def test_results_columns_appends_objectives_not_already_columns(
        self,
    ) -> None:
        from precis.quest.results_table import results_columns

        assert results_columns([]) == RESULTS_COLUMNS
        # barrier and energy are fixed columns — dedup'd in place, not
        # re-appended after the fixed block.
        assert results_columns([("barrier", "min"), ("energy", "min")]) == (
            RESULTS_COLUMNS
        )
        # an objective outside the fixed block appends after it.
        cols = results_columns([("log_tof", "max")])
        assert cols == (*RESULTS_COLUMNS, "log_tof")

    def test_default_energy_objective_shows_the_converged_relax(
        self, store: Any
    ) -> None:
        qid = self._energy_quest(store)
        winner = _mk_candidate(store, qid, "ReSub", _dopant_ops("Ag", 1))
        _converge(store, winner, energy=-137.984)
        pd = _mk_candidate(store, qid, "PdSub", _dopant_ops("Cu", 1))
        _converge(store, pd, energy=-132.04336773213768)
        waiting = _mk_candidate(store, qid, "AuSub", _dopant_ops("Zn", 1))

        fr = quest_frontier(store, qid)
        rows = build_results_rows(store, qid, fr=fr)
        by_sid = {r["ref_id"]: r for r in rows}
        # the converged relax result is the row's objective value — %.6g,
        # the leaderboard/frontier-summary precision, not the 3-fig measure
        # shorthand that would collapse -132.043 to -132.
        assert by_sid[pd]["energy"] == "-132.043"
        assert by_sid[pd]["band"] == "beaten"  # ReSub's relax is lower
        assert by_sid[winner]["energy"] == "-137.984"
        assert by_sid[winner]["band"] == "frontier"
        # never measured → '-', not a fabricated zero
        assert by_sid[waiting]["energy"] == "-"

        from precis.quest.results_table import render_results_table, results_columns

        text = render_results_table(rows, columns=results_columns(fr.objectives))
        assert "energy" in text.splitlines()[0]
        assert "-132.043" in text
        assert "-137.984" in text

    def test_tick_prompt_results_table_carries_the_energy(self, store: Any) -> None:
        qid = self._energy_quest(store)
        pd = _mk_candidate(store, qid, "PdSub", _dopant_ops("Ag", 1))
        _converge(store, pd, energy=-132.04336773213768)
        quest = store.get_ref(kind="quest", id=qid)
        prompt = build_tick_prompt(store, quest)
        assert "## Results so far" in prompt
        assert "energy" in prompt
        assert "-132.043" in prompt

    def test_view_results_toon_includes_the_objective_column(self, store: Any) -> None:
        qid = self._energy_quest(store)
        pd = _mk_candidate(store, qid, "PdSub", _dopant_ops("Ag", 1))
        _converge(store, pd, energy=-132.04336773213768)
        body = QuestHandler(hub=Hub(store=store)).get(id=qid, view="results").body
        assert "energy" in body
        assert "-132.043" in body
        text = (
            QuestHandler(hub=Hub(store=store))
            .get(id=qid, view="results", args={"format": "text"})
            .body
        )
        assert "text table (as embedded in the tick prompt)" in text
        assert "-132.043" in text

    def test_declared_rubric_objective_not_among_fixed_columns(
        self, store: Any
    ) -> None:
        """A rubric objective that isn't one of the fixed pathway columns
        (log_tof for a kinetics quest) gets its own column too."""
        qid = self._energy_quest(store, "A kinetics striving")
        store.stamp_ref_meta(
            qid, {"rubric_objectives": [{"key": "log_tof", "sense": "max"}]}
        )
        sid = _mk_candidate(store, qid, "active", _dopant_ops("Ag", 1))
        _converge(store, sid)
        store.stamp_ref_meta(sid, {"log_tof": 3.7, "kinetics_trusted": True})

        fr = quest_frontier(store, qid)
        assert fr.objectives == [("log_tof", "max")]
        rows = build_results_rows(store, qid, fr=fr)
        assert rows[0]["log_tof"] == "3.7"
        body = QuestHandler(hub=Hub(store=store)).get(id=qid, view="results").body
        assert "log_tof" in body and "3.7" in body


# ── trusted filter + measure sort (gr592277) ──────────────────────────────
#
# gr592277: no quest view answered "which candidate is trusted AND best" —
# trust was a per-row prose flag with no filter, and the leaderboard sorted
# by the primary objective only. The fix: a shared tri-state verdict
# (:func:`precis.quest.frontier.candidate_trusted`), ``args={'trusted':
# true}`` on frontier/leaderboard/results, ``args={'sort': '<measure>'}``
# ranking by any carried measure, and the empty-frontier headline naming
# the trusted-complement handles per axis.


class TestCandidateTrusted:
    """The tri-state verdict the views' ``trusted`` filter and the results
    ``trusted`` column share."""

    @staticmethod
    def _cand(flags: dict[str, Any]) -> Any:
        from precis.quest.frontier import Candidate

        return Candidate(
            ref_id=1,
            handle="st1",
            name="x",
            measures={},
            converged=True,
            flags=flags,
        )

    def test_no_signal_is_unknown_not_untrusted(self) -> None:
        from precis.quest.frontier import candidate_trusted

        assert candidate_trusted(self._cand({})) is None

    def test_affirmative_stamp_is_trusted(self) -> None:
        from precis.quest.frontier import candidate_trusted

        assert candidate_trusted(self._cand({"barrier_trusted": True})) is True
        assert candidate_trusted(self._cand({"kinetics_trusted": True})) is True

    def test_any_unconfirmed_mark_is_untrusted(self) -> None:
        from precis.quest.frontier import candidate_trusted

        for flags in (
            {"barrier_trusted": False},
            {"selectivity_trusted": False},
            {"kinetics_trusted": False},
            {"barrier_trusted": True, "kinetics_trusted": False},
            {"network_stale": True},
            {"P_side_untrusted_value": 0.4},
            {"U_L_abs_untrusted_value": 0.9},
        ):
            assert candidate_trusted(self._cand(flags)) is False, flags

    def test_unbounded_flag_is_not_an_untrusted_stash(self) -> None:
        """``span_at_Uopt_unbounded``/``U_opt_unbounded`` end in
        ``_unbounded``, not ``_untrusted_value`` — a window-clamped axis
        still rides as trusted."""
        from precis.quest.frontier import candidate_trusted

        assert (
            candidate_trusted(
                self._cand({"barrier_trusted": True, "span_at_Uopt_unbounded": 1.2})
            )
            is True
        )


class TestFrontierEmptyComplement:
    """The empty-frontier headline names, per untrusted axis, the converged
    handles that DO carry it trusted (≤10 — the qu164903 question)."""

    @staticmethod
    def _pc(sid: int, measures: dict[str, float], untrusted: frozenset[str]) -> Any:
        from precis.quest.frontier import Candidate, ProvisionalCandidate

        return ProvisionalCandidate(
            candidate=Candidate(
                ref_id=sid,
                handle=f"st{sid}",
                name=f"c{sid}",
                measures={},
                converged=True,
            ),
            measures=measures,
            untrusted_keys=untrusted,
            reasons=[],
        )

    def test_names_the_trusted_complement(self) -> None:
        from precis.handlers.quest import _frontier_empty_lines
        from precis.quest.frontier import FrontierResult

        fr = FrontierResult(
            objectives=[("barrier", "min"), ("U_L_abs", "min")],
            provisional=[
                self._pc(
                    1,
                    {"barrier": 0.5, "U_L_abs": 0.9},
                    frozenset({"barrier", "U_L_abs"}),
                ),
                # st2's barrier is trusted — it sits provisional only
                # because its U_L_abs is unconfirmed.
                self._pc(2, {"barrier": 0.7, "U_L_abs": 0.8}, frozenset({"U_L_abs"})),
            ],
        )
        line = _frontier_empty_lines(fr)[0]
        assert "barrier ×1 (trusted: st2)" in line
        assert "U_L_abs ×2 (trusted: none)" in line

    def test_complement_over_cap_counts_instead(self) -> None:
        from precis.handlers.quest import _TRUSTED_NAMES_CAP, _frontier_empty_lines
        from precis.quest.frontier import FrontierResult

        fr = FrontierResult(
            objectives=[("barrier", "min")],
            provisional=[
                self._pc(1, {"barrier": 0.5}, frozenset({"barrier"})),
                *(
                    self._pc(10 + i, {"barrier": 0.6 + i * 0.01}, frozenset())
                    for i in range(_TRUSTED_NAMES_CAP + 1)
                ),
            ],
        )
        line = _frontier_empty_lines(fr)[0]
        assert f"(trusted: {_TRUSTED_NAMES_CAP + 1} candidates)" in line
        assert "st10" not in line


class TestLeaderboardSortArg:
    """``leaderboard(fr, sort=(key, sense))`` — the within-band sort key
    the handler's ``args={'sort': k}`` resolves to."""

    def _fr(self) -> Any:
        from precis.quest.frontier import Candidate, FrontierResult

        return FrontierResult(
            objectives=[("barrier", "min")],
            frontier=[
                Candidate(
                    ref_id=1,
                    handle="st1",
                    name="a",
                    measures={"barrier": 0.3, "energy": -1.0},
                    converged=True,
                ),
                Candidate(
                    ref_id=2,
                    handle="st2",
                    name="b",
                    measures={"barrier": 0.9, "energy": -5.0},
                    converged=True,
                ),
            ],
        )

    def test_sort_param_reorders_and_keeps_the_column_visible(self) -> None:
        from precis.quest.frontier import leaderboard

        rows, schema = leaderboard(self._fr())
        assert [r["design"] for r in rows] == ["st1", "st2"]  # barrier min
        assert "energy" not in schema

        rows, schema = leaderboard(self._fr(), sort=("energy", "min"))
        assert [r["design"] for r in rows] == ["st2", "st1"]
        assert "energy" in schema  # sorted-on measure stays visible

    def test_sort_sense_max_orders_descending(self) -> None:
        from precis.quest.frontier import leaderboard

        fr = self._fr()
        fr.frontier[0].measures["log_tof"] = 3.7
        fr.frontier[1].measures["log_tof"] = 5.1
        rows, _schema = leaderboard(fr, sort=("log_tof", "max"))
        assert [r["design"] for r in rows] == ["st2", "st1"]


class TestTrustedSortViews:
    """``args={'trusted': true, 'sort': '<measure>'}`` on the three
    candidate views — the "trusted AND best" read (gr592277)."""

    def _mixed_quest(self, store: Any) -> tuple[int, dict[str, int]]:
        """One frontier-trusted candidate, one untrusted provisional, one
        unmeasured awaiting (rubric: barrier min)."""
        qid = _mk_quest(store)
        good = _mk_candidate(store, qid, "Ag good", _dopant_ops("Ag", 1))
        _converge(store, good)
        store.stamp_ref_meta(good, {"barrier": 0.4, "barrier_trusted": True})
        bad = _mk_candidate(store, qid, "Cu bad", _dopant_ops("Cu", 1))
        _converge(store, bad)
        store.stamp_ref_meta(bad, {"barrier": 0.3, "barrier_trusted": False})
        waiting = _mk_candidate(store, qid, "Zn wait", _dopant_ops("Zn", 1))
        return qid, {"good": good, "bad": bad, "waiting": waiting}

    @staticmethod
    def _handle(sid: int) -> str:
        from precis.utils import handle_registry

        h = handle_registry.try_format("structure", sid)
        assert h is not None
        return h

    def test_results_trusted_filter_keeps_only_yes_rows(self, store: Any) -> None:
        qid, sids = self._mixed_quest(store)
        h = QuestHandler(hub=Hub(store=store))
        handles = {name: self._handle(sid) for name, sid in sids.items()}
        body = h.get(id=qid, view="results", args={"trusted": True}).body
        assert "filter: trusted only" in body
        assert handles["good"] in body
        assert handles["bad"] not in body
        assert handles["waiting"] not in body  # "-" rows are not "yes"

    def test_trusted_filter_applies_to_all_views(self, store: Any) -> None:
        qid, sids = self._mixed_quest(store)
        h = QuestHandler(hub=Hub(store=store))
        good_h = self._handle(sids["good"])
        bad_h = self._handle(sids["bad"])
        for view in ("frontier", "leaderboard"):
            body = h.get(id=qid, view=view, args={"trusted": True}).body
            assert "trusted only" in body, view
            assert good_h in body, view
            assert bad_h not in body, view

    def test_all_filtered_out_says_why(self, store: Any) -> None:
        qid = _mk_quest(store)
        sid = _mk_candidate(store, qid, "Cu bad", _dopant_ops("Cu", 1))
        _converge(store, sid)
        store.stamp_ref_meta(sid, {"barrier": 0.3, "barrier_trusted": False})
        h = QuestHandler(hub=Hub(store=store))
        handle = self._handle(sid)
        for view in ("results", "leaderboard", "frontier"):
            body = h.get(id=qid, view=view, args={"trusted": True}).body
            assert "no candidates pass the trusted filter" in body, view
        # and the unfiltered views still show the candidate
        assert handle in h.get(id=qid, view="results").body

    def test_results_sort_orders_rows_by_measure(self, store: Any) -> None:
        """Lineage order groups by dopant (Ag before Cu); sorting by
        barrier must re-order numerically — the untrusted ≈0.3 ahead of
        the trusted 0.4, the unmeasured row last."""
        qid, sids = self._mixed_quest(store)
        h = QuestHandler(hub=Hub(store=store))
        handles = {name: self._handle(sid) for name, sid in sids.items()}
        body = h.get(
            id=qid, view="results", args={"sort": "barrier", "budget": 100_000}
        ).body
        assert "sorted by barrier (min)" in body
        positions = {name: body.index(h) for name, h in handles.items()}
        assert positions["bad"] < positions["good"] < positions["waiting"]

    def test_sort_declared_max_sense_orders_descending(self, store: Any) -> None:
        qid = _mk_quest(store)
        store.stamp_ref_meta(
            qid, {"rubric_objectives": [{"key": "log_tof", "sense": "max"}]}
        )
        a = _mk_candidate(store, qid, "Ag", _dopant_ops("Ag", 1))
        _converge(store, a)
        store.stamp_ref_meta(a, {"log_tof": 3.7, "kinetics_trusted": True})
        b = _mk_candidate(store, qid, "Cu", _dopant_ops("Cu", 1))
        _converge(store, b)
        store.stamp_ref_meta(b, {"log_tof": 5.1, "kinetics_trusted": True})
        h = QuestHandler(hub=Hub(store=store))
        body = h.get(id=qid, view="results", args={"sort": "log_tof"}).body
        assert "sorted by log_tof (max)" in body
        a_pos = body.index(self._handle(a))
        b_pos = body.index(self._handle(b))
        assert b_pos < a_pos

    def test_bad_args_raise_bad_input(self, store: Any) -> None:
        from precis.errors import BadInput

        qid, _sids = self._mixed_quest(store)
        h = QuestHandler(hub=Hub(store=store))
        with pytest.raises(BadInput):
            h.get(id=qid, view="results", args={"trusted": "yes"})
        with pytest.raises(BadInput):
            h.get(id=qid, view="leaderboard", args={"sort": 5})
        with pytest.raises(BadInput) as exc_info:
            h.get(id=qid, view="frontier", args={"sort": "nosuchaxis"})
        # the refusal names the measures a candidate actually carries
        assert "barrier" in str(exc_info.value)

    def test_empty_frontier_headline_names_trusted_handles(self, store: Any) -> None:
        """The qu164903 shape: every converged candidate fails a required
        axis — the per-axis untrusted counts name the complement (the
        trusted-carrying handles)."""
        qid = _mk_quest(store)
        store.stamp_ref_meta(
            qid,
            {
                "rubric_objectives": [
                    {"key": "barrier", "sense": "min"},
                    {"key": "U_L_abs", "sense": "min"},
                ]
            },
        )
        # Trusted barrier, never measured U_L_abs → provisional on the
        # missing axis; it is the trusted-barrier complement.
        a = _mk_candidate(store, qid, "Ag half", _dopant_ops("Ag", 1))
        _converge(store, a)
        store.stamp_ref_meta(a, {"barrier": 0.4, "barrier_trusted": True})
        b = _mk_candidate(store, qid, "Cu bad", _dopant_ops("Cu", 1))
        _converge(store, b)
        store.stamp_ref_meta(
            b, {"barrier": 0.5, "U_L_abs": 0.9, "barrier_trusted": False}
        )
        body = QuestHandler(hub=Hub(store=store)).get(id=qid, view="frontier").body
        assert f"barrier ×1 (trusted: {self._handle(a)})" in body
        assert "U_L_abs ×1 (trusted: none)" in body
