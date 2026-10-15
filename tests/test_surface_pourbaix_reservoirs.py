"""gr489164 defect 2: closed-shell adsorbates are U-flat, and a spectator
reservoir (an NO feed first) sets an element's chemical potential.

Pure arithmetic over a ``coverage.json``-shaped payload, no engine. The
``mu`` table mirrors the scan's own reservoir pricing (``coverage.py``:
open-shell fragments element by element, closed-shell ones against their
own gas): mu_H -3.4, mu_O -7.0, mu_N(½N2) -8.3, mu_NO(gas) -12.0 eV.
"""

from __future__ import annotations

import math

import pytest

from precis_pathway import surface_pourbaix as sp

AREA = 10.0


def _payload() -> dict:
    ads = {}
    for frag, g in (("NH2", 0.05), ("NO", -0.2), ("H", 0.0)):
        ads[frag] = {
            "site": "fcc",
            "points": [
                {
                    "n": 1,
                    "theta": 0.25,
                    "energy": -100.0 + g * AREA,
                    "gamma": g,
                    "converged": True,
                    "detached": False,
                }
            ],
        }
    return {
        "model": "mace:large",
        "conditions": {"temperature": 298.15, "pressures": {}},
        "mu": {
            "H": -3.4,
            "O": -7.0,
            "OH": -10.4,
            "NH2": -8.3 + 2 * -3.4,
            "NO": -12.0,
        },
        "facets": [{"facet": "111", "area": AREA, "adsorbates": ads}],
        "warnings": [],
    }


@pytest.mark.parametrize("frag", ["NO", "NH3", "H2O", "N2O", "NH2OH", "NO@fcc"])
def test_closed_shell_adsorbates_are_u_flat(frag: str) -> None:
    """The scan prices a closed-shell adsorbate against its own gas, so it
    takes or gives no proton-electron pairs. NO* used to tilt as nu = +2
    (elemental N + O), which put it on the map only at anodic U."""
    assert sp.electrons_released(frag) == 0
    assert sp.electrons_released(frag, [sp.Reservoir("NO", "N")]) == 0


def test_reservoir_axis_is_generic_and_validated() -> None:
    no = sp.Reservoir("NO", "N")
    assert no.nu_element == -2  # mu_N = mu_NO - mu_O(U); mu_O slope +2
    assert sp.electrons_released("NH2", [no]) == -4
    assert sp.electrons_released("N", [no]) == -2
    assert sp.electrons_released("NH2") == -2
    assert sp.feed_reservoir("NO") == no
    assert sp.feed_reservoir("NO3") is None  # not a closed-shell gas here
    assert sp.feed_reservoir("H2O") is None  # nothing outside H/O to set
    for bad in (("NH2", "N"), ("NO", "O"), ("NO", "C")):
        with pytest.raises(ValueError):
            sp.Reservoir(*bad)
    with pytest.raises(ValueError):
        sp.Reservoir("NO", "N", 0.0)
    with pytest.raises(ValueError):
        sp.reservoirs_from([{"species": "NO", "element": "N"}] * 2)
    assert sp.reservoirs_from([{"species": "NO", "element": "N", "activity": 0.1}]) == [
        sp.Reservoir("NO", "N", 0.1)
    ]


def test_no_feed_reprices_n_fragments_from_the_scans_mu_table() -> None:
    """mu_N(NO feed) = mu_NO - mu_O = -5.0 eV against the scan's ½N2 -8.3:
    +3.3 eV per N, so NH2* at n = 1 drops by 3.3 / area and tilts with
    nu = -4; NO* (its own gas) and H* do not move."""
    p = _payload()
    shifts, notes = sp.reservoir_shifts(p, [sp.Reservoir("NO", "N")])
    assert notes == []
    assert shifts["N"] == pytest.approx(3.3)
    plain = {a.adsorbate: a for a in sp.anchors_from_coverage(p)}
    fed = {
        a.adsorbate: a
        for a in sp.anchors_from_coverage(p, reservoirs=[sp.Reservoir("NO", "N")])
    }
    assert fed["NH2"].gamma0 == pytest.approx(plain["NH2"].gamma0 - 0.33)
    assert fed["NH2"].nu == -4 and plain["NH2"].nu == -2
    assert fed["NO"].gamma0 == plain["NO"].gamma0 and fed["NO"].nu == 0
    assert fed["H"].gamma0 == plain["H"].gamma0 and fed["H"].nu == -1


def test_reservoir_activity_shifts_by_kt_ln_a() -> None:
    p = _payload()
    s1, _ = sp.reservoir_shifts(p, [sp.Reservoir("NO", "N", 1.0)])
    s2, _ = sp.reservoir_shifts(p, [sp.Reservoir("NO", "N", 0.01)])
    assert s2["N"] - s1["N"] == pytest.approx(sp.K_B_EV * 298.15 * math.log(0.01))


def test_a_reservoir_the_scan_cannot_price_is_not_applied() -> None:
    p = _payload()
    del p["mu"]["NO"]
    shifts, notes = sp.reservoir_shifts(p, [sp.Reservoir("NO", "N")])
    assert shifts == {} and "no mu" in notes[0]
    fed = {
        a.adsorbate: a
        for a in sp.anchors_from_coverage(p, reservoirs=[sp.Reservoir("NO", "N")])
    }
    assert fed["NH2"].nu == -2  # never half-applied


def test_sweep_records_the_reservoirs() -> None:
    out = sp.sweep({"mace:large": _payload()}, reservoirs=[sp.Reservoir("NO", "N")])
    assert out["reservoirs"] == [
        {"species": "NO", "element": "N", "activity": 1.0, "nu_element": -2.0}
    ]
    assert out["reservoir_shift_eV"]["mace:large"]["N"] == pytest.approx(3.3)
    assert out["reservoir_notes"] == []
