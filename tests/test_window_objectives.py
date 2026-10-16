"""SPSO slice 6 — window/Shapley quest objectives, the harvest stamp,
active anchor proposals, and the leaderboard's validity column.

Pure-function tests reuse ``test_staircase_masks``'s hand-checkable grid:
``U`` = -1..1 in 0.25 steps (9 points), ``pH`` = 0..7 in 1.0 steps
(8 points) — one U column measures 2.0 V·pH, the grid 18.0 V·pH. A rung
``(delta_g0, n_e=1, eps=0)`` is viable for ``U <= -delta_g0`` and forbids
the columns above it.
"""

from __future__ import annotations

import re
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import numpy as np
import pytest

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.quest import compute as compute_mod
from precis.quest import window_objectives as wo
from precis.quest.frontier import leaderboard, quest_frontier
from precis.store import Store
from precis.store.types import Tag
from precis_pathway import staircase_masks as sm
from precis_pathway import surface_pourbaix as sp

U = np.linspace(-1.0, 1.0, 9)
PH = np.linspace(0.0, 7.0, 8)
COL_VPH = 2.0
GRID_VPH = 18.0


def _resting(wanted: np.ndarray | None = None) -> sm.RestingMap:
    w = np.ones((PH.size, U.size), dtype=bool) if wanted is None else wanted
    return sm.RestingMap(U, PH, w)


def _rung(name: str, g0: float, n_e: int = 1, **kw: Any) -> sm.Rung:
    return sm.Rung(name=name, delta_g0=g0, n_e=n_e, **kw)


def _cols(r: sm.RestingMap, *u_values: float) -> np.ndarray:
    m = np.zeros(r.shape, dtype=bool)
    for u in u_values:
        m[:, int(np.argmin(np.abs(r.u_rhe - u)))] = True
    return m


# ── window_measures (pure) ────────────────────────────────────────────────


def test_one_rung_window_is_hand_checkable() -> None:
    """dg = -0.5 + U <= 0 → viable U <= 0.5: the last two columns are
    forbidden, the window is the other seven."""
    r = _resting()
    route = sm.Route("main", (_rung("r1", -0.5),))
    out = wo.window_measures(r, [route], None, eps_eV=None, her_margin_eV=0.0)
    assert out["window_area_VpH"] == pytest.approx(7 * COL_VPH)
    assert out["window_fraction"] == pytest.approx(7 / 9)
    # one rung owns every lost cell: sumsq = 1 there, ipr = 1.
    assert out["shapley_sumsq_raw"] == pytest.approx(2 * COL_VPH)
    assert out["shapley_ipr"] == pytest.approx(1.0)
    assert out["ridge_churn"] == pytest.approx(0.0)
    assert out["bottleneck_rung"] == "r1"
    assert out["her_margin_at_Uopt"] is None
    assert any("HER" in n for n in out["window_notes"])


def test_two_overlapping_rungs_split_the_shapley() -> None:
    """r2 alone forbids cols {0, .25, .5}; r1+r2 both forbid {.75, 1} —
    shares 1 and 1/2+1/2; window = cols -1..-.25."""
    r = _resting()
    route = sm.Route("main", (_rung("r1", -0.5), _rung("r2", 0.25)))
    out = wo.window_measures(r, [route], None)
    assert out["window_area_VpH"] == pytest.approx(4 * COL_VPH)
    assert out["window_fraction"] == pytest.approx(4 / 9)
    # sumsq·area: 3 cols at 1.0 (r2 only) + 2 cols at 0.5 (tied)
    assert out["shapley_sumsq_raw"] == pytest.approx(3 * COL_VPH + 2 * COL_VPH * 0.5)
    # ipr: 2.0 on the tied cols, 1.0 on r2-only cols → (3*1 + 2*2) / 5
    assert out["shapley_ipr"] == pytest.approx(7 / 5)
    assert out["ridge_churn"] == pytest.approx(0.0)


def test_adjacent_dominance_gives_ridge_churn() -> None:
    """An oxidation rung owns the left tail, a reduction rung owns
    everything right of U≈0 — their dominant regions abut."""
    r = _resting()
    route = sm.Route(
        "main",
        (
            _rung("red", 0.0, eps_eV=-0.01),  # forbids U >= -0.01 → cols 0..1
            _rung("ox", 0.0, n_e=-1),  # viable U >= 0 → forbids -1..-0.25
        ),
    )
    out = wo.window_measures(r, [route], None)
    # ox owns cols -1..-0.25, red owns cols 0..1 — they abut at -0.25|0,
    # so every cell in those two columns (8 pH rows each) differs from a
    # dominant-holding neighbour.
    assert out["ridge_churn"] == pytest.approx(16 / 72)
    assert out["window_area_VpH"] == pytest.approx(0.0)


def test_her_dominating_gives_negative_margin() -> None:
    """HER's limiting step is always below the main route's → no window,
    and the margin at the resting centroid is negative."""
    r = _resting()
    main = sm.Route("norr", (_rung("r1", 0.0),))  # limiting dg = U
    her = sm.Route("her", (_rung("h1", -0.5, kind="her"),))  # limiting dg = U-0.5
    out = wo.window_measures(r, [main], her)
    assert out["window_area_VpH"] == pytest.approx(0.0)
    assert out["her_margin_at_Uopt"] == pytest.approx(-0.5)
    assert any("empty" in n for n in out["window_notes"])


def test_centroid_in_forbidden_gap_snaps_to_live_column() -> None:
    """Two disjoint in-window strips (cols {-1,-0.75,-0.5} and {0.5,0.75})
    put the area-weighted centroid nearest the forbidden col at -0.25|0.
    The read snaps to the nearest in-window column (-0.5): with main dg =
    -1.6 + 2U and HER dg = -0.6 + U (HER loses on every wanted column),
    the margin is 1.0 - U, so 1.5 exactly at the snapped point."""
    r = _resting(_cols(_resting(), -1.0, -0.75, -0.5, 0.5, 0.75))
    main = sm.Route("norr", (_rung("m", -1.6, n_e=2),))
    her = sm.Route("her", (_rung("h", -0.6, kind="her"),))
    out = wo.window_measures(r, [main], her)
    assert out["her_margin_at_Uopt"] == pytest.approx(1.5)
    assert any("forbidden gap" in n for n in out["window_notes"])


def test_her_margin_positive_when_main_wins() -> None:
    """HER's limiting step sits above the main's → mask lets the whole
    resting map through and the margin is positive."""
    r = _resting()
    main = sm.Route("norr", (_rung("r1", -0.5),))
    her = sm.Route("her", (_rung("h1", 0.4, kind="her"),))
    out = wo.window_measures(r, [main], her)
    assert out["window_fraction"] == pytest.approx(7 / 9)
    assert out["her_margin_at_Uopt"] is not None
    assert out["her_margin_at_Uopt"] > 0


def test_missing_inputs_return_nones_with_notes() -> None:
    out = wo.window_measures(None, [], None)
    for k in wo.WINDOW_MEASURE_KEYS:
        assert out[k] is None
    assert out["bottleneck_rung"] is None
    assert "no resting map" in out["window_notes"]
    assert "no main routes" in out["window_notes"]

    out = wo.window_measures(_resting(), [sm.Route("empty")], None)
    assert all(out[k] is None for k in wo.WINDOW_MEASURE_KEYS)
    assert "no main routes" in out["window_notes"]


def test_eps_fills_only_unset_rungs() -> None:
    """``eps_eV`` is the quest's tolerance for rungs that carry none; a
    rung's own eps always wins."""
    r = _resting()
    route = sm.Route("main", (_rung("a", 0.25), _rung("b", 0.25, eps_eV=0.5)))
    out = wo.window_measures(r, [route], None, eps_eV=0.4)
    # a: dg = 0.25+U <= 0.4 → forbids U > 0.15 → cols .25,.5,.75,1
    # b: dg = 0.25+U <= 0.5 → forbids U > 0.25 → cols .5,.75,1
    # window = cols -1..0 → 5 cols
    assert out["window_fraction"] == pytest.approx(5 / 9)


def test_best_route_is_the_largest_window() -> None:
    r = _resting()
    tight = sm.Route("tight", (_rung("t", 0.25),))
    wide = sm.Route("wide", (_rung("w", -0.75),))
    out = wo.window_measures(r, [tight, wide], None)
    assert out["window_fraction"] == pytest.approx(8 / 9)
    assert any("wide" in n for n in out["window_notes"])


# ── slice 7: reset measures beside the thermodynamic window ──────────────


def test_reset_window_is_the_window_intersected_with_reset_ok() -> None:
    """dg = -0.5 + U: the thermodynamic window is cols U <= 0.5 (7), the
    guaranteed window is cols U <= 0.3 (6) — stamped beside, never
    blended."""
    r = _resting()
    route = sm.Route("main", (_rung("r1", -0.5),))
    out = wo.window_measures(r, [route], None)
    assert out["window_area_VpH"] == pytest.approx(7 * COL_VPH)
    assert out["reset_window_area"] == pytest.approx(6 * COL_VPH)
    assert out["reset_window_fraction"] == pytest.approx(6 / 9)
    assert out["reset_estimators"] == {"r1": "overdrive"}
    assert out["reset_limited_by"] == "r1"


def test_reset_window_dies_on_a_shallow_flat_rung() -> None:
    """A chemical step 0.1 eV downhill is viable everywhere and
    guaranteed nowhere — the whole map is the hysteretic band and the
    reset window is empty (the thermodynamic window is untouched)."""
    r = _resting()
    route = sm.Route("main", (_rung("ch", -0.1, n_e=0, kind="chemical"),))
    out = wo.window_measures(r, [route], None)
    assert out["window_fraction"] == pytest.approx(1.0)
    assert out["reset_window_area"] == pytest.approx(0.0)
    assert out["reset_window_fraction"] == pytest.approx(0.0)


def test_reset_margins_price_bep_and_neb_edges() -> None:
    """Rung kinetics carried through the adapter: the BEP edge prices
    (β − floor)/α, the barrier edge its measured E_a, the bare edge η."""
    r = _resting()
    route = sm.Route(
        "main",
        (
            _rung("a", -0.5, bep=(0.5, 0.4)),  # m = 0.8 → U <= -0.3
            _rung("b", -0.5, barrier_ea=0.05),  # m = 0.05 → U <= 0.45
            _rung("c", -0.5),  # m = η = 0.2 → U <= 0.3
        ),
    )
    out = wo.window_measures(r, [route], None)
    assert out["reset_estimators"] == {
        "a": "bep",
        "b": "neb",
        "c": "overdrive",
    }
    assert out["reset_limited_by"] == "a"
    # window (all rungs viable: U <= 0.5) ∩ reset (a needs U <= -0.3)
    assert out["window_fraction"] == pytest.approx(7 / 9)
    assert out["reset_window_fraction"] == pytest.approx(3 / 9)


def test_reset_eta_is_a_quest_knob() -> None:
    r = _resting()
    route = sm.Route("main", (_rung("r1", -0.5),))
    out = wo.window_measures(r, [route], None, reset_eta_eV=0.6)
    # guaranteed dg <= -0.6 → U <= -0.1 → cols through -0.25 (4)
    assert out["reset_window_area"] == pytest.approx(4 * COL_VPH)


def test_reset_measures_survive_a_missing_contour_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The contour module absent degrades to a note, not a crash."""
    monkeypatch.setitem(sys.modules, "precis_pathway.reset_contour", None)
    r = _resting()
    route = sm.Route("main", (_rung("r1", -0.5),))
    out = wo.window_measures(r, [route], None)
    assert out["reset_window_area"] is None
    assert any("reset_contour" in n for n in out["window_notes"])
    assert out["window_area_VpH"] == pytest.approx(7 * COL_VPH)


# ── stamp_window_measures (store) ─────────────────────────────────────────

AREA = 10.0
SPEC = {
    "cell": {"a": 8.4, "b": 8.4, "c": 24.0, "pbc": [True, True, False]},
    "ops": [{"op": "add_atom", "element": "Pd", "frac": [0.0, 0.0, 0.5]}],
}


def _payload(model: str, gammas: dict[str, list[float]]) -> dict[str, Any]:
    """Same synthetic coverage payload shape as test_pourbaix_map."""
    ads = {}
    for frag, gs in gammas.items():
        ads[frag] = {
            "mu": -3.0,
            "site": "fcc",
            "c_ads": 0.0,
            "points": [
                {
                    "n": i + 1,
                    "theta": (i + 1) / 4,
                    "energy": -100.0 + g * AREA,
                    "gamma": g,
                    "converged": True,
                    "detached": False,
                }
                for i, g in enumerate(gs)
            ],
            "best": {"theta": 0.25, "n": 1, "gamma": min(gs)},
        }
    return {
        "model": model,
        "facets": [
            {
                "facet": "111",
                "miller": [1, 1, 1],
                "area": AREA,
                "n_top": 4,
                "n_slab": 36,
                "e_slab": -100.0,
                "e_bulk_per_atom": -3.0,
                "gamma_clean": 0.1,
                "adsorbates": ads,
                "winner": {"adsorbate": None, "theta": 0.0, "gamma": 0.0},
            }
        ],
        "warnings": [],
    }


def _sweep(payloads: dict[str, dict[str, Any]], **kw: Any) -> dict[str, Any]:
    kw.setdefault("u_window", (-0.5, 0.5))
    kw.setdefault("grid", 21)
    kw.setdefault("sigma_e_ev", 0.05)
    return sp.sweep(payloads, **kw)


def _mk_quest(store: Store, hub: Hub, **meta: Any) -> int:
    resp = QuestHandler(hub=hub).put(text="window quest")
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None
    qid = int(m.group(1))
    if meta:
        store.stamp_ref_meta(qid, meta)
    return qid


SPEC_B = {
    "cell": {"a": 8.4, "b": 8.4, "c": 24.0, "pbc": [True, True, False]},
    "ops": [{"op": "add_atom", "element": "Pt", "frac": [0.0, 0.0, 0.4]}],
}


def _mk_candidate(store: Store, qid: int, name: str = "Pd") -> int:
    spec = SPEC if name != "B" else SPEC_B
    sid = compute_mod.ensure_candidate(store, qid, {"name": name, "structure": spec})
    assert sid is not None
    store.structure_record_run(
        sid,
        fidelity="ml",
        on_version=1,
        converged=True,
        n_steps=5,
        max_disp=0.0,
        energy=-5.0,
    )
    return sid


def _scan(store: Store, sid: int, sweep: dict[str, Any], **meta: Any) -> int:
    # The job's model is the one the pooled sweep drew (c2c634274: a sweep
    # pooling models the footing's jobs didn't scan is dropped as foreign).
    swept = sorted(sweep.get("models") or ["mace:medium"])
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="scan",
        parent_id=sid,
        meta={
            "job_type": "surface_coverage_scan",
            "anchor_key": "a" * 64,
            "model": swept[0],
            "engine_version": "0.24.1",
            "corrections": None,
            "che_sweep": sweep,
            **meta,
        },
    )
    store.add_tag(
        ref.id, Tag.closed("STATUS", "succeeded"), set_by="agent", replace_prefix=True
    )
    return ref.id


def _fake_routes(
    monkeypatch: pytest.MonkeyPatch,
    routes: list[sm.Route],
    her: sm.Route | None,
    missing: list[str] | None = None,
) -> None:
    mod = ModuleType("precis_pathway.staircase_routes")

    def routes_for_pathway(_store: Any, _pathway_ref: Any) -> Any:
        return SimpleNamespace(
            routes=routes, her=her, footing=None, missing=missing or []
        )

    monkeypatch.setattr(mod, "routes_for_pathway", routes_for_pathway, raising=False)
    monkeypatch.setitem(sys.modules, "precis_pathway.staircase_routes", mod)


def _structure_ref(store: Store, sid: int) -> Any:
    return store.fetch_refs_by_ids({sid})[sid]


def test_stamp_runs_without_the_rubric_flag(
    store: Store, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr489165: the fields stamp whenever the scan ran — the rubric
    declaration only promotes them to Pareto axes and the logbook. An
    unflagged quest gets the stamps but no per-candidate logbook entry."""
    entries: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "precis.quest.logbook.append_entry", lambda *_a, **kw: entries.append(kw)
    )
    qid = _mk_quest(store, hub)  # no rubric_objectives → default energy
    sid = _mk_candidate(store, qid)
    _scan(store, sid, _sweep({"m": _payload("m", {"H": [-0.02]})}))
    store.insert_ref(
        kind="pathway", slug=f"pwtest{sid}", title="pw", meta={"candidate_ref": sid}
    )
    _fake_routes(monkeypatch, [sm.Route("norr", (_rung("r1", 0.13),))], None)

    note = wo.stamp_window_measures(store, qid, _structure_ref(store, sid))
    assert note is not None and "window measures" in note
    meta = dict(store.fetch_refs_by_ids({sid})[sid].meta or {})
    assert meta["window_area_VpH"] is not None
    assert meta["window_fraction"] is not None
    assert meta["shapley_ipr"] is not None
    assert meta["window_notes"]
    assert meta[wo.WINDOW_INPUTS_KEY]
    assert not entries, "no logbook entry on an unflagged quest"


def test_flagged_quest_logs_the_stamp(
    store: Store, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rubric declaration is what announces the stamp on the logbook."""
    entries: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "precis.quest.logbook.append_entry", lambda *_a, **kw: entries.append(kw)
    )
    qid = _mk_quest(
        store, hub, rubric_objectives=[{"key": "window_area_VpH", "sense": "max"}]
    )
    sid = _mk_candidate(store, qid)
    _scan(store, sid, _sweep({"m": _payload("m", {"H": [-0.02]})}))
    store.insert_ref(
        kind="pathway", slug=f"pwtest{sid}", title="pw", meta={"candidate_ref": sid}
    )
    _fake_routes(monkeypatch, [sm.Route("norr", (_rung("r1", 0.13),))], None)

    assert wo.stamp_window_measures(store, qid, _structure_ref(store, sid))
    assert len(entries) == 1 and entries[0]["entry_type"] == "result"


def test_her_margin_declared_but_pathway_lacks_network_her(
    store: Store, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr489168: the quest asks her_margin_at_Uopt but the pathway ran
    without network.her — the stamp names the missing config flag instead
    of leaving a silent None."""
    qid = _mk_quest(
        store,
        hub,
        rubric_objectives=[{"key": "her_margin_at_Uopt", "sense": "max"}],
    )
    sid = _mk_candidate(store, qid)
    _scan(store, sid, _sweep({"m": _payload("m", {"H": [-0.02]})}))
    store.insert_ref(
        kind="pathway",
        slug=f"pwtest{sid}",
        title="pw",
        meta={"candidate_ref": sid, "config": {"network": "ammonia"}},
    )
    _fake_routes(monkeypatch, [sm.Route("norr", (_rung("r1", 0.13),))], None)

    wo.stamp_window_measures(store, qid, _structure_ref(store, sid))
    meta = dict(store.fetch_refs_by_ids({sid})[sid].meta or {})
    assert meta.get("her_margin_at_Uopt") is None
    assert any("without network.her" in n for n in meta["window_notes"])


def test_her_margin_warn_needs_the_declaration_and_the_flag_off(
    store: Store, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The warn is the ask↔config mismatch: undeclared quests and
    network.her-on pathways (the adapter's own ``missing`` case) don't
    get it. The flag is checked in both stored spellings — the YAML
    ``network: {her: true}`` mapping and the ``Config.to_dict()`` flat
    ``network_her`` (gr489168 follow-up: the stored pathway config
    serialises ``network`` to the bare kind string, so only the flat
    key is live)."""
    for meta_extra, declared in (
        ({"config": {"network": {"kind": "ammonia", "her": True}}}, True),
        ({"config": {"network": "ammonia", "network_her": True}}, True),
        ({"config": {"network": "ammonia"}}, False),
    ):
        qid = _mk_quest(
            store,
            hub,
            rubric_objectives=[{"key": "her_margin_at_Uopt", "sense": "max"}]
            if declared
            else [{"key": "window_area_VpH", "sense": "max"}],
        )
        sid = _mk_candidate(store, qid)
        _scan(store, sid, _sweep({"m": _payload("m", {"H": [-0.02]})}))
        store.insert_ref(
            kind="pathway",
            slug=f"pwtest{sid}",
            title="pw",
            meta={"candidate_ref": sid, **meta_extra},
        )
        _fake_routes(monkeypatch, [sm.Route("norr", (_rung("r1", 0.13),))], None)

        wo.stamp_window_measures(store, qid, _structure_ref(store, sid))
        meta = dict(store.fetch_refs_by_ids({sid})[sid].meta or {})
        assert meta.get("her_margin_at_Uopt") is None
        assert not any("without network.her" in n for n in meta["window_notes"]), (
            declared,
            meta["window_notes"],
        )


def test_her_margin_warn_still_fires_on_flat_network_her_false(
    store: Store, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stored ``network_her: false`` (the ``to_dict`` spelling) is HER
    genuinely off — the note still fires."""
    qid = _mk_quest(
        store,
        hub,
        rubric_objectives=[{"key": "her_margin_at_Uopt", "sense": "max"}],
    )
    sid = _mk_candidate(store, qid)
    _scan(store, sid, _sweep({"m": _payload("m", {"H": [-0.02]})}))
    store.insert_ref(
        kind="pathway",
        slug=f"pwtest{sid}",
        title="pw",
        meta={
            "candidate_ref": sid,
            "config": {"network": "ammonia", "network_her": False},
        },
    )
    _fake_routes(monkeypatch, [sm.Route("norr", (_rung("r1", 0.13),))], None)

    wo.stamp_window_measures(store, qid, _structure_ref(store, sid))
    meta = dict(store.fetch_refs_by_ids({sid})[sid].meta or {})
    assert meta.get("her_margin_at_Uopt") is None
    assert any("without network.her" in n for n in meta["window_notes"])


def test_stamp_stamps_measures_and_validity(
    store: Store, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    qid = _mk_quest(
        store,
        hub,
        rubric_objectives=[
            {"key": "energy", "sense": "min"},
            {"key": "window_area_VpH", "sense": "max"},
        ],
        operating_conditions={"U_RHE": -0.3, "pH": 7},
    )
    sid = _mk_candidate(store, qid)
    _scan(store, sid, _sweep({"m": _payload("m", {"H": [-0.02]})}))
    store.insert_ref(
        kind="pathway", slug=f"pwtest{sid}", title="pw", meta={"candidate_ref": sid}
    )
    route = sm.Route("norr", (_rung("r1", 0.13),))  # viable U <= -0.13
    her = sm.Route("her", (_rung("h1", 0.5, kind="her"),))
    _fake_routes(monkeypatch, [route], her)

    note = wo.stamp_window_measures(store, qid, _structure_ref(store, sid))
    assert note is not None and "window measures" in note
    meta = dict(store.fetch_refs_by_ids({sid})[sid].meta or {})
    # window: wanted = H region (U <= ~0.2) ∩ rung (U <= -0.13) ∩ her
    # (0.13+U <= 0.5+U → 0.13 <= 0.5 always) → U in [-0.5, -0.15]: 8 cols
    # of the 21-point grid.
    assert meta["window_fraction"] == pytest.approx(8 / 21)
    assert meta["window_area_VpH"] == pytest.approx(
        meta["window_fraction"] * 15.225, rel=0.02
    )
    assert meta["bottleneck_rung"] == "r1"
    # centroid of the 8 window cols is U = -0.325: (0.5-0.325)-(0.13-0.325)
    assert meta["her_margin_at_Uopt"] == pytest.approx(0.37, abs=0.01)
    v = meta["validity_region_at_point"]
    assert v["termination"] == "H* at 0.25 ML"
    assert "uncorrected footing" in v["trust"]
    assert v["footing"]["engine_version"] == "0.24.1"

    # idempotent: a second pass changes nothing and stays silent
    assert wo.stamp_window_measures(store, qid, _structure_ref(store, sid)) is None


def test_stamp_survives_missing_routes_module(
    store: Store, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B2's adapter absent → measures stay None, notes name it, validity
    still lands (it needs only the map)."""
    qid = _mk_quest(
        store,
        hub,
        rubric_objectives=[{"key": "window_area_VpH", "sense": "max"}],
        operating_conditions={"U_RHE": -0.3, "pH": 7},
    )
    sid = _mk_candidate(store, qid)
    _scan(store, sid, _sweep({"m": _payload("m", {"H": [-0.02]})}))
    store.insert_ref(
        kind="pathway", slug=f"pwtest{sid}", title="pw", meta={"candidate_ref": sid}
    )
    monkeypatch.setitem(sys.modules, "precis_pathway.staircase_routes", None)
    note = wo.stamp_window_measures(store, qid, _structure_ref(store, sid))
    meta = dict(store.fetch_refs_by_ids({sid})[sid].meta or {})
    assert note is not None
    assert meta.get("window_area_VpH") is None
    assert any("staircase_routes" in n for n in meta["window_notes"])
    assert meta["validity_region_at_point"]["termination"] == "H* at 0.25 ML"


def test_frontier_splits_on_window_keys(store: Store, hub: Hub) -> None:
    """Declared as rubric objectives, the stamped scalars drive the split."""
    qid = _mk_quest(
        store,
        hub,
        rubric_objectives=[
            {"key": "window_area_VpH", "sense": "max"},
            {"key": "shapley_ipr", "sense": "min"},
        ],
    )
    a = _mk_candidate(store, qid, "A")
    b = _mk_candidate(store, qid, "B")
    store.stamp_ref_meta(a, {"window_area_VpH": 5.0, "shapley_ipr": 2.0})
    store.stamp_ref_meta(b, {"window_area_VpH": 3.0, "shapley_ipr": 1.0})
    fr = quest_frontier(store, qid)
    assert sorted(c.ref_id for c in fr.frontier) == sorted([a, b])
    # a strictly worse on both → dominated
    store.stamp_ref_meta(b, {"window_area_VpH": 6.0, "shapley_ipr": 1.0})
    fr = quest_frontier(store, qid)
    assert [c.ref_id for c in fr.frontier] == [b]
    assert [c.ref_id for c in fr.dominated] == [a]


def test_unflagged_quest_leaderboard_bit_identical(store: Store, hub: Hub) -> None:
    """Snapshot a frontier/leaderboard on an unflagged quest: stamping
    nothing means identical output."""
    qid = _mk_quest(store, hub)
    sid = _mk_candidate(store, qid)
    fr1 = quest_frontier(store, qid)
    rows1, schema1 = leaderboard(fr1)
    wo.stamp_window_measures(store, qid, _structure_ref(store, sid))
    fr2 = quest_frontier(store, qid)
    rows2, schema2 = leaderboard(fr2)
    assert schema1 == schema2
    assert rows1 == rows2


def test_leaderboard_shows_validity_column(
    store: Store, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    qid = _mk_quest(
        store,
        hub,
        rubric_objectives=[
            {"key": "energy", "sense": "min"},
            {"key": "window_area_VpH", "sense": "max"},
        ],
        operating_conditions={"U_RHE": -0.3, "pH": 7},
    )
    sid = _mk_candidate(store, qid)
    _scan(store, sid, _sweep({"m": _payload("m", {"H": [-0.02]})}))
    store.insert_ref(
        kind="pathway", slug=f"pwtest{sid}", title="pw", meta={"candidate_ref": sid}
    )
    _fake_routes(monkeypatch, [sm.Route("norr", (_rung("r1", 0.1),))], None)
    wo.stamp_window_measures(store, qid, _structure_ref(store, sid))
    rows, schema = leaderboard(quest_frontier(store, qid))
    assert "validity" in schema
    row = next(r for r in rows if r["design"] == f"st{sid}")
    assert "H* at 0.25 ML" in row["validity"]


def test_propose_anchor_scans_ranks_widest_first(store: Store, hub: Hub) -> None:
    qid = _mk_quest(
        store, hub, rubric_objectives=[{"key": "window_area_VpH", "sense": "max"}]
    )
    a = _mk_candidate(store, qid, "A")
    b = _mk_candidate(store, qid, "B")

    def _strip(u: float, band: float) -> dict[str, Any]:
        return {
            "U_RHE": u,
            "below": {"adsorbate": "H", "n": 7},
            "above": {"adsorbate": "H", "n": 9},
            "band_propagated": band,
            "band_model_form": None,
            "n_models": 1,
        }

    sweep_a = _sweep({"m": _payload("m", {"H": [-0.02]})})
    # Narrow strip listed first: the widest must win regardless of order.
    sweep_a["needs_anchor"] = [_strip(0.3, 0.01), _strip(0.1, 0.05)]
    sweep_b = _sweep({"m": _payload("m", {"H": [-0.02]})})
    sweep_b["needs_anchor"] = [_strip(-0.2, 0.21)]
    _scan(store, a, sweep_a)
    _scan(store, b, sweep_b, anchor_key="b" * 64)

    props = wo.propose_anchor_scans(store, qid)
    assert len(props) == 2
    assert props[0]["structure_ref"] == b  # widest band first
    assert props[0]["band_propagated_V"] == pytest.approx(0.21)
    assert props[0]["params"]["adsorbates"] == ["H"]
    assert props[0]["params"]["coverage_grid_extension"]["n"] == [7, 9]
    assert props[1]["structure_ref"] == a

    # propose-only: one note lands on the logbook, deduped across calls
    notes = [
        c for c in store.chunks.list_chunks_for_ref(qid) if c.chunk_kind == "quest_log"
    ]
    assert sum("anchor-scan proposal" in (c.text or "") for c in notes) == 1
    again = wo.propose_anchor_scans(store, qid)
    assert [p["structure_ref"] for p in again] == [b, a]
    notes2 = [
        c for c in store.chunks.list_chunks_for_ref(qid) if c.chunk_kind == "quest_log"
    ]
    assert len(notes2) == len(notes)
    assert props[1]["params"]["around_U_RHE"] == pytest.approx(0.1)


def test_propose_anchor_scans_unflagged_quest_writes_nothing(
    store: Store, hub: Hub
) -> None:
    qid = _mk_quest(store, hub)  # no window objective declared
    a = _mk_candidate(store, qid, "A")
    sweep = _sweep({"m": _payload("m", {"H": [-0.02]})})
    sweep["needs_anchor"] = [
        {
            "U_RHE": 0.1,
            "below": {"adsorbate": "H", "n": 7},
            "above": {"adsorbate": "H", "n": 9},
            "band_propagated": 0.05,
            "band_model_form": None,
            "n_models": 1,
        }
    ]
    _scan(store, a, sweep)
    quest = store.get_ref(kind="quest", id=qid)
    assert quest is not None
    before = dict(quest.meta or {})
    props = wo.propose_anchor_scans(store, qid)
    assert len(props) == 1  # still returned to the caller
    notes = [
        c for c in store.chunks.list_chunks_for_ref(qid) if c.chunk_kind == "quest_log"
    ]
    assert not any("anchor-scan proposal" in (c.text or "") for c in notes)
    after = store.get_ref(kind="quest", id=qid)
    assert after is not None
    assert dict(after.meta or {}) == before


def test_window_keys_default_to_max_sense_and_optional(store: Store, hub: Hub) -> None:
    from precis.quest.frontier import _objectives_for, _optional_objectives_for

    qid = _mk_quest(
        store,
        hub,
        rubric_objectives=[
            {"key": "energy"},
            {"key": "window_area_VpH"},
            {"key": "her_margin_at_Uopt"},
            {"key": "shapley_ipr"},
            {"key": "ridge_churn", "optional": False},
        ],
    )
    assert dict(_objectives_for(store, qid)) == {
        "energy": "min",
        "window_area_VpH": "max",
        "her_margin_at_Uopt": "max",
        "shapley_ipr": "min",
        "ridge_churn": "min",
    }
    assert _optional_objectives_for(store, qid) == frozenset(
        {"window_area_VpH", "her_margin_at_Uopt", "shapley_ipr"}
    )


def test_stamp_stamps_the_guaranteed_window_beside_the_thermodynamic_one(
    store: Store, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 7: declared ``reset_window_area`` stamps the guaranteed
    window (window ∩ reset_ok) next to the thermodynamic one, plus the
    per-edge estimator labels and the limiting edge."""
    qid = _mk_quest(
        store,
        hub,
        rubric_objectives=[
            {"key": "window_area_VpH", "sense": "max"},
            {"key": "reset_window_area", "sense": "max"},
        ],
        operating_conditions={"U_RHE": -0.3, "pH": 7},
    )
    sid = _mk_candidate(store, qid)
    _scan(store, sid, _sweep({"m": _payload("m", {"H": [-0.02]})}))
    store.insert_ref(
        kind="pathway", slug=f"pwreset{sid}", title="pw", meta={"candidate_ref": sid}
    )
    # viable dg <= 0 → U <= 0.125 (13 cols); guaranteed dg <= -0.2 →
    # U <= -0.075 (9 cols). Both boundaries land mid-cell.
    route = sm.Route("norr", (_rung("r1", -0.125),))
    _fake_routes(monkeypatch, [route], None)

    note = wo.stamp_window_measures(store, qid, _structure_ref(store, sid))
    assert note is not None
    meta = dict(store.fetch_refs_by_ids({sid})[sid].meta or {})
    # 21-pt grid over [-0.5, 0.5]: the H* region is wanted for U <= 0.15
    # (14 cols), the thermodynamic window cuts it to 13, the guaranteed
    # window to 9.
    assert meta["window_fraction"] == pytest.approx(13 / 21)
    assert meta["reset_window_fraction"] == pytest.approx(9 / 21)
    assert meta["reset_window_area"] == pytest.approx((9 / 21) * 15.225, rel=0.02)
    assert meta["reset_limited_by"] == "r1"
    assert meta["reset_estimators"] == {"r1": "overdrive"}
