"""Stored per-transition kinetics: joins, provenance and optional annotations."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from precis_web.pathway_step_kinetics import attach_step_kinetics
from precis_web.routes.refs import _pathway_graph_payload
from tests._node import needs_node

from .test_pathway_detail import (
    _annot_block,
    _node_json,
    _run_url_probe,
    _seed_pathway,
    _step_kinetics_script,
)

_GRAPH = {
    "nodes": [
        {"id": "A", "rel_energy": 0.0},
        {"id": "NH3~history", "rel_energy": 0.2},
        {"id": "*~NH3", "rel_energy": 0.3},
    ],
    "links": [
        {"source": "A", "target": "NH3~history", "kind": "reaction", "barrier": 0.6},
        {
            "source": "NH3~history",
            "target": "*~NH3",
            "kind": "desorption",
            "removed": {"NH3": 1},
        },
    ],
}


def _v1() -> dict[str, Any]:
    return {
        "conditions": {"temperature": 400.0, "pressures": {"NH3": 0.1}},
        "product": "NH3",
        "selectivity": 0.75,
        "drc": {"X_RC": {"A->NH3": 0.8, "balance": 0.2}},
        "drc_source": "chain",
        "transitions": [
            {
                "name": "A->NH3",
                "from": "A",
                "to": "NH3",
                "kind": "reaction",
                "barrier_eV": 0.6,
                "dG_eV": 0.2,
                "k_f": 12.0,
                "k_b": 2.0,
                "net_rate": 0.0,
            },
            {
                "name": "NH3->*+NH3(g)",
                "from": "NH3",
                "to": "*",
                "kind": "desorption",
                "gas": "NH3",
                "net_rate": 0.01,
            },
        ],
    }


def _links(results: dict[str, Any]) -> list[dict[str, Any]]:
    graph = _pathway_graph_payload(_GRAPH, results=results, edges=results.get("edges"))
    assert graph is not None
    return graph["links"]


def test_v1_transition_join_strips_history_and_preserves_zero() -> None:
    results = {
        "kinetics": _v1(),
        "edges": [{"name": "A->NH3", "barrier": {"mean": 0.6}}],
    }
    reaction, desorption = _links(results)
    step = reaction["step_kinetics"]
    assert step["name"] == "A->NH3"
    assert step["source"] == "neb"
    quantities = step["quantities"]
    expected = {
        "ea_forward": 0.6,
        "ea_reverse": 0.4,
        "k_f": 12.0,
        "k_b": 2.0,
        "net_rate": 0.0,
        "X_RC": 0.8,
    }
    for key, value in expected.items():
        assert quantities[key]["value"] == pytest.approx(value)
        assert quantities[key]["solve"] == (
            "Rate control: chain estimate." if key == "X_RC" else "v1 chain"
        )
        assert quantities[key]["environment"]["T"] == 400.0
    assert "z_i" not in quantities
    assert desorption["step_kinetics"]["product"] == "NH3"
    assert desorption["step_kinetics"]["quantities"]["selectivity"]["value"] == 0.75


@pytest.mark.parametrize("barrier", [0.0, 0.6])
@pytest.mark.parametrize("source", [None, "none", "neb", "bep"])
def test_barrier_number_is_not_evidence_of_neb(
    barrier: float, source: str | None
) -> None:
    link: dict[str, Any] = {
        "source": "NH3",
        "target": "*~nh3",
        "kind": "supply",
        "link_type": "desorption",
        "barrier": barrier,
    }
    if source is not None:
        link["barrier_source"] = source
    attach_step_kinetics([link], {})
    assert link["step_kinetics"]["source"] == (source or "none")


def test_v2_only_has_diagnostics_without_inventing_chain_rates() -> None:
    reaction, desorption = _links(
        {
            "kinetics_v2": {
                "environment": {"T": 450.0, "p_total": 2.0},
                "tof_mf": 0.01,
                "drc": {"A->NH3": 0.4, "balance": 0.6},
                "drc_source": "mf",
                "reversibility": {"A->NH3": 0.25},
                "selectivity": {"NH3": 0.6},
            }
        }
    )
    quantities = reaction["step_kinetics"]["quantities"]
    assert quantities["X_RC"]["value"] == 0.4
    assert quantities["z_i"]["value"] == 0.25
    for key in ("X_RC", "z_i"):
        assert quantities[key]["solve"].startswith(
            "Rate control: mean-field estimate." if key == "X_RC" else "v2 mean-field"
        )
        assert quantities[key]["environment"]["T"] == 450.0
    for key in ("k_f", "k_b", "net_rate", "ea_forward", "ea_reverse"):
        assert key not in quantities
    assert desorption["step_kinetics"]["quantities"]["selectivity"]["value"] == 0.6


@pytest.mark.parametrize("results", [{}, {"kinetics": None, "kinetics_v2": None}])
def test_no_solve_keeps_quantities_absent(results: dict[str, Any]) -> None:
    for link in _links(results):
        assert link["step_kinetics"]["quantities"] == {}
        assert link["step_kinetics"]["arrhenius"] == []


def test_mixed_solves_keep_their_own_environment() -> None:
    reaction = _links(
        {
            "kinetics": _v1(),
            "kinetics_v2": {
                "environment": {"T": 500.0},
                "tof_mf": 0.2,
                "drc": {"A->NH3": 0.4, "balance": 0.6},
                "drc_source": "mf",
            },
        }
    )[0]
    quantities = reaction["step_kinetics"]["quantities"]
    assert quantities["k_f"]["solve"] == "v1 chain"
    assert quantities["k_f"]["environment"]["T"] == 400.0
    assert (
        quantities["X_RC"]["solve"]
        == "Rate control: mean-field estimate. Under audit (gr564356)."
    )
    assert quantities["X_RC"]["environment"]["T"] == 500.0


def test_nonfinite_and_null_rates_are_absent() -> None:
    record = _v1()
    record["transitions"][0].update(k_f=None, k_b=float("nan"), net_rate=float("inf"))
    quantities = _links({"kinetics": record})[0]["step_kinetics"]["quantities"]
    assert not {"k_f", "k_b", "net_rate"} & quantities.keys()


def test_ambiguous_physical_transition_is_not_guessed() -> None:
    record = _v1()
    first = record["transitions"][0]
    first["name"] = "A->NH3~one"
    record["transitions"].append({**first, "name": "A->NH3~two", "k_f": 99.0})
    quantities = _links({"kinetics": record})[0]["step_kinetics"]["quantities"]
    assert "k_f" not in quantities
    assert "net_rate" not in quantities


def test_exact_history_transition_precedes_physical_match() -> None:
    record = _v1()
    first = record["transitions"][0]
    record["transitions"].append({**first, "name": "A->NH3~history", "k_f": 99.0})
    step = _links({"kinetics": record})[0]["step_kinetics"]
    assert step["name"] == "A->NH3~history"
    assert step["quantities"]["k_f"]["value"] == 99.0


def test_failed_mean_field_diagnostics_keep_chain_provenance() -> None:
    quantities = _links(
        {
            "kinetics": _v1(),
            "kinetics_v2": {
                "environment": {"T": 400.0},
                "tof_mf": None,
                "mf_status": {"ok": False},
                "drc": {"A->NH3": 0.8, "balance": 0.2},
                "drc_source": "chain",
                "reversibility": {"A->NH3": 0.25},
            },
        }
    )[0]["step_kinetics"]["quantities"]
    assert quantities["X_RC"]["solve"] == "Rate control: chain estimate."
    assert quantities["z_i"]["solve"] == "v1 chain"
    assert quantities["X_RC"]["value"] == 0.8
    assert quantities["z_i"]["value"] == 0.25


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("failed", ["drc", "reversibility", "selectivity"])
def test_v2_only_observable_failure_keeps_per_quantity_provenance(
    nested: bool, failed: str
) -> None:
    record: dict[str, Any] = {
        "environment": {"T": 450.0, "p_total": 2.0},
        "tof_mf": 1.0,
        "mf_status": {"ok": True},
        "drc": {"A->NH3": 0.8, "balance": 0.2},
        "drc_source": "mf",
        "reversibility": {"A->NH3": 0.25},
        "selectivity": {"NH3": 0.6},
    }
    record["drc_source"] = "chain" if failed == "drc" else "mf"
    status = record["mf_status"] if nested else record
    status["failed_observables"] = [
        {"name": failed, "reason": "observable time budget spent"}
    ]
    reaction, desorption = _links({"kinetics_v2": record})
    quantities = {
        **reaction["step_kinetics"]["quantities"],
        **desorption["step_kinetics"]["quantities"],
    }
    for name, key, value in (
        ("drc", "X_RC", 0.8),
        ("reversibility", "z_i", 0.25),
        ("selectivity", "selectivity", 0.6),
    ):
        assert quantities[key]["value"] == value
        expected_solve = "v1 chain" if name == failed else "v2 mean-field"
        if key == "X_RC":
            expected_solve = (
                "Rate control: chain estimate."
                if name == failed
                else "Rate control: mean-field estimate."
            )
        assert quantities[key]["solve"].startswith(expected_solve)
        assert quantities[key]["environment"] == record["environment"]


def test_equal_drc_values_are_not_evidence_of_chain_fallback() -> None:
    reaction = _links(
        {
            "kinetics": _v1(),
            "kinetics_v2": {
                "tof_mf": 1.0,
                "mf_status": {"ok": True, "failed_observables": []},
                "drc": {"A->NH3": 0.8, "balance": 0.2},
                "drc_source": "mf",
            },
        }
    )[0]
    assert (
        reaction["step_kinetics"]["quantities"]["X_RC"]["solve"]
        == "Rate control: mean-field estimate. Under audit (gr564356)."
    )


@pytest.mark.parametrize("turnover", [-0.1, 0.0, None, float("nan"), float("inf")])
def test_turnover_does_not_establish_unrecorded_control_source(
    turnover: float | None,
) -> None:
    link: dict[str, Any] = {"source": "A", "target": "B"}
    attach_step_kinetics(
        [link],
        {
            "kinetics": {"drc": {"X_RC": {"A->B": 0.8, "balance": 0.2}}},
            "kinetics_v2": {
                "tof_mf": turnover,
                "mf_status": {"ok": True},
                "drc": {"A->B": 0.8, "balance": 0.2},
                "reversibility": {"A->B": 0.25},
            },
        },
    )
    quantities = link["step_kinetics"]["quantities"]
    assert quantities["X_RC"]["value"] == pytest.approx(0.8)
    assert quantities["X_RC"]["solve"] == (
        "Rate control source was not recorded; the values are unreliable."
    )
    if turnover is not None and math.isfinite(turnover):
        assert quantities["z_i"]["solve"] == "v2 mean-field"


def _sweep() -> dict[str, Any]:
    fixture = Path(__file__).parents[1] / "fixtures" / "kinetics_sweep.json"
    return json.loads(fixture.read_text(encoding="utf-8"))


def _temperature_sweep() -> dict[str, Any]:
    """Extend the real aggregate schema with a known temperature dependence."""
    sweep = _sweep()
    seed = sweep["points"][0]
    sweep["points"] += [
        {**seed, "T": t, "tof_mf": 1e12 * math.exp(-0.5 / (8.617333262145e-5 * t))}
        for t in (380.0, 400.0, 420.0)
    ]
    # Keep the other pressure's real records; fit only this known series.
    sweep["points"] = [
        p
        for p in sweep["points"]
        if p["p_total"] != seed["p_total"] or 380 <= p["T"] <= 420
    ]
    return sweep


def test_real_sweep_fixture_has_too_few_temperatures_for_inset() -> None:
    sweep = _sweep()
    assert all(
        "tof_mf" in point and "transitions" not in point for point in sweep["points"]
    )
    link = _links({"kinetics": _v1(), "kinetics_sweep": sweep})[0]
    assert link["step_kinetics"]["arrhenius"] == []


def test_arrhenius_fits_aggregate_turnover_at_fixed_pressure() -> None:
    links = _links({"kinetics": _v1(), "kinetics_sweep": _temperature_sweep()})
    link = links[0]
    series = link["step_kinetics"]["arrhenius"]
    assert series == links[1]["step_kinetics"]["arrhenius"]
    assert len(series) == 1
    assert series[0]["environment"] == {
        "p_total": 0.1,
        "pressures": _sweep()["points"][0]["pressures"],
    }
    for item in series:
        assert item["solve"] == "v2 mean-field"
        assert item["ea_eV"] == pytest.approx(0.5)
        assert item["prefactor"] == pytest.approx(1e12)
        assert [point["T"] for point in item["points"]] == [380.0, 400.0, 420.0]
        assert [point["T"] for point in item["read_off"]] == [390.0, 410.0]
        for point in item["read_off"]:
            assert point["turnover"] == pytest.approx(
                1e12 * math.exp(-0.5 / (8.617333262145e-5 * point["T"]))
            )


@pytest.mark.parametrize(
    "sweep",
    [None, {}, {"points": [{"T": 380.0, "tof_mf": 1.0}, {"T": 420.0, "tof_mf": 2.0}]}],
)
def test_arrhenius_requires_three_temperatures(sweep: Any) -> None:
    link = _links({"kinetics": _v1(), "kinetics_sweep": sweep})[0]
    assert link["step_kinetics"]["arrhenius"] == []


@pytest.mark.parametrize("rate", [None, 0.0, -1.0, float("nan"), float("inf")])
def test_arrhenius_requires_positive_finite_turnover(rate: float | None) -> None:
    sweep = _temperature_sweep()
    sweep["points"][-1]["tof_mf"] = rate
    link = _links({"kinetics_sweep": sweep})[0]
    assert link["step_kinetics"]["arrhenius"] == []


def test_arrhenius_chooses_most_temperatures_without_mixing_composition() -> None:
    sweep = _temperature_sweep()
    seed = sweep["points"][-1]
    sweep["points"] += [
        {**seed, "T": t, "pressures": {"NO": 0.05, "H2": 0.05}, "tof_mf": 2.0}
        for t in (370.0, 390.0, 410.0, 430.0)
    ]
    # Repeated temperatures must not let the shorter composition set win.
    sweep["points"] += [dict(seed) for _ in range(5)]
    series = _links({"kinetics_sweep": sweep})[0]["step_kinetics"]["arrhenius"]
    assert len(series) == 1
    assert series[0]["environment"]["p_total"] == 0.1
    assert series[0]["environment"]["pressures"] == {"NO": 0.05, "H2": 0.05}
    assert [p["T"] for p in series[0]["points"]] == [370.0, 390.0, 410.0, 430.0]
    assert series[0]["prefactor"] == pytest.approx(2.0)
    assert series[0]["ea_eV"] == pytest.approx(0.0)


def test_arrhenius_read_off_stays_inside_stored_range() -> None:
    record = _v1()
    record["conditions"]["temperature"] = 380.0
    series = _links({"kinetics": record, "kinetics_sweep": _temperature_sweep()})[0][
        "step_kinetics"
    ]["arrhenius"]
    assert [p["T"] for p in series[0]["read_off"]] == [390.0]


@needs_node
def test_quantity_layer_url_round_trip_and_master_toggle(tmp_path: Path) -> None:
    link = _links({"kinetics": _v1()})[0]
    got = _node_json(
        tmp_path,
        _annot_block()
        + "\nconst link = "
        + json.dumps(link)
        + """;
const layers = pwReadLayers(new URLSearchParams('layer_kin_k_f=1'));
const qs = new URLSearchParams();
pwWriteLayers(qs, layers);
console.log(JSON.stringify({
  query: qs.toString(), restored: pwReadLayers(qs),
  label: PWStepKinetics.label(link, pwLayerPresentation(layers)),
  hidden: PWStepKinetics.label(link, pwLayerPresentation({...layers, kinetics:false})),
  hover: PWStepKinetics.text(link),
}));
""",
    )
    assert got["query"] == "layer_kin_k_f=1"
    assert got["restored"]["kin_k_f"] is True
    assert got["restored"]["kin_k_b"] is False
    assert "k_f" in got["label"]
    assert got["hidden"] == ""
    assert "v1 chain" in got["hover"]
    assert "400" in got["hover"]


@needs_node
@pytest.mark.parametrize(
    ("values", "source", "expected"),
    [
        ({"A->NH3": 1.0, "missing": None}, "mf", "No usable rate control."),
        ({"A->NH3": 150.995}, None, "No usable rate control."),
        ({"A->NH3": 1.0}, None, "source was not recorded"),
        ({"A->NH3": 1.0}, "mf", "mean-field estimate"),
        ({"A->NH3": 0.99995}, "chain", "chain estimate"),
    ],
)
def test_step_control_label_and_hover_follow_recorded_rule(
    tmp_path, values, source, expected
):
    link = _links({"kinetics_v2": {"drc": values, "drc_source": source}})[0]
    got = _node_json(
        tmp_path,
        _step_kinetics_script()
        + "\nconst link = "
        + json.dumps(link)
        + "; console.log(JSON.stringify({label: PWStepKinetics.label(link, "
        + "{kinetics:true, kin_source:false, kin_X_RC:true}), "
        + "hover: PWStepKinetics.text(link)}));",
    )
    assert expected in got["label"] and expected in got["hover"]
    if expected == "No usable rate control.":
        assert "X_RC=" not in got["label"]
        assert "X_RC=" not in got["hover"]


@needs_node
@pytest.mark.parametrize("source", ["mf", "chain", None])
def test_step_control_missing_from_usable_map_is_explicitly_absent(
    tmp_path: Path, source: str | None
) -> None:
    link: dict[str, Any] = {"source": "C", "target": "D"}
    attach_step_kinetics(
        [link], {"kinetics_v2": {"drc": {"A->B": 1}, "drc_source": source}}
    )
    assert "X_RC" not in link["step_kinetics"]["quantities"]
    got = _node_json(
        tmp_path,
        _step_kinetics_script()
        + "\nconst link = "
        + json.dumps(link)
        + "; console.log(JSON.stringify({label: PWStepKinetics.label(link, "
        + "{kinetics:true, kin_source:false, kin_X_RC:true}), "
        + "hover: PWStepKinetics.text(link)}));",
    )
    for text in (got["label"], got["hover"]):
        assert "X_RC=absent" in text
        assert link["step_kinetics"]["drc_note"] in text
        assert "No usable rate control" not in text


@needs_node
def test_arrhenius_markup_labels_whole_network_and_escapes_text(
    tmp_path: Path,
) -> None:
    plain = _links({"kinetics": _v1()})[0]
    swept = _links({"kinetics": _v1(), "kinetics_sweep": _temperature_sweep()})[0]
    swept["step_kinetics"]["arrhenius"][0]["environment"]["composition"] = (
        '<img src=x onerror="alert(1)">'
    )
    got = _node_json(
        tmp_path,
        _step_kinetics_script()
        + "\nconst links = "
        + json.dumps([plain, swept])
        + "; console.log(JSON.stringify(links.map(PWStepKinetics.inset)));",
    )
    assert got[0] == ""
    assert "<svg" in got[1]
    assert "Arrhenius" in got[1]
    assert "apparent, whole network" in got[1]
    assert "ln(turnover)" in got[1]
    assert "Fitted apparent prefactor" in got[1]
    assert "Per-step rates are not stored in sweeps." in got[1]
    assert "k_f" not in got[1]
    assert "<img" not in got[1]
    assert "&lt;img" in got[1]


@needs_node
def test_arrhenius_renders_once_for_pathway_without_transition_events(
    tmp_path: Path,
) -> None:
    links = _links({"kinetics": _v1(), "kinetics_sweep": _temperature_sweep()})
    got = _node_json(
        tmp_path,
        _step_kinetics_script()
        + "\nconst links = "
        + json.dumps(links)
        + """;
const panel = {innerHTML: ''};
const document = {getElementById: id => id === 'pw-arrhenius-inset' ? panel : null};
PWStepKinetics.renderInset(links);
const initial = panel.innerHTML;
PWStepKinetics.renderInset(links.slice().reverse());
const rerendered = panel.innerHTML;
PWStepKinetics.renderInset([]);
console.log(JSON.stringify({initial, rerendered, empty: panel.innerHTML}));
""",
    )
    assert got["initial"].count('<figure class="pw-arrhenius">') == 1
    assert "apparent, whole network" in got["initial"]
    assert got["rerendered"] == got["initial"]
    assert got["empty"] == ""


@needs_node
def test_pathway_rate_label_is_rendered_from_url(
    client, runtime, tmp_path: Path
) -> None:
    _seed_pathway(
        runtime.store,
        meta={"graph": _GRAPH, "results": {"kinetics": _v1()}},
        body_text=None,
    )
    html = client.get("/refs/pathway/171696").text
    assert "/static/pathway-step-kinetics.js" in html
    # _v1's step has zero net rate, which the default turnover floor cuts.
    out = _run_url_probe(
        tmp_path, html, search="?layer_kin_k_f=1&layer_turnover_filter=0"
    )
    assert "k_f" in out["svg"]
    assert next(layer for layer in out["layers"] if layer["key"] == "kin_k_f")[
        "checked"
    ]


@needs_node
def test_pathway_transition_hover_and_label_escape_stored_text(
    client, runtime, tmp_path: Path
) -> None:
    record = _v1()
    record["transitions"][0]["name"] = '<img src=x onerror="alert(1)">'
    record["conditions"]["entropy_model"] = '<script>alert("stored")</script>'
    _seed_pathway(
        runtime.store,
        meta={"graph": _GRAPH, "results": {"kinetics": record}},
        body_text=None,
    )
    html = client.get("/refs/pathway/171696").text
    # _v1's step has zero net rate, which the default turnover floor cuts.
    out = _run_url_probe(
        tmp_path, html, search="?layer_kin_k_f=1&layer_turnover_filter=0"
    )
    assert "<img" not in out["svg"]
    assert "<script" not in out["svg"]
    assert "&lt;img" in out["svg"]
    assert "&lt;script" in out["svg"]
