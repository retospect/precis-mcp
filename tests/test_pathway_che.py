"""CHE potential-lever post-processing (``precis_pathway.che``).

Pure graph-dict math — no autocatpath, no store, no potential-energy code — so
these run on the host, unlike the autocatpath-gated ``test_pathway_plugin`` suite.
The fixture is a hand-built NO→reduction graph with one associative fork, shaped
like the ammonia template's ``node_link_data`` (nodes carry ``rel_energy``;
``links`` carry ``barrier`` / ``kind``).
"""

from __future__ import annotations

import math

from precis_pathway import che


def _graph() -> dict:
    # Reduction chain NO → NO+H → HNO → HNO+H → H2NO, with a fork NO+H → NOH.
    # rel_energy in eV; supply edges add one reservoir H (electrochemical steps).
    nodes = [
        {"id": "NO", "rel_energy": 0.00},
        {"id": "NO+H", "rel_energy": 0.30},
        {"id": "HNO", "rel_energy": 0.10},
        {"id": "NOH", "rel_energy": 0.25},
        {"id": "HNO+H", "rel_energy": 0.55},
        {"id": "H2NO", "rel_energy": 0.20},
    ]
    links = [
        {"source": "NO", "target": "NO+H", "kind": "supply"},
        {"source": "NO+H", "target": "HNO", "barrier": 0.80, "kind": "reaction"},
        {"source": "NO+H", "target": "NOH", "barrier": 1.10, "kind": "reaction"},
        {"source": "HNO", "target": "HNO+H", "kind": "supply"},
        {"source": "HNO+H", "target": "H2NO", "barrier": 0.60, "kind": "reaction"},
    ]
    return {"nodes": nodes, "links": links}


_RESULTS = {"pathway": ["NO", "NO+H", "HNO", "HNO+H", "H2NO"], "target": "H2NO"}


def test_h_count_parses_fragments_and_subscripts() -> None:
    assert che.h_count("NO") == 0
    assert che.h_count("N+O") == 0
    assert che.h_count("NO@top") == 0
    assert che.h_count("NH3") == 3
    assert che.h_count("H2O") == 2
    assert che.h_count("NH2+H") == 3
    assert che.h_count("OH+H") == 2


def test_n_h_per_node_is_reservoir_h_relative_to_root() -> None:
    nh = che.n_h_per_node(_graph(), _RESULTS)
    assert nh == {"NO": 0, "NO+H": 1, "HNO": 1, "NOH": 1, "HNO+H": 2, "H2NO": 2}


def test_shifted_energies_apply_che_slope() -> None:
    g = _graph()
    at0 = che.shifted_energies(g, 0.0)
    assert at0["NO+H"] == 0.30
    # A node with n_H=2 shifts by 2·U; the root (n_H=0) never moves.
    at_neg = che.shifted_energies(g, -0.5)
    assert math.isclose(at_neg["HNO+H"], 0.55 + 2 * (-0.5))
    assert at_neg["NO"] == 0.0


def test_limiting_potential_is_worst_electrochemical_step() -> None:
    lp = che.limiting_potential(_graph(), _RESULTS)
    assert lp is not None
    # Supply steps: NO→NO+H (ΔG0=0.30), HNO→HNO+H (ΔG0=0.45); the latter binds.
    assert lp["limiting_step"] == "HNO→HNO+H"
    assert math.isclose(lp["U_L"], -0.45)
    assert lp["delta_n_H"] == 1


def test_limiting_potential_none_without_electrochemical_step() -> None:
    g = {
        "nodes": [{"id": "A", "rel_energy": 0.0}, {"id": "B", "rel_energy": 0.5}],
        "links": [{"source": "A", "target": "B", "barrier": 1.0, "kind": "reaction"}],
    }
    assert che.limiting_potential(g) is None


def test_optimal_span_potential_minimizes_convex_pl() -> None:
    g = _graph()
    osp = che.optimal_span_potential(g, _RESULTS)
    assert osp is not None
    u_opt, span_opt = osp["U_opt"], osp["span_at_Uopt"]
    span0 = che.span_at_potential(g, 0.0, _RESULTS)
    assert span0 is not None
    # The optimum is no worse than the U=0 span, and lands inside the window.
    assert span_opt <= span0 + 1e-9
    lo, hi = osp["window"]
    assert lo <= u_opt <= hi
    # Brute-force grid confirms the closed-form minimizer (convexity check).
    grid = [
        che.span_at_potential(g, lo + (hi - lo) * i / 4000, _RESULTS)
        for i in range(4001)
    ]
    grid_min = min(v for v in grid if v is not None)
    assert math.isclose(span_opt, grid_min, abs_tol=1e-3)


def test_fork_probabilities_favor_lower_barrier() -> None:
    forks = che.fork_probabilities(_graph(), _RESULTS)
    fork = next(f for f in forks if f["state"] == "NO+H")
    assert fork["insufficient_data"] is False
    frac = {b["target"]: b["fraction"] for b in fork["branches"]}
    assert math.isclose(sum(frac.values()), 1.0)
    assert frac["HNO"] > 0.99  # 0.30 eV lower barrier → dominates at 298 K
    assert fork["temperature"] == che.T_DEFAULT


def test_fork_guard_reports_insufficient_data() -> None:
    g = _graph()
    # Blank one competing barrier → the fork must refuse to fabricate a ratio.
    for e in g["links"]:
        if e["source"] == "NO+H" and e["target"] == "NOH":
            e["barrier"] = None
    fork = next(f for f in che.fork_probabilities(g, _RESULTS) if f["state"] == "NO+H")
    assert fork["insufficient_data"] is True
    assert all("fraction" not in b for b in fork["branches"])
    # …and selectivity refuses to score, rather than inventing P_side.
    assert che.selectivity_penalty(g, _RESULTS) is None


def test_selectivity_penalty_is_off_path_leakage() -> None:
    p_side = che.selectivity_penalty(_graph(), _RESULTS)
    assert p_side is not None
    assert 0.0 < p_side < 0.01  # tiny — the on-path branch wins the fork


def test_she_from_rhe_is_minus_59mv_per_ph() -> None:
    # 0.0592 V/pH at 298.15 K; RHE→SHE at pH 7 shifts by ≈ −0.414 V.
    u_she = che.she_from_rhe(0.0, 7.0)
    assert math.isclose(u_she, -0.0592 * 7, abs_tol=2e-3)
    assert che.she_from_rhe(0.5, 0.0) == 0.5  # pH 0 → no shift


def test_persist_stamps_n_h_and_electro_block() -> None:
    from precis_pathway import persist

    artifact = {
        "content_key": "ck",
        "autocatpath_version": "test",
        "config": {"slab": {"element": "Pd"}},
        "config_snapshot_yaml": "slab: {element: Pd}\n",
        "results_json": dict(_RESULTS),
        "graph_json": _graph(),
        "warnings": [],
        "structures_extxyz": {},
    }
    meta = persist.pathway_meta(artifact)
    nodes = {n["id"]: n for n in meta["graph"]["nodes"]}
    assert nodes["HNO+H"]["n_H"] == 2
    assert nodes["NO"]["n_H"] == 0
    electro = meta["results"]["electro"]
    assert math.isclose(electro["U_L"], -0.45)
    assert meta["results"]["U_L"] == electro["U_L"]
    assert meta["results"]["span_at_Uopt"] is not None


def test_che_summary_bundle_shape() -> None:
    s = che.che_summary(_graph(), _RESULTS)
    assert math.isclose(s["U_L"], -0.45)
    assert s["limiting_step"] == "HNO→HNO+H"
    assert s["span_at_Uopt"] is not None
    assert s["span_at_UL"] is not None
    assert 0.0 < s["P_side"] < 0.01
    assert s["temperature"] == che.T_DEFAULT
    assert any(f["state"] == "NO+H" for f in s["forks"])
