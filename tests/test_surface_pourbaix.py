"""Surface-Pourbaix slice 1: the inner CHE sweep is pure arithmetic over a
coverage payload, so these tests run without the engine.

The synthetic payload mirrors ``autocatpath.coverage.scan``'s shape (one
facet, ``adsorbates[frag].points[{n, theta, energy, gamma, converged,
detached}]``) with numbers chosen so every boundary is checkable by hand.
"""

from __future__ import annotations

import math

import pytest

from precis_pathway import surface_pourbaix as sp

AREA = 10.0  # Å²; n_top = 4 so n=1 is 0.25 ML


def _payload(model: str, gammas: dict[str, list[float]], **flags) -> dict:
    """``gammas[frag]`` = gamma0 per n = 1..len (eV/Å²)."""
    ads = {}
    for frag, gs in gammas.items():
        ads[frag] = {
            "mu": -3.0,
            "site": "fcc",
            "c_ads": 0.0,
            "points": [
                {
                    "n": i + 1,
                    "theta": (i + 1) / 4,
                    "energy": -100.0 + g * AREA,
                    "gamma": g,
                    "converged": True,
                    "detached": flags.get("detached", {}).get((frag, i + 1), False),
                }
                for i, g in enumerate(gs)
            ],
            "best": {"theta": 0.25, "n": 1, "gamma": min(gs)},
        }
    return {
        "model": model,
        "facets": [
            {
                "facet": "111",
                "miller": [1, 1, 1],
                "area": AREA,
                "n_top": 4,
                "n_slab": 36,
                "e_slab": -100.0,
                "e_bulk_per_atom": -3.0,
                "gamma_clean": 0.1,
                "adsorbates": ads,
                "winner": {"adsorbate": None, "theta": 0.0, "gamma": 0.0},
            }
        ],
        "warnings": [],
    }


# ── the CHE arithmetic ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("frag", "nu"),
    [
        ("H", -1),
        ("OH", 1),
        ("O", 2),
        ("N", 0),
        ("NH2", -2),
        ("H2O", 0),
        ("C", 4),
        ("OH@fcc", 1),
    ],
)
def test_electrons_released_follows_the_reservoir_conventions(
    frag: str, nu: int
) -> None:
    """``mu_H = ½G(H2) - eU``, ``mu_O = G(H2O) - 2mu_H``, ``mu_C = G(CH4) -
    4mu_H``, ``mu_N = ½G(N2)``: an adsorbate releases ``2n_O + 4n_C - n_H``
    proton-electron pairs when formed from those reservoirs."""
    assert sp.electrons_released(frag) == nu


def test_electrons_released_refuses_an_unpriced_element() -> None:
    with pytest.raises(ValueError, match="Cl"):
        sp.electrons_released("Cl")


def test_prefac_is_nernst_at_298K() -> None:
    assert pytest.approx(0.05916, abs=2e-5) == sp.PREFAC_V
    assert sp.u_she(0.0, 7.0) == pytest.approx(-0.414, abs=1e-3)
    assert sp.u_rhe(sp.u_she(-0.3, 7.0), 7.0) == pytest.approx(-0.3)


def test_anchor_slopes_have_the_che_sign() -> None:
    """H* gets *less* favourable at higher U (gamma rises), O*/OH* more."""
    anchors = {
        a.key: a
        for a in sp.anchors_from_coverage(
            _payload("m", {"H": [-0.02], "O": [0.04], "OH": [0.01]})
        )
    }
    assert anchors[("H", 1)].slope == pytest.approx(+1 / AREA)
    assert anchors[("OH", 1)].slope == pytest.approx(-1 / AREA)
    assert anchors[("O", 1)].slope == pytest.approx(-2 / AREA)
    assert anchors[("H", 1)].gamma(0.5) == pytest.approx(-0.02 + 0.05)


def test_anchors_skip_what_the_che_cannot_price_and_flag_detached() -> None:
    payload = _payload(
        "m", {"H": [-0.02, -0.03], "Cl": [0.0]}, detached={("H", 2): True}
    )
    anchors = sp.anchors_from_coverage(payload)
    assert {a.adsorbate for a in anchors} == {"H"}
    assert [a.detached for a in sorted(anchors, key=lambda a: a.n)] == [False, True]
    assert [a.n for a in sp.usable(anchors)] == [1]


def test_propagated_bar_is_sqrt2_sigma_over_area_plus_mu_term() -> None:
    a = sp.anchors_from_coverage(
        _payload("m", {"O": [0.0, 0.0]}), sigma_e_ev=0.05, sigma_mu_ev={"O": 0.1}
    )
    by_n = {x.n: x for x in a}
    assert by_n[1].sigma_gamma == pytest.approx(math.sqrt(2 * 0.05**2 + 0.1**2) / AREA)
    assert by_n[2].sigma_gamma == pytest.approx(
        math.sqrt(2 * 0.05**2 + (2 * 0.1) ** 2) / AREA
    )


# ── envelope + boundaries ─────────────────────────────────────────────────


def test_envelope_and_boundary_are_closed_form() -> None:
    """One H anchor (gamma = -0.02 + U/10) and the clean surface: H* wins
    below U* = 0.2 V, clean above. The boundary is the exact crossing, not a
    grid point, and the propagated band is sigma_H / |slope|."""
    out = sp.sweep(
        {"m": _payload("m", {"H": [-0.02]})},
        u_window=(-0.5, 0.5),
        grid=11,
        sigma_e_ev=0.05,
    )
    winners = [(r["U_RHE"], r["winner"]) for r in out["envelope"]]
    assert winners[0][1] == {"adsorbate": "H", "n": 1, "theta": 0.25}
    assert winners[-1][1] is None
    (b,) = out["boundaries"]
    assert b["U_RHE"] == pytest.approx(0.2)
    assert b["below"] == {"adsorbate": "H", "n": 1} and b["above"] is None
    sigma_h = math.sqrt(2) * 0.05 / AREA
    assert b["band_propagated"] == pytest.approx(sigma_h / (1 / AREA))
    assert b["band_model_form"] is None and b["n_models"] == 1
    assert out["needs_anchor"][0]["U_RHE"] == pytest.approx(0.2)


def test_two_anchors_cross_each_other() -> None:
    """H* (slope +1/A) and OH* (slope -1/A) at the same gamma0 cross at
    U = 0 exactly: H* below, OH* above (both beat clean there)."""
    out = sp.sweep(
        {"m": _payload("m", {"H": [-0.05], "OH": [-0.05]})},
        u_window=(-0.4, 0.4),
        grid=9,
    )
    assert [
        (b["below"]["adsorbate"], b["above"]["adsorbate"], round(b["U_RHE"], 6))
        for b in out["boundaries"]
    ] == [("H", "OH", 0.0)]


def test_model_form_band_is_the_spread_of_the_boundary_across_models() -> None:
    """Two models place the H*/clean boundary at 0.2 V and 0.3 V; the pooled
    anchor sits in between and the model-form band is their sample std —
    reported beside the propagated band, not added to it."""
    out = sp.sweep(
        {"a": _payload("a", {"H": [-0.02]}), "b": _payload("b", {"H": [-0.03]})},
        u_window=(-0.5, 0.5),
        grid=21,
        sigma_e_ev=0.05,
    )
    (b,) = out["boundaries"]
    assert b["U_RHE"] == pytest.approx(0.25)
    assert b["n_models"] == 2
    assert b["band_model_form"] == pytest.approx(sp._std([0.2, 0.3]))
    assert b["band_propagated"] == pytest.approx(
        math.sqrt(2) * 0.05 / AREA / (1 / AREA)
    )
    assert out["anchor_model_form"]["H:1"]["gamma0_model_form"] == pytest.approx(
        sp._std([-0.02, -0.03])
    )
    assert out["n_models"] == 2 and out["models"] == ["a", "b"]


def test_point_names_the_resting_termination_and_she_view() -> None:
    out = sp.sweep({"m": _payload("m", {"H": [-0.02]})}, point_u_rhe=-0.3, ph=7.0)
    assert out["point"]["winner"]["adsorbate"] == "H"
    assert out["point"]["U_SHE"] == pytest.approx(-0.3 - sp.PREFAC_V * 7)
    text = sp.render_text(out)
    assert "resting termination is the H* at 0.25 ML" in text
    assert "propagated ±" in text and "model-form n/a" in text


def test_sweep_rejects_a_bad_window_or_grid() -> None:
    with pytest.raises(ValueError):
        sp.sweep({"m": _payload("m", {"H": [-0.02]})}, u_window=(0.5, -0.5))
    with pytest.raises(ValueError):
        sp.sweep({"m": _payload("m", {"H": [-0.02]})}, grid=1)


def test_compare_lowest_theta_reports_per_adsorbate_deltas() -> None:
    """Slice-1 acceptance: at the lowest coverage the per-adsorbate
    formation energy (gamma*area/n) is what a pathway's single-adsorbate
    node measured; the check returns the delta per shared species."""
    anchors = sp.anchors_from_coverage(
        _payload("m", {"H": [-0.02, -0.03], "O": [0.04]})
    )
    rows = sp.compare_lowest_theta(anchors, {"H": -0.25, "O": 0.5, "N": 1.0})
    assert [(r["adsorbate"], r["theta"]) for r in rows] == [("H", 0.25), ("O", 0.25)]
    assert rows[0]["g_ads_anchor"] == pytest.approx(-0.2)
    assert rows[0]["delta"] == pytest.approx(0.05)
