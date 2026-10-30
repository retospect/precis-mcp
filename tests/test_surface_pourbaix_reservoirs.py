"""gr489164 defect 2: closed-shell adsorbates are U-flat, and a spectator
reservoir (an NO feed first) sets an element's chemical potential.

Pure arithmetic over a ``coverage.json``-shaped payload, no engine. The
``mu`` table mirrors the scan's own reservoir pricing (``coverage.py``:
open-shell fragments element by element, closed-shell ones against their
own gas): mu_H -3.4, mu_O -7.0, mu_N(½N2) -8.3, mu_NO(gas) -12.0 eV.
"""

from __future__ import annotations

import copy
import json
import math
from dataclasses import replace

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


def test_mixture_gamma0_sums_the_per_species_reservoir_shifts() -> None:
    """NH2+H, counts 2:1, under the NO feed: only the open-shell NH2 moves
    (3.3 eV per N), so gamma0 = gamma - 2 * 3.3 / area; its nu sums the
    species (NH2 -4 under the feed, H -1)."""
    p = _payload()
    p["facets"][0]["mixtures"] = [
        {
            "species": ["NH2", "H"],
            "label": "NH2+H",
            "points": [
                {
                    "counts": {"NH2": 2, "H": 1},
                    "n": 3,
                    "theta": 0.75,
                    "energy": -100.0,
                    "gamma": 0.07,
                    "converged": True,
                    "detached": False,
                }
            ],
        }
    ]
    res = [sp.Reservoir("NO", "N")]
    key = ("NH2+H", (("NH2", 2), ("H", 1)))
    plain = {a.key: a for a in sp.anchors_from_coverage(p)}
    fed = {a.key: a for a in sp.anchors_from_coverage(p, reservoirs=res)}
    single_shift = plain[("NH2", 1)].gamma0 - fed[("NH2", 1)].gamma0
    assert single_shift == pytest.approx(0.33)
    assert plain[key].gamma0 == pytest.approx(0.07)
    assert fed[key].gamma0 == pytest.approx(0.07 - 2 * single_shift)
    assert plain[key].slope == pytest.approx(-(2 * -2 + 1 * -1) / AREA)
    assert fed[key].slope == pytest.approx(-(2 * -4 + 1 * -1) / AREA)
    assert fed[key].counts == (("NH2", 2), ("H", 1))


def test_pool_anchors_keeps_the_reservoirs() -> None:
    """Pooling must not drop the feed reservoir: N* keeps nu = -2 under NO."""
    p = _payload()
    p["facets"][0]["adsorbates"]["N"] = {
        "site": "fcc",
        "points": [
            {
                "n": 1,
                "theta": 0.25,
                "energy": -100.0,
                "gamma": 0.1,
                "converged": True,
                "detached": False,
            }
        ],
    }
    res = [sp.Reservoir("NO", "N")]
    anchors = sp.anchors_from_coverage(p, reservoirs=res)
    assert {a.adsorbate: a for a in anchors}["N"].nu == -2
    pooled = {a.adsorbate: a for a in sp.pool_anchors({"m": anchors})}
    assert pooled["N"].nu == -2
    assert pooled["N"].slope == pytest.approx(+2 / AREA)


# --- slice 8 Part A: Anchor.from_dict / perturb_anchors / resweep -----------

TOL = 1e-9  # stored sweep values are unrounded floats, so 1e-9 is the bar


def _point(g: float, n: int = 1) -> dict:
    return {
        "n": n,
        "theta": 0.25 * n,
        "energy": -100.0 + g * AREA,
        "gamma": g,
        "converged": True,
        "detached": False,
    }


def _two_reservoir_payload() -> dict:
    """NH2/H/CH3 anchors plus an NH2+H mixture. Of the two requested
    reservoirs NO->N is unpriceable (no gas mu for NO) and CO->C is priced
    (+2.0 eV per C), so exactly one is applied."""
    p = _payload()
    ads = p["facets"][0]["adsorbates"]
    ads["H"]["points"] = [_point(-0.12)]
    ads["NO"]["points"] = [_point(-0.14)]
    ads["CH3"] = {"site": "fcc", "points": [_point(0.32)]}
    p["mu"].update({"CH3": -9.0 + 3 * -3.4, "CO": -14.0})
    del p["mu"]["NO"]
    p["facets"][0]["mixtures"] = [
        {
            "species": ["NH2", "H"],
            "label": "NH2+H",
            "points": [{**_point(0.6, 3), "counts": {"NH2": 2, "H": 1}}],
        }
    ]
    return p


RESERVOIRS = [sp.Reservoir("NO", "N"), sp.Reservoir("CO", "C")]


def _stored_sweep() -> dict:
    out = sp.sweep(
        {"mace:large": _two_reservoir_payload()},
        reservoirs=RESERVOIRS,
        u_window=(-1.0, 0.5),
        grid=61,
    )
    return json.loads(json.dumps(out))  # as the job stores it


def test_applied_reservoirs_keeps_only_the_priced_ones() -> None:
    che = _stored_sweep()
    assert len(che["reservoirs"]) == 2
    assert list(che["reservoir_shift_eV"]["mace:large"]) == ["C"]
    assert sp.applied_reservoirs(che, "mace:large") == [sp.Reservoir("CO", "C")]
    assert sp.applied_reservoirs(che, "other-model") == []
    assert sp.applied_reservoirs({}, "m") == []


def test_from_dict_round_trips_with_an_unpriced_reservoir() -> None:
    che = _stored_sweep()
    applied = sp.applied_reservoirs(che, "mace:large")
    originals = {
        a.key: a
        for a in sp.pool_anchors(
            {
                "mace:large": sp.anchors_from_coverage(
                    _two_reservoir_payload(), reservoirs=RESERVOIRS
                )
            }
        )
    }
    assert len(che["anchors"]) == len(originals) == 5
    mixture_seen = False
    for d in che["anchors"]:
        got = sp.Anchor.from_dict(d, applied)
        want = originals[got.key]
        assert got.gamma0 == want.gamma0
        assert got.sigma_gamma == want.sigma_gamma
        assert got.slope == pytest.approx(want.slope, abs=TOL)
        assert got.nu == pytest.approx(want.nu, abs=TOL)
        assert got.counts == want.counts
        assert got == want
        mixture_seen = mixture_seen or bool(got.counts)
    assert mixture_seen
    # The unpriced NO->N reservoir must not leak in: NH2 keeps nu = -2, CH3
    # (3 H at -1, C at -2 under the applied CO feed) sits at -5.
    by_ads = {a["adsorbate"]: sp.Anchor.from_dict(a, applied) for a in che["anchors"]}
    assert by_ads["NH2"].nu == -2
    assert by_ads["CH3"].nu == -5
    wrong = sp.Anchor.from_dict(che["anchors"][0], [sp.Reservoir("NO", "N")])
    assert wrong != sp.Anchor.from_dict(che["anchors"][0], applied)


def test_resweep_at_sign_zero_reproduces_the_stored_sweep() -> None:
    che = _stored_sweep()
    applied = sp.applied_reservoirs(che, "mace:large")
    anchors = [sp.Anchor.from_dict(d, applied) for d in che["anchors"]]
    before = copy.deepcopy(che)
    again = sp.resweep(che, sp.perturb_anchors(anchors, ("H", 1), 0))
    assert che == before  # the input is never mutated
    assert again is not che and again["anchors"] is not che["anchors"]
    # resweep copies needs_anchor and point as stored, on purpose: at a
    # nonzero shift they go stale, here they are equal only because sign 0
    assert again["needs_anchor"] == che["needs_anchor"]
    assert again["anchors"] == che["anchors"]
    assert len(again["envelope"]) == len(che["envelope"]) == 61
    for new, old in zip(again["envelope"], che["envelope"], strict=True):
        assert new["winner"] == old["winner"]
        assert new["U_RHE"] == pytest.approx(old["U_RHE"], abs=TOL)
        assert new["gamma"] == pytest.approx(old["gamma"], abs=TOL)
    # a real staircase CH3 -> H -> NO, not a vacuous match; CH3's crossing
    # at -0.6 V exists only because its slope kept the applied CO feed's nu
    assert [b["U_RHE"] for b in che["boundaries"]] == pytest.approx(
        [-0.6, -0.2], abs=1e-6
    )
    assert len(again["boundaries"]) == len(che["boundaries"])
    for new, old in zip(again["boundaries"], che["boundaries"], strict=True):
        assert new["below"] == old["below"] and new["above"] == old["above"]
        assert new["U_RHE"] == pytest.approx(old["U_RHE"], abs=TOL)
        assert new["band_propagated"] == pytest.approx(old["band_propagated"], abs=TOL)
        assert new["band_model_form"] == old["band_model_form"]
        assert new["n_models"] == old["n_models"]


def test_resweep_follows_the_shifted_anchors() -> None:
    che = _stored_sweep()
    applied = sp.applied_reservoirs(che, "mace:large")
    anchors = [sp.Anchor.from_dict(d, applied) for d in che["anchors"]]
    shifted = [
        replace(a, gamma0=a.gamma0 + 0.05) if a.adsorbate == "H" else a for a in anchors
    ]
    moved = sp.resweep(che, shifted)
    # H* is the middle step; lifting it 0.05 eV (to -0.07 + 0.1 U) puts it
    # above the NO line (-0.14) for U > -0.7 and above CH3 (0.12 + 0.5 U)
    # for U > -0.475, so the staircase collapses to CH3 -> NO at -0.52 V
    assert [
        (b["below"]["adsorbate"], b["above"]["adsorbate"]) for b in moved["boundaries"]
    ] == [("CH3", "NO")]
    assert moved["boundaries"][0]["U_RHE"] == pytest.approx(-0.52, abs=1e-6)
    assert che["boundaries"][0]["U_RHE"] == pytest.approx(-0.6, abs=1e-6)
    assert [a["gamma0"] for a in moved["anchors"] if a["adsorbate"] == "H"] == [
        pytest.approx(-0.12 + 0.05)
    ]


def test_resweep_refuses_a_multi_model_sweep() -> None:
    p = _two_reservoir_payload()
    che = sp.sweep({"mace:large": p, "mace:medium": p}, reservoirs=RESERVOIRS)
    assert che["n_models"] == 2
    anchors = [sp.Anchor.from_dict(d, []) for d in che["anchors"]]
    with pytest.raises(ValueError, match="n_models=2"):
        sp.resweep(che, anchors)


def _anc(
    adsorbate: str,
    n: int,
    gamma0: float,
    sigma: float = 0.02,
    counts: tuple[tuple[str, int], ...] = (),
) -> sp.Anchor:
    return sp.Anchor(
        adsorbate=adsorbate,
        n=n,
        theta=0.25 * n,
        energy=-100.0,
        gamma0=gamma0,
        area=AREA,
        site=None,
        converged=True,
        detached=False,
        sigma_gamma=sigma,
        model="m",
        counts=counts,
    )


def test_perturb_wanted_up_others_down_and_sign_zero_is_identity() -> None:
    anchors = [_anc("H", 1, -0.1, 0.02), _anc("O", 1, -0.15, 0.03), _anc("H", 2, 0.1)]
    plus = sp.perturb_anchors(anchors, ("H", 1), +1)
    assert [a.gamma0 for a in plus] == pytest.approx([-0.08, -0.18, 0.08])
    minus = sp.perturb_anchors(anchors, ("H", 1), -1)
    assert [a.gamma0 for a in minus] == pytest.approx([-0.12, -0.12, 0.12])
    assert sp.perturb_anchors(anchors, ("H", 1), 0) == anchors
    assert [a.gamma0 for a in anchors] == [-0.1, -0.15, 0.1]  # inputs untouched
    assert all(p.sigma_gamma == a.sigma_gamma for p, a in zip(plus, anchors))


def test_perturb_clean_wanted_moves_every_anchor_down_at_plus_one() -> None:
    anchors = [_anc("H", 1, -0.1, 0.02), _anc("O", 1, -0.15, 0.03)]
    for clean in (None, ("*", 0)):
        got = sp.perturb_anchors(anchors, clean, +1)
        assert [a.gamma0 for a in got] == pytest.approx([-0.12, -0.18])
        got = sp.perturb_anchors(anchors, clean, -1)
        assert [a.gamma0 for a in got] == pytest.approx([-0.08, -0.12])


def test_perturb_matches_a_mixture_on_adsorbate_and_total_n() -> None:
    counts = (("NH2", 2), ("H", 1))
    mix = _anc("NH2+H", 3, 0.0, 0.02, counts)
    single = _anc("NH2", 3, 0.0, 0.02)
    # (label, n) is what the map's regions key on, so the mixture is wanted ...
    got = sp.perturb_anchors([mix, single], ("NH2+H", 3), +1)
    assert [a.gamma0 for a in got] == pytest.approx([0.02, -0.02])
    # ... while Anchor.key's (label, counts) form never matches.
    assert sp.perturb_anchors([mix], mix.key, +1)[0].gamma0 == pytest.approx(-0.02)
    # A single-species anchor and a mixture sharing label and n both take
    # the wanted sign: the map cannot tell them apart either.
    twin = _anc("H", 3, 0.0, 0.02, (("H", 3),))
    got = sp.perturb_anchors([twin, _anc("H", 3, 0.0, 0.02)], ("H", 3), +1)
    assert [a.gamma0 for a in got] == pytest.approx([0.02, 0.02])


def _crossing_u(anchors: list[sp.Anchor]) -> float:
    """U* of the H -> O boundary on a fine grid over (-1, 1) V."""
    bs = sp.boundaries({"m": anchors}, anchors, u_window=(-1.0, 1.0), grid=201)
    (b,) = [b for b in bs if b.below == ("H", 1) and b.above == ("O", 1)]
    return b.u_star


def test_opposed_shift_moves_a_boundary_a_uniform_shift_leaves() -> None:
    """H* (wanted; nu -1, slope +0.1) wins below the H -> O crossing at
    U* = 0.5 V, where O* (nu 2, slope -0.2) takes over: gamma_H = -0.3 +
    0.1 U, gamma_O = -0.15 - 0.2 U. That crossing depends on the gamma0
    difference, so a uniform shift leaves it; the opposed shift moves it by
    2 sigma / 0.3."""
    sigma = 0.03
    anchors = [_anc("H", 1, -0.3, sigma), _anc("O", 1, -0.15, sigma)]
    assert _crossing_u(anchors) == pytest.approx(0.5)
    uniform = [replace(a, gamma0=a.gamma0 + sigma) for a in anchors]
    assert _crossing_u(uniform) == pytest.approx(0.5)
    lose = sp.perturb_anchors(anchors, ("H", 1), +1)  # wanted loses
    win = sp.perturb_anchors(anchors, ("H", 1), -1)  # wanted gains
    assert _crossing_u(lose) == pytest.approx(0.5 - 2 * sigma / 0.3)
    assert _crossing_u(win) == pytest.approx(0.5 + 2 * sigma / 0.3)
