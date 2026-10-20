"""precis_flow slice-1 tests: cfdtest invariants on the reduced-units API.

The dimension-free contract under test: a ``Domain`` carries only shape
(mask, h*, ports); ``solve`` and ``trace`` see no dimensional quantity; the
regime card is where dimensions re-enter.
"""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

import numpy as np

import precis_flow
from precis_flow import (
    Domain,
    Ports,
    RegimeCard,
    Seed,
    nonuniformity,
    rasterize_holes,
    solve,
    trace,
)
from precis_flow.flow import _masked_gradient
from precis_flow.tracer import EXIT_FRACTION, MAX_STEPS

NX, NY = 60, 20


def _rect_domain(
    nx: int = NX,
    ny: int = NY,
    holes: np.ndarray | None = None,
    z_plane_star: float = 0.99,
) -> Domain:
    """Uniform rectangle, full-width ports on the first/last column."""
    mask = np.ones((ny, nx), dtype=bool)
    depth = (
        rasterize_holes(mask, np.asarray(holes, dtype=float), radius_star=0.05)
        if holes is not None
        else np.zeros((ny, nx))
    )
    inlet = np.zeros((ny, nx), dtype=bool)
    inlet[:, 0] = True
    outlet = np.zeros((ny, nx), dtype=bool)
    outlet[:, -1] = True
    return Domain(
        mask=mask,
        h_star=1.0 + depth,
        ports=Ports(inlet=inlet, outlet=outlet),
        z_plane_star=z_plane_star,
    )


def _seed(domain: Domain, z_hi: float = 0.5) -> Seed:
    return Seed.across_inlet(domain, z_lo=0.0, z_hi=z_hi)


def _holes(rng: np.random.Generator, n: int, depth: float) -> np.ndarray:
    return np.column_stack(
        [
            rng.uniform(0.15, 0.85, n),
            rng.uniform(0.2, 0.8, n) * (NY / NX),
            np.full(n, depth),
        ]
    )


# --- flow invariants (ported from cfdtest test_flow.py) ---------------------


def test_pressure_bounded_by_ports():
    f = solve(_rect_domain())
    p = f.p_star[f.domain.mask]
    assert p.max() <= 1.0 + 1e-9
    assert p.min() >= 0.0 - 1e-9


def test_pressure_decreases_inlet_to_outlet():
    f = solve(_rect_domain())
    row = f.p_star[f.domain.ny // 2]
    row = row[np.isfinite(row)]
    assert row[0] > row[-1]
    assert (np.diff(row) <= 1e-6).mean() > 0.9


def test_resistance_positive_and_finite():
    f = solve(_rect_domain())
    assert np.isfinite(f.r_star) and f.r_star > 0


def test_drilling_lowers_resistance():
    rng = np.random.default_rng(0)
    f0 = solve(_rect_domain())
    f = solve(_rect_domain(holes=_holes(rng, 12, depth=3.0)))
    assert f.r_star < f0.r_star


def test_mass_conservation_undrilled_interior():
    # div*(h* U*) ~ 0 in the interior away from ports for a uniform-gap channel.
    d = _rect_domain()
    f = solve(d)
    hux = d.h_star * f.ux_star
    huy = d.h_star * f.uy_star
    div = _masked_gradient(hux, d.mask, d.dx_star, 1) + _masked_gradient(
        huy, d.mask, d.dx_star, 0
    )
    interior = d.mask & ~d.ports.inlet & ~d.ports.outlet
    scale = np.abs(f.ux_star[interior]).max() + 1e-12
    assert np.median(np.abs(div[interior])) < 0.1 * scale * d.dx_star + 1e-6


def test_masked_gradient_constant_is_zero():
    d = _rect_domain()
    field = np.where(d.mask, 7.0, 0.0)
    assert np.allclose(_masked_gradient(field, d.mask, d.dx_star, 1)[d.mask], 0.0)
    assert np.allclose(_masked_gradient(field, d.mask, d.dx_star, 0)[d.mask], 0.0)


# --- analytic check: uniform rectangle reproduces Poiseuille R* -------------


def test_uniform_rectangle_poiseuille_r_star():
    d = _rect_domain()
    f = solve(d)
    # Discrete Poiseuille: linear p* between the pinned port columns gives
    # face flux 1/(nx-1) per row, so Q* = ny/(nx-1) and R* = (nx-1)/ny.
    assert np.isclose(f.r_star, (NX - 1) / NY, rtol=1e-6)
    row = f.p_star[NY // 2]
    assert np.allclose(row, 1.0 - np.arange(NX) / (NX - 1), atol=1e-9)
    # And the depth-averaged interior velocity is uniform.
    interior_ux = f.ux_star[NY // 2, 1 : NX - 1]
    assert np.allclose(interior_ux, interior_ux.mean(), rtol=1e-6)


# --- hole rasterisation invariants (ported from test_geometry.py) -----------


def test_overlapping_holes_take_max_depth():
    d = _rect_domain()
    holes = np.array([[0.5, 0.15, 1.0], [0.5, 0.15, 2.5]])
    depth = rasterize_holes(d.mask, holes, radius_star=0.05)
    assert np.isclose(depth.max(), 2.5)
    assert depth.max() <= 2.5 + 1e-9


def test_hole_outside_footprint_is_clipped():
    mask = np.ones((NY, NX), dtype=bool)
    mask[:5, :5] = False  # cut a corner
    depth = rasterize_holes(mask, np.array([[0.03, 0.03, 3.0]]), radius_star=0.06)
    assert (depth[~mask] == 0).all()


# --- nonuniformity invariants (ported from test_cost.py) --------------------

RECT = ((0.25, 0.75), (0.0, NY / NX))
BINS = (10, 4)


def _grid_points():
    xs = np.linspace(RECT[0][0] + 0.01, RECT[0][1] - 0.01, BINS[0])
    ys = np.linspace(RECT[1][0] + 0.01, RECT[1][1] - 0.01, BINS[1])
    gx, gy = np.meshgrid(xs, ys)
    return np.column_stack([gx.ravel(), gy.ravel()])


def test_uniform_spread_is_low_nonuniformity():
    nu, kept = nonuniformity(_grid_points(), RECT[0], RECT[1], BINS)
    assert nu < 0.05
    assert kept.shape[0] == BINS[0] * BINS[1]


def test_single_cluster_is_high_nonuniformity():
    nu, _ = nonuniformity(np.tile([0.5, 0.15], (40, 1)), RECT[0], RECT[1], BINS)
    assert nu > 0.95


def test_no_points_is_worst():
    nu, kept = nonuniformity(np.empty((0, 2)), RECT[0], RECT[1], BINS)
    assert nu == 1.0 and kept.shape[0] == 0


def test_points_outside_rect_are_dropped():
    pts = np.array([[0.05, 0.15], [0.95, 0.15]])
    nu, kept = nonuniformity(pts, RECT[0], RECT[1], BINS)
    assert kept.shape[0] == 0 and nu == 1.0


# --- tracer invariants (ported from test_tracker.py) ------------------------


def test_flat_channel_preserves_height():
    # Flat floor => w~ = 0 and no dispersion, so low-seeded particles cannot
    # reach the tracking plane.
    d = _rect_domain()
    res = trace(solve(d), _seed(d, z_hi=0.2), 150, d_v_star=6.0, seed_rng=1)
    assert res.breakthrough < 0.02
    assert res.exited.all()


def test_holes_increase_breakthrough():
    rng = np.random.default_rng(0)
    flat = trace(
        solve(_rect_domain()), _seed(_rect_domain()), 150, d_v_star=6.0, seed_rng=1
    ).breakthrough
    d = _rect_domain(holes=_holes(rng, 12, depth=3.0))
    drilled = trace(solve(d), _seed(d), 150, d_v_star=6.0, seed_rng=1).breakthrough
    assert drilled > flat


def test_resolved_termination_and_counts():
    rng = np.random.default_rng(0)
    d = _rect_domain(holes=_holes(rng, 10, depth=2.0))
    res = trace(solve(d), _seed(d), 150, d_v_star=6.0, seed_rng=1)
    # Terminates by resolving enough particles OR retiring all (stuck
    # detector), without ever reaching the safety cap; every particle is
    # accounted exactly once.
    assert res.steps_used < MAX_STEPS
    assert (
        int((res.broke | res.exited).sum()) + int((~res.broke & ~res.exited).sum())
        == 150
    )
    assert 0.0 <= res.breakthrough <= 1.0
    assert res.resolved_fraction >= EXIT_FRACTION - 1e-9 or res.trapped_fraction > 0
    assert np.isclose(res.trapped_fraction, 1.0 - res.resolved_fraction)
    # residence times: resolved in t*, trapped are NaN
    assert np.isfinite(res.residence[res.broke | res.exited]).all()
    assert np.isnan(res.residence[~res.broke & ~res.exited]).all()


def test_crossing_points_inside_domain_and_counted_once():
    rng = np.random.default_rng(0)
    d = _rect_domain(holes=_holes(rng, 10, depth=2.5))
    res = trace(solve(d), _seed(d), 150, d_v_star=6.0, seed_rng=2)
    pts = res.crossing_points()
    assert pts.shape[0] == int(res.broke.sum())
    assert np.isfinite(pts).all()
    assert (pts[:, 0] >= 0).all() and (pts[:, 0] <= 1.0).all()
    assert (pts[:, 1] >= 0).all() and (pts[:, 1] <= d.width_star).all()


def test_determinism_same_seed():
    rng = np.random.default_rng(0)
    d = _rect_domain(holes=_holes(rng, 1, depth=2.0))
    f = solve(d)
    a = trace(f, _seed(d), 120, d_v_star=6.0, seed_rng=3).breakthrough
    b = trace(f, _seed(d), 120, d_v_star=6.0, seed_rng=3).breakthrough
    assert a == b


def test_d_v_star_is_a_required_argument():
    # No hidden default: the regime card or the caller states it.
    sig = inspect.signature(trace)
    assert sig.parameters["d_v_star"].default is inspect.Parameter.empty


# --- frames recording (web-player contract) ---------------------------------


def test_record_frames_returns_positions_on_even_t_star():
    rng = np.random.default_rng(0)
    d = _rect_domain(holes=_holes(rng, 8, depth=2.0))
    res = trace(solve(d), _seed(d), 100, d_v_star=6.0, seed_rng=5, record_frames=8)
    assert res.frames is not None and res.frame_t is not None
    assert res.frames.shape == (8, 100, 3)
    assert res.frames.dtype == np.float32
    assert res.frame_t.shape == (8,)
    assert np.all(np.diff(res.frame_t) > 0)  # evenly spaced, increasing
    # frames[-1] matches the final positions; z* is a local-gap fraction.
    assert np.allclose(res.frames[-1], res.positions, atol=1e-6)
    assert (res.frames[:, :, 2] >= 0.0).all()
    assert (res.frames[:, :, 2] <= 1.0).all()


def test_no_frames_by_default():
    res = trace(solve(_rect_domain()), _seed(_rect_domain()), 50, 6.0, 7)
    assert res.frames is None and res.frame_t is None


# --- scale invariance --------------------------------------------------------


def test_same_shape_different_size_identical_reduced_results():
    domain = _rect_domain()
    f1 = solve(domain)
    f2 = solve(_rect_domain())  # a second, identical mask
    assert np.allclose(np.nan_to_num(f1.p_star), np.nan_to_num(f2.p_star), atol=0.0)
    assert np.allclose(f1.ux_star, f2.ux_star, atol=0.0)
    assert f1.r_star == f2.r_star

    # Same shape (h0/L = 0.01) at two very different sizes.
    card_mm = RegimeCard.from_physical(
        L=1e-3,
        h0=1e-5,
        u0=1e-2,
        mu=1e-3,
        rho=1000.0,
        particle_d=1e-6,
        rho_p=1100.0,
        molecular_d=1e-9,
        mean_free_path=3e-10,
    )
    card_half_m = RegimeCard.from_physical(
        L=0.5,
        h0=5e-3,
        u0=1e-2,
        mu=1e-3,
        rho=1000.0,
        particle_d=1e-6,
        rho_p=1100.0,
        molecular_d=1e-9,
        mean_free_path=3e-10,
    )
    assert card_mm.thinness.value == card_half_m.thinness.value
    assert card_mm.reynolds.value != card_half_m.reynolds.value
    assert card_mm.ok and not card_half_m.ok


# --- regime card --------------------------------------------------------------


def test_card_refuses_fast_wide_point_by_reynolds():
    # Spec worked point: water, h0 = 5 mm, L = 500 mm, U0 = 5 cm/s
    # -> Re_h . h0/L = 2.5, refused: 3-D Navier-Stokes needed.
    card = RegimeCard.from_physical(
        L=0.5,
        h0=5e-3,
        mu=1e-3,
        rho=1000.0,
        u0=0.05,
        particle_d=1e-6,
        rho_p=1100.0,
        molecular_d=1e-9,
        mean_free_path=3e-10,
    )
    assert np.isclose(card.reynolds.value, 2.5)
    assert not card.ok
    assert card.failures == ("reynolds",)
    assert card.thinness.ok and card.stokes.ok and card.peclet.ok


def test_card_accepts_thin_slow_point():
    # Spec worked point: water, h0 = 1 mm, L = 100 mm, U0 = 1 cm/s
    # -> Re_h . h0/L = 0.1, marginal, holds.
    card = RegimeCard.from_physical(
        L=0.1,
        h0=1e-3,
        mu=1e-3,
        rho=1000.0,
        u0=0.01,
        particle_d=1e-6,
        rho_p=1100.0,
        molecular_d=1e-9,
        mean_free_path=3e-10,
    )
    assert np.isclose(card.reynolds.value, 0.1)
    assert card.ok
    assert card.failures == ()


def test_card_dp_or_u0_gives_same_u0():
    a = RegimeCard.from_physical(L=0.1, h0=1e-3, mu=1e-3, rho=1000.0, u0=0.01)
    b = RegimeCard.from_physical(
        L=0.1, h0=1e-3, mu=1e-3, rho=1000.0, dp=12.0 * 1e-3 * 0.01 * 0.1 / 1e-6
    )
    assert np.isclose(a.u0, b.u0)
    assert np.isclose(a.reynolds.value, b.reynolds.value)


def test_gas_nm_gap_fails_knudsen():
    # 1 nm lumen with gas: Kn >> 1, routes to the slice-3 nano propagator.
    card = RegimeCard.from_physical(
        L=1e-4, h0=1e-9, mu=1.8e-5, rho=1.2, u0=1e-3, mean_free_path=1e-7
    )
    assert np.isclose(card.knudsen.value, 100.0)
    assert not card.ok
    assert "knudsen" in card.failures


def test_unsupplied_lines_are_not_applicable_not_failures():
    card = RegimeCard.from_physical(L=0.1, h0=1e-3, mu=1e-3, rho=1000.0, u0=0.01)
    assert card.ok
    assert not card.stokes.applicable
    assert not card.knudsen.applicable
    assert card.failures == ()


# --- kernel boundary (mirrors test_precis_chain_import_boundary.py) ---------

_FLOW_SRC = Path(__file__).resolve().parent.parent / "src" / "precis_flow"

_FORBIDDEN_PREFIXES = ("precis.", "precis_", "asa_bot", "asa_slack", "hexfold")
_SELF = "precis_flow"


def _is_forbidden(module: str | None) -> bool:
    if module is None:
        return False
    if module == _SELF or module.startswith(f"{_SELF}."):
        return False
    return module.startswith(_FORBIDDEN_PREFIXES) or module == "precis"


def test_precis_flow_imports_nothing_first_party() -> None:
    offenders: list[str] = []
    for path in sorted(_FLOW_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_forbidden(alias.name):
                        offenders.append(f"{path}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue
                if _is_forbidden(node.module):
                    offenders.append(f"{path}: from {node.module} import ...")
    assert not offenders, (
        "src/precis_flow must import nothing first-party (numpy/scipy kernel "
        f"boundary, docs/backlog/se-flow-kernel.md): {offenders}"
    )


def test_precis_flow_third_party_imports_are_numpy_scipy_only() -> None:
    allowed = {"numpy", "scipy"}
    extras: set[str] = set()

    for path in sorted(_FLOW_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                roots = [(node.module or "").split(".")[0]]
            else:
                continue
            for root in roots:
                if not root or root == _SELF:
                    continue
                if root not in sys.stdlib_module_names and root not in allowed:
                    extras.add(root)
    assert not extras, f"precis_flow gained a non-numpy/scipy dep: {sorted(extras)}"


def test_public_api_is_exported():
    for name in (
        "Domain",
        "Ports",
        "Field",
        "Paths",
        "Seed",
        "RegimeCard",
        "RegimeLine",
        "solve",
        "trace",
        "nonuniformity",
        "rasterize_holes",
    ):
        assert hasattr(precis_flow, name)
