"""gr489164 defect 1 (Reto 2026-10-15): the staircase mask in the one-rung
CHE convention, plus a thermal tolerance for chemical rungs.

The engine's ``che_affine`` splits every proton-coupled step into an H*
adsorption rung (``n_e = 1``) and a U-flat hydrogenation (``n_e = 0``).
With a zero tolerance, one uphill hydrogenation masked the whole grid, so
every affine pathway on prod reported "outside the viable window" on every
route. The fixture is the real ``che_affine`` of pw483780 (st479058) and
pw483766 (st261823), trimmed to route, steps and the engine's ``U_L``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from precis.quest import window_objectives as wo
from precis_pathway import staircase_masks as sm
from precis_pathway.staircase_routes import render_staircase, routes_from_results

_FIXTURE = Path(__file__).parent / "fixtures" / "pathway" / "spso_che_affine.json"


def _pathway(label: str) -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))[label]


def _rung(name: str, dg0: float, n_e: int, kind: str = "electrochemical") -> sm.Rung:
    return sm.Rung(name=name, delta_g0=dg0, n_e=n_e, kind=kind)


def test_volmer_and_its_hydrogenation_merge_into_one_pcet_rung() -> None:
    route = sm.Route(
        "r",
        (
            _rung("*->NO@N", -1.848, 0, "chemical"),
            _rung("NO@N->NO+H", -0.306, 1),
            _rung("NO+H->HNO", 0.544, 0, "chemical"),
            _rung("H2NO->NH2+O", -0.416, 0, "chemical"),
        ),
    )
    pcet = sm.pcet_route(route)
    assert [r.name for r in pcet.rungs] == ["*->NO@N", "NO@N->HNO", "H2NO->NH2+O"]
    merged = pcet.rung("NO@N->HNO")
    assert merged.delta_g0 == pytest.approx(0.238)
    assert merged.n_e == 1
    assert merged.kind == "electrochemical"
    assert sm.u_limiting(pcet) == pytest.approx(-0.238)


def test_a_chemical_step_that_keeps_the_h_is_not_merged() -> None:
    """``X+H -> Y+H`` leaves the H* on the surface: not its hydrogenation."""
    route = sm.Route(
        "r", (_rung("X->X+H", -0.3, 1), _rung("X+H->Y+H", 0.2, 0, "chemical"))
    )
    assert sm.pcet_route(route) is route


def test_chemical_rungs_default_to_a_thermal_tolerance() -> None:
    assert sm.DEFAULT_EPS_EV == 0.3
    chem = _rung("NO@N->N+O", 0.113, 0, "chemical")
    assert chem.eps == 0.3 and bool(chem.viable(0.0))
    assert not bool(_rung("a->b", 0.31, 0, "chemical").viable(-1.0))
    electro = _rung("a->a+H", 0.05, 1)
    assert electro.eps == 0.0 and not bool(electro.viable(0.0))
    assert _rung("a->b", 0.5, 0, "chemical").eps == 0.3
    assert sm.Rung("a->b", 0.5, 0, kind="chemical", eps_eV=0.6).eps == 0.6


def test_pw483780_u_l_in_the_one_rung_convention() -> None:
    """The review's number: on st479058, NO* + H+ + e- -> HNO* is
    -0.306 + 0.544 = +0.238 eV, so U_L = -0.24 V — not the engine's
    split-convention +0.28 V, which is just -ΔG_H*."""
    sr = routes_from_results(_pathway("pw483780_st479058"))
    via_hno = [
        name
        for name in sr.route_stats
        if name.startswith("* -> NO@N -> NO+H -> HNO -> HNO+H -> HNOH")
    ]
    assert via_hno
    stats = sr.route_stats[via_hno[0]]
    assert stats["U_L"] == pytest.approx(-0.238, abs=0.005)
    assert stats["U_L_engine"] == pytest.approx(0.2798, abs=0.001)


@pytest.mark.parametrize("label", ["pw483780_st479058", "pw483766_st261823"])
def test_affine_pathways_no_longer_report_every_route_outside(label: str) -> None:
    sr = routes_from_results(_pathway(label))
    assert len(sr.pcet_routes) == len(sr.routes) > 0
    text = render_staircase(sr, _pathway(label), u_rhe=-0.5)
    blocks = text.split("\nroute ")[1:]
    assert len(blocks) == len(sr.routes)
    outside = [b for b in blocks if "outside the viable window" in b]
    assert len(outside) < len(blocks)


@pytest.mark.parametrize("label", ["pw483780_st479058", "pw483766_st261823"])
def test_affine_pathways_get_a_nonzero_operating_window(label: str) -> None:
    sr = routes_from_results(_pathway(label))
    u = np.linspace(-1.0, 0.5, 61)
    resting = sm.RestingMap.full(u, np.array([0.0, 14.0]))
    out = wo.window_measures(
        resting, sr.routes, None, eps_eV=None, pcet_routes=sr.pcet_routes
    )
    assert out["window_area_VpH"] > 0.0
    assert out["window_fraction"] > 0.0


def test_window_measures_merges_by_itself_when_not_given_pcet_routes() -> None:
    route = sm.Route(
        "r", (_rung("NO@N->NO+H", -0.306, 1), _rung("NO+H->HNO", 0.544, 0, "chemical"))
    )
    u = np.linspace(-1.0, 0.5, 61)
    resting = sm.RestingMap.full(u, np.array([0.0, 14.0]))
    out = wo.window_measures(resting, [route], None)
    # viable below U_L = -0.238 V: about 0.76 V of the 1.5 V axis
    assert out["window_fraction"] == pytest.approx(0.762 / 1.5, abs=0.03)
    assert out["bottleneck_rung"] == "NO@N->HNO"
