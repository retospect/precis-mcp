"""Spring + umbrella FIRE relaxation (precis_surface.relax)."""

from __future__ import annotations

import math

import numpy as np
from numpy.typing import NDArray

from precis_surface import relax


def _patch(scale: float = 1.0) -> tuple[NDArray[np.float64], NDArray[np.int64]]:
    """A planar coronene-like patch: a central hexagon ring plus the six
    hexagons around it, bond length 1.42 * scale."""
    a = 1.42 * scale
    pts: dict[tuple[int, int], int] = {}
    atoms: list[list[float]] = []
    bonds: set[tuple[int, int]] = set()

    def vid(p: tuple[float, float]) -> int:
        key = (round(p[0] * 1000), round(p[1] * 1000))
        if key not in pts:
            pts[key] = len(atoms)
            atoms.append([p[0], p[1], 0.0])
        return pts[key]

    centres = [(0.0, 0.0)] + [
        (
            math.sqrt(3.0) * a * math.cos(math.radians(60 * k + 30)),
            math.sqrt(3.0) * a * math.sin(math.radians(60 * k + 30)),
        )
        for k in range(6)
    ]
    for cx, cy in centres:
        ring = [
            vid(
                (
                    cx + a * math.cos(math.radians(60 * k)),
                    cy + a * math.sin(math.radians(60 * k)),
                )
            )
            for k in range(6)
        ]
        for k in range(6):
            i, j = ring[k], ring[(k + 1) % 6]
            bonds.add((min(i, j), max(i, j)))
    return np.asarray(atoms), np.asarray(sorted(bonds), dtype=np.int64)


def _lengths(x: NDArray[np.float64], b: NDArray[np.int64]) -> NDArray[np.float64]:
    return np.linalg.norm(x[b[:, 0]] - x[b[:, 1]], axis=1)


def test_distorted_patch_returns_to_graphene_bonds() -> None:
    atoms, bonds = _patch()
    rng = np.random.default_rng(0)
    bent = atoms + rng.normal(scale=0.15, size=atoms.shape)
    assert np.abs(_lengths(bent, bonds) - 1.42).max() > 0.15
    x, info = relax.relax_net(bent, bonds, max_steps=6000)
    assert info["converged"]
    # a relaxed finite patch keeps its interior at 1.42; the rim contracts a
    # little (2-coordinated atoms have no umbrella), so bound the whole patch
    length = _lengths(x, bonds)
    assert abs(float(length.mean()) - 1.42) < 0.03
    assert length.min() > 1.36 and length.max() < 1.48
    assert float(relax.theta_p_deg(x, bonds).max()) < 2.0


def test_gradient_matches_finite_differences() -> None:
    atoms, bonds = _patch()
    rng = np.random.default_rng(1)
    x = atoms + rng.normal(scale=0.1, size=atoms.shape)
    nbr = relax._neighbours(len(x), bonds)
    p13 = relax._pairs_13(nbr)
    centres, around = relax._umbrella_sets(nbr)
    assert len(centres) > 0 and len(p13) > 0

    def ef(pos: NDArray[np.float64]) -> tuple[float, NDArray[np.float64]]:
        return relax.energy_grad(pos, bonds, p13, centres, around)

    _e, g = ef(x)
    h = 1e-6
    worst = 0.0
    for i in (0, 5, len(x) - 1):
        for c in range(3):
            xp, xm = x.copy(), x.copy()
            xp[i, c] += h
            xm[i, c] -= h
            fd = (ef(xp)[0] - ef(xm)[0]) / (2 * h)
            worst = max(worst, abs(fd - g[i, c]))
    assert worst < 1e-6


def test_theta_p_of_planar_net_is_zero_and_of_a_pyramid_is_not() -> None:
    atoms, bonds = _patch()
    assert float(relax.theta_p_deg(atoms, bonds).max()) < 1e-6
    # lift one 3-coordinated atom out of the plane of its neighbours
    nbr = relax._neighbours(len(atoms), bonds)
    centre = next(a for a, ns in enumerate(nbr) if len(ns) == 3)
    lifted = atoms.copy()
    lifted[centre, 2] += 0.5
    assert float(relax.theta_p_deg(lifted, bonds).max()) > 10.0


def test_per_atom_strain_is_nan_off_the_three_coordinated_atoms() -> None:
    """The viewer's angle-strain layers index by atom, so the per-atom
    forms keep every atom: NaN for the patch's 2-coordinated rim, a value
    for each 3-coordinated atom, and theta_p_deg is exactly the non-NaN
    part."""
    atoms, bonds = _patch()
    nbr = relax._neighbours(len(atoms), bonds)
    three = np.array([len(ns) == 3 for ns in nbr])
    assert 0 < three.sum() < len(atoms)
    for per_atom in (relax.theta_p_by_atom, relax.angle_dev_by_atom):
        vals = per_atom(atoms, bonds)
        assert vals.shape == (len(atoms),)
        assert np.array_equal(np.isnan(vals), ~three)
    tp = relax.theta_p_by_atom(atoms, bonds)
    assert np.array_equal(relax.theta_p_deg(atoms, bonds), tp[~np.isnan(tp)])


def test_angle_dev_is_zero_on_flat_graphene_and_sees_in_plane_shear() -> None:
    atoms, bonds = _patch()
    dev = relax.angle_dev_by_atom(atoms, bonds)
    assert float(np.nanmax(dev)) < 1e-6
    # shear in the plane: theta_p stays 0, the 120-degree measure does not
    sheared = atoms.copy()
    sheared[:, 0] += 0.3 * sheared[:, 1]
    assert float(np.nanmax(relax.theta_p_by_atom(sheared, bonds))) < 1e-6
    assert float(np.nanmax(relax.angle_dev_by_atom(sheared, bonds))) > 5.0


def test_surface_tether_gradient_and_pull() -> None:
    atoms, bonds = _patch()
    rng = np.random.default_rng(2)
    x = atoms + rng.normal(scale=0.2, size=atoms.shape)
    curve = np.array([[0.0, 0.0], [10.0, 0.0]])  # the z = 0 plane
    foot = relax._surface_foot(x, curve)
    assert np.allclose(foot[0][:, 1], 0.0) and np.allclose(np.abs(foot[1][:, 1]), 1.0)
    nbr = relax._neighbours(len(x), bonds)
    p13 = relax._pairs_13(nbr)
    centres, around = relax._umbrella_sets(nbr)

    def ef(pos: NDArray[np.float64]) -> tuple[float, NDArray[np.float64]]:
        return relax.energy_grad(
            pos, bonds, p13, centres, around, foot=foot, k_surface=0.5
        )

    _e, g = ef(x)
    h = 1e-6
    for i in (1, 7):
        for c in range(3):
            xp, xm = x.copy(), x.copy()
            xp[i, c] += h
            xm[i, c] -= h
            assert abs((ef(xp)[0] - ef(xm)[0]) / (2 * h) - g[i, c]) < 1e-6
    # a strong tether keeps a perturbed patch on the plane
    y, _info = relax.relax_net(x, bonds, surface=curve, k_surface=5.0, max_steps=6000)
    assert float(np.abs(y[:, 2]).max()) < 0.05
