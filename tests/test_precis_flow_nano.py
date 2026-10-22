"""precis_flow.nano — the slice-3 acceptance tests of
docs/backlog/se-flow-kernel.md: free-molecular test-particle Monte
Carlo with diffuse wall reflection, on the Clausing cylinder and on a
hexfold lumen's atom-site wall.
"""

from __future__ import annotations

import numpy as np
import pytest
from scipy import stats as scipy_stats

from hexfold.build import build as hexfold_build
from hexfold.lattice import tube_radius
from precis_flow import AtomWall, CylinderLumen, RegimeCard, propagate
from precis_flow.nano import _cosine_dirs

#: carbon's Bondi vdW radius (Å) — the wall sphere radius for graphene
R_VDW_C = 1.70


def _tube_sites(n: int, length: int) -> tuple[np.ndarray, float]:
    """``tube(n, n, len=length)`` from the hexfold generator: the atom
    sites (Å) and the wall radius ``R_axis`` (Å)."""
    net = hexfold_build(f"hexfold 0.1\norigin t\nt: tube({n},{n}, len={length})\n")
    assert net.seed3 is not None
    return np.asarray(net.seed3, dtype=float), tube_radius(n, n)


def _hex_wall(n: int, length: int, scale: float = 1.0) -> AtomWall:
    pos, _r_axis = _tube_sites(n, length)
    return AtomWall.from_atoms(pos * scale, np.full(pos.shape[0], R_VDW_C) * scale)


def test_clausing_cylinder() -> None:
    """Clausing: a straight cylinder with diffuse walls (alpha = 1) at
    n = 10^5 transmits 0.6720 / 0.5136 / 0.1910 at L/R = 1 / 2 / 10,
    within ±0.005 absolute (3σ ≈ 0.0045), and transmission falls
    monotonically with L/R."""
    got = []
    for lr, want in ((1, 0.6720), (2, 0.5136), (10, 0.1910)):
        paths = propagate(CylinderLumen(1.0 / lr), 100_000, 1.0, seed_rng=0)
        assert paths.trapped_fraction == 0.0
        assert paths.breakthrough == pytest.approx(want, abs=0.005)
        got.append(paths.breakthrough)
    assert got == sorted(got, reverse=True)


def test_long_tube_asymptote() -> None:
    """For L/R ≥ 50 transmission approaches 8R/3L from below."""
    paths = propagate(CylinderLumen(1.0 / 50), 20_000, 1.0, seed_rng=0)
    asymp = 8.0 / (3.0 * 50)
    t = paths.breakthrough
    assert t < asymp  # approaches from below
    assert t / asymp > 0.85  # and is close to it


def test_specular_limit() -> None:
    """alpha = 0 on a straight tube gives transmission exactly 1."""
    paths = propagate(CylinderLumen(0.1), 2_000, 0.0, seed_rng=0)
    assert paths.breakthrough == 1.0
    assert paths.broke.all()


def test_cosine_law() -> None:
    """Re-emitted directions from a diffuse wall pass χ² against cos θ
    (p > 0.01, 10⁵ samples)."""
    rng = np.random.default_rng(1)
    dirs = _cosine_dirs(rng, np.tile([1.0, 0.0, 0.0], (100_000, 1)))
    cos_t = dirs[:, 0]  # angle to the inward normal
    assert cos_t.min() >= 0.0 and cos_t.max() <= 1.0
    bins = 20
    counts, edges = np.histogram(cos_t, bins=bins, range=(0.0, 1.0))
    # pdf(μ) = 2μ on [0,1] → expected ∝ μ_hi² - μ_lo²
    exp = 100_000 * (edges[1:] ** 2 - edges[:-1] ** 2)
    chi2, p = scipy_stats.chisquare(counts, exp)
    assert p > 0.01, f"cosine law χ² p = {p:.4g} (χ² = {chi2:.1f})"


def test_equilibrium_closed_lumen() -> None:
    """A closed lumen run long gives a uniform number density per
    accessible volume bin (χ², p > 0.01), with no build-up at the
    walls."""
    wall = CylinderLumen(0.3, closed=True)
    paths = propagate(
        wall, 2_000, 1.0, seed_rng=0, record_frames=100, max_flights=2_000
    )
    assert paths.frames is not None and paths.frame_t is not None
    # iid samples across the swarm at a few late frame times (well past
    # burn-in; samples within a particle's time series are correlated,
    # so thin widely rather than take every frame)
    fr = paths.frames[-10::5]  # 2 frames × 2000 particles
    pts = np.empty((fr.shape[0] * fr.shape[1], 3))
    pts[:, :2] = fr.reshape(-1, 3)[:, :2]
    pts[:, 2] = fr.reshape(-1, 3)[:, 2] * 2 * wall.radius_star - wall.radius_star
    # bins: 4 axial slabs × 5 radial shells, volume-weighted
    nx_bin, nr_bin = 4, 5
    r = np.hypot(pts[:, 1], pts[:, 2]) / wall.radius_star  # r/R in [0,1]
    x = np.clip(pts[:, 0], 0.0, 1.0)
    edges_r2 = np.linspace(0.0, 1.0, nr_bin + 1)  # equal-area shells
    counts = np.zeros((nx_bin, nr_bin))
    xi = np.clip((x * nx_bin).astype(int), 0, nx_bin - 1)
    ri = np.clip(np.searchsorted(edges_r2, r * r, side="right") - 1, 0, nr_bin - 1)
    np.add.at(counts, (xi, ri), 1)
    exp = np.full((nx_bin, nr_bin), pts.shape[0] / (nx_bin * nr_bin))
    chi2, p = scipy_stats.chisquare(counts.ravel(), exp.ravel())
    assert p > 0.01, f"equilibrium χ² p = {p:.4g} (χ² = {chi2:.1f})"
    # no wall build-up: outermost shell's density matches the mean
    dens = counts.sum(axis=0) / counts.sum()
    assert dens[-1] == pytest.approx(1.0 / nr_bin, rel=0.3)


def test_size_invariance() -> None:
    """The same shape at two absolute scales with the same seed gives
    identical transmission and identical t* residences — the
    dimension-free claim, bitwise at an exactly-representable scale
    factor."""
    w1 = _hex_wall(6, 8, scale=1.0)
    w4 = _hex_wall(6, 8, scale=4.0)
    p1 = propagate(w1, 300, 1.0, seed_rng=7)
    p4 = propagate(w4, 300, 1.0, seed_rng=7)
    assert p4.breakthrough == p1.breakthrough
    np.testing.assert_array_equal(p4.residence, p1.residence)
    np.testing.assert_array_equal(p4.positions, p1.positions)


def test_hexfold_lumen_corrugation() -> None:
    """A tube from the hexfold generator, wall = the atom sites as vdW
    spheres: transmission within 10 % of the smooth-cylinder Clausing
    value at R_eff = R_axis − r_vdW and the same L/R_eff.

    Measured deviation (the corrugation effect): a (8,8) tube of 8
    periods (R_axis ≈ 5.43 Å, R_eff ≈ 3.73 Å, L/R_eff ≈ 5.3) transmits
    ~0.27 vs the smooth cylinder's ~0.29 — the vdW corrugation costs
    ~7 % transmission at alpha = 1."""
    wall = _hex_wall(8, 8)
    n = 3_000
    lumen = propagate(wall, n, 1.0, seed_rng=0)
    cyl = propagate(CylinderLumen(wall.r_eff), n, 1.0, seed_rng=0)
    assert lumen.trapped_fraction < 0.02  # no leaks through the wall
    dev = (cyl.breakthrough - lumen.breakthrough) / cyl.breakthrough
    assert abs(dev) <= 0.10, (
        f"lumen {lumen.breakthrough:.3f} vs cylinder "
        f"{cyl.breakthrough:.3f} at R_eff — deviation {dev:.1%}"
    )


def test_paths_contract_and_determinism() -> None:
    """The propagator returns the trace Paths contract — transmission
    as breakthrough, t* residences, the (K, n, 3) frames blob — and a
    repeated seed_rng gives bitwise-equal Paths."""
    wall = _hex_wall(6, 8)
    a = propagate(wall, 300, 1.0, seed_rng=3, record_frames=6)
    assert 0.0 < a.breakthrough <= 1.0
    assert a.frames is not None and a.frames.shape == (6, 300, 3)
    assert a.frame_t is not None and len(a.frame_t) == 6
    assert a.residence[a.broke].size > 0
    assert np.isnan(a.residence[~a.broke & ~a.exited]).all()
    psi = a.frames[:, :, 2]
    assert psi.min() >= 0.0 and psi.max() <= 1.0
    b = propagate(wall, 300, 1.0, seed_rng=3, record_frames=6)
    np.testing.assert_array_equal(a.residence, b.residence)
    np.testing.assert_array_equal(a.positions, b.positions)
    np.testing.assert_array_equal(a.frames, b.frames)


def test_regime_card_routes() -> None:
    """Kn ≥ 10 names knudsen; 0.01 < Kn < 10 refuses naming DSMC;
    a holding card routes continuum."""
    phys = dict(L=1e-8, h0=1e-9, mu=2e-5, rho=0.7, u0=300.0)
    card = RegimeCard.from_physical(**phys, mean_free_path=5e-8)  # Kn = 50
    assert card.route == "knudsen"
    card = RegimeCard.from_physical(**phys, mean_free_path=1e-9)  # Kn = 1
    with pytest.raises(ValueError, match="DSMC"):
        _ = card.route
    card = RegimeCard.from_physical(  # Kn = 5e-3, continuum holds
        L=0.1, h0=1e-3, mu=1e-3, rho=1000.0, u0=0.01, mean_free_path=5e-6
    )
    assert card.route == "continuum"


def test_alpha_and_n_validated() -> None:
    with pytest.raises(ValueError, match="alpha"):
        propagate(CylinderLumen(0.5), 10, 1.5, seed_rng=0)
    with pytest.raises(ValueError, match="n"):
        propagate(CylinderLumen(0.5), 0, 1.0, seed_rng=0)
