"""Explicit pH terms on stored CHE reads (gr559664, td564087).

Hand values use kT = 0.02569258 eV at 298.15 K. The SHE checks allow
1 mV for the existing 298.15 K Nernst prefactor's rounded 0.0592 V/pH.
"""

from __future__ import annotations

import copy
from itertools import pairwise
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from urllib.parse import parse_qsl

import pytest

from precis.errors import BadInput
from precis.utils import reaction_graph
from precis_pathway.environment import Environment
from precis_pathway.handler import PathwayHandler
from precis_pathway.ph_terms import (
    product_protonation,
    protonate_graph,
    released_products,
)
from precis_pathway.staircase_routes import (
    render_staircase,
    routes_for_pathway,
    routes_from_results,
)
from precis_pathway.toon_views import analysis_text


def _environment(
    ph: float | None = 7.0, *, she: bool = False, temperature: float = 298.15
) -> Environment:
    ec: dict[str, float] = {"U_vs_SHE" if she else "U_vs_RHE": -0.3}
    if ph is not None:
        ec["pH"] = ph
    return Environment.from_dict({"T": temperature, "electrochemical": ec})


def _results(product: str = "NH3") -> dict[str, Any]:
    terminal = f"*~{product.lower()}"
    return {
        "substrate": "NO",
        "target": product,
        "pathway": ["NO", product, terminal],
        "network_digest": "fixture-ph-terms",
        "corrections": {"gas": "fixture"},
        "che_affine": {
            "scale": "RHE",
            "T": 298.15,
            "pH": 0.0,
            "per_route": [
                {
                    "route": f"NO -> {product} -> {terminal}",
                    "steps": [
                        {"edge": "reduce", "delta_g0": 0.2, "n_e": 1},
                        {"edge": "desorb", "delta_g0": 0.4, "n_e": 0},
                    ],
                    "U_L": -0.2,
                    "U_opt": -0.3,
                    "span_at_UL": 0.8,
                }
            ],
        },
        "her": {
            "dG_H_star": -0.1,
            "U_L_her": -0.1,
            "product": "H2",
            "leaf": "*~h2",
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
        },
    }


def _meta() -> dict[str, Any]:
    return {
        "status": "ready",
        "config": {},
        "results": _results(),
        "graph": {
            "nodes": [
                {"id": "NO", "rel_energy": 0.0, "energy": 0.0, "n_H": 0},
                {"id": "NH3", "rel_energy": 0.2, "energy": 0.2, "n_H": 1},
                {"id": "*~nh3", "rel_energy": 0.6, "energy": 0.6, "n_H": 1},
            ],
            "links": [
                {"source": "NO", "target": "NH3", "delta_e": 0.2, "barrier": 0.7},
                {
                    "source": "NH3",
                    "target": "*~nh3",
                    "delta_e": 0.4,
                    "barrier": 0.8,
                },
            ],
        },
    }


@pytest.mark.parametrize(
    ("product", "ph", "expected"),
    [("NH3", 7.0, -0.13325), ("NH3", 12.0, -0.00004565), ("NH2OH", 4.0, -0.11857)],
)
def test_product_desorption_matches_hand_values(
    product: str, ph: float, expected: float
) -> None:
    env = _environment(ph)
    term = product_protonation(product, env)
    assert term == pytest.approx(expected, abs=0.001)
    sr = routes_from_results(_results(product), environment=env)
    assert sr.routes[0].rungs[-1].delta_g0 == pytest.approx(0.4 + expected, abs=0.001)
    assert sr.routes[0].rungs[0].delta_g0 == 0.2


def test_product_term_uses_environment_temperature() -> None:
    cold = product_protonation("NH3", _environment(7.0))
    hot = product_protonation("NH3", _environment(7.0, temperature=596.3))
    assert cold is not None and hot is not None
    assert hot == pytest.approx(2 * cold)


def test_unknown_product_has_no_protonation_term_and_says_so() -> None:
    env = _environment(7.0)
    assert product_protonation("N2", env) is None
    sr = routes_from_results(_results("N2"), environment=env)
    assert sr.routes[0].rungs[-1].delta_g0 == 0.4
    text = render_staircase(sr, u_rhe=-0.3)
    assert "product_protonation" in text
    assert "N2" in text.upper()
    assert "no term" in text or "unavailable" in text


def test_pcet_only_fork_is_ph_invariant_on_rhe() -> None:
    results = _results()
    results["che_affine"]["per_route"] = [
        {
            "route": f"NO -> {product}",
            "steps": [{"edge": f"NO->{product}", "delta_g0": energy, "n_e": 1}],
        }
        for product, energy in (("HNO", 0.2), ("NOH", 0.5))
    ]
    low = routes_from_results(results, environment=_environment(2.0))
    high = routes_from_results(results, environment=_environment(12.0))
    assert low.routes == high.routes
    assert low.pcet_routes == high.pcet_routes
    for sr in (low, high):
        energies = [float(route.limiting_g(-0.3)) for route in sr.routes]
        assert energies == pytest.approx([-0.1, 0.2])
        assert energies[1] - energies[0] == pytest.approx(0.3)


def test_fixed_she_maps_to_hand_rhe_and_moves_each_pcet_rung() -> None:
    env = _environment(7.0, she=True)
    assert env.u_rhe is not None
    assert env.u_rhe == pytest.approx(0.1144, abs=0.001)
    assert env.electrochemical["U_vs_SHE"] == -0.3
    sr = routes_from_results(_results(), environment=env)
    assert float(sr.routes[0].rungs[0].delta_g(env.u_rhe)) == pytest.approx(
        0.3144, abs=0.001
    )
    text = render_staircase(sr, u_rhe=env.u_rhe)
    assert "fixed_SHE" in text and "eV" in text


@pytest.mark.parametrize(
    "ec",
    [
        {"U_vs_SHE": -0.3},
        {"U_vs_SHE": -0.3, "U_vs_RHE": -0.3, "pH": 7.0},
        {"U_vs_SHE": float("nan"), "pH": 7.0},
        {"U_vs_SHE": True, "pH": 7.0},
    ],
)
def test_fixed_she_requires_one_scale_and_a_known_ph(ec: dict[str, Any]) -> None:
    with pytest.raises(BadInput):
        Environment.from_dict({"T": 298.15, "electrochemical": ec})


def test_her_uses_slab_h_star_and_che_h2_reference_at_fixed_she() -> None:
    low_env, high_env = _environment(7.0, she=True), _environment(12.0, she=True)
    assert low_env.u_rhe is not None and high_env.u_rhe is not None
    low = routes_from_results(_results(), environment=low_env)
    high = routes_from_results(_results(), environment=high_env)
    assert low.her is not None and high.her is not None
    assert [r.delta_g0 for r in low.her.rungs] == pytest.approx([-0.1, 0.1])
    assert sum(r.delta_g0 for r in low.her.rungs) == pytest.approx(0.0)
    assert [float(r.delta_g(low_env.u_rhe)) for r in low.her.rungs] == pytest.approx(
        [0.0144, 0.2144], abs=0.001
    )
    difference = float(high.her.limiting_g(high_env.u_rhe)) - float(
        low.her.limiting_g(low_env.u_rhe)
    )
    assert difference == pytest.approx(0.296, abs=0.001)
    text = render_staircase(low, u_rhe=low_env.u_rhe)
    assert "HER_competitor" in text


@pytest.mark.parametrize("h_star", [None, float("nan"), float("inf"), True])
def test_her_without_measured_h_star_is_unavailable_even_with_edges(
    h_star: Any,
) -> None:
    results = _results()
    results["her"]["dG_H_star"] = h_star
    sr = routes_from_results(results, environment=_environment())
    assert sr.her is None
    text = render_staircase(sr, u_rhe=-0.3)
    assert "HER" in text and "unavailable" in text


def test_her_with_no_export_is_unavailable() -> None:
    results = _results()
    del results["her"]
    sr = routes_from_results(results, environment=_environment())
    assert sr.her is None
    assert "unavailable" in render_staircase(sr, u_rhe=-0.3)


def test_no_ph_preserves_staircase_and_analysis_output_byte_for_byte() -> None:
    env = _environment(None, temperature=500.0)
    results = _results()
    assert product_protonation("NH3", env) is None
    baseline = routes_from_results(results)
    selected = routes_from_results(results, environment=env)
    assert selected == baseline
    assert (
        render_staircase(selected, results, u_rhe=-0.3).encode()
        == render_staircase(baseline, results, u_rhe=-0.3).encode()
    )
    meta = _meta()
    assert (
        analysis_text(meta, U=-0.3, environment=env).encode()
        == analysis_text(meta, U=-0.3).encode()
    )


def test_environment_reads_leave_stored_results_and_graph_untouched() -> None:
    meta = _meta()
    original = copy.deepcopy(meta)
    env = _environment(7.0, she=True)
    routes_from_results(meta["results"], environment=env)
    analysis_text(meta, environment=env)
    assert meta == original


def test_routes_for_pathway_passes_environment_to_stored_adapter() -> None:
    meta = _meta()
    store = Mock()
    store.get_ref.return_value = SimpleNamespace(meta=meta)
    sr = routes_for_pathway(store, "pw-fixture", environment=_environment())
    assert sr.routes[0].rungs[-1].delta_g0 == pytest.approx(0.26675, abs=0.001)


def test_analysis_reprices_graph_and_names_terms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []
    original = reaction_graph.most_endergonic_step

    def capture(graph: dict[str, Any], root: str, target: str) -> dict[str, Any] | None:
        seen.append(graph)
        return original(graph, root, target)

    monkeypatch.setattr(reaction_graph, "most_endergonic_step", capture)
    text = analysis_text(_meta(), environment=_environment(7.0, she=True))
    assert seen
    graph = seen[-1]
    nodes = {node["id"]: node for node in graph["nodes"]}
    assert nodes["NH3"]["rel_energy"] == pytest.approx(0.3144, abs=0.001)
    assert nodes["*~nh3"]["rel_energy"] == pytest.approx(0.58115, abs=0.001)
    assert graph["links"][-1]["delta_e"] == pytest.approx(0.26675, abs=0.001)
    assert [link["barrier"] for link in graph["links"]] == [0.7, 0.8]
    for name in ("product_protonation", "fixed_SHE", "HER_competitor"):
        assert name in text


@pytest.mark.parametrize("view", ["analysis", "staircase"])
def test_handler_environment_read_renders_terms_without_compute(
    view: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta = _meta()
    ref = SimpleNamespace(id=11, slug="pw-fixture", title="NO to NH3", meta=meta)
    store = Mock()
    store.get_ref.return_value = ref
    store.links_for.return_value = []
    store.fetch_refs_by_ids.return_value = {}
    handler = PathwayHandler.__new__(PathwayHandler)
    setattr(handler, "hub", SimpleNamespace(live_store=store))
    submit = Mock(side_effect=AssertionError("a read must not submit work"))
    monkeypatch.setattr("precis.handlers.job.JobHandler.put", submit)
    response = handler.get(
        id="pw-fixture",
        view=view,
        args={"environment": _environment(7.0, she=True).to_dict()},
    )
    assert response.body is not None
    for name in ("product_protonation", "fixed_SHE", "HER_competitor"):
        assert name in response.body
    submit.assert_not_called()


def _analysis_read(meta: dict[str, Any], env: Environment, *, via_handler: bool) -> str:
    if not via_handler:
        return analysis_text(meta, environment=env)
    ref = SimpleNamespace(id=11, slug="pw-fixture", title="NO to NH3", meta=meta)
    store = Mock()
    store.get_ref.return_value = ref
    handler = PathwayHandler.__new__(PathwayHandler)
    setattr(handler, "hub", SimpleNamespace(live_store=store))
    response = handler.get(
        id="pw-fixture", view="analysis", args={"environment": env.to_dict()}
    )
    assert response.body is not None
    return response.body


@pytest.mark.parametrize(
    ("section", "key"),
    [("conditions", "potential_U"), ("electrochemistry", "U_vs_RHE")],
)
@pytest.mark.parametrize("explicit_u", [None, -0.4])
@pytest.mark.parametrize("environment_arg", [False, True])
def test_staircase_ph_read_resolves_legacy_optimal_potential(
    section: str, key: str, explicit_u: float | None, environment_arg: bool
) -> None:
    meta = _meta()
    meta["config"] = {section: {key: "optimal"}}
    meta["results"]["U_opt"] = -0.65
    before = copy.deepcopy(meta)
    ref = SimpleNamespace(id=11, slug="pw-fixture", title="NO to NH3", meta=meta)
    store = Mock()
    store.get_ref.return_value = ref
    store.links_for.return_value = []
    store.fetch_refs_by_ids.return_value = {}
    handler = PathwayHandler.__new__(PathwayHandler)
    setattr(handler, "hub", SimpleNamespace(live_store=store))
    args: dict[str, Any] = {"pH": 7.0}
    if environment_arg:
        ec = {"pH": 7.0}
        if explicit_u is not None:
            ec["U_vs_RHE"] = explicit_u
        args = {"environment": {"T": 298.15, "electrochemical": ec}}
    elif explicit_u is not None:
        args["U"] = explicit_u
    expected_u = -0.65 if explicit_u is None else explicit_u

    response = handler.get(id="pw-fixture", view="staircase", args=args)

    assert response.body is not None
    assert f"U_RHE {expected_u:+.3f} V" in response.body
    assert "pH 7" in response.body
    assert "pH terms:" in response.body
    assert "HER_competitor" in response.body
    reduction = next(
        line
        for line in response.body.splitlines()
        if line.lstrip().startswith("reduce ")
    )
    assert f"ΔG(U) {0.2 + expected_u:+.3f} eV" in reduction
    protonation = next(
        line
        for line in response.body.splitlines()
        if "term product_protonation:" in line
    )
    assert float(protonation.split()[-2]) == pytest.approx(-0.13325, abs=0.001)
    assert meta == before


@pytest.mark.parametrize("environment_arg", [False, True])
def test_staircase_without_ph_preserves_legacy_operating_potential(
    environment_arg: bool,
) -> None:
    meta = _meta()
    meta["config"] = {
        "environment": {"T": 298.15, "electrochemical": {"U_vs_RHE": -0.3}}
    }
    before = copy.deepcopy(meta)
    ref = SimpleNamespace(id=11, slug="pw-fixture", title="NO to NH3", meta=meta)
    store = Mock()
    store.get_ref.return_value = ref
    store.links_for.return_value = []
    store.fetch_refs_by_ids.return_value = {}
    handler = PathwayHandler.__new__(PathwayHandler)
    setattr(handler, "hub", SimpleNamespace(live_store=store))
    args = {"environment": {"T": 298.15}} if environment_arg else {}

    response = handler.get(id="pw-fixture", view="staircase", args=args)

    assert response.body is not None
    assert "U_RHE +0.000 V" in response.body
    reduction = next(
        line
        for line in response.body.splitlines()
        if line.lstrip().startswith("reduce ")
    )
    energy = float(reduction.split("ΔG(U) ", 1)[1].split()[0])
    assert energy == pytest.approx(0.2, abs=0.001)
    baseline = handler._staircase(store, ref, meta, U=0.0)
    # get() prefixes the ref line and any environment header.
    assert response.body.encode().endswith(baseline.encode())
    assert meta == before


@pytest.mark.parametrize("with_n_h", [False, True])
@pytest.mark.parametrize("environment_arg", [False, True])
def test_staircase_ph_read_defaults_to_stored_canonical_potential(
    with_n_h: bool, environment_arg: bool
) -> None:
    meta = _meta()
    meta["config"] = {
        "environment": {
            "T": 298.15,
            "electrochemical": {"U_vs_RHE": -0.3, "pH": 7},
        }
    }
    if not with_n_h:
        for node in meta["graph"]["nodes"]:
            del node["n_H"]
    before = copy.deepcopy(meta)
    ref = SimpleNamespace(id=11, slug="pw-fixture", title="NO to NH3", meta=meta)
    store = Mock()
    store.get_ref.return_value = ref
    store.links_for.return_value = []
    store.fetch_refs_by_ids.return_value = {}
    handler = PathwayHandler.__new__(PathwayHandler)
    setattr(handler, "hub", SimpleNamespace(live_store=store))
    args = (
        {"environment": {"T": 298.15, "electrochemical": {"pH": 7}}}
        if environment_arg
        else {"pH": 7}
    )

    response = handler.get(id="pw-fixture", view="staircase", args=args)

    assert response.body is not None
    assert "U_RHE -0.300 V" in response.body
    reduction = next(
        line
        for line in response.body.splitlines()
        if line.lstrip().startswith("reduce ")
    )
    energy = float(reduction.split("ΔG(U) ", 1)[1].split()[0])
    assert energy == pytest.approx(-0.1, abs=0.001)
    assert "product_protonation" in response.body
    assert meta == before


@pytest.mark.parametrize("via_handler", [False, True])
def test_ph_only_analysis_without_n_h_keeps_stored_footing(
    via_handler: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta = _meta()
    meta["config"] = {"environment": _environment().to_dict()}
    for node in meta["graph"]["nodes"]:
        del node["n_H"]
    before = copy.deepcopy(meta)
    env = Environment.from_dict({"T": 298.15, "electrochemical": {"pH": 7}})
    lever = Mock(side_effect=AssertionError("no n_H: cannot re-lever"))
    monkeypatch.setattr(reaction_graph, "at_potential", lever)
    seen: list[dict[str, Any]] = []
    original = reaction_graph.energetic_span

    def capture(graph: dict[str, Any], root: str, target: str) -> float | None:
        seen.append(graph)
        return original(graph, root, target)

    monkeypatch.setattr(reaction_graph, "energetic_span", capture)
    text = _analysis_read(meta, env, via_handler=via_handler)
    assert "Potential not re-levered: graph has no n_H" in text
    assert "using stored footing" in text
    assert "graph term product_protonation" in text
    assert "energetic span (whole-path apparent barrier):" in text
    assert seen
    nodes = {node["id"]: node for node in seen[-1]["nodes"]}
    assert nodes["NH3"]["rel_energy"] == pytest.approx(0.2)
    assert nodes["*~nh3"]["rel_energy"] == pytest.approx(0.46675, abs=0.001)
    assert [link["barrier"] for link in seen[-1]["links"]] == [0.7, 0.8]
    lever.assert_not_called()
    assert meta == before


@pytest.mark.parametrize("via_handler", [False, True])
@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"environment": _environment().to_dict()}, -0.3),
        ({"environment": _environment(she=True).to_dict()}, 0.1141),
        (
            {
                "environment": _environment().to_dict(),
                "conditions": {"potential_U": -0.8},
            },
            -0.3,
        ),
        (
            {
                "environment": {"T": 298.15},
                "conditions": {"potential_U": -0.8},
            },
            -0.8,
        ),
        ({"conditions": {"potential_U": -0.4}}, -0.4),
        ({"conditions": {"potential_U": "optimal"}}, -0.65),
        (
            {
                "environment": {"T": 298.15, "electrochemical": {"U_vs_RHE": 0}},
                "conditions": {"potential_U": -0.8},
            },
            0.0,
        ),
        ({}, 0.0),
    ],
)
def test_ph_only_analysis_resolves_stored_environment_before_legacy(
    via_handler: bool,
    config: dict[str, Any],
    expected: float,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meta = _meta()
    meta["config"] = config
    meta["results"]["U_opt"] = -0.65
    before = copy.deepcopy(meta)
    env = Environment.from_dict({"T": 298.15, "electrochemical": {"pH": 7}})
    lever = Mock(wraps=reaction_graph.at_potential)
    monkeypatch.setattr(reaction_graph, "at_potential", lever)
    text = _analysis_read(meta, env, via_handler=via_handler)
    lever.assert_called_once()
    assert lever.call_args.args[1] == pytest.approx(expected, abs=0.0001)
    assert f"at U = {expected:+.2f} V vs RHE" in text
    assert "graph term product_protonation" in text
    assert "Potential not re-levered" not in text
    assert meta == before


def test_explicit_potential_still_refuses_graph_without_n_h() -> None:
    meta = _meta()
    for node in meta["graph"]["nodes"]:
        del node["n_H"]
    with pytest.raises(BadInput, match="carries no n_H"):
        _analysis_read(meta, _environment(), via_handler=True)


def _speciated_meta(landing: str = "*~nh3") -> dict[str, Any]:
    meta = _meta()
    results = meta["results"]
    adsorbed = "NH3+O" if landing == "O~nh3" else "NH3"
    results["speciation"] = {
        "pH": 7,
        "temperature": 298.15,
        "species": {"NH3": {"as": "NH4+", "pKa": 9.25, "dG": -0.1331}},
    }
    route = results["che_affine"]["per_route"][0]
    route["route"] = f"NO -> {adsorbed} -> {landing}"
    route["steps"][-1]["delta_g0"] = 0.4 - 0.1331
    results["pathway"][1] = adsorbed
    results["pathway"][-1] = landing
    meta["graph"]["nodes"][1]["id"] = adsorbed
    meta["graph"]["links"][0]["target"] = adsorbed
    terminal = meta["graph"]["nodes"][-1]
    terminal.update(id=landing, rel_energy=0.6 - 0.1331, energy=0.6 - 0.1331)
    meta["graph"]["links"][-1].update(
        source=adsorbed, target=landing, delta_e=0.4 - 0.1331
    )
    return meta


@pytest.mark.parametrize("landing", ["*~nh3", "O~nh3"])
@pytest.mark.parametrize("ph, correction", [(7.0, -0.13325), (12.0, -0.00004565)])
def test_explicit_ph_replaces_stored_speciation_in_graph_and_staircase(
    landing: str, ph: float, correction: float
) -> None:
    meta = _speciated_meta(landing)
    original = copy.deepcopy(meta)
    env = _environment(ph)
    sr = routes_from_results(meta["results"], environment=env)
    rung = sr.routes[0].rungs[-1]
    assert rung.delta_g0 == pytest.approx(0.4 + correction, abs=0.001)
    assert dict(rung.terms)["stored_product_protonation_removed"] == pytest.approx(
        0.1331
    )
    graph = protonate_graph(
        reaction_graph.at_potential(meta["graph"], -0.3), env, meta["results"]
    )
    terminal = graph["nodes"][-1]
    assert terminal["id"] == landing
    assert terminal["rel_energy"] == pytest.approx(0.3 + correction, abs=0.001)
    assert terminal["energy"] == pytest.approx(0.3 + correction, abs=0.001)
    assert graph["links"][-1]["delta_e"] == pytest.approx(0.4 + correction, abs=0.001)
    assert "stored_product_protonation_removed" in analysis_text(meta, environment=env)
    assert meta == original


def test_no_ph_retains_stored_speciation_and_output_bytes() -> None:
    meta = _speciated_meta()
    env = _environment(None)
    results = meta["results"]
    baseline = routes_from_results(results)
    selected = routes_from_results(results, environment=env)
    assert selected == baseline
    assert selected.routes[0].rungs[-1].delta_g0 == 0.4 - 0.1331
    assert (
        render_staircase(selected, results, u_rhe=-0.3).encode()
        == render_staircase(baseline, results, u_rhe=-0.3).encode()
    )
    assert (
        analysis_text(meta, U=-0.3, environment=env).encode()
        == analysis_text(meta, U=-0.3).encode()
    )
    assert protonate_graph(meta["graph"], env, results) == meta["graph"]


@pytest.mark.parametrize("her", [None, {}, [], "unavailable", {"unknown": -0.1}])
def test_malformed_her_export_reports_unavailable(her: Any) -> None:
    results = _results()
    results["her"] = her
    sr = routes_from_results(results, environment=_environment())
    assert sr.her is None
    text = render_staircase(sr, u_rhe=-0.3)
    assert "HER_competitor unavailable" in text


def _mid_route_release_meta() -> dict[str, Any]:
    meta = _meta()
    meta["results"]["pathway"] = ["NH3+O", "O", "O+H"]
    meta["results"]["che_affine"]["per_route"] = [
        {
            "route": "NH3+O -> O -> O+H",
            "steps": [
                {"edge": "release_product", "delta_g0": 0.4, "n_e": 0},
                {"edge": "supply_hydrogen", "delta_g0": 0.2, "n_e": 1},
            ],
        }
    ]
    meta["graph"] = {
        "nodes": [
            {"id": "NH3+O", "rel_energy": 0.0, "energy": 0.0, "n_H": 0},
            {"id": "O", "rel_energy": 0.4, "energy": 0.4, "n_H": 0},
            {"id": "O+H", "rel_energy": 0.6, "energy": 0.6, "n_H": 1},
        ],
        "links": [
            {"source": "NH3+O", "target": "O", "delta_e": 0.4, "barrier": 0.8},
            {
                "source": "O",
                "target": "O+H",
                "kind": "supply",
                "delta_e": 0.2,
                "barrier": 0.0,
            },
        ],
    }
    return meta


def test_mid_route_release_reprices_one_rung_and_all_downstream_graph_states() -> None:
    meta = _mid_route_release_meta()
    env = _environment(7.0)
    sr = routes_from_results(meta["results"], environment=env)
    released, supplied = sr.routes[0].rungs
    assert released.name == "release_product"
    assert released.delta_g0 == pytest.approx(0.26675, abs=0.001)
    assert dict(released.terms)["product_protonation"] == pytest.approx(
        -0.13325, abs=0.001
    )
    assert supplied.name == "supply_hydrogen"
    assert supplied.delta_g0 == 0.2
    assert supplied.terms == ()
    graph = protonate_graph(
        reaction_graph.at_potential(meta["graph"], -0.3), env, meta["results"]
    )
    nodes = {node["id"]: node for node in graph["nodes"]}
    for key in ("energy", "rel_energy"):
        assert nodes["NH3+O"][key] == 0.0
        assert nodes["O"][key] == pytest.approx(0.26675, abs=0.001)
        assert nodes["O+H"][key] == pytest.approx(0.16675, abs=0.001)
    assert graph["links"][0]["delta_e"] == pytest.approx(0.26675, abs=0.001)
    assert graph["links"][1]["delta_e"] == pytest.approx(-0.1)


def test_analysis_without_affine_export_still_reprices_and_names_graph_terms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meta = _mid_route_release_meta()
    del meta["results"]["che_affine"]
    seen: list[dict[str, Any]] = []
    original = reaction_graph.most_endergonic_step

    def capture(graph: dict[str, Any], root: str, target: str) -> dict[str, Any] | None:
        seen.append(graph)
        return original(graph, root, target)

    monkeypatch.setattr(reaction_graph, "most_endergonic_step", capture)
    text = analysis_text(meta, environment=_environment(7.0))
    assert seen
    assert seen[-1]["nodes"][1]["rel_energy"] == pytest.approx(0.26675, abs=0.001)
    assert seen[-1]["nodes"][2]["rel_energy"] == pytest.approx(0.16675, abs=0.001)
    assert "graph term product_protonation (NH3+O->O):" in text
    term_line = next(line for line in text.splitlines() if "graph term" in line)
    assert "eV" in term_line
    assert "-0.133" in term_line


def test_url_rhe_override_replaces_she_scale_and_keeps_ph() -> None:
    original = _environment(7.0, she=True)
    changed = Environment.from_url({"u": "-0.4"}, default=original)
    assert changed.electrochemical == {"U_vs_RHE": -0.4, "pH": 7.0}
    assert changed.u_rhe == -0.4
    assert original.electrochemical == {"U_vs_SHE": -0.3, "pH": 7.0}


def test_graph_only_fixed_she_names_the_energy_contribution() -> None:
    meta = _mid_route_release_meta()
    del meta["results"]["che_affine"]
    env = _environment(7.0, she=True)
    graph = protonate_graph(meta["graph"], env, meta["results"])
    contributions = {(edge, name): value for edge, name, value in graph["ph_terms"]}
    assert contributions["O->O+H", "fixed_SHE"] == pytest.approx(0.4144, abs=0.001)
    text = analysis_text(meta, environment=env)
    assert "graph term fixed_SHE (O->O+H): +0.414" in text


def test_untagged_unknown_product_with_spectator_reports_no_term() -> None:
    meta = _mid_route_release_meta()
    meta["graph"]["nodes"][0]["id"] = "N2+O"
    meta["graph"]["links"][0]["source"] = "N2+O"
    meta["results"]["pathway"][0] = "N2+O"
    meta["results"]["che_affine"]["per_route"][0]["route"] = "N2+O -> O -> O+H"
    env = _environment()
    sr = routes_from_results(meta["results"], environment=env)
    assert sr.routes[0].rungs[0].delta_g0 == 0.4
    assert "product_protonation N2" in render_staircase(sr, u_rhe=-0.3)
    graph = protonate_graph(meta["graph"], env, meta["results"])
    assert graph["nodes"][1]["rel_energy"] == 0.4
    assert any("N2" in note and "no term" in note for note in graph["ph_notes"])


def _cumulative_release_meta() -> dict[str, Any]:
    meta = _meta()
    paths = [
        ["NH3+H2O", "NH3~h2o", "*~h2o+nh3"],
        ["NH3+H2O", "H2O~nh3", "*~h2o+nh3"],
    ]
    meta["results"]["pathway"] = paths[0]
    meta["results"]["che_affine"]["per_route"] = [
        {
            "route": " -> ".join(path),
            "steps": [
                {"edge": "release_first", "delta_g0": 0.4, "n_e": 0},
                {"edge": "release_second", "delta_g0": 0.4, "n_e": 0},
            ],
        }
        for path in paths
    ]
    meta["graph"] = {
        "nodes": [
            {"id": name, "energy": energy, "rel_energy": energy, "n_H": 0}
            for name, energy in (
                ("NH3+H2O", 0.0),
                ("NH3~h2o", 0.4),
                ("H2O~nh3", 0.4),
                ("*~h2o+nh3", 0.8),
            )
        ],
        "links": [
            {"source": a, "target": b, "delta_e": 0.4, "barrier": 0.8}
            for path in paths
            for a, b in pairwise(path)
        ],
    }
    return meta


def test_cumulative_landing_tag_reprices_only_new_ammonia() -> None:
    meta = _cumulative_release_meta()
    sr = routes_from_results(meta["results"], environment=_environment())
    assert released_products("NH3~h2o", "*~h2o+nh3") == ("NH3",)
    assert sr.routes[0].rungs[-1].delta_g0 == pytest.approx(0.26675, abs=0.001)
    assert dict(sr.routes[0].rungs[-1].terms)["product_protonation"] == pytest.approx(
        -0.13325, abs=0.001
    )


def test_both_release_orders_reach_the_same_shift_without_refusal() -> None:
    meta = _cumulative_release_meta()
    original = copy.deepcopy(meta)
    env = _environment()
    sr = routes_from_results(meta["results"], environment=env)
    totals = [sum(rung.delta_g0 for rung in route.rungs) for route in sr.routes]
    assert totals == pytest.approx([0.66675, 0.66675], abs=0.001)
    graph = protonate_graph(meta["graph"], env, meta["results"])
    nodes = {node["id"]: node for node in graph["nodes"]}
    assert nodes["*~h2o+nh3"]["rel_energy"] == pytest.approx(0.66675, abs=0.001)
    for route in (graph["links"][:2], graph["links"][2:]):
        assert sum(edge["delta_e"] for edge in route) == pytest.approx(
            0.66675, abs=0.001
        )
    assert "product_protonation" in analysis_text(meta, environment=env)
    assert "product_protonation" in _analysis_read(meta, env, via_handler=True)
    assert meta == original


@pytest.mark.parametrize("disconnected", [False, True])
@pytest.mark.parametrize("reverse_nodes", [False, True])
def test_analysis_prices_productive_route_with_release_free_cycle(
    disconnected: bool, reverse_nodes: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta = _meta()
    graph = meta["graph"]
    if disconnected:
        graph["nodes"].extend(
            {"id": name, "rel_energy": 0.0, "energy": 0.0, "n_H": 0}
            for name in ("O@a", "O@b")
        )
        graph["links"].extend(
            {"source": a, "target": b, "delta_e": 0.0, "barrier": 0.1}
            for a, b in (("O@a", "O@b"), ("O@b", "O@a"))
        )
    else:
        graph["nodes"].extend(
            {"id": name, "rel_energy": energy, "energy": energy, "n_H": 0}
            for name, energy in (("A", 0.05), ("B", 0.1))
        )
        graph["links"][:1] = [
            {"source": a, "target": b, "delta_e": energy, "barrier": 0.5}
            for a, b, energy in (
                ("NO", "A", 0.05),
                ("A", "B", 0.05),
                ("B", "A", -0.05),
                ("B", "NH3", 0.1),
            )
        ]
        meta["results"]["pathway"] = ["NO", "A", "B", "NH3", "*~nh3"]
        route = meta["results"]["che_affine"]["per_route"][0]
        route["route"] = " -> ".join(meta["results"]["pathway"])
        route["steps"][:1] = [
            {"edge": "prepare_A", "delta_g0": 0.05, "n_e": 0},
            {"edge": "prepare_B", "delta_g0": 0.05, "n_e": 0},
            {"edge": "reduce", "delta_g0": 0.1, "n_e": 1},
        ]
    if reverse_nodes:
        graph["nodes"].reverse()
    before = copy.deepcopy(meta)
    span = Mock(wraps=reaction_graph.energetic_span)
    monkeypatch.setattr(reaction_graph, "energetic_span", span)

    text = _analysis_read(meta, _environment(), via_handler=True)

    span.assert_called_once()
    priced = span.call_args.args[0]
    nodes = {node["id"]: node for node in priced["nodes"]}
    for key in ("energy", "rel_energy"):
        assert nodes["NH3"][key] == pytest.approx(-0.1, abs=0.001)
        assert nodes["*~nh3"][key] == pytest.approx(0.16675, abs=0.001)
        if disconnected:
            for name in ("O@a", "O@b"):
                assert nodes[name][key] == pytest.approx(0.0)
    release = next(edge for edge in priced["links"] if edge["target"] == "*~nh3")
    assert release["delta_e"] == pytest.approx(0.26675, abs=0.001)
    assert "graph term product_protonation (NH3->*~nh3):" in text
    assert "most endergonic route step at U:" in text
    assert meta == before


def test_rootless_graph_prices_landing_independently_of_node_order() -> None:
    graph = {
        "nodes": [
            {"id": name, "energy": energy, "rel_energy": energy, "n_H": n_h}
            for name, energy, n_h in (
                ("*~nh3", 0.6, 1),
                ("NH3", 0.2, 1),
                ("NO@N", 0.0, 0),
                ("NO@O", 0.0, 0),
            )
        ],
        "links": [
            {"source": a, "target": b, "delta_e": energy, "barrier": 0.7}
            for a, b, energy in (
                ("NO@N", "NO@O", 0.0),
                ("NO@O", "NO@N", 0.0),
                ("NO@O", "NH3", 0.2),
                ("NH3", "*~nh3", 0.4),
            )
        ],
    }
    before = copy.deepcopy(graph)
    env = Environment.from_dict(
        {"T": 298.15, "electrochemical": {"pH": 7, "U_vs_RHE": 0}}
    )

    priced = protonate_graph(graph, env, {})
    upstream_first = protonate_graph(
        {**graph, "nodes": list(reversed(graph["nodes"]))}, env, {}
    )

    nodes = {node["id"]: node for node in priced["nodes"]}
    assert nodes == {node["id"]: node for node in upstream_first["nodes"]}
    assert priced["links"] == upstream_first["links"]
    assert priced["ph_terms"] == upstream_first["ph_terms"]
    for key in ("energy", "rel_energy"):
        assert nodes["*~nh3"][key] == pytest.approx(0.46675, abs=0.001)
    assert priced["links"][-1]["delta_e"] == pytest.approx(0.26675, abs=0.001)
    assert graph == before


def test_analysis_refuses_cycle_carrying_release_with_next_step() -> None:
    meta = _meta()
    meta["graph"]["links"].append(
        {"source": "*~nh3", "target": "NO", "delta_e": -0.6, "barrier": 0.5}
    )
    before = copy.deepcopy(meta)

    with pytest.raises(
        BadInput, match="inconsistent released-product histories"
    ) as exc:
        _analysis_read(meta, _environment(), via_handler=True)

    assert exc.value.next == (
        "read view='analysis' without environment to inspect the stored graph"
    )
    assert meta == before


@pytest.mark.parametrize(
    ("source", "target", "expected"),
    [
        ("NH3~nh3", "*~nh3+nh3", ("NH3",)),
        ("NH3+NH3~h2o", "*~nh3+h2o+nh3", ("NH3", "NH3")),
        ("NH3~h2o+nh3", "*~nh3+nh3+h2o", ("NH3",)),
        ("N2~nh3", "*~nh3+n2", ("N2",)),
        ("NH3+O~nh3", "O~nh3", ("NH3",)),
    ],
)
def test_landing_tags_are_product_multisets(
    source: str, target: str, expected: tuple[str, ...]
) -> None:
    assert released_products(source, target) == expected


def test_unknown_new_product_in_cumulative_tag_reports_no_term() -> None:
    meta = _cumulative_release_meta()
    sr = routes_from_results(meta["results"], environment=_environment())
    water_release = sr.routes[1].rungs[-1]
    assert water_release.delta_g0 == pytest.approx(0.4)
    assert water_release.terms == ()
    assert "product_protonation H2O" in render_staircase(sr, u_rhe=-0.3)
    graph = protonate_graph(meta["graph"], _environment(), meta["results"])
    assert any(
        "H2O (H2O~nh3->*~h2o+nh3)" in note and "no term" in note
        for note in graph["ph_notes"]
    )


def test_she_url_round_trip_preserves_stated_record_and_resolved_potential() -> None:
    original = _environment(7.0, she=True)
    restored = Environment.from_url(dict(parse_qsl(original.to_url())))
    assert restored.to_dict() == original.to_dict()
    assert restored.basis == original.basis
    assert restored.electrochemical == {"U_vs_SHE": -0.3, "pH": 7.0}
    assert restored.u_rhe == pytest.approx(0.1141, abs=0.001)
