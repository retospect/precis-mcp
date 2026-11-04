"""Stored pathway optima keep thermodynamic and kinetic validity separate."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from precis.quest import pourbaix_map, staircase, window_objectives
from precis_pathway import optimum
from precis_pathway.handler import PathwayHandler


@pytest.fixture
def sweep() -> dict[str, Any]:
    return json.loads(
        (Path(__file__).parent / "fixtures" / "kinetics_sweep.json").read_text(
            encoding="utf-8"
        )
    )


def _che() -> dict[str, Any]:
    return {
        "scale": "RHE",
        "T": 310.0,
        "pH": 7.0,
        "per_route": [
            {
                "route": "A -> B",
                "steps": [{"edge": "A->B", "delta_g0": 0.4, "n_e": 1}],
                "U_L": -0.4,
                "U_opt": -0.55,
                "span_at_UL": 0.7,
                "span_at_Uopt": 0.6,
            }
        ],
    }


def _ref(results: dict[str, Any], **meta: Any) -> SimpleNamespace:
    return SimpleNamespace(
        id=71,
        kind="pathway",
        slug="stored-optimum",
        title="Stored optimum",
        meta={"status": "ready", "results": results, **meta},
    )


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock()
    mock.get_ref.return_value = None
    mock.fetch_refs_by_ids.return_value = {}
    mock.links_for.return_value = []
    monkeypatch.setattr(staircase, "structures_for_pathway", lambda *_: [])
    monkeypatch.setattr(pourbaix_map, "maps_for_structure", lambda *_a, **_kw: [])
    return mock


def _validity(text: str) -> None:
    assert "thermodynamic window at the stored T" in text
    assert "kinetic optimum at the run U" in text


def test_both_halves_keep_stored_conditions_and_provenance(
    store: MagicMock, sweep: dict[str, Any]
) -> None:
    ref = _ref(
        {
            "che_affine": _che(),
            "kinetics_sweep": sweep,
            "kinetics_v2": {
                "environment": {
                    "electrochemical": {"provider": "che", "U_vs_RHE": -1.0}
                }
            },
        },
        config={"conditions": {"potential_U": -0.25}},
    )
    before = copy.deepcopy(ref.meta)
    text = optimum.optimum_text(store, ref)

    _validity(text)
    assert "pw71" in text
    assert "results.che_affine" in text
    assert "results.kinetics_sweep.optimum" in text
    assert "310" in text and "450" in text
    assert "Run U: -1 V vs RHE" in text
    assert "results.kinetics_v2.environment.electrochemical.U_vs_RHE" in text
    assert "-0.25" not in text
    assert "tof" in text and "234.9" in text
    assert "composition" in text and "NO" in text and "H2" in text
    assert "joint" in text.lower()
    assert ref.meta == before
    store.stamp_ref_meta.assert_not_called()
    store.insert_ref.assert_not_called()


def test_sweep_run_potential_takes_precedence_over_solve(
    store: MagicMock, sweep: dict[str, Any]
) -> None:
    sweep["environment"] = {"electrochemical": {"provider": "che", "U_vs_RHE": 0.0}}
    text = optimum.optimum_text(
        store,
        _ref(
            {
                "kinetics_sweep": sweep,
                "kinetics_v2": {
                    "environment": {
                        "electrochemical": {"provider": "che", "U_vs_RHE": -1.0}
                    }
                },
            },
            config={"conditions": {"potential_U": -0.25}},
        ),
    )

    assert "Run U: 0 V vs RHE" in text
    assert "results.kinetics_sweep.environment.electrochemical.U_vs_RHE" in text


@pytest.mark.parametrize("potential", [-0.25, "optimal"])
@pytest.mark.parametrize("recorded_u", [None, True, "-1.0", float("nan")])
def test_profile_potential_cannot_fill_missing_run_evidence(
    store: MagicMock,
    sweep: dict[str, Any],
    potential: float | str,
    recorded_u: Any,
) -> None:
    text = optimum.optimum_text(
        store,
        _ref(
            {
                "kinetics_sweep": sweep,
                "U_opt": -0.875,
                "kinetics_v2": {
                    "environment": {
                        "electrochemical": {"provider": "che", "U_vs_RHE": recorded_u}
                    }
                },
            },
            config={
                "conditions": {"potential_U": potential},
                "electrochemistry": {"U_vs_RHE": -0.25},
                "environment": {
                    "electrochemical": {"provider": "che", "U_vs_RHE": -0.25}
                },
            },
        ),
    )

    assert "Run U: unknown — run U is not recorded" in text
    assert "-0.25" not in text
    assert "-0.875" not in text


def test_che_without_coverage_keeps_route_scalars_and_names_next_scan(
    store: MagicMock,
) -> None:
    text = optimum.optimum_text(store, _ref({"che_affine": _che()}, candidate_ref=81))

    _validity(text)
    assert "A -> B" in text
    for key in ("U_L", "U_opt", "span_at_UL", "span_at_Uopt"):
        assert key in text
    for value in ("-0.4", "-0.55", "0.7", "0.6"):
        assert value in text
    assert "no stored coverage scan" in text.lower()
    assert "resting" in text.lower() and "unknown" in text.lower()
    assert "surface_coverage_scan" in text and "81" in text
    assert "no stored kinetic optimum" in text.lower()
    assert "recompute_kinetics" in text


def test_kinetics_without_che_still_reports_fixture_optimum(
    store: MagicMock, sweep: dict[str, Any]
) -> None:
    text = optimum.optimum_text(store, _ref({"kinetics_sweep": sweep}))

    _validity(text)
    assert "no stored thermodynamic data" in text.lower()
    assert "put(" in text and "run" in text
    assert "results.kinetics_sweep.optimum" in text
    assert "450" in text and "234.9" in text
    assert "run U" in text and "unknown" in text.lower()


def test_neither_half_names_both_next_calls(store: MagicMock) -> None:
    text = optimum.optimum_text(store, _ref({}))

    _validity(text)
    assert "no stored thermodynamic data" in text.lower()
    assert "no stored kinetic optimum" in text.lower()
    assert "put(" in text and "run" in text and "recompute_kinetics" in text


def test_null_optimum_does_not_choose_a_sweep_point(
    store: MagicMock, sweep: dict[str, Any]
) -> None:
    sweep["optimum"]["value"] = None
    text = optimum.optimum_text(store, _ref({"kinetics_sweep": sweep}))

    assert "no stored kinetic optimum" in text.lower()
    assert "recompute_kinetics" in text
    assert "234.9" not in text


def test_thermodynamic_u_opt_does_not_fill_an_unknown_kinetic_run_u(
    store: MagicMock, sweep: dict[str, Any]
) -> None:
    text = optimum.optimum_text(
        store,
        _ref({"che_affine": _che(), "U_opt": -0.875, "kinetics_sweep": sweep}),
    )

    run_line = next(
        line for line in text.splitlines() if line.lower().startswith("run u")
    )
    assert "unknown" in run_line.lower()
    assert "-0.875" not in run_line


def test_stored_objective_and_constraint_are_not_reoptimized(
    store: MagicMock, sweep: dict[str, Any]
) -> None:
    sweep["optimum"].update(
        objective="selectivity",
        value=0.9375,
        subject_to={"tof_min": 25.0},
    )
    sweep["points"][0]["tof_mf"] = 987654.0
    text = optimum.optimum_text(store, _ref({"kinetics_sweep": sweep}))

    assert "selectivity" in text and "0.9375" in text
    assert "subject_to" in text and "tof_min" in text and "25" in text
    assert "450" in text
    assert "987654" not in text


def test_coverage_uses_candidate_map_and_reports_measure_sources(
    store: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = SimpleNamespace(id=81, kind="structure", meta={"params": {}})
    foreign = SimpleNamespace(id=82, kind="structure", meta={"params": {}})
    store.get_ref.return_value = candidate
    store.fetch_refs_by_ids.return_value = {81: candidate, 82: foreign}
    monkeypatch.setattr(
        staircase, "structures_for_pathway", lambda *_: [foreign, candidate]
    )
    calls: list[int] = []

    def maps(_store: Any, structure: Any, **_kw: Any) -> list[dict[str, Any]]:
        calls.append(structure.id)
        return [
            {
                "scan_jobs": [901 if structure.id == 81 else 902],
                "sweep_job": 901 if structure.id == 81 else 902,
                "anchor_key": "stored-anchor",
                "engine_version": "0.25.0",
                "corrections": None,
                "conditions": {"temperature": 310.0},
                "U_RHE_window": [-1.0, 0.0],
                "pH_window": [0.0, 14.0],
                "grid": 5,
                "regions": [
                    {
                        "key": None,
                        "label": "clean",
                        "U_RHE_range": [-1.0, 0.0],
                    }
                ],
                "incomplete": [],
            }
        ]

    monkeypatch.setattr(pourbaix_map, "maps_for_structure", maps)
    measures = MagicMock(
        return_value={
            "window_area_VpH": 5.25,
            "window_fraction": 0.375,
            "shapley_ipr": 1.25,
            "bottleneck_rung": "A->B",
            "her_margin_at_Uopt": None,
            "window_notes": [],
        }
    )
    monkeypatch.setattr(window_objectives, "window_measures", measures)
    text = optimum.optimum_text(
        store,
        _ref(
            {"che_affine": _che()},
            candidate_ref=81,
            config={"conditions": {"potential_U": -0.5}},
        ),
    )

    measures.assert_called_once()
    assert calls == [81]
    assert "jo901" in text and "jo902" not in text
    assert "window_area_VpH" in text and "5.25" in text
    assert "window_fraction" in text and "0.375" in text
    assert "shapley_ipr" in text and "1.25" in text
    assert "pw71" in text and "results.che_affine" in text


def test_handler_dispatch_needs_no_engine_initialization(
    store: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = _ref({})
    store.get_ref.return_value = ref
    render = MagicMock(return_value="stored optima")
    monkeypatch.setattr(optimum, "optimum_text", render)
    handler = PathwayHandler.__new__(PathwayHandler)
    setattr(handler, "hub", SimpleNamespace(live_store=store))

    response = handler.get(id="stored-optimum", view="optimum")

    render.assert_called_once_with(store, ref)
    assert response.body is not None
    assert "stored optima" in response.body
    assert "pw71" in response.body


@pytest.mark.parametrize("temperature", [310.0, 310.0 + 5e-7])
@pytest.mark.parametrize(
    ("coads", "termination", "fraction", "area"),
    [
        ({}, None, "0.6", "10.875"),
        ({"H": 3}, {"adsorbate": "H", "n": 3}, "0.6", "10.875"),
        ({"H": 3}, {"adsorbate": "H", "n": 3, "counts": {}}, "0.6", "10.875"),
        (
            {"NO": 1, "H": 3},
            {"adsorbate": "NO+H", "n": 4, "counts": {"NO": 1, "H": 3}},
            "0.6",
            "10.875",
        ),
        (
            {"NO": 1, "H": 3},
            {"adsorbate": "NO+H", "n": 4, "counts": {"NO": 2, "H": 2}},
            "0",
            "0",
        ),
    ],
    ids=["clean", "single-species", "empty-counts", "mixed-match", "mixed-mismatch"],
)
def test_real_window_measures_use_the_stored_staircase(
    store: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    temperature: float,
    coads: dict[str, int],
    termination: dict[str, Any] | None,
    fraction: str,
    area: str,
) -> None:
    candidate = SimpleNamespace(id=81, meta={"params": {"coads": coads}})
    store.get_ref.return_value = candidate
    monkeypatch.setattr(
        pourbaix_map,
        "maps_for_structure",
        lambda *_: [
            {
                "scan_jobs": [901],
                "sweep_job": 901,
                "conditions": {"temperature": temperature},
                "U_RHE_window": [-1.0, 0.0],
                "pH_window": [0.0, 14.0],
                "grid": 5,
                "regions": [{"U_RHE_range": [-1.0, 0.0], "termination": termination}],
            }
        ],
    )
    text = optimum.optimum_text(store, _ref({"che_affine": _che()}, candidate_ref=81))

    # Three of five U cells satisfy 0.4 + U <= 0, if the termination matches.
    assert f"window_fraction = {fraction}" in text.splitlines()
    assert f"window_area_VpH = {area}" in text.splitlines()
    assert "surface_coverage_scan" not in text
    if fraction != "0":
        assert "shapley_ipr = 1" in text
    assert "no HER competitor" in text


@pytest.mark.parametrize(
    ("conditions", "scan_at"),
    [
        ({"temperature": 600.0}, "600 K"),
        ({"temperature": 310.001}, "310.001 K"),
        ({}, "an unrecorded temperature"),
        (None, "an unrecorded temperature"),
    ],
)
def test_coverage_temperature_must_match_pathway_for_window_measures(
    store: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    sweep: dict[str, Any],
    conditions: dict[str, float] | None,
    scan_at: str,
) -> None:
    store.get_ref.return_value = SimpleNamespace(id=81, meta={})
    monkeypatch.setattr(
        pourbaix_map,
        "maps_for_structure",
        lambda *_: [
            {
                "scan_jobs": [901],
                "sweep_job": 901,
                "conditions": conditions,
                "U_RHE_window": [-1.0, 0.0],
                "pH_window": [0.0, 14.0],
                "regions": [{"U_RHE_range": [-1.0, 0.0], "termination": None}],
            }
        ],
    )
    measures = MagicMock()
    monkeypatch.setattr(window_objectives, "window_measures", measures)

    text = optimum.optimum_text(
        store,
        _ref({"che_affine": _che(), "kinetics_sweep": sweep}, candidate_ref=81),
    )

    measures.assert_not_called()
    assert f"Resting state is from a scan at {scan_at}" in text
    assert "validity at the pathway's T = 310 K is unavailable" in text
    assert "Window area and Shapley unavailable" in text
    assert "st81 coverage scans jo901; sweep jo901" in text
    assert "window_area_VpH =" not in text
    assert "shapley_ipr =" not in text
    assert "Feasible U_RHE intervals" not in text
    assert (
        "Next: put(kind='job', job_type='surface_coverage_scan', parent_id=81" in text
    )
    assert "results.kinetics_sweep.optimum" in text
    assert "recompute_kinetics" not in text


@pytest.mark.parametrize(
    ("delta_g0", "regions", "intervals"),
    [
        (0.4, [(-1.0, 0.0)], ["[-1, -0.4]"]),
        (1.4, [(-1.0, 0.0)], []),
        (0.4, [(-1.0, -0.8), (-0.6, -0.4)], ["[-1, -0.8]", "[-0.6, -0.4]"]),
    ],
    ids=["rung-limited", "empty", "disjoint-resting-regions"],
)
def test_scan_domain_is_separate_from_sampled_feasible_intervals(
    store: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    delta_g0: float,
    regions: list[tuple[float, float]],
    intervals: list[str],
) -> None:
    store.get_ref.return_value = SimpleNamespace(id=81, meta={})
    monkeypatch.setattr(
        pourbaix_map,
        "maps_for_structure",
        lambda *_: [
            {
                "U_RHE_window": [-1.0, 0.0],
                "conditions": {"temperature": 310.0},
                "pH_window": [0.0, 14.0],
                "grid": 101,
                "regions": [
                    {"U_RHE_range": list(bounds), "termination": None}
                    for bounds in regions
                ],
            }
        ],
    )
    che = _che()
    che["per_route"][0]["steps"][0]["delta_g0"] = delta_g0

    text = optimum.optimum_text(store, _ref({"che_affine": che}, candidate_ref=81))

    assert "U_RHE scan domain = [-1.0, 0.0] V" in text
    assert "U_RHE window =" not in text
    feasible = next(
        line
        for line in text.splitlines()
        if line.startswith("Feasible U_RHE intervals (sampled):")
    )
    assert "A -> B" in feasible
    if intervals:
        assert "V vs RHE" in feasible
        for interval in intervals:
            assert interval in feasible
        assert feasible.count("[") == len(intervals)
        assert "[-1, 0]" not in feasible
    else:
        assert "none" in feasible.lower()
        assert "[" not in feasible


def test_stored_span_names_engine_potential_separately_from_pcet_limit(
    store: MagicMock,
) -> None:
    results = json.loads(
        (
            Path(__file__).parent / "fixtures" / "pathway" / "spso_che_affine.json"
        ).read_text(encoding="utf-8")
    )["pw483780_st479058"]

    text = optimum.optimum_text(store, _ref(results))

    route_line = next(
        line
        for line in text.splitlines()
        if line.startswith("Route ") and "HNOH -> NH+OH ->" in line
    )
    assert "U_L = -0.238182 V vs RHE (derived in the PCET convention)" in route_line
    assert "span_at_UL = 2.1481 eV (at engine U_L = 0.279842 V vs RHE)" in route_line
    assert "results.che_affine.per_route[1]" in route_line


def test_declared_floor_is_not_claimed_as_applied(
    store: MagicMock, sweep: dict[str, Any]
) -> None:
    text = optimum.optimum_text(
        store,
        _ref(
            {"kinetics_sweep": sweep},
            config={"objective": {"subject_to": {"tof_min": 25.0}}},
        ),
    )

    assert "rate floor unrecorded" in text
    assert "config.objective.subject_to" in text
    assert "not proof of the sweep's effective constraint" in text


def test_chemical_blocker_cannot_claim_a_viable_limiting_potential(
    store: MagicMock,
) -> None:
    che = _che()
    route = che["per_route"][0]
    route["route"] = "A -> B -> C"
    route["steps"].append({"edge": "B->C", "delta_g0": 0.5, "n_e": 0})
    text = optimum.optimum_text(store, _ref({"che_affine": che}))

    assert "U_L = n/a" in text
    assert "blocked by B->C" in text
    assert "U-flat" in text
    assert "U_L = -0.4" not in text
