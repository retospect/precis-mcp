"""Shared pathway points: projection, lookup-only reads and explicit solve validation."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from urllib.parse import parse_qsl

import pytest

from precis.errors import BadInput
from precis.quest.environment_basis import environment_basis
from precis.response import Response
from precis_pathway.environment import Environment, lookup_environment
from precis_pathway.handler import PathwayHandler

_BLOCK: dict[str, Any] = {
    "T": 350.0,
    "p_total": 10.0,
    "gases": {"inputs": {"NO": 1.0, "H2": 3.0}, "inerts": {"Ar": 1.0}},
    "sticking": {"NO": {"s0": 0.5, "source": "pa123"}},
    "electrochemical": {"U_vs_RHE": -0.3, "pH": 7.0, "ion_conc_M": 0.1},
}


def test_projects_config_electrochemistry_without_turning_window_into_point() -> None:
    env = Environment.from_config(
        {
            "electrochemistry": {
                "T": 350.0,
                "U_vs_RHE": -0.3,
                "pH": 7.0,
                "ion_conc_M": 0.1,
                "U_window": [-1.5, 0.5],
            }
        }
    )
    assert env.T == 350.0
    assert env.electrochemical == _BLOCK["electrochemical"]
    assert env.to_dict()["gases"]["unlisted"] == "reference"
    assert "U_window" not in env.to_dict()


def test_projects_config_conditions_with_legacy_precedence() -> None:
    config: dict[str, Any] = {
        "conditions": {
            "temperature": 350.0,
            "pressures": {"NO": 2.0},
            "potential_U": -0.3,
            "pH": 7.0,
        },
        "electrochemistry": {"T": 290.0, "U_vs_RHE": -0.8, "pH": 2.0},
        "kinetics": {"sticking": {"NO": 0.5}},
    }
    env = Environment.from_config(config)
    assert env.T == 350.0
    assert env.p_total == 2.0
    assert env.electrochemical == {"U_vs_RHE": -0.3, "pH": 7.0}
    assert env.to_dict()["sticking"] == {"NO": {"s0": 0.5, "source": None}}


def test_resolved_record_preserves_scaled_pressures_without_rescaling() -> None:
    record: dict[str, Any] = {
        **_BLOCK,
        "p_total_stated": True,
        "composition": {"NO": 0.2, "H2": 0.6, "Ar": 0.2},
        "gases": {"inputs": {"NO": 2.0, "H2": 6.0}, "inerts": {"Ar": 2.0}},
    }
    env = Environment.from_record(record)
    assert env.basis == Environment.from_dict(_BLOCK).basis
    assert env.p_total == 10.0
    assert env.to_dict()["gases"]["inputs"] == {"NO": 2.0, "H2": 6.0}
    assert "composition" not in env.to_dict()
    assert "p_total_stated" not in env.to_dict()


def test_resolved_far_preset_and_legacy_reference_policy_survive_projection() -> None:
    far = Environment.from_record(
        {"T": 350.0, "gases": {"inputs": {"NO": 1.0}, "preset": "far"}}
    )
    assert far.to_dict()["gases"]["outputs"] == "far"
    legacy = Environment.from_record({"T": 298.15, "legacy": True})
    assert legacy.to_dict()["gases"]["unlisted"] == "reference"
    assert legacy.basis == environment_basis({})


def test_projects_quest_operating_conditions() -> None:
    env = Environment.from_operating_conditions(
        {
            "T": 350.0,
            "p_total": 10.0,
            "gases": _BLOCK["gases"],
            "sticking": _BLOCK["sticking"],
            "U_RHE": -0.3,
            "pH": 7.0,
            "ion_conc_M": 0.1,
            "window": {"U": [-1.0, 0.0]},
        }
    )
    assert env.basis == Environment.from_dict(_BLOCK).basis
    assert "window" not in env.to_dict()


def test_url_roundtrip_preserves_full_environment_basis() -> None:
    env = Environment.from_dict(_BLOCK)
    decoded = Environment.from_url(dict(parse_qsl(env.to_url())))
    assert decoded.to_dict() == env.to_dict()
    assert decoded.basis == env.basis


@pytest.mark.parametrize("params", [{"u": "-0.4", "ph": "8"}, {"U": "-0.4", "pH": "8"}])
def test_legacy_url_aliases_override_point_and_preserve_default_reservoir(
    params: dict[str, str],
) -> None:
    base = Environment.from_dict(_BLOCK)
    env = Environment.from_url(params, default=base)
    assert env.electrochemical == {"U_vs_RHE": -0.4, "pH": 8.0, "ion_conc_M": 0.1}
    assert env.to_dict()["gases"] == base.to_dict()["gases"]
    assert env.T == 350.0
    assert base.electrochemical == _BLOCK["electrochemical"]


def test_legacy_url_without_a_default_keeps_unknown_coordinates_unknown() -> None:
    env = Environment.from_url({"u": "-0.3"})
    assert env.electrochemical == {"U_vs_RHE": -0.3}
    assert env.to_dict()["gases"]["unlisted"] == "reference"


@pytest.mark.parametrize(
    "config",
    [
        {"environment": _BLOCK},
        {
            "environment": {
                "T": 350.0,
                "gases": {"inputs": {"NO": 1.0}, "outputs": "far"},
            }
        },
        {
            "conditions": {"temperature": 350.0, "pressures": {"NO": 0.5}},
            "kinetics": {"sticking": {"NO": 0.5}},
        },
        {},
    ],
)
def test_basis_matches_existing_quest_projection(config: dict[str, Any]) -> None:
    assert Environment.from_config(config).basis == environment_basis(config)


@pytest.mark.parametrize(("potential", "ph"), [(-1, 7), (-1.0, 7.0)])
def test_electrochemical_basis_preserves_persisted_numeric_types(
    potential: int | float, ph: int | float
) -> None:
    block: dict[str, Any] = {
        "T": 350,
        "electrochemical": {"provider": "che", "U_vs_RHE": potential, "pH": ph},
    }
    assert Environment.from_dict(block).basis == environment_basis(
        {"environment": block}
    )


@pytest.mark.parametrize(
    "block",
    [
        {"T": 0},
        {"T": -1},
        {"T": True},
        {"T": float("nan")},
        {"T": 350, "p_total": 0},
        {"T": 350, "p_total": 10},
        {"T": 350, "gases": {"inputs": {"NO": -1}}},
        {"T": 350, "gases": {"outputs": {"NH3": 0}}},
        {"T": 350, "sticking": {"NO": {"s0": 1.1}}},
        {"T": 350, "electrochemical": {"pH": -0.1}},
        {"T": 350, "electrochemical": {"pH": 14.1}},
        {"T": 350, "electrochemical": {"ion_conc_M": 0}},
        {"T": 350, "electrochemical": {"ion_conc_M": -1}},
        {"T": 350, "electrochemical": {"U_vs_RHE": float("inf")}},
        {"T": 350, "electrochemical": {"pH": True}},
        {"T": 350, "electrochemical": {"provider": "unknown", "U_vs_RHE": -0.3}},
        {"T": 350, "electrochemical": {"provider": "che"}},
        {"T": 350, "electrochemical": {"provider": "che", "U_vs_RHE": None}},
    ],
)
def test_bad_environment_values_refuse_with_next_step(block: dict[str, Any]) -> None:
    with pytest.raises(BadInput) as exc:
        Environment.from_dict(block)
    assert exc.value.next


def test_numeric_strings_preserve_environment_basis() -> None:
    numeric = Environment.from_dict(_BLOCK)
    strings = Environment.from_dict(
        {
            "T": "350.0",
            "p_total": "10.0",
            "gases": {
                "inputs": {"NO": "1.0", "H2": "3.0"},
                "inerts": {"Ar": "1.0"},
            },
            "sticking": {"NO": {"s0": "0.5", "source": "pa123"}},
            "electrochemical": {"U_vs_RHE": "-0.3", "pH": "7.0", "ion_conc_M": "0.1"},
        }
    )
    assert strings.matches(numeric)
    assert strings.basis == numeric.basis


@pytest.mark.parametrize("value", ["hot", "NaN", "inf", "-Infinity", float("inf")])
@pytest.mark.parametrize("axis", ["T", "p_total", "pressure", "sticking", "pH"])
def test_non_numeric_and_nonfinite_values_still_refuse(
    axis: str, value: str | float
) -> None:
    block = copy.deepcopy(_BLOCK)
    if axis == "pressure":
        block["gases"]["inputs"]["NO"] = value
    elif axis == "sticking":
        block["sticking"]["NO"]["s0"] = value
    elif axis == "pH":
        block["electrochemical"]["pH"] = value
    else:
        block[axis] = value
    with pytest.raises(BadInput, match="finite number") as exc:
        Environment.from_dict(block)
    assert exc.value.next


@pytest.mark.parametrize(
    "params",
    [
        {"env": "not JSON"},
        {"env": "[]"},
        {"u": "nan"},
        {"ph": "15"},
        {"ph": "seven"},
    ],
)
def test_bad_url_values_refuse_with_next_step(params: dict[str, str]) -> None:
    with pytest.raises(BadInput) as exc:
        Environment.from_url(params)
    assert exc.value.next


def _meta() -> dict[str, Any]:
    return {
        "config": {"environment": copy.deepcopy(_BLOCK)},
        "results": {
            "kinetics_v2": {
                "tof_mf": 2.5,
                "environment": {
                    "T": 350.0,
                    "gases": {"inputs": {"NO": 2.0, "H2": 6.0}, "inerts": {"Ar": 2.0}},
                    "sticking": copy.deepcopy(_BLOCK["sticking"]),
                    "electrochemical": copy.deepcopy(_BLOCK["electrochemical"]),
                },
            }
        },
    }


def _sweep_meta() -> dict[str, Any]:
    meta = _meta()
    meta["results"]["kinetics_sweep"] = {
        "environment": copy.deepcopy(meta["results"]["kinetics_v2"]["environment"]),
        "points": [
            {"T": t, "p_total": p, "tof_mf": t / p}
            for t in (300.0, 400.0)
            for p in (5.0, 15.0)
        ],
    }
    return meta


def test_lookup_returns_exact_stored_record() -> None:
    meta = _meta()
    found = lookup_environment(meta, Environment.from_dict(_BLOCK))
    assert found["status"] == "exact"
    assert found["source"] == "kinetics_v2"
    assert found["record"] == meta["results"]["kinetics_v2"]


def test_che_provider_survives_projection_url_and_stored_lookup() -> None:
    block: dict[str, Any] = {
        "T": 350,
        "gases": {"inputs": {"NO": 1}, "outputs": "far"},
        "electrochemical": {"provider": "che", "U_vs_RHE": -0.3},
    }
    env = Environment.from_dict(block)
    assert Environment.from_url(dict(parse_qsl(env.to_url()))).to_dict() == block
    record = {"environment": block, "tof_mf": 2.5}
    found = lookup_environment({"results": {"kinetics_v2": record}}, env)
    assert found["status"] == "exact"
    assert found["record"] == record


def _real_sweep_meta() -> dict[str, Any]:
    sweep = json.loads(
        (Path(__file__).parent / "fixtures/kinetics_sweep.json").read_text(
            encoding="utf-8"
        )
    )
    return {"results": {"kinetics_sweep": sweep}}


def _real_sweep_environment(**changes: Any) -> Environment:
    return Environment.from_dict(
        {
            "T": 350.0,
            "p_total": 10.0,
            "gases": {"inputs": {"NO": 0.1, "H2": 1.0}},
            **changes,
        }
    )


def test_real_sweep_brackets_temperature_and_pressure_without_environment() -> None:
    meta = _real_sweep_meta()
    found = lookup_environment(meta, _real_sweep_environment())
    assert found["status"] == "bracketed"
    assert found["source"] == "kinetics_sweep"
    assert found["bounds"]["T"] == [298.0, 450.0]
    assert found["bounds"]["p_total"] == pytest.approx([0.1, 30.0])
    assert found["corners"] == meta["results"]["kinetics_sweep"]["points"]
    assert "tof_mf" not in found


def test_real_sweep_pressure_roundoff_keeps_bracket_on_one_pressure_slice() -> None:
    meta = _real_sweep_meta()
    found = lookup_environment(meta, _real_sweep_environment(p_total=30.0))
    assert found["status"] == "bracketed"
    assert found["bounds"]["T"] == [298.0, 450.0]
    assert found["bounds"]["p_total"] == pytest.approx([30.0, 30.0])
    assert found["corners"] == [
        meta["results"]["kinetics_sweep"]["points"][1],
        meta["results"]["kinetics_sweep"]["points"][3],
    ]


@pytest.mark.parametrize("temperature", [298.0, 450.0])
@pytest.mark.parametrize("pressure", [0.1, 30.0])
def test_real_sweep_exact_corners_use_recorded_pressures(
    temperature: float, pressure: float
) -> None:
    meta = _real_sweep_meta()
    found = lookup_environment(
        meta, _real_sweep_environment(T=temperature, p_total=pressure)
    )
    assert found["status"] == "exact"
    assert found["source"] == "kinetics_sweep"
    assert found["record"] in meta["results"]["kinetics_sweep"]["points"]
    assert found["record"]["T"] == temperature
    assert found["record"]["p_total"] == pytest.approx(pressure)


@pytest.mark.parametrize(
    "electrochemical", [{"U_vs_RHE": -0.3}, {"pH": 7.0}, {"ion_conc_M": 0.1}]
)
@pytest.mark.parametrize("temperature", [298.0, 350.0])
def test_real_sweep_explains_missing_electrochemical_provenance(
    electrochemical: dict[str, float], temperature: float
) -> None:
    meta = _real_sweep_meta()
    # Neither graph config nor a different solve proves the sweep's potential.
    requested = _real_sweep_environment(
        T=temperature,
        p_total=0.1 if temperature == 298.0 else 10.0,
        electrochemical=electrochemical,
    )
    meta["config"] = {"environment": requested.to_dict()}
    meta["results"]["kinetics_v2"] = {
        "environment": {**requested.to_dict(), "T": 500.0},
        "tof_mf": 99.0,
    }
    found = lookup_environment(meta, requested)
    assert found["status"] == "none"
    assert "electrochemical" in found["reason"].lower()
    assert "record" not in found
    assert "corners" not in found


def test_real_sweep_cannot_answer_a_different_gas_composition() -> None:
    requested = _real_sweep_environment(gases={"inputs": {"NO": 1.0, "H2": 1.0}})
    assert lookup_environment(_real_sweep_meta(), requested)["status"] == "none"


@pytest.mark.parametrize("temperature", [298.0, 350.0])
@pytest.mark.parametrize(
    ("changes", "condition"),
    [
        ({"sticking": {"NO": {"s0": 0.00001, "source": "pa123"}}}, "sticking"),
        ({"gases": {"inputs": {"NO": 0.1, "H2": 1}, "outputs": "far"}}, "outputs"),
        ({"gases": {"inputs": {"NO": 0.1}, "outputs": {"H2": 1}}}, "outputs"),
        ({"gases": {"inputs": {"NO": 0.1}, "inerts": {"H2": 1}}}, "inerts"),
        (
            {"gases": {"inputs": {"NO": 0.1, "H2": 1}, "unlisted": "reference"}},
            "unlisted",
        ),
    ],
)
def test_real_sweep_refuses_unrecorded_conditions_without_discarding_them(
    changes: dict[str, Any], condition: str, temperature: float
) -> None:
    requested = _real_sweep_environment(
        T=temperature,
        p_total=0.1 if temperature == 298.0 else 10.0,
        **changes,
    )
    before = requested.to_dict()
    found = lookup_environment(_real_sweep_meta(), requested)
    assert found["status"] == "none"
    assert condition in found["reason"]
    assert "record" not in found
    assert "corners" not in found
    assert requested.to_dict() == before


def test_real_sweep_refuses_reported_far_and_sticking_request() -> None:
    requested = _real_sweep_environment(
        T=298,
        p_total=0.1,
        gases={"inputs": {"NO": 0.1, "H2": 1}, "outputs": "far"},
        sticking={"NO": {"s0": 0.00001, "source": "pa123"}},
    )
    found = lookup_environment(_real_sweep_meta(), requested)
    assert found["status"] == "none"
    assert "sticking" in found["reason"]


def test_nonlegacy_solve_does_not_inherit_config_electrochemistry() -> None:
    meta = _meta()
    record = meta["results"]["kinetics_v2"]["environment"]
    record["legacy"] = False
    record["electrochemical"] = None
    # The graph has a potential, but this explicit D1 solve did not state one.
    assert lookup_environment(meta, Environment.from_dict(_BLOCK))["status"] == "none"
    thermal = {key: value for key, value in _BLOCK.items() if key != "electrochemical"}
    assert lookup_environment(meta, Environment.from_dict(thermal))["status"] == "exact"


def test_lookup_legacy_pathway_projects_reference_environment() -> None:
    meta: dict[str, Any] = {
        "config": {
            "conditions": {"temperature": 350.0, "pressures": {"NO": 1.0}},
            "electrochemistry": {"U_vs_RHE": -0.3, "pH": 7.0},
        },
        "results": {"kinetics": {"tof": 2.0}},
    }
    env = Environment.from_config(meta["config"])
    found = lookup_environment(meta, env)
    assert found["status"] == "exact"
    assert found["source"] == "kinetics"
    assert env.electrochemical == {"U_vs_RHE": -0.3, "pH": 7.0}


@pytest.mark.parametrize("d1_config", [False, True])
def test_legacy_resolve_matches_its_recorded_conditions_not_graph_config(
    d1_config: bool,
) -> None:
    config: dict[str, Any] = {
        "conditions": {"temperature": 300.0, "pressures": {"NO": 1.0}},
        "electrochemistry": {"U_vs_RHE": -0.3, "pH": 7.0},
    }
    if d1_config:
        config["environment"] = {
            "T": 300.0,
            "gases": {"inputs": {"NO": 1.0}, "unlisted": "reference"},
            "electrochemical": {"U_vs_RHE": -0.3, "pH": 7.0},
        }
    record: dict[str, Any] = {
        "tof": 12.0,
        "conditions": {"temperature": 450.0, "pressures": {"NO": 4.0}},
    }
    meta: dict[str, Any] = {"config": config, "results": {"kinetics": record}}
    solved = Environment.from_config(
        {
            "conditions": record["conditions"],
            "electrochemistry": config["electrochemistry"],
        }
    )
    found = lookup_environment(meta, solved)
    assert found["status"] == "exact"
    assert found["record"]["tof"] == 12.0
    assert lookup_environment(meta, Environment.from_config(config))["status"] == "none"


def test_v2_without_persisted_basis_matches_despite_resolved_total_roundoff() -> None:
    requested = Environment.from_dict(
        {
            "T": 350.0,
            "p_total": 0.3,
            "gases": {"inputs": {"NO": 0.1, "H2": 0.2}},
            "electrochemical": {"U_vs_RHE": -0.3, "pH": 7.0},
        }
    )
    # The engine scales a composition to the stated total. Summing those
    # resolved partial pressures need not reconstruct that total bit-for-bit.
    scale = 0.3 / (0.1 + 0.2)
    resolved: dict[str, Any] = {
        "T": 350.0,
        "p_total": 0.3,
        "p_total_stated": True,
        "gases": {"inputs": {"NO": 0.1 * scale, "H2": 0.2 * scale}},
        "electrochemical": {"U_vs_RHE": -0.3, "pH": 7.0},
    }
    assert Environment.from_record(resolved).p_total != requested.p_total
    assert Environment.from_record(resolved).basis != requested.basis
    record: dict[str, Any] = {
        "tof_mf": 42.0,
        "environment": resolved,
    }
    meta: dict[str, Any] = {"results": {"kinetics_v2": record}}
    found = lookup_environment(meta, requested)
    assert found["status"] == "exact"
    assert found["record"]["tof_mf"] == 42.0

    # Roundoff tolerance must not hide different feed pressures or potential.
    resolved["gases"]["inputs"]["NO"] *= 1.01
    assert lookup_environment(meta, requested)["status"] == "none"
    resolved["gases"]["inputs"]["NO"] = 0.1 * scale
    resolved["electrochemical"]["U_vs_RHE"] = -0.4
    assert lookup_environment(meta, requested)["status"] == "none"


def test_lookup_finds_previous_v2_after_temperature_recompute() -> None:
    meta = _meta()
    previous = copy.deepcopy(meta["results"]["kinetics_v2"])
    meta["results"]["kinetics_v2_previous"] = previous
    meta["results"]["kinetics_v2"]["environment"]["T"] = 450.0
    meta["results"]["kinetics_v2"]["tof_mf"] = 99.0
    found = lookup_environment(meta, Environment.from_dict(_BLOCK))
    assert found["status"] == "exact"
    assert found["source"] == "kinetics_v2_previous"
    assert found["record"] == previous
    current = lookup_environment(meta, Environment.from_dict({**_BLOCK, "T": 450.0}))
    assert current["source"] == "kinetics_v2"
    assert current["record"]["tof_mf"] == 99.0


def test_lookup_prefers_exact_previous_sweep_over_current_bracket() -> None:
    point: dict[str, Any] = {
        "T": 350,
        "p_total": 1,
        "pressures": {"NO": 1},
        "tof_mf": 2,
    }
    meta: dict[str, Any] = {
        "results": {
            "kinetics_sweep": {
                "points": [
                    {**point, "T": temperature, "tof_mf": 9}
                    for temperature in (300, 400)
                ]
            },
            "kinetics_sweep_previous": {"points": [point]},
        }
    }
    env = Environment.from_dict({"T": 350, "gases": {"inputs": {"NO": 1}}})
    found = lookup_environment(meta, env)
    assert found["status"] == "exact"
    assert found["source"] == "kinetics_sweep_previous"
    assert found["record"] == point
    assert "corners" not in found


def test_lookup_finds_previous_sweep_when_no_current_sweep_exists() -> None:
    point: dict[str, Any] = {
        "T": 350,
        "p_total": 1,
        "pressures": {"NO": 1},
        "tof_mf": 2,
    }
    meta: dict[str, Any] = {"results": {"kinetics_sweep_previous": {"points": [point]}}}
    env = Environment.from_dict({"T": 350, "gases": {"inputs": {"NO": 1}}})
    found = lookup_environment(meta, env)
    assert found["status"] == "exact"
    assert found["source"] == "kinetics_sweep_previous"
    assert found["record"] == point


@pytest.mark.parametrize("current_temperature", [350, 450])
def test_sweep_lookup_prefers_current_and_falls_back_to_previous(
    current_temperature: int,
) -> None:
    point: dict[str, Any] = {
        "T": 350,
        "p_total": 1,
        "pressures": {"NO": 1},
        "tof_mf": 2,
    }
    meta: dict[str, Any] = {
        "results": {
            "kinetics_sweep": {
                "points": [{**point, "T": current_temperature, "tof_mf": 9}]
            },
            "kinetics_sweep_previous": {"points": [point]},
        }
    }
    env = Environment.from_dict({"T": 350, "gases": {"inputs": {"NO": 1}}})
    found = lookup_environment(meta, env)
    assert found["status"] == "exact"
    expected_source = (
        "kinetics_sweep" if current_temperature == 350 else "kinetics_sweep_previous"
    )
    assert found["source"] == expected_source
    assert found["record"]["tof_mf"] == (9 if current_temperature == 350 else 2)


@pytest.mark.parametrize(
    ("changes", "condition"),
    [
        ({"electrochemical": {"U_vs_RHE": -1}}, "electrochemical"),
        ({"sticking": {"NO": 0.5}}, "sticking"),
        ({"gases": {"inputs": {"NO": 1}, "outputs": "far"}}, "gases.outputs"),
    ],
)
def test_previous_sweep_refuses_unrecorded_conditions(
    changes: dict[str, Any],
    condition: str,
) -> None:
    meta: dict[str, Any] = {
        "results": {
            "kinetics_sweep_previous": {
                "points": [
                    {"T": 350, "p_total": 1, "pressures": {"NO": 1}, "tof_mf": 2}
                ]
            }
        }
    }
    env = Environment.from_dict({"T": 350, "gases": {"inputs": {"NO": 1}}, **changes})
    found = lookup_environment(meta, env)
    assert found["status"] == "none"
    assert condition in found["reason"]
    assert "record" not in found
    assert "corners" not in found


@pytest.mark.parametrize("provenance", ["sweep", "row", "absent"])
def test_retained_sweep_does_not_inherit_new_solve_potential(provenance: str) -> None:
    meta = _sweep_meta()
    sweep = meta["results"]["kinetics_sweep"]
    if provenance == "row":
        for row in sweep["points"]:
            total = row["p_total"]
            row["environment"] = {
                **copy.deepcopy(sweep["environment"]),
                "T": row["T"],
                "gases": {
                    "inputs": {"NO": total * 0.2, "H2": total * 0.6},
                    "inerts": {"Ar": total * 0.2},
                },
            }
    if provenance != "sweep":
        sweep.pop("environment")
    # _finalize retains the successful old sweep beside newly accepted
    # kinetics when the new sweep is degraded, marking the rejected attempt.
    meta["results"]["kinetics_v2_previous"] = copy.deepcopy(
        meta["results"]["kinetics_v2"]
    )
    meta["results"]["kinetics_v2"]["environment"]["electrochemical"]["U_vs_RHE"] = -0.5
    meta["config"]["environment"]["electrochemical"]["U_vs_RHE"] = -0.5
    meta["results"]["kinetics_sweep_rejected"] = {
        "kinetics_sweep": {"points": [], "optimum": None},
        "reason": "kinetics_sweep degraded: no optimum; stored record kept",
    }
    corner = {**_BLOCK, "T": 300.0, "p_total": 5.0}
    requested = Environment.from_dict(
        {
            **corner,
            "electrochemical": {**_BLOCK["electrochemical"], "U_vs_RHE": -0.5},
        }
    )
    assert lookup_environment(meta, requested)["status"] == "none"
    old = lookup_environment(meta, Environment.from_dict(corner))
    if provenance == "absent":
        assert old["status"] == "none"
    else:
        assert old["status"] == "exact"
        assert old["source"] == "kinetics_sweep"
        assert old["record"]["tof_mf"] == 60.0


@pytest.mark.parametrize(
    ("temperature", "pressure", "status"),
    [(300.0, 5.0, "exact"), (360.0, 10.0, "bracketed")],
)
def test_matching_sweep_rows_do_not_inherit_unrelated_provenance_refusal(
    temperature: float, pressure: float, status: str
) -> None:
    meta = _sweep_meta()
    meta["results"].pop("kinetics_v2")
    sweep = meta["results"]["kinetics_sweep"]
    base = sweep.pop("environment")
    for row in sweep["points"]:
        total = row["p_total"]
        row["environment"] = {
            **copy.deepcopy(base),
            "T": row["T"],
            "gases": {
                "inputs": {"NO": total * 0.2, "H2": total * 0.6},
                "inerts": {"Ar": total * 0.2},
            },
        }
    # An earlier row cannot establish electrochemistry, but subsequent
    # records independently establish the requested exact point or cell.
    sweep["points"].insert(0, {"T": 300.0, "p_total": 5.0, "tof_mf": 99.0})
    environment = Environment.from_dict(
        {**_BLOCK, "T": temperature, "p_total": pressure}
    )
    found = lookup_environment(meta, environment)
    assert found["status"] == status
    assert found["source"] == "kinetics_sweep"
    assert "reason" not in found


def test_lookup_no_stored_result_never_manufactures_a_solve_from_config() -> None:
    env = Environment.from_dict(_BLOCK)
    assert lookup_environment({"config": {"environment": _BLOCK}}, env) == {
        "status": "none",
        "environment_basis": env.basis,
    }


@pytest.mark.parametrize("source", ["kinetics_sweep", "kinetics_sweep_previous"])
def test_lookup_brackets_complete_adjacent_sweep_without_interpolating(
    source: str,
) -> None:
    meta = _sweep_meta()
    meta["results"][source] = meta["results"].pop("kinetics_sweep")
    env = Environment.from_dict({**_BLOCK, "T": 360.0})
    found = lookup_environment(meta, env)
    assert found["status"] == "bracketed"
    assert found["source"] == source
    assert found["bounds"] == {"T": [300.0, 400.0], "p_total": [5.0, 15.0]}
    assert len(found["corners"]) == 4
    assert "tof_mf" not in found


def test_lookup_exact_sweep_corner_uses_its_own_record() -> None:
    meta = _sweep_meta()
    env = Environment.from_dict({**_BLOCK, "T": 300.0, "p_total": 5.0})
    found = lookup_environment(meta, env)
    assert found["status"] == "exact"
    assert found["source"] == "kinetics_sweep"
    assert found["record"]["tof_mf"] == 60.0


@pytest.mark.parametrize("damage", ["missing", "failed", "unsolved"])
def test_incomplete_sweep_corners_cannot_bracket(damage: str) -> None:
    meta = _sweep_meta()
    points = meta["results"]["kinetics_sweep"]["points"]
    if damage == "missing":
        points.pop()
    elif damage == "failed":
        points[-1]["error"] = "solve failed"
    else:
        points[-1]["tof_mf"] = None
    env = Environment.from_dict({**_BLOCK, "T": 360.0})
    assert lookup_environment(meta, env)["status"] == "none"


def test_failed_interior_sweep_slice_cannot_be_bridged_by_outer_corners() -> None:
    meta = _sweep_meta()
    meta["results"]["kinetics_sweep"]["points"].extend(
        [{"T": 350.0, "p_total": p, "error": "solve failed"} for p in (5.0, 15.0)]
    )
    env = Environment.from_dict({**_BLOCK, "T": 360.0})
    assert lookup_environment(meta, env)["status"] == "none"


@pytest.mark.parametrize(("temperature", "pressure"), [(350.0, 5.0), (360.0, 10.0)])
def test_different_sweep_feed_cannot_supply_exact_result_or_bridge_slice(
    temperature: float,
    pressure: float,
) -> None:
    meta = _sweep_meta()
    # These solves have the same total pressures but a different NO:H2 feed.
    # They must neither supply an exact result nor disappear from the grid,
    # allowing the outer 300/400 K corners to bracket across them.
    meta["results"]["kinetics_sweep"]["points"].extend(
        [
            {
                "T": 350.0,
                "p_total": total,
                "pressures": {"NO": total * 0.4, "H2": total * 0.4, "Ar": total * 0.2},
                "tof_mf": 99.0,
            }
            for total in (5.0, 15.0)
        ]
    )
    env = Environment.from_dict({**_BLOCK, "T": temperature, "p_total": pressure})
    assert lookup_environment(meta, env)["status"] == "none"


@pytest.mark.parametrize(
    "changes",
    [
        {"T": 450.0},
        {"p_total": 20.0},
        {"electrochemical": {**_BLOCK["electrochemical"], "U_vs_RHE": -0.4}},
        {"electrochemical": {**_BLOCK["electrochemical"], "pH": 8.0}},
        {"electrochemical": {**_BLOCK["electrochemical"], "ion_conc_M": 0.2}},
        {"gases": {"inputs": {"NO": 2.0, "H2": 2.0}, "inerts": {"Ar": 1.0}}},
    ],
)
def test_outside_sweep_or_changed_unswept_coordinate_cannot_match(
    changes: dict[str, Any],
) -> None:
    env = Environment.from_dict({**_BLOCK, "T": 360.0, **changes})
    assert lookup_environment(_sweep_meta(), env)["status"] == "none"


def _handler(meta: dict[str, Any]) -> PathwayHandler:
    ref = SimpleNamespace(id=11, slug="pw-a", title="NO to NH3", meta=meta)
    handler = PathwayHandler.__new__(PathwayHandler)
    store = SimpleNamespace(get_ref=lambda *, kind, id: ref)
    setattr(handler, "hub", SimpleNamespace(live_store=store))
    return handler


@pytest.mark.parametrize("view", ["analysis", "staircase"])
def test_handler_environment_args_forward_che_point_without_compute(
    view: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = _handler(_meta())
    render = Mock(return_value=Response(body="stored thermodynamics"))
    monkeypatch.setattr(handler, "_get_view", render)
    submit = Mock(side_effect=AssertionError("a read must not submit work"))
    monkeypatch.setattr("precis.handlers.job.JobHandler.put", submit)
    response = handler.get(id="pw-a", view=view, args={"environment": _BLOCK})
    assert response.body is not None
    assert "stored result: exact" in response.body
    assert Environment.from_dict(_BLOCK).basis in response.body
    assert render.call_args.kwargs["U"] == -0.3
    assert render.call_args.kwargs["pH"] == (7.0 if view == "staircase" else None)
    submit.assert_not_called()


def test_handler_missing_kinetics_says_none_without_showing_baseline_or_compute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submit = Mock(side_effect=AssertionError("a read must not submit work"))
    monkeypatch.setattr("precis.handlers.job.JobHandler.put", submit)
    response = _handler(_meta()).get(
        id="pw-a",
        view="kinetics",
        args={"environment": {**_BLOCK, "T": 500.0}},
    )
    assert response.body is not None
    assert "stored result: none" in response.body
    assert "No stored kinetics at this point" in response.body
    assert "2.5" not in response.body
    submit.assert_not_called()


def test_handler_missing_sweep_electrochemistry_explains_none_without_compute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meta = _real_sweep_meta()
    environment = _real_sweep_environment(electrochemical={"U_vs_RHE": -0.3})
    lookup = lookup_environment(meta, environment)
    submit = Mock(side_effect=AssertionError("a read must not submit work"))
    monkeypatch.setattr("precis.handlers.job.JobHandler.put", submit)
    response = _handler(meta).get(
        id="pw-a", view="kinetics", args={"environment": environment.to_dict()}
    )
    assert response.body is not None
    assert "stored result: none" in response.body
    assert "No stored kinetics at this point" in response.body
    assert lookup["reason"] in response.body
    assert "electrochemical" in response.body
    submit.assert_not_called()


def test_handler_exact_kinetics_renders_matching_record_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meta = _meta()
    matching = copy.deepcopy(meta["results"]["kinetics_v2"])
    meta["results"]["kinetics"] = matching
    meta["results"]["kinetics_v2"]["environment"]["T"] = 500.0
    render = Mock(return_value="matched kinetics")
    monkeypatch.setattr("precis_pathway.toon_views.kinetics_text", render)
    response = _handler(meta).get(
        id="pw-a", view="kinetics", args={"environment": _BLOCK}
    )
    assert response.body is not None
    assert "stored result: exact" in response.body
    rendered_meta = render.call_args.args[0]
    assert rendered_meta["results"]["kinetics"] == matching
    assert "kinetics_v2" not in rendered_meta["results"]
    assert meta["results"]["kinetics_v2"]["environment"]["T"] == 500.0


def test_handler_renders_previous_v2_when_current_solve_is_at_another_temperature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meta = _meta()
    previous = copy.deepcopy(meta["results"]["kinetics_v2"])
    meta["results"]["kinetics_v2_previous"] = previous
    meta["results"]["kinetics_v2"]["environment"]["T"] = 450.0
    meta["results"]["kinetics_v2"]["tof_mf"] = 99.0
    meta["results"]["kinetics"] = copy.deepcopy(meta["results"]["kinetics_v2"])
    render = Mock(return_value="previous kinetics")
    monkeypatch.setattr("precis_pathway.toon_views.kinetics_text", render)
    response = _handler(meta).get(
        id="pw-a", view="kinetics", args={"environment": _BLOCK}
    )
    assert response.body is not None
    assert "stored result: exact" in response.body
    rendered_meta = render.call_args.args[0]
    assert rendered_meta["results"]["kinetics_v2"] == previous
    assert "kinetics" not in rendered_meta["results"]
    assert meta["results"]["kinetics_v2"]["environment"]["T"] == 450.0


def test_handler_renders_previous_sweep_point_without_current_kinetics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    point: dict[str, Any] = {
        "T": 350,
        "p_total": 1,
        "pressures": {"NO": 1},
        "tof_mf": 2,
    }
    meta: dict[str, Any] = {
        "results": {
            "kinetics_sweep": {"points": [{**point, "T": 450, "tof_mf": 99}]},
            "kinetics_sweep_previous": {"points": [point]},
        }
    }
    render = Mock(side_effect=AssertionError("sweep points have only scalar kinetics"))
    monkeypatch.setattr("precis_pathway.toon_views.kinetics_text", render)
    submit = Mock(side_effect=AssertionError("a read must not submit work"))
    monkeypatch.setattr("precis.handlers.job.JobHandler.put", submit)
    response = _handler(meta).get(
        id="pw-a",
        view="kinetics",
        args={"environment": {"T": 350, "gases": {"inputs": {"NO": 1}}}},
    )
    assert response.body is not None
    assert "stored result: exact" in response.body
    assert json.dumps(point, sort_keys=True) in response.body
    assert '"tof_mf": 99' not in response.body
    render.assert_not_called()
    submit.assert_not_called()


def test_handler_bracketed_kinetics_shows_stored_corners_without_compute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submit = Mock(side_effect=AssertionError("a read must not submit work"))
    monkeypatch.setattr("precis.handlers.job.JobHandler.put", submit)
    response = _handler(_sweep_meta()).get(
        id="pw-a",
        view="kinetics",
        args={"environment": {**_BLOCK, "T": 360.0}},
    )
    assert response.body is not None
    assert "stored result: bracketed" in response.body
    assert "not interpolated" in response.body
    assert '"T": 300.0' in response.body
    assert '"T": 400.0' in response.body
    submit.assert_not_called()


@pytest.mark.parametrize(
    "args",
    [
        {"environment": _BLOCK, "U": -0.3},
        {"environment": _BLOCK, "pH": 7.0},
        {"environment": {"T": 350.0, "electrochemical": {"pH": 15}}},
    ],
)
def test_handler_refuses_conflicting_or_invalid_environment_with_next(
    args: dict[str, Any],
) -> None:
    with pytest.raises(BadInput) as exc:
        _handler(_meta()).get(id="pw-a", view="kinetics", args=args)
    assert exc.value.next


@pytest.mark.parametrize(
    "ec", [{"pH": 15.0}, {"ion_conc_M": -1.0}, {"U_vs_RHE": float("nan")}]
)
def test_recompute_refuses_bad_electrochemistry_before_engine_or_job(
    ec: dict[str, float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meta = _meta()
    meta["results"].update({"nodes": {"A": 0}, "edges": [{"a": "A"}]})
    submit = Mock(side_effect=AssertionError("invalid point must not submit work"))
    monkeypatch.setattr("precis.handlers.job.JobHandler.put", submit)
    with pytest.raises(BadInput) as exc:
        _handler(meta).put(
            id="pw-a",
            mode="recompute_kinetics",
            args={"environment": {**_BLOCK, "electrochemical": ec}},
        )
    assert exc.value.next
    submit.assert_not_called()
