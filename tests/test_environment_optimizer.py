"""Stored-point objectives: real sweep numbers, masks, missing evidence and read surface."""

from __future__ import annotations

import copy
import json
import math
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from precis.quest.environment_optimizer import (
    KB_EV_K,
    EnvironmentPoint,
    barrier_term,
    bulk_check,
    evaluate_point,
)
from precis_pathway.staircase_routes import routes_from_results

POINT = EnvironmentPoint(0.0, 7.0, 298.0, {"NO": 0.1, "H2": 0.9}, 1e-6)
MAP: dict[str, Any] = {
    "regions": [{"termination": None, "label": "clean", "U_RHE_range": [-2.0, 2.0]}]
}
BULK: dict[str, Any] = {
    "ion_conc_M": 1e-6,
    "point": {"U_RHE": 0.0, "pH": 7.0, "verdict": "stable"},
}


def _results() -> dict[str, Any]:
    return {
        "target": "NH3",
        "che_affine": {
            "scale": "RHE",
            "T": 298.0,
            "pH": 7.0,
            "per_route": [
                {
                    "route": "NO@N -> N+O",
                    "steps": [{"edge": "split", "delta_g0": -0.5, "n_e": 0}],
                }
            ],
        },
        "her": {"edges": [{"edge": "her", "delta_g0": 0.715, "n_e": 1}]},
        "score": {"selectivity": {"margin_eV": 0.2}},
        "edges": [{"name": "split", "barrier": {"mean": 0.1}}],
        "kinetics_v2": {
            "environment": {"T": 298.0, "gases": {"inputs": POINT.pressures}},
            "tof_mf": 1.0,
            "selectivity": {"NH3": 0.9},
            "bep_flux_share": 0.0,
            "mf_status": {"ok": True, "state_err": 1e-12},
        },
    }


def _evaluate(
    results: dict[str, Any], point: EnvironmentPoint = POINT, **kwargs: Any
) -> Any:
    return evaluate_point(
        results,
        routes_from_results(results),
        point,
        target="NH3",
        tof_floor=1e-3,
        **kwargs,
    )


def test_real_sweep_grid_reproduces_rate_and_selectivity() -> None:
    sweep = json.loads(
        (Path(__file__).parent / "fixtures/kinetics_sweep.json").read_text(
            encoding="utf-8"
        )
    )
    row = sweep["points"][0]
    point = replace(POINT, T=row["T"], pressures=row["pressures"])
    result = _evaluate({"kinetics_sweep": sweep}, point)
    assert result.terms["r"].value == pytest.approx(
        KB_EV_K * 298 * math.log(184.86813894782972 / 1e-3)
    )
    assert result.terms["s"].value == pytest.approx(
        KB_EV_K * 298 * math.log(0.9991960023012091 / (1 - 0.9991960023012091))
    )
    assert "kinetics_sweep.points.T = 298 K" in result.terms["r"].reason
    assert result.checks["bep_flux_share"].value is None
    assert result.mask.value is None
    outside = _evaluate({"kinetics_sweep": sweep}, replace(point, T=299.0))
    assert outside.terms["r"].value is None
    assert "no stored kinetics" in outside.terms["r"].reason
    other_gas = _evaluate(
        {"kinetics_sweep": sweep}, replace(point, pressures={"Ar": row["p_total"]})
    )
    assert other_gas.terms["s"].value is None


@pytest.mark.parametrize("u", [-2.0, -0.5, 0.0, 0.5, 2.0])
def test_pw549461_shaped_screening_point(u: float) -> None:
    results = _results()
    results["che_affine"]["per_route"][0]["steps"][0]["delta_g0"] = 0.325
    results["kinetics_v2"].update(tof_mf=7e-53, bep_flux_share=1.0)
    results["edges"] = []
    before = copy.deepcopy(results)
    result = _evaluate(results, replace(POINT, U=u))
    assert result.mask.value == pytest.approx(0.0)
    assert result.objective.value == -math.inf
    assert result.terms["r"].value == pytest.approx(-2.9, abs=0.01)
    assert result.terms["m_th"].value == pytest.approx(-0.025)
    assert result.limiting == "r"
    assert result.terms["m_kin"].flags[0].startswith("BEP")
    assert result.checks["termination"].value is None
    if u == 0:
        assert result.terms["m_HER"].value == pytest.approx(0.39)
    assert results == before


def test_absent_terms_never_become_defaults() -> None:
    result = _evaluate({})
    for term in [*result.terms.values(), result.mask, result.objective]:
        assert term.value is None
        assert term.reason
    optional = barrier_term(None, None, 1.0)
    assert optional.value is None and optional.reason


@pytest.mark.parametrize(
    "cls", ["noise", "reverse", "consumed_by_other", "unresolved", "broken"]
)
def test_negative_tof_class_is_absent_with_reason(cls: str) -> None:
    results = _results()
    results["kinetics_v2"]["tof_mf"] = -1e-5
    results["kinetics_v2"]["mf_status"]["tof_sign"] = {"class": cls}
    term = _evaluate(results).terms["r"]
    assert term.value is None
    assert cls in term.reason


def test_positive_kmc_measurement_supersedes_negative_mf() -> None:
    results = _results()
    results["kinetics_v2"].update(
        tof_mf=-1.0, tof_kmc={"median": 0.1, "p5": 0.01, "p95": 1.0}
    )
    results["kinetics_v2"]["mf_status"]["tof_sign"] = {"class": "reverse"}
    term = _evaluate(results).terms["r"]
    assert term.value == pytest.approx(KB_EV_K * 298 * math.log(100))
    assert "tof_kmc.median" in term.reason


@pytest.mark.parametrize("source", ["tof_mf", "tof_kmc"])
def test_zero_tof_has_no_log_rate_and_fails_floor(source: str) -> None:
    results = _results()
    results["kinetics_v2"][source] = 0.0 if source == "tof_mf" else {"median": 0.0}
    result = _evaluate(results, map_entry=MAP, coads={}, bulk=BULK)
    assert result.terms["r"].value is None
    assert result.terms["r"].reason == "TOF is zero, so ln(TOF/floor) is undefined"
    assert result.checks["rate_floor"].value == 0.0
    assert result.mask.value == 0.0
    assert result.objective.value == -math.inf


def test_mask_pass_and_minimum_of_all_five_terms() -> None:
    result = _evaluate(_results(), map_entry=MAP, coads={}, bulk=BULK, include_ea=True)
    assert result.mask.value == pytest.approx(1.0)
    assert result.objective.value == pytest.approx(result.terms["s"].value)
    assert result.limiting == "s"
    assert "m_Ea" not in result.terms


@pytest.mark.parametrize(
    "failure", ["termination", "bulk", "reset", "bep", "settled", "poisoned", "floor"]
)
def test_each_mask_failure_excludes_the_point(failure: str) -> None:
    results = _results()
    surface = copy.deepcopy(MAP)
    bulk = copy.deepcopy(BULK)
    if failure == "termination":
        surface["regions"][0].update(termination={"adsorbate": "H", "n": 1}, label="H*")
    elif failure == "bulk":
        bulk["point"]["verdict"] = "dissolved"
    elif failure == "reset":
        results["edges"][0]["barrier"]["mean"] = 1.0
    elif failure == "bep":
        results["kinetics_v2"]["bep_flux_share"] = 0.6
    elif failure == "settled":
        results["kinetics_v2"]["mf_status"]["state_err"] = 0.1
    elif failure == "poisoned":
        results["kinetics_v2"]["mf_status"]["diagnosis"] = {"kind": "poisoned_surface"}
    else:
        results["kinetics_v2"]["tof_mf"] = 1e-4
    result = _evaluate(results, map_entry=surface, coads={}, bulk=bulk)
    assert result.mask.value == pytest.approx(0.0)
    assert result.objective.value == -math.inf


def test_missing_floor_or_selectivity_cannot_make_a_successful_objective() -> None:
    results = _results()
    results["kinetics_v2"]["selectivity"] = {}
    result = _evaluate(results, map_entry=MAP, coads={}, bulk=BULK)
    assert result.mask.value == pytest.approx(1.0)
    assert result.objective.value is None
    assert result.terms["s"].value is None and result.terms["s"].reason
    result = evaluate_point(
        results, routes_from_results(results), POINT, target="NH3", tof_floor=None
    )
    assert result.terms["r"].value is None
    assert "TOF_floor" in result.terms["r"].reason


def test_bulk_requires_matching_concentration_and_point() -> None:
    for point in [
        replace(POINT, ion_conc_M=1e-3),
        replace(POINT, pH=8),
        replace(POINT, U=0.1),
    ]:
        term = bulk_check(BULK, point)
        assert term.value is None and term.reason


def test_bulk_skips_non_dict_cells() -> None:
    bulk = {"ion_conc_M": 1e-6, "cells": [None, "invalid", 1, [], BULK["point"]]}
    assert bulk_check(bulk, POINT).value == 1.0
    bulk["cells"] = [None, "invalid", 1, []]
    term = bulk_check(bulk, POINT)
    assert term.value is None
    assert term.reason == "bulk verdict absent at this U/pH"


def test_pcet_merge_and_optional_barrier_screen() -> None:
    results = _results()
    results["che_affine"]["per_route"] = [
        {
            "route": "NO -> NO+H -> HNO",
            "steps": [
                {"edge": "NO->NO+H", "delta_g0": 0.2, "n_e": 1},
                {"edge": "NO+H->HNO", "delta_g0": 0.1, "n_e": 0},
            ],
        }
    ]
    assert _evaluate(results, replace(POINT, U=-0.4)).terms[
        "m_th"
    ].value == pytest.approx(0.1)
    results = _results()
    results["kinetics_v2"]["bep_flux_share"] = 1.0
    result = _evaluate(results, include_ea=True)
    assert result.terms["m_Ea"].value == pytest.approx(
        KB_EV_K * 298 * math.log(KB_EV_K * 298 / 4.135667696e-15) - 0.1
    )
    assert result.mask.value == pytest.approx(0.0)


def test_objective_view_renders_stored_points_without_writes() -> None:
    from precis_pathway.handler import PathwayHandler

    sweep = json.loads(
        (Path(__file__).parent / "fixtures/kinetics_sweep.json").read_text(
            encoding="utf-8"
        )
    )
    meta = {
        "status": "ready",
        "results": {**_results(), "kinetics_sweep": sweep},
        "config": {
            "conditions": {"potential_U": 0.0, "pH": 7.0},
            "slab": {"coads": {}},
            "objective": {"subject_to": {"tof": [1e-3, None]}},
        },
    }
    ref = SimpleNamespace(id=11, slug="objective", title="objective", meta=meta)
    store = SimpleNamespace(get_ref=lambda **kw: ref)
    handler: Any = PathwayHandler.__new__(PathwayHandler)
    handler.hub = SimpleNamespace(live_store=store)
    body = handler.get(id="objective", view="objective").body
    assert body is not None
    assert "Stored environment objectives (eV)" in body
    assert "Point 5:" in body
    assert "kinetics_sweep.points.T" in body
    assert "m_th=" in body and "M=" in body and "J=" in body
    assert "floor 0.001 /s" in body
