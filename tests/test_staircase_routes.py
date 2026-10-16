"""SPSO slice 3: the ``che_affine`` → staircase adapter
(``precis_pathway.staircase_routes``) and the ``view='staircase'`` surfaces
on the pathway and structure kinds.

The fixture results dict mirrors the 0.25.0 engine export
(``pipeline._che_affine`` / ``_her_report``): ``che_affine.per_route``
steps carry ``{edge, delta_g0, n_e}`` with ``n_e`` proton-electron pairs
consumed (the 2026-10-13 sign ruling), the route string gives each step's
node names, and the optional ``her`` block is the HER competitor.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytest.importorskip("autocatpath")

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.structure import StructureHandler
from precis.store import Store
from precis.store.types import Tag
from precis_pathway import surface_pourbaix as sp
from precis_pathway.handler import PathwayHandler
from precis_pathway.staircase_routes import (
    render_staircase,
    routes_for_pathway,
    routes_from_results,
)

AREA = 10.0


def _results(*, her: bool = True) -> dict[str, Any]:
    """A 0.25.0-shape results.json: one scored route plus the HER branch."""
    r: dict[str, Any] = {
        "substrate": "NO",
        "target": "NH3",
        "network_digest": "digest0123456789",
        "corrections": {"gas": "catsel19"},
        "nodes": {
            "*": {"mean": 0.0, "std": 0.0},
            "NO": {"mean": -0.4, "std": 0.05},
            "N+OH": {"mean": -0.1, "std": 0.08},
            "H": {"mean": -0.1, "std": 0.03},
            "*~nh3": {"mean": 0.2, "std": None},
        },
        "U_L": -0.15,
        "selectivity": {
            "side_products": [{"leaf": "*~h2", "product": "H2", "span_eV": 0.9}]
        },
        "edges": [
            {
                "name": "no_ads",
                "reactant": "*",
                "product": "NO",
                "delta_e": {"mean": -0.5, "std": 0.05, "n": 1},
                "barrier": {"mean": 0.4, "std": 0.05, "n": 2},
            },
            {
                "name": "no_reduce",
                "reactant": "NO",
                "product": "N+OH",
                "delta_e": {"mean": 0.2, "std": 0.08, "n": 1},
                "bep": {"alpha": 0.55, "beta": 0.6, "n_fit": 3, "rmse": 0.04},
                "barrier": {"mean": 0.9, "std": 0.1, "n": 3},
            },
            {
                "name": "nh3_desorb",
                "reactant": "N+OH",
                "product": "*~nh3",
                "delta_e": {"mean": 0.3, "std": None, "n": 1},
            },
        ],
        "che_affine": {
            "scale": "RHE",
            "T": 298.15,
            "pH": 0.0,
            "per_route": [
                {
                    "route": "* -> NO -> N+OH -> *~nh3",
                    "steps": [
                        {"edge": "no_ads", "delta_g0": -0.4, "n_e": 0},
                        {"edge": "no_reduce", "delta_g0": 0.3, "n_e": 2},
                        {"edge": "nh3_desorb", "delta_g0": 0.3, "n_e": 0},
                    ],
                    "U_L": -0.15,
                    "U_opt": -0.1,
                    "span_at_UL": 0.7,
                }
            ],
        },
    }
    if her:
        r["her"] = {
            "U_L_her": -0.2,
            "product": "H2",
            "leaf": "*~h2",
            "dG_H_star": -0.1,
            "edges": [
                {
                    "step": "Volmer",
                    "edge": "*->H",
                    "reactant": "*",
                    "product": "H",
                    "delta_g0": -0.1,
                    "n_e": 1,
                },
                {
                    "step": "Heyrovsky",
                    "edge": "H->*~h2",
                    "reactant": "H",
                    "product": "*~h2",
                    "delta_g0": 0.1,
                    "n_e": 1,
                },
            ],
        }
    return r


# ── the adapter (pure) ──────────────────────────────────────────────────


def test_routes_from_results_builds_typed_routes_and_footing() -> None:
    sr = routes_from_results(_results(), engine_version="0.25.0")
    assert len(sr.routes) == 1
    route = sr.routes[0]
    assert route.name == "* -> NO -> N+OH -> *~nh3"
    kinds = [r.kind for r in route.rungs]
    assert kinds == ["chemical", "electrochemical", "desorption"]
    assert [r.name for r in route.rungs] == ["no_ads", "no_reduce", "nh3_desorb"]
    assert route.rungs[1].n_e == 2
    # ΔG(U) = ΔG0 + n_e·U: at -0.30 V the reduction rung drops by 0.6 eV
    assert float(route.rungs[1].delta_g(-0.30)) == pytest.approx(-0.3)
    # sigma = hypot of the endpoints' recorded stds
    assert route.rungs[0].sigma == pytest.approx(0.05)
    assert route.rungs[1].sigma == pytest.approx((0.05**2 + 0.08**2) ** 0.5)
    assert route.rungs[2].sigma == pytest.approx(0.08)
    assert sr.her is not None and all(r.kind == "her" for r in sr.her.rungs)
    assert sr.her_stats is not None
    assert sr.her_stats["U_L_her"] == pytest.approx(-0.2)
    assert sr.route_stats[route.name]["U_L"] == pytest.approx(-0.15)
    assert sr.footing["engine_version"] == "0.25.0"
    assert sr.footing["network_digest"] == "digest0123456789"
    assert sr.footing["corrections"] == {"gas": "catsel19"}
    assert sr.missing == []


def test_desorption_label_is_route_terminal_only() -> None:
    """Reto 2026-10-16 ruling (b): a mid-route edge into a ``~`` species
    is not labelled desorption; the route-terminal one is."""
    r = _results(her=False)
    r["che_affine"]["per_route"] = [
        {
            "route": "* -> NO -> *~no -> *~nh3",
            "steps": [
                {"edge": "*->NO", "delta_g0": -0.4, "n_e": 0},
                {"edge": "NO->*~no", "delta_g0": 0.2, "n_e": 0},
                {"edge": "*~no->*~nh3", "delta_g0": 0.1, "n_e": 0},
            ],
        }
    ]
    sr = routes_from_results(r, engine_version="0.25.0")
    kinds = [rung.kind for rung in sr.routes[0].rungs]
    assert kinds == ["chemical", "chemical", "desorption"]


def test_mid_route_tilde_step_is_not_merged_as_pcet() -> None:
    """The terminal-only label leaves the masks unchanged: a mid-route
    chemical step into a ``~`` landing never folds into a PCET rung."""
    from precis_pathway.staircase_masks import pcet_route

    r = _results(her=False)
    r["che_affine"]["per_route"] = [
        {
            "route": "* -> X -> X+H -> *~xh -> *~y",
            "steps": [
                {"edge": "*->X", "delta_g0": -0.2, "n_e": 0},
                {"edge": "X->X+H", "delta_g0": 0.1, "n_e": 1},
                {"edge": "X+H->*~xh", "delta_g0": 0.1, "n_e": 0},
                {"edge": "*~xh->*~y", "delta_g0": 0.1, "n_e": 0},
            ],
        }
    ]
    route = routes_from_results(r, engine_version="0.25.0").routes[0]
    assert route.rungs[2].kind == "chemical"
    assert pcet_route(route) == route


def test_routes_from_results_flags_a_pre_0250_results() -> None:
    sr = routes_from_results({"substrate": "NO", "nodes": {}})
    assert sr.routes == [] and sr.her is None
    assert any("che_affine absent (engine < 0.25.0)" in m for m in sr.missing)
    assert any("network_digest" in m for m in sr.missing)
    assert any("corrections" in m for m in sr.missing)


def test_routes_from_results_flags_missing_her_and_empty_routes() -> None:
    r = _results(her=False)  # her off, but H2 is a recorded side product
    sr = routes_from_results(r)
    assert sr.her is None
    assert any("her block absent though H2" in m for m in sr.missing)
    che = r["che_affine"]
    assert isinstance(che, dict)
    che["per_route"] = []
    sr = routes_from_results(r)
    assert sr.routes == []
    assert any("per_route empty" in m for m in sr.missing)


def test_rungs_carry_edge_kinetics_when_recorded() -> None:
    """Slice-7 ladder inputs: ``edges[name].bep`` → the rung's ``bep``
    tuple, ``edges[name].barrier.mean`` → ``barrier_ea``; an edge with
    neither keeps both ``None`` (the overdrive proxy's input)."""
    sr = routes_from_results(_results(), engine_version="0.25.0")
    a, b, c = sr.routes[0].rungs
    assert a.barrier_ea == pytest.approx(0.4) and a.bep is None
    assert b.bep == pytest.approx((0.55, 0.6)) and b.barrier_ea == pytest.approx(0.9)
    assert c.bep is None and c.barrier_ea is None


def test_kinetics_malformed_blocks_attach_nothing() -> None:
    r = _results()
    edges = r["edges"]
    assert isinstance(edges, list)
    edges[0]["bep"] = {"alpha": 0.5}  # beta missing → not a BEP record
    edges[0]["barrier"] = "n/a"  # not an Estimate dict → nothing
    edges[1]["bep"] = {"alpha": 0.5, "beta": None}
    sr = routes_from_results(r)
    a, b, _c = sr.routes[0].rungs
    assert a.bep is None and a.barrier_ea is None
    assert b.bep is None and b.barrier_ea == pytest.approx(0.9)


def test_reset_estimators_flow_through_the_adapter() -> None:
    """The adapted rungs feed the reset ladder directly: no_ads prices
    'neb' (barrier only), no_reduce 'bep' (BEP beats barrier),
    nh3_desorb 'overdrive' (no kinetics recorded)."""
    from precis_pathway import reset_contour as rc

    sr = routes_from_results(_results())
    edges = rc.reset_edges(sr.routes[0])
    assert [e.estimator for e in edges] == ["neb", "bep", "overdrive"]
    assert edges[0].margin_eV == pytest.approx(0.4)
    assert edges[1].margin_eV == pytest.approx(0.6 / 0.55)


def test_sigma_is_none_when_no_spread_was_recorded() -> None:
    r = _results()
    for est in r["nodes"].values():
        est.pop("std", None)
    sr = routes_from_results(r)
    assert all(rung.sigma is None for rung in sr.routes[0].rungs)


def test_routes_for_pathway_reads_the_stored_ref(store: Store) -> None:
    pw = store.insert_ref(
        kind="pathway",
        slug="pw_stair",
        title="NO → NH3 on Pd",
        meta={
            "status": "ready",
            "autocatpath_version": "0.25.0",
            "results": _results(),
        },
    )
    sr = routes_for_pathway(store, pw)
    assert len(sr.routes) == 1 and sr.her is not None
    assert sr.footing["engine_version"] == "0.25.0"
    sr2 = routes_for_pathway(store, "pw_stair")  # slug resolves too
    assert len(sr2.routes) == 1
    gone = routes_for_pathway(store, "pw_nope")
    assert gone.routes == [] and any("no pathway" in m for m in gone.missing)
    computing = store.insert_ref(
        kind="pathway",
        slug="pw_computing",
        title="t",
        meta={"status": "computing"},
    )
    sr3 = routes_for_pathway(store, computing)
    assert sr3.routes == [] and any("not computed" in m for m in sr3.missing)


# ── the text render ─────────────────────────────────────────────────────


def test_render_staircase_reports_rungs_limiting_her_and_footing() -> None:
    r = _results()
    sr = routes_from_results(r, engine_version="0.25.0")
    text = render_staircase(sr, r, u_rhe=-0.30, title="t", ph=0.0)
    assert "U_RHE -0.300 V" in text
    assert "ΔG(U) = ΔG0 + n_e·U" in text
    assert "no_reduce" in text and "-0.300" in text
    assert "limiting rung at U = -0.30 V: nh3_desorb" in text
    assert "U_L -0.150 V" in text and "U_opt -0.100 V" in text
    assert "U_L_her -0.200 V" in text and "HER margin" in text
    assert "engine 0.25.0" in text and "digest012345" in text
    assert "missing:" not in text


def test_render_staircase_marks_a_u_flat_blocker_in_the_header() -> None:
    """gr491369: a chemical/desorption rung over its eps blocks the route
    at every U — the header names it instead of a bare U_L."""
    r = _results()
    steps = r["che_affine"]["per_route"][0]["steps"]
    steps[2]["delta_g0"] = 0.8  # nh3_desorb: U-flat, over the 0.3 eV eps
    sr = routes_from_results(r, engine_version="0.25.0")
    text = render_staircase(sr, r, u_rhe=-0.30, title="t", ph=0.0)
    assert "U_L n/a — blocked by nh3_desorb (ΔG0 +0.800 eV, U-flat)" in text
    # the rung lines and the engine split value still render as provenance
    assert "nh3_desorb" in text and "U_opt -0.100 V" in text


def test_render_staircase_keeps_the_u_l_text_without_a_blocker() -> None:
    r = _results()  # nh3_desorb ΔG0 0.3 sits exactly at eps — not blocking
    sr = routes_from_results(r, engine_version="0.25.0")
    text = render_staircase(sr, r, u_rhe=-0.30, title="t", ph=0.0)
    assert "— U_L -0.150 V (one PCET rung per H+ + e-; engine split -0.150 V)" in text
    assert "blocked by" not in text


def test_render_staircase_resting_and_mismatch_lines() -> None:
    sr = routes_from_results(_results(), engine_version="0.25.0")
    text = render_staircase(
        sr,
        _results(),
        u_rhe=-0.30,
        structures=[
            {
                "handle": "st99",
                "resting": "H* at 1.00 ML",
                "assumed": "clean",
                "mismatch": "coads mismatch: the pathway assumed clean, "
                "but the resting termination at -0.30 V is H* at 1.00 ML",
            }
        ],
    )
    assert "staircase starts from H* at 1.00 ML (resting at -0.30 V on st99)" in text
    assert "coads mismatch" in text


# ── the kind views ──────────────────────────────────────────────────────


def _pathway(store: Store, results: dict[str, Any] | None, slug: str) -> Any:
    return store.insert_ref(
        kind="pathway",
        slug=slug,
        title="NO → NH3 on Pd",
        meta={
            "status": "ready",
            "autocatpath_version": "0.25.0",
            "config": {"conditions": {"potential_U": -0.3}},
            "results": results or {},
        },
    )


def _handler(hub: Hub) -> PathwayHandler:
    h = PathwayHandler(hub=hub)
    h._register_with(hub)
    return h


def _structure(store: Store, hub: Hub, slug: str = "pd_stair") -> Any:
    StructureHandler(hub=hub).put(
        id=slug,
        text=json.dumps(
            {
                "cell": {"a": 10.0, "b": 10.0, "c": 10.0, "pbc": [True, True, False]},
                "ops": [{"op": "add_atom", "element": "Pd", "frac": [0.0, 0.0, 0.0]}],
            }
        ),
    )
    return store.get_ref(kind="structure", id=slug)


def _payload(model: str) -> dict[str, Any]:
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
                "adsorbates": {
                    "H": {
                        "mu": -3.0,
                        "site": "fcc",
                        "c_ads": 0.0,
                        "points": [
                            {
                                "n": 1,
                                "theta": 0.25,
                                "energy": -100.2,
                                "gamma": -0.02,
                                "converged": True,
                                "detached": False,
                            }
                        ],
                        "best": {"theta": 0.25, "n": 1, "gamma": -0.02},
                    }
                },
                "winner": {"adsorbate": None, "theta": 0.0, "gamma": 0.0},
            }
        ],
        "warnings": [],
    }


def _scan_job(store: Store, structure_id: int, sweep: dict[str, Any]) -> int:
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="scan",
        parent_id=structure_id,
        meta={
            "job_type": "surface_coverage_scan",
            "anchor_key": "a" * 64,
            "model": "m",
            "engine_version": "0.24.0",
            "corrections": None,
            "che_sweep": sweep,
        },
    )
    store.add_tag(
        ref.id, Tag.closed("STATUS", "succeeded"), set_by="agent", replace_prefix=True
    )
    return ref.id


def test_pathway_staircase_view_retilts_and_defaults_to_operating_point(
    store: Store, hub: Hub
) -> None:
    _pathway(store, _results(), "pw_stair_view")
    h = _handler(hub)
    body = h.get(id="pw_stair_view", view="staircase").body
    # config records potential_U -0.3 → the default U is the operating point
    assert "U_RHE -0.300 V" in body
    assert "no_reduce" in body and "limiting rung" in body
    body = h.get(id="pw_stair_view", view="staircase", args={"U": 0.0}).body
    assert "U_RHE +0.000 V" in body
    assert "limiting rung at U = +0.00 V: no_reduce" in body


def test_pathway_staircase_view_is_honest_on_an_old_engine(
    store: Store, hub: Hub
) -> None:
    _pathway(store, {"substrate": "NO", "nodes": {}}, "pw_old")
    body = _handler(hub).get(id="pw_old", view="staircase").body
    assert "che_affine absent (engine < 0.25.0)" in body


def test_pathway_staircase_names_the_resting_surface_and_mismatch(
    store: Store, hub: Hub
) -> None:
    st = _structure(store, hub, "pd_link")
    pw = _pathway(store, _results(), "pw_link")
    store.add_link(
        src_ref_id=st.id, dst_ref_id=pw.id, relation="related-to", set_by="system"
    )
    sweep = sp.sweep(
        {"m": _payload("m")}, u_window=(-0.5, 0.5), grid=21, sigma_e_ev=0.05
    )
    _scan_job(store, st.id, sweep)
    body = _handler(hub).get(id="pw_link", view="staircase").body
    # at the default -0.30 V H* wins, while the pathway assumed clean (no coads)
    assert "staircase starts from H* at 0.25 ML" in body
    assert "coads mismatch" in body


def test_structure_staircase_view_picks_the_best_linked_pathway(
    store: Store, hub: Hub
) -> None:
    st = _structure(store, hub, "pd_best")
    affine_pw = _pathway(store, _results(), "pw_new")
    old_pw = _pathway(store, {"substrate": "NO", "nodes": {}}, "pw_old2")
    for pw in (affine_pw, old_pw):
        store.add_link(
            src_ref_id=st.id,
            dst_ref_id=pw.id,
            relation="related-to",
            set_by="system",
        )
    sh = StructureHandler(hub=hub)
    sh._register_with(hub)
    body = sh.get(id="pd_best", view="staircase").body
    assert "best linked pathway" in body and "reaction staircase" in body
    # the affine-carrying pathway outranks the older one — its rungs render
    assert "no_reduce" in body
    # a structure with no pathway answers honestly
    _structure(store, hub, "pd_none")
    body = sh.get(id="pd_none", view="staircase").body
    assert "no staircase" in body


# ── the picked point (gr489471) ─────────────────────────────────────────


def test_pathway_staircase_point_adds_state_column_worst_and_window(
    store: Store, hub: Hub
) -> None:
    _pathway(store, _results(), "pw_point")
    h = _handler(hub)
    body = h.get(id="pw_point", view="staircase", args={"U": 0.0, "pH": 7}).body
    # no_reduce (n_e 2, ΔG0 +0.3) is uphill at 0 V and opens below −0.15 V
    assert "[electro_uphill]" in body
    assert "point U_RHE +0.000 V, pH 7: window outside" in body
    assert "worst rung (route 1): electro_uphill: no_reduce" in body
    assert "opens below -0.15 V" in body
    # point='U,pH' is the same pick
    same = h.get(id="pw_point", view="staircase", args={"point": "0.0,7"}).body
    assert same == body
    # no args, or U alone (the plain re-tilt): no point block
    for args in (None, {"U": 0.0}):
        plain = h.get(id="pw_point", view="staircase", args=args).body
        assert "point U_RHE" not in plain and "[electro_uphill]" not in plain


def test_pathway_point_args_are_validated(store: Store, hub: Hub) -> None:
    _pathway(store, _results(), "pw_point_bad")
    h = _handler(hub)
    with pytest.raises(BadInput, match="view='staircase' only"):
        h.get(id="pw_point_bad", view="analysis", args={"U": 0.0, "pH": 7})
    with pytest.raises(BadInput, match="point is 'U,pH'"):
        h.get(id="pw_point_bad", view="staircase", args={"point": "0.1"})
    with pytest.raises(BadInput, match="not both"):
        h.get(id="pw_point_bad", view="staircase", args={"point": "0,1", "U": 0})


def test_pathway_point_on_another_termination_is_off_map(
    store: Store, hub: Hub
) -> None:
    st = _structure(store, hub, "pd_point_map")
    pw = _pathway(store, _results(), "pw_point_map")
    store.add_link(
        src_ref_id=st.id, dst_ref_id=pw.id, relation="related-to", set_by="system"
    )
    sweep = sp.sweep(
        {"m": _payload("m")}, u_window=(-0.5, 0.5), grid=21, sigma_e_ev=0.05
    )
    _scan_job(store, st.id, sweep)
    body = (
        _handler(hub)
        .get(id="pw_point_map", view="staircase", args={"U": -0.3, "pH": 1})
        .body
    )
    # H* rests at −0.30 V while the pathway ran on clean Pd: every rung is
    # off-map with the termination named, even the ones viable on clean
    assert "[off_map]" in body
    assert "[viable]" not in body and "[electro_uphill]" not in body
    assert "map: H* at 0.25 ML here; pathway computed on clean" in body


def test_structure_staircase_forwards_the_point(store: Store, hub: Hub) -> None:
    st = _structure(store, hub, "pd_point_fwd")
    pw = _pathway(store, _results(), "pw_point_fwd")
    store.add_link(
        src_ref_id=st.id, dst_ref_id=pw.id, relation="related-to", set_by="system"
    )
    sh = StructureHandler(hub=hub)
    sh._register_with(hub)
    body = sh.get(id="pd_point_fwd", view="staircase", args={"point": "-0.2,3"}).body
    assert "point U_RHE -0.200 V, pH 3: window" in body
    with pytest.raises(BadInput):
        sh.get(id="pd_point_fwd", view="staircase", args={"point": "nope"})
