"""``staircase_states.point_states`` — the one rung-state verdict the
picker, the ``/states`` JSON and ``view='staircase'`` share (gr489471,
``docs/backlog/spso-point-picker.md``)."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path
from typing import Any

from precis_pathway.staircase_routes import routes_from_results
from precis_pathway.staircase_states import STATES, point_states

_FIXTURE = Path(__file__).parent / "fixtures" / "pathway" / "spso_che_affine.json"

#: H* rests above +0.10 V, clean below +0.05 V; (0.05, 0.10) is a gap.
_MAP: dict[str, Any] = {
    "regions": [
        {
            "termination": None,
            "label": "clean",
            "U_RHE_range": [-0.6, 0.05],
        },
        {
            "termination": {"adsorbate": "H", "n": 9, "theta": 1.0},
            "label": "H* at 1.00 ML",
            "U_RHE_range": [0.1, 0.5],
        },
    ]
}


def _results(chem_dg0: float = -0.5, her_dg0: float | None = 0.35) -> dict[str, Any]:
    """NO -> NO+H (Volmer, +0.2) -> HNO (chemical, +0.1) -> N+OH: the
    first two merge into one PCET rung NO->HNO, ΔG0 +0.3, opening below
    −0.30 V."""
    res: dict[str, Any] = {
        "che_affine": {
            "scale": "RHE",
            "per_route": [
                {
                    "route": "NO -> NO+H -> HNO -> N+OH",
                    "steps": [
                        {"edge": "NO->NO+H", "delta_g0": 0.2, "n_e": 1},
                        {"edge": "NO+H->HNO", "delta_g0": 0.1, "n_e": 0},
                        {"edge": "HNO->N+OH", "delta_g0": chem_dg0, "n_e": 0},
                    ],
                }
            ],
        }
    }
    if her_dg0 is not None:
        res["her"] = {"edges": [{"edge": "*->H", "delta_g0": her_dg0, "n_e": 1}]}
    return res


def _states(u: float, coads: dict[str, int] | None = None, **kw: Any) -> Any:
    sr = routes_from_results(_results(**kw))
    return point_states(sr, _MAP, u_rhe=u, ph=7.0, coads=coads or {})


def _by_name(ps: Any) -> dict[str, Any]:
    return {r.name: r for r in ps.routes[0].rungs}


def test_viable_point_inside_the_window() -> None:
    ps = _states(-0.35)
    assert {r.state for r in ps.routes[0].rungs} == {"viable"}
    assert ps.in_window and ps.window == "inside"
    assert ps.termination == "clean" and ps.off_map_reason is None


def test_electro_uphill_names_where_it_opens() -> None:
    ps = _states(0.0)
    pcet = _by_name(ps)["NO->HNO"]
    assert pcet.state == "electro_uphill"
    assert abs(pcet.opens_at_U - (-0.30)) < 1e-9
    assert "opens below -0.30 V" in pcet.reason
    assert not ps.in_window and ps.window == "outside"
    assert ps.worst is not None and ps.worst["rung"] == "NO->HNO"


def test_thermally_blocked_chemical_rung() -> None:
    ps = _states(-0.35, chem_dg0=0.45)
    rung = _by_name(ps)["HNO->N+OH"]
    assert rung.state == "thermally_blocked"
    assert rung.reason == "0.45 eV > 0.30 eV"
    assert ps.worst is not None and ps.worst["state"] == "thermally_blocked"


def test_off_map_wins_over_uphill_on_every_rung() -> None:
    # At +0.30 V the PCET rung is also uphill; off_map takes precedence.
    ps = _states(0.30)
    assert {r.state for r in ps.routes[0].rungs} == {"off_map"}
    assert ps.off_map_reason == "map: H* at 1.00 ML here; pathway computed on clean"
    assert ps.worst is not None and "H* at 1.00 ML" in ps.worst["line"]
    assert not ps.in_window


def test_pathway_computed_on_the_resting_surface_is_not_off_map() -> None:
    ps = _states(0.30, coads={"H": 9})
    assert ps.off_map_reason is None
    assert _by_name(ps)["NO->HNO"].state == "electro_uphill"


def test_a_gap_in_the_map_is_unmapped_not_off_map() -> None:
    ps = _states(0.07)
    assert not ps.mapped and ps.termination is None
    assert ps.off_map_reason is None
    assert "no region" in ps.notes[0]


def test_her_winning_is_a_window_label_not_a_rung_state() -> None:
    ps = _states(-0.35, her_dg0=0.2)
    assert {r.state for r in ps.routes[0].rungs} == {"viable"}
    assert ps.window == "outside (HER)" and not ps.in_window
    assert ps.her_margin is not None and ps.her_margin < 0


def test_split_rungs_inherit_their_pcet_parent() -> None:
    ps = _states(0.0)
    parent = _by_name(ps)["NO->HNO"]
    assert parent.split_children == ("NO->NO+H", "NO+H->HNO")
    for split in parent.split_children:
        inherited = ps.state_of(0, split)
        assert inherited is not None and inherited.state == "electro_uphill"
    assert ps.state_of(0, "HNO->N+OH").state == "viable"


def test_worst_ties_break_on_largest_overshoot() -> None:
    # Two uphill electro rungs at +0.40 V on the H* surface: the one
    # further above its tolerance is the worst.
    ps = _states(0.40, coads={"H": 9}, chem_dg0=-0.5)
    assert ps.worst is not None and ps.worst["state"] == "electro_uphill"
    assert ps.worst["rung"] == "NO->HNO"
    assert STATES[0] == "off_map" and STATES[-1] == "viable"


def test_without_a_map_the_surface_is_not_checked() -> None:
    sr = routes_from_results(_results())
    ps = point_states(sr, None, u_rhe=0.30, ph=1.0, coads={})
    assert ps.termination is None and ps.off_map_reason is None
    assert ps.notes == ("no surface-Pourbaix map — starting surface not checked",)


def test_ph_moves_no_state() -> None:
    sr = routes_from_results(_results())
    a = point_states(sr, _MAP, u_rhe=-0.1, ph=0.0, coads={}).as_dict()
    b = point_states(sr, _MAP, u_rhe=-0.1, ph=13.0, coads={}).as_dict()
    a.pop("ph")
    b.pop("ph")
    assert a == b


def test_as_dict_is_json_ready() -> None:
    blob = json.loads(json.dumps(_states(0.0).as_dict()))
    assert blob["routes"][0]["rungs"][0]["split_children"] == [
        "NO->NO+H",
        "NO+H->HNO",
    ]


def test_as_dict_nulls_non_finite_values() -> None:
    sr = routes_from_results(_results())
    ps = point_states(sr, _MAP, u_rhe=-0.35, ph=7.0, coads={})
    bad = replace(ps, her_margin=math.nan)
    blob = json.dumps(bad.as_dict(), allow_nan=False)
    assert json.loads(blob)["her_margin"] is None


def test_spso_fixture_reports_its_thermal_bottleneck() -> None:
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    sr = routes_from_results(data["pw483780_st479058"])
    ps = point_states(sr, None, u_rhe=-0.3, ph=7.0, coads={})
    assert ps.worst is not None
    assert ps.worst["state"] == "thermally_blocked"
    # route 1's own block is the U-flat NH3 desorption off O*
    assert _by_name(ps)["NH3+O->O"].state == "thermally_blocked"
