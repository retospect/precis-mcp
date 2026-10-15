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


# ── correction footing (gr486949) ─────────────────────────────────────────


def _corr(*, gas_set: bool = False, h_star_set: bool = False) -> dict:
    """The ``corrections`` block an engine records on its coverage.json:
    enabled levers plus the per-model sets actually applied."""
    return {
        "version": "2026-10-02",
        "gas": {
            "enabled": True,
            "anchors": ["H2", "N2", "H2O"],
            "sets": {"m": {"key": ["mace", "medium"]}} if gas_set else {},
            "unknown": [] if gas_set else [["mace", "large"]],
        },
        "h_star": {
            "enabled": True,
            "host_metal": "Pd",
            "sets": {"m": {"key": ["mace", "medium", "Pd"]}} if h_star_set else {},
            "unanchored": [] if h_star_set else [["mace", "large", "Pd"]],
        },
    }


def test_correction_footprint_is_what_the_engine_applied() -> None:
    """The footprint is the recorded sets, not the request: no block (an old
    engine), a lever switched off, or a lever with no matching table entry
    all count as not applied."""
    p = _payload("m", {"H": [-0.02]})
    assert sp.correction_footprint(p) == frozenset()
    p["corrections"] = _corr(gas_set=True, h_star_set=True)
    assert sp.correction_footprint(p) == {"gas", "h_star"}
    p["corrections"]["gas"]["enabled"] = False
    assert sp.correction_footprint(p) == {"h_star"}
    p["corrections"]["gas"]["enabled"] = True
    p["corrections"]["gas"]["sets"] = {}
    assert sp.correction_footprint(p) == {"h_star"}


def test_drop_uncorrected_splits_mixed_correction_footings() -> None:
    """gr486949: pooling a corrected model with an uncorrected one prices the
    correction into the anchors — the undercorrected model drops out of the
    pooled sweep; uniform footings keep it whole."""
    a = _payload("a", {"H": [-0.02]})
    a["corrections"] = _corr(gas_set=True, h_star_set=True)
    b = _payload("b", {"H": [-0.03]})
    b["corrections"] = _corr()  # levers on, nothing applied
    kept, dropped = sp.drop_uncorrected({"a": a, "b": b})
    assert set(kept) == {"a"} and dropped == ["b"]
    # a missing lever short of full correction drops the same way
    c = _payload("c", {"H": [-0.04]})
    c["corrections"] = _corr(gas_set=True)
    kept2, dropped2 = sp.drop_uncorrected({"a": a, "c": c})
    assert set(kept2) == {"a"} and dropped2 == ["c"]
    # uniform footings keep the pool, corrected or not
    kept3, dropped3 = sp.drop_uncorrected({"a": a, "d": dict(a)})
    assert set(kept3) == {"a", "d"} and dropped3 == []
    kept4, dropped4 = sp.drop_uncorrected({"b": b, "e": dict(b)})
    assert set(kept4) == {"b", "e"} and dropped4 == []


def test_drop_uncorrected_never_empties_the_pool() -> None:
    """Incomparable footprints (each model missing a different lever — no
    engine table produces one) keep everything: there is no corrected
    model to prefer."""
    a = _payload("a", {"H": [-0.02]})
    a["corrections"] = _corr(gas_set=True)
    b = _payload("b", {"H": [-0.03]})
    b["corrections"] = _corr(h_star_set=True)
    kept, dropped = sp.drop_uncorrected({"a": a, "b": b})
    assert set(kept) == {"a", "b"} and dropped == []


def test_every_model_in_the_band_has_a_correction_set() -> None:
    """gr486949's invariant: when the pool holds a model on the union
    correction footprint, every model kept in the band applied that full
    set — a model the table has no entry for (or a strict subset) never
    shares the model-form band with a corrected one."""

    def _p(tag: str, **kw: bool) -> dict:
        p = _payload(tag, {"H": [-0.02]})
        p["corrections"] = _corr(**kw)
        return p

    pools = [
        {
            "a": _p("a", gas_set=True, h_star_set=True),
            "b": _p("b", gas_set=True, h_star_set=True),
        },
        {"a": _p("a", gas_set=True, h_star_set=True), "b": _p("b")},
        {"a": _p("a", gas_set=True, h_star_set=True), "b": _p("b", gas_set=True)},
        {"a": _p("a"), "b": _p("b")},
        {"a": _p("a", gas_set=True), "b": _p("b", h_star_set=True)},
    ]
    for pool in pools:
        footprints = {m: sp.correction_footprint(p) for m, p in pool.items()}
        union = frozenset().union(*footprints.values())
        kept, dropped = sp.drop_uncorrected(pool)
        if any(fp == union for fp in footprints.values()):
            # a fully-corrected model exists: every band member applied it
            assert all(footprints[m] == union for m in kept)
            assert set(dropped) == {m for m, fp in footprints.items() if fp != union}
            if union:
                assert all(footprints[m] for m in kept)
        else:
            # incomparable footprints: nothing to prefer, keep the pool
            assert set(kept) == set(pool) and dropped == []


# ── co-adsorbed mixtures (gr489475) ───────────────────────────────────────


def _mix_point(
    no: int, h: int, gamma: float, *, detached: bool = False, theta: float | None = None
) -> dict:
    n = no + h
    return {
        "counts": {"NO": no, "H": h},
        "n": n,
        "theta": n / 8 if theta is None else theta,
        "theta_by_species": {"NO": no / 8, "H": h / 8},
        "ratio": [no, h],
        "energy": -100.0 + gamma * AREA,
        "gamma": gamma,
        "converged": True,
        "detached": detached,
        "shift": 0.0,
    }


#: A hand-written ``facets[0].mixtures`` block: NO+H at 3:1, 2:2, 1:3, plus a
#: detached 1:1 point and an all-zero point the parser must skip.
MIXTURES: list[dict] = [
    {
        "species": ["NO", "H"],
        "label": "NO+H",
        "mu": {"NO": -12.0, "H": -3.0},
        "c_ads": {"NO": 0.0, "H": 0.0},
        "site": {"NO": "fcc", "H": "hcp"},
        "points": [
            _mix_point(3, 1, -0.05),
            _mix_point(2, 2, 0.0),
            _mix_point(1, 3, -0.04),
            _mix_point(1, 1, -0.01, detached=True, theta=0.25),
            _mix_point(0, 0, 0.0, theta=0.0),
        ],
    }
]


def _mixed_payload(mixtures: list[dict] | None = None) -> dict:
    p = _payload("m", {"H": [-0.02], "NO": [-0.01]})
    p["facets"][0]["mixtures"] = MIXTURES if mixtures is None else mixtures
    return p


def _mix_anchors(**kw) -> dict[tuple, sp.Anchor]:
    return {
        a.key: a
        for a in sp.anchors_from_coverage(_mixed_payload(), **kw)
        if a.adsorbate == "NO+H"
    }


def test_mixture_points_parse_into_count_carrying_anchors() -> None:
    anchors = sp.anchors_from_coverage(_mixed_payload())
    mixed = _mix_anchors()
    k31 = ("NO+H", (("NO", 3), ("H", 1)))
    assert k31 in mixed
    a = mixed[k31]
    assert a.adsorbate == "NO+H"
    assert a.n == 4
    assert a.counts == (("NO", 3), ("H", 1))
    assert a.key == k31
    assert a.theta == pytest.approx(0.5)
    # NO is closed-shell (nu 0), H nu = -1 -> slope = +n_H / area
    assert a.slope == pytest.approx(+1 / AREA)
    assert mixed[("NO+H", (("NO", 1), ("H", 3)))].slope == pytest.approx(+3 / AREA)
    assert a.as_dict()["counts"] == {"NO": 3, "H": 1}
    # single-species anchors in the same payload are unchanged
    singles = {x.key: x for x in anchors if not x.counts}
    assert set(singles) == {("H", 1), ("NO", 1)}
    assert "counts" not in singles[("H", 1)].as_dict()
    assert singles[("H", 1)].slope == pytest.approx(+1 / AREA)


def test_mixture_flags_detached_and_skips_all_zero_counts() -> None:
    mixed = _mix_anchors()
    assert len(mixed) == 4  # 3:1, 2:2, 1:3, detached 1:1; the 0:0 point is gone
    assert all(a.n > 0 for a in mixed.values())
    assert mixed[("NO+H", (("NO", 1), ("H", 1)))].detached is True
    assert mixed[("NO+H", (("NO", 3), ("H", 1)))].detached is False


def test_payload_without_mixtures_is_unchanged() -> None:
    plain = _payload("m", {"H": [-0.02, -0.03], "O": [0.04]})
    assert "mixtures" not in plain["facets"][0]
    anchors = sp.anchors_from_coverage(plain)
    assert all(a.counts == () and "counts" not in a.as_dict() for a in anchors)
    assert all(a.key == (a.adsorbate, a.n) for a in anchors)
    out = sp.sweep({"m": plain}, point_u_rhe=-0.3)
    assert all(
        row["winner"] is None or "counts" not in row["winner"]
        for row in out["envelope"]
    )
    assert "counts" not in (out["point"]["winner"] or {})
    # an explicit empty block is the same as no block
    empty = _payload("m", {"H": [-0.02, -0.03], "O": [0.04]})
    empty["facets"][0]["mixtures"] = []
    assert [a.as_dict() for a in sp.anchors_from_coverage(empty)] == [
        a.as_dict() for a in anchors
    ]


def test_mixture_sigma_sums_the_per_species_mu_bars() -> None:
    mixed = _mix_anchors(sigma_e_ev=0.05, sigma_mu_ev={"NO": 0.1, "H": 0.2})
    a = mixed[("NO+H", (("NO", 3), ("H", 1)))]
    assert a.sigma_gamma == pytest.approx(
        math.sqrt(2 * 0.05**2 + (3 * 0.1 + 1 * 0.2) ** 2) / AREA
    )


@pytest.mark.parametrize(
    "bad_species", [["Cl", "H"], ["NO", "Cl"]], ids=["first", "second"]
)
def test_mixture_naming_an_unpriceable_species_is_skipped_whole(
    bad_species: list[str],
) -> None:
    mix = {
        **MIXTURES[0],
        "species": bad_species,
        "label": "+".join(bad_species),
        "points": [
            {**pt, "counts": dict(zip(bad_species, pt["counts"].values(), strict=True))}
            for pt in MIXTURES[0]["points"]
        ],
    }
    anchors = sp.anchors_from_coverage(_mixed_payload([mix]))
    assert [a for a in anchors if a.counts] == []
    assert {a.adsorbate for a in anchors} == {"H", "NO"}


def test_mixture_sweep_winner_carries_counts_and_pool_keeps_splits_apart() -> None:
    out = sp.sweep({"m": _mixed_payload()}, grid=16, point_u_rhe=-1.0)
    # at U = -1 V the 1:3 point (slope +0.3) is the lowest: -0.04 - 0.3 = -0.34
    assert out["point"]["winner"] == {
        "adsorbate": "NO+H",
        "n": 4,
        "theta": 0.5,
        "counts": {"NO": 1, "H": 3},
    }
    assert out["envelope"][0]["winner"]["counts"] == {"NO": 1, "H": 3}
    mixed = [a for a in out["anchors"] if a["adsorbate"] == "NO+H"]
    assert sorted(tuple(a["counts"].items()) for a in mixed) == sorted(
        [
            (("NO", 3), ("H", 1)),
            (("NO", 2), ("H", 2)),
            (("NO", 1), ("H", 3)),
        ]
    )
    # the 3:1 and 1:3 points share label and n but are distinct anchors
    assert len({tuple(a["counts"].items()) for a in mixed if a["n"] == 4}) == 3
    assert any(s.get("counts") == {"NO": 1, "H": 1} for s in out["skipped_detached"])


def test_anchor_spread_names_a_mixture_by_its_counts() -> None:
    spread = sp.anchor_spread(
        {
            "a": sp.anchors_from_coverage(_mixed_payload()),
            "b": sp.anchors_from_coverage(_mixed_payload()),
        }
    )
    entry = spread["NO+H:NO3H1"]
    assert entry["n_models"] == 2
    assert entry["n"] == 4
    assert entry["adsorbate"] == "NO+H"
    assert entry["counts"] == {"NO": 3, "H": 1}
    assert "counts" not in spread["H:1"]
    assert "NO+H:NO1H3" in spread


def test_pool_anchors_keeps_mixture_counts_and_n() -> None:
    pooled = sp.pool_anchors({"a": sp.anchors_from_coverage(_mixed_payload())})
    mixed = {a.key: a for a in pooled if a.counts}
    a = mixed[("NO+H", (("NO", 3), ("H", 1)))]
    assert a.n == 4 and a.slope == pytest.approx(+1 / AREA)


def test_sweep_boundary_between_clean_and_a_mixture_carries_counts() -> None:
    out = sp.sweep({"m": _mixed_payload()}, grid=61)
    ends = [
        end
        for b in out["boundaries"]
        for end in (b["below"], b["above"])
        if end is not None and end["adsorbate"] == "NO+H"
    ]
    assert ends, "a mixed termination wins somewhere in the window"
    for end in ends:
        assert end["n"] == sum(end["counts"].values())
        assert set(end["counts"]) == {"NO", "H"}
