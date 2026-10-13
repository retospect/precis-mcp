"""Slices 4+5: per-rung masks, the operating window, the Shapley field and
its reductions — pure numpy, no engine, so every number is checkable by
hand.

Grid used throughout: ``U`` = -1..1 in 0.25 steps (9 points), ``pH`` =
0..7 in 1.0 steps (8 points). Areas use the module's cell measure — each
grid point owns a dU×dpH cell (half a step beyond the axis ends) — so one
column measures 8 × 0.25 = 2.0 V·pH and the whole grid 72 × 0.25 =
18.0 V·pH. A rung ``(delta_g0, n_e=1, eps=0)`` has ``delta_g = g0 + U``:
viable for ``U <= -delta_g0`` (a reduction goes downhill at negative U)
and forbids the columns above it."""

from __future__ import annotations

from itertools import permutations

import numpy as np
import pytest

from precis_pathway import staircase_masks as sm
from precis_pathway.surface_pourbaix import PREFAC_V

U = np.linspace(-1.0, 1.0, 9)
PH = np.linspace(0.0, 7.0, 8)
COL_VPH = 2.0  # one U column across all pH rows: 8 cells * 0.25
GRID_VPH = 18.0  # 72 cells * 0.25


def _resting(wanted: np.ndarray | None = None) -> sm.RestingMap:
    w = np.ones((PH.size, U.size), dtype=bool) if wanted is None else wanted
    return sm.RestingMap(U, PH, w)


def _rung(name: str, g0: float, n_e: int = 1, **kw) -> sm.Rung:
    return sm.Rung(name=name, delta_g0=g0, n_e=n_e, **kw)


def _cols(r: sm.RestingMap, *u_values: float) -> np.ndarray:
    """Boolean grid with the named U columns set across all pH rows."""
    m = np.zeros(r.shape, dtype=bool)
    for u in u_values:
        m[:, int(np.argmin(np.abs(r.u_rhe - u)))] = True
    return m


# ── input contract ───────────────────────────────────────────────────────


def test_resting_map_validates_and_measures() -> None:
    r = _resting()
    assert r.shape == (8, 9)
    assert r.total_area == pytest.approx(GRID_VPH)
    assert r.area(r.wanted) == pytest.approx(GRID_VPH)
    with pytest.raises(ValueError, match="strictly increasing"):
        sm.RestingMap(U[::-1], PH, np.ones((8, 9), bool))
    with pytest.raises(ValueError, match="shape"):
        sm.RestingMap(U, PH, np.ones((9, 8), bool))


def test_rung_kind_and_duplicate_names_are_rejected() -> None:
    with pytest.raises(ValueError, match="kind"):
        sm.Rung(name="x", delta_g0=0.0, n_e=1, kind="adsorption")
    with pytest.raises(ValueError, match="duplicate"):
        sm.Route("r", (_rung("a", 0.0), _rung("a", 0.1)))


def test_rung_mask_is_a_vertical_line_and_ph_flat() -> None:
    """delta_g = g0 + n_e*U <= eps on the RHE scale: no pH dependence, a
    boundary at u* = (eps - g0)/n_e, viable on the low-U side."""
    r = _resting()
    m = sm.rung_mask(_rung("ads", -0.5), r)
    assert np.all(m.viable == m.viable[0:1, :])
    assert np.array_equal(m.forbidden, _cols(r, 0.75, 1.0))
    assert m.rung.u_star == pytest.approx(0.5)
    assert m.rung.u_star_she(7.0) == pytest.approx(0.5 - PREFAC_V * 7)


def test_reduction_goes_downhill_at_negative_u() -> None:
    """The CHE sign convention the engine export uses: n_e counts
    proton-electron pairs *consumed*, so a reduction step (n_e > 0) gets
    easier at more negative U."""
    r = _resting()
    red = sm.rung_mask(_rung("red", 0.3), r)
    assert bool(red.viable[0, int(np.argmin(np.abs(r.u_rhe + 0.5)))])
    assert bool(red.forbidden[0, int(np.argmin(np.abs(r.u_rhe)))])
    # and an oxidation (n_e < 0) flips the viable side
    ox = sm.rung_mask(_rung("ox", 0.3, n_e=-1), r)
    assert ox.rung.u_star == pytest.approx(0.3)
    assert np.array_equal(ox.forbidden, _cols(r, -1.0, -0.75, -0.5, -0.25, 0.0, 0.25))


def test_n_e_and_eps_move_the_boundary() -> None:
    r = _resting()
    # n_e = 2 halves the slope: u* = (eps - g0)/n_e = 0.25
    assert np.array_equal(
        sm.rung_mask(_rung("o", -0.5, n_e=2), r).forbidden,
        _cols(r, 0.5, 0.75, 1.0),
    )
    # eps = +0.3 tolerates slightly endergonic steps
    assert np.array_equal(
        sm.rung_mask(_rung("tol", -0.2, eps_eV=0.3), r).forbidden,
        _cols(r, 0.75, 1.0),
    )
    # chemical rung (n_e = 0): viable everywhere or nowhere
    assert sm.rung_mask(_rung("ch", 0.0, n_e=0, kind="chemical"), r).viable.all()
    assert not sm.rung_mask(_rung("ch", 0.1, n_e=0, kind="chemical"), r).viable.any()


def test_sigma_marks_the_uncertain_band_without_moving_the_mask() -> None:
    r = _resting()
    m = sm.rung_mask(_rung("ads", -0.5, sigma=0.1), r)
    # |delta_g - eps| <= sigma  <=>  u in [0.4, 0.6]  <=>  the u = 0.5 col
    assert np.array_equal(m.uncertain, _cols(r, 0.5))
    plain = sm.rung_mask(_rung("ads", -0.5), r)
    assert not plain.uncertain.any()
    assert np.array_equal(m.viable, plain.viable)


# ── the window and its extremes ──────────────────────────────────────────


def test_window_extremes_and_unknown_toggle() -> None:
    """All toggles off = the resting map (§7.5); all on intersects every
    mask. Unknown names are refused, and the active order follows the
    route, not the caller."""
    r = _resting()
    route = sm.Route("main", (_rung("ads", -0.5), _rung("des", 0.0)))
    off = sm.operating_window(r, route, [])
    assert np.array_equal(off.mask, r.wanted)
    assert off.fraction == pytest.approx(1.0)
    on = sm.operating_window(r, route)
    assert np.array_equal(on.mask, ~_cols(r, 0.25, 0.5, 0.75, 1.0))
    assert on.area == pytest.approx(5 * COL_VPH)
    sub = sm.operating_window(r, route, ["des", "ads"])
    assert sub.active_rungs == ("ads", "des")
    with pytest.raises(ValueError, match="unknown"):
        sm.operating_window(r, route, ["nope"])


def test_window_respects_the_resting_mask() -> None:
    """A resting-wanted region cuts the window even when rungs pass."""
    wanted = _cols(_resting(), -0.25, 0.0, 0.25, 0.5, 0.75, 1.0)
    r = _resting(wanted)
    route = sm.Route("main", (_rung("ads", -0.5),))
    on = sm.operating_window(r, route)
    # the rung forbids U > 0.5 inside the wanted region
    assert np.array_equal(on.mask, _cols(r, -0.25, 0.0, 0.25, 0.5))
    assert on.area == pytest.approx(4 * COL_VPH)


def test_her_margin() -> None:
    """Main limiting rung is the chemical step (flat 0.2 eV, above
    -0.3 + U for U <= 0.5); HER's is 0.5 + U. The window needs
    main + margin <= HER, i.e. U >= m - 0.3 (and U > 0.5 passes whenever
    the margin is under 0.8 eV)."""
    r = _resting()
    main = sm.Route(
        "n2rr", (_rung("ads", -0.3), _rung("chem", 0.2, n_e=0, kind="chemical"))
    )
    her = sm.Route("her", (_rung("h_ads", 0.5, kind="her"),))
    m = sm.her_mask(r, main, her, margin=0.2)  # U >= -0.1 -> cols U >= 0.0
    assert np.array_equal(m, ~_cols(r, -1.0, -0.75, -0.5, -0.25))
    m0 = sm.her_mask(r, main, her, margin=0.0)  # U >= -0.3 -> cols U >= -0.25
    assert np.array_equal(m0, ~_cols(r, -1.0, -0.75, -0.5))
    # and inside the window it acts like part of the baseline
    on = sm.operating_window(r, main, [], her=her, her_margin=0.2)
    assert np.array_equal(on.mask, m)
    with pytest.raises(ValueError, match="no rungs"):
        sm.her_mask(r, main, sm.Route("empty"))


# ── Shapley: the design's own overlap example ────────────────────────────


def test_two_overlapping_rungs_split_credit_leave_one_out_is_zero() -> None:
    """Two rungs forbid the same two columns. Each alone kills the whole
    corner, so leave-one-out sees no marginal cost for either — while
    Shapley splits the lost 4.0 V·pH evenly (§8.1)."""
    r = _resting()
    route = sm.Route("main", (_rung("ads", -0.5), _rung("des", -0.5)))

    def lost(active: list[str]) -> float:
        w = sm.operating_window(r, route, active)
        return r.area(w.lost)

    assert lost([]) == 0.0
    assert (
        lost(["ads"])
        == lost(["des"])
        == lost(["ads", "des"])
        == pytest.approx(2 * COL_VPH)
    )
    loo = lost(["ads", "des"]) - lost(["des"])
    assert loo == pytest.approx(0.0)

    sf = sm.shapley_field(r, route)
    assert sf.global_exact and sf.n_samples is None
    assert sf.global_values == pytest.approx([COL_VPH, COL_VPH])
    assert sf.global_values.sum() == pytest.approx(sf.lost_area)
    # at a corner point each forbidding rung owns half the loss
    pt = sf.at(1.0, 3.0)
    assert pt == pytest.approx({"ads": 0.5, "des": 0.5})


def test_three_rungs_hand_checkable() -> None:
    """A and B forbid cols {0.75, 1.0}; C also forbids {0.25, 0.5}.
    Subset losses: L(A)=L(B)=4, L(C)=L(any pair or triple)=8 V·pH, so
    phi = (4/3, 4/3, 16/3)."""
    r = _resting()
    route = sm.Route("main", (_rung("a", -0.5), _rung("b", -0.5), _rung("c", 0.0)))
    sf = sm.shapley_field(r, route)
    assert sf.global_values == pytest.approx([4 / 3, 4 / 3, 16 / 3])
    assert sf.lost_area == pytest.approx(4 * COL_VPH)
    # pointwise: the shared corner splits three ways, C's own cols fully
    col8 = sf.field[:, 0, 8]
    col5 = sf.field[:, 0, 5]
    assert col8 == pytest.approx([1 / 3, 1 / 3, 1 / 3])
    assert col5 == pytest.approx([0.0, 0.0, 1.0])
    # the global game is the area-weighted field sum (Shapley linearity)
    w = r.cell_areas()
    assert sf.global_values == pytest.approx((w[None] * sf.field).sum(axis=(1, 2)))


def test_field_sums_to_the_lost_window_at_every_point() -> None:
    """The §8.3 invariant: at each grid point the per-rung values sum to
    the total lost window there (0 or 1)."""
    r = _resting(_cols(_resting(), 0.0, 0.25, 0.5, 0.75, 1.0))
    route = sm.Route(
        "main",
        (
            _rung("a", -0.5),
            _rung("b", -0.5),
            _rung("c", 0.0, kind="desorption"),
        ),
    )
    sf = sm.shapley_field(r, route)
    np.testing.assert_allclose(sf.field.sum(axis=0), sf.lost)
    assert np.array_equal(sf.lost, _cols(r, 0.25, 0.5, 0.75, 1.0))


def test_shapley_is_exact_over_small_subsets_via_enumeration() -> None:
    """Brute-force reference on 3 rungs: average each rung's marginal over
    every ordering, computed straight from operating_window, equals the
    module's values."""
    r = _resting()
    route = sm.Route("main", (_rung("a", -0.5), _rung("b", -0.6), _rung("c", 0.2)))
    names = [x.name for x in route.rungs]

    def lost(active: tuple[str, ...]) -> float:
        return r.area(sm.operating_window(r, route, active).lost)

    ref = np.zeros(3)
    for perm in permutations(range(3)):
        s: tuple[str, ...] = ()
        prev = lost(s)
        for i in perm:
            s = s + (names[i],)
            cur = lost(s)
            ref[i] += cur - prev
            prev = cur
    ref /= 6
    sf = sm.shapley_field(r, route)
    assert sf.global_values == pytest.approx(ref)


# ── reductions ───────────────────────────────────────────────────────────


def test_ipr_is_one_for_a_dominant_rung_and_n_for_shared_blame() -> None:
    r = _resting()
    route = sm.Route("main", (_rung("a", -0.5), _rung("b", -0.5), _rung("c", 0.0)))
    red = sm.reduce_field(sm.shapley_field(r, route))
    # shared corner (3 equal shares): ipr = 3; C-only cols: ipr = 1; rest 0
    assert red.ipr[0, 8] == pytest.approx(3.0)
    assert red.ipr[0, 5] == pytest.approx(1.0)
    assert red.ipr[0, 0] == 0.0
    # raw sum of squares tracks the same concentration: 1/3 vs 1
    assert red.sumsq[0, 8] == pytest.approx(1 / 3)
    assert red.sumsq[0, 5] == pytest.approx(1.0)
    # mean over lost cells: (16 cells * 3 + 16 cells * 1) / 32 cells = 2
    assert red.ipr_mean == pytest.approx(2.0)


def test_dominant_map_marks_ties_and_churn_ignores_them() -> None:
    """Per-point shares are 1/|F_p|, so every shared cell is an exact tie;
    the dominant map marks those -2 instead of picking one by index, and
    churn excludes tie cells (and no-loss cells) entirely."""
    r = _resting()
    route = sm.Route("main", (_rung("a", -0.5), _rung("b", -0.5), _rung("c", 0.0)))
    red = sm.reduce_field(sm.shapley_field(r, route))
    names = red.dominant_names
    assert red.dominant[0, 8] == -2  # a/b/c all tie in the shared corner
    assert names[red.dominant[0, 5]] == "c"  # c alone
    assert red.dominant[0, 0] == -1  # nothing lost
    # c's region borders only tie and no-loss cells -> no counted ridge
    assert red.churn == 0.0


def test_churn_counts_ridges_between_dominants() -> None:
    """a alone owns cols {0.75, 1.0} (forbids U > 0.5); b alone owns
    cols {-1, …, 0.5} (an oxidation rung, n_e = -1, forbids U < 0.6). The
    col6|col7 border churns: 8 cells on each side, 16 of 72."""
    r = _resting()
    route = sm.Route("main", (_rung("a", -0.5), _rung("b", 0.6, n_e=-1)))
    red = sm.reduce_field(sm.shapley_field(r, route))
    names = red.dominant_names
    assert names[red.dominant[0, 8]] == "a"
    assert names[red.dominant[0, 0]] == "b"
    assert red.churn == pytest.approx(16 / 72)


def test_dominant_map_and_churn_do_not_depend_on_rung_order() -> None:
    """Tied cells are marked -2 by value, not by argmax order: permuting
    the route relabels the dominant ids but the *named* rung per cell and
    the churn are unchanged."""
    r = _resting()

    def named(red: sm.Reductions) -> np.ndarray:
        names = np.array(red.dominant_names, dtype=object)
        return np.where(
            red.dominant >= 0, names[np.clip(red.dominant, 0, None)], red.dominant
        )

    # a and c tie on cols {0.75, 1.0}: an argmax implementation would
    # report "a" or "c" depending on route order
    rungs = (_rung("a", -0.5), _rung("b", 0.6, n_e=-1), _rung("c", -0.5))
    red1 = sm.reduce_field(sm.shapley_field(r, sm.Route("m1", rungs)))
    red2 = sm.reduce_field(
        sm.shapley_field(r, sm.Route("m2", (rungs[2], rungs[1], rungs[0])))
    )
    np.testing.assert_array_equal(named(red1), named(red2))
    assert red1.churn == red2.churn
    # and on a field with a real ridge the churn itself is order-free
    pair = (_rung("a", -0.5), _rung("b", 0.6, n_e=-1))
    c1 = sm.reduce_field(sm.shapley_field(r, sm.Route("m1", pair))).churn
    c2 = sm.reduce_field(sm.shapley_field(r, sm.Route("m2", (pair[1], pair[0])))).churn
    assert c1 == c2 == pytest.approx(16 / 72)


def test_churn_is_zero_on_a_single_rung_field() -> None:
    """One rung owns every lost cell, so there are no identity ridges —
    even though its boundary borders cells with no dominant."""
    r = _resting()
    route = sm.Route("main", (_rung("a", -0.5),))
    red = sm.reduce_field(sm.shapley_field(r, route))
    assert red.churn == 0.0
    assert red.ipr[0, 8] == pytest.approx(1.0)


def test_per_rung_stats_and_smooth_max() -> None:
    r = _resting()
    route = sm.Route("main", (_rung("a", -0.5), _rung("b", -0.5), _rung("c", 0.0)))
    sf = sm.shapley_field(r, route)
    red = sm.reduce_field(sf, temperature=0.05)
    stats = {s.rung: s for s in red.per_rung}
    assert stats["a"].area == pytest.approx(4 / 3)
    assert stats["a"].max == pytest.approx(1 / 3)
    assert stats["c"].max == pytest.approx(1.0)
    assert stats["a"].mean == pytest.approx(sf.field[0].mean())
    # smooth-max approaches max as tau -> 0, stays above it, and is
    # masked to 0 where nothing is lost (not T*ln N)
    assert red.smooth_max is not None
    assert np.all(red.smooth_max >= sf.field.max(axis=0) - 1e-12)
    assert red.smooth_max[sf.lost == 0] == pytest.approx(0.0)
    assert red.smooth_max[sf.lost == 1].min() > 0.0
    tighter = sm.reduce_field(sf, temperature=1e-6)
    assert tighter.smooth_max is not None
    np.testing.assert_allclose(tighter.smooth_max, sf.field.max(axis=0), atol=1e-4)
    with pytest.raises(ValueError, match="temperature"):
        sm.reduce_field(sf, temperature=0.0)


def test_reductions_carry_provenance() -> None:
    r = _resting()
    her = sm.Route("her", (_rung("h", 0.5, kind="her"),))
    route = sm.Route("main", (_rung("a", -0.5), _rung("c", 0.2, n_e=0)))
    sf = sm.shapley_field(r, route, her=her, her_margin=0.1)
    red = sm.reduce_field(sf)
    assert red.route == "main" and red.rungs == ("a", "c")
    assert red.her == "her" and red.her_margin == pytest.approx(0.1)
    assert red.shape == (8, 9)
    assert red.u_window == (-1.0, 1.0) and red.ph_window == (0.0, 7.0)


# ── the sampled path (N > 16) ────────────────────────────────────────────


def test_sampling_is_seeded_warns_and_stays_efficient() -> None:
    """18 rungs all forbidding the same corner: enumeration is skipped,
    a warning fires, the seed reproduces the estimate, and each ordering's
    marginals still sum to the total lost area."""
    r = _resting()
    route = sm.Route(
        "main",
        tuple(_rung(f"r{i}", -0.5) for i in range(sm.EXACT_SHAPLEY_MAX_RUNGS + 2)),
    )
    with pytest.warns(UserWarning, match="sampled"):
        sf1 = sm.shapley_field(r, route, seed=7, n_samples=2000)
        sf2 = sm.shapley_field(r, route, seed=7, n_samples=2000)
    assert not sf1.global_exact and sf1.n_samples == 2000
    np.testing.assert_array_equal(sf1.global_values, sf2.global_values)
    assert sf1.global_values.sum() == pytest.approx(sf1.lost_area)
    # the per-point field is still exact: 18 equal shares in the corner
    assert sf1.field[:, 0, 8] == pytest.approx(np.full(18, 1 / 18))
    # symmetric game: the estimate should be near 1/18 of the loss each
    assert sf1.global_values == pytest.approx(np.full(18, sf1.lost_area / 18), rel=0.3)


# ── renderer ─────────────────────────────────────────────────────────────


def test_render_text_covers_window_ranking_ipr_and_churn() -> None:
    r = _resting()
    route = sm.Route("main", (_rung("a", -0.5), _rung("b", -0.5), _rung("c", 0.0)))
    sf = sm.shapley_field(r, route)
    red = sm.reduce_field(sf)
    text = sm.render_text(sf, red)
    assert "V·pH" in text and "operating window" in text
    assert text.index("  c:") < text.index("  a:")  # ranked by lost window
    assert "effective bottlenecks 2.00" in text
    assert "ridge churn 0.0%" in text
