"""Canonical molecular body frame: invariance, kinds, determinism."""

from __future__ import annotations

import numpy as np
import pytest

from precis.cad.vec import pose
from precis_se.properties.body_frame import (
    BodyFrame,
    canonical_body_frame,
    euler_xyz_deg,
    relative_rotation,
)

_T = np.array(
    [[1, 1, 1], [1, -1, -1], [-1, 1, -1], [-1, -1, 1]], dtype=float
) / np.sqrt(3.0)

MOLS: dict[str, tuple[list[str], np.ndarray]] = {
    "NO": (["N", "O"], np.array([[0, 0, 0], [1.155, 0, 0]], dtype=float)),
    "CO2": (
        ["O", "C", "O"],
        np.array([[-1.16, 0, 0], [0, 0, 0], [1.16, 0, 0]], dtype=float),
    ),
    "H2O": (
        ["O", "H", "H"],
        np.array([[0, 0, 0.117], [0, 0.757, -0.469], [0, -0.757, -0.469]]),
    ),
    "NH3": (
        ["N", "H", "H", "H"],
        np.array(
            [
                [0, 0, 0.116],
                [0.939, 0, -0.271],
                [-0.4697, 0.8135, -0.271],
                [-0.4697, -0.8135, -0.271],
            ]
        ),
    ),
    "CH4": (["C", "H", "H", "H", "H"], np.vstack([np.zeros(3), 1.089 * _T])),
    "CHFClBr": (
        ["C", "H", "F", "Cl", "Br"],
        np.vstack(
            [np.zeros(3), 1.09 * _T[0], 1.39 * _T[1], 1.77 * _T[2], 1.94 * _T[3]]
        ),
    ),
}


def _random_pose(seed: int, xyz: np.ndarray) -> np.ndarray:
    rng = np.random.default_rng(seed)
    xf = pose(rng.uniform(-5, 5, 3), rng.uniform(-3, 3, 3))
    return xyz @ xf.R.T + xf.t


def _same_atom_sets(
    sym: list[str], a: np.ndarray, b: np.ndarray, tol: float = 1e-6
) -> bool:
    used: set[int] = set()
    for i in range(len(sym)):
        hit = next(
            (
                j
                for j in range(len(sym))
                if j not in used
                and sym[j] == sym[i]
                and np.linalg.norm(a[i] - b[j]) < tol
            ),
            None,
        )
        if hit is None:
            return False
        used.add(hit)
    return True


@pytest.mark.parametrize("name", list(MOLS))
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_body_coordinates_invariant_under_rigid_motion(name: str, seed: int) -> None:
    sym, xyz = MOLS[name]
    ref = canonical_body_frame(sym, xyz)
    moved = _random_pose(seed, xyz)
    fr = canonical_body_frame(sym, moved)
    assert fr.kind == ref.kind
    assert np.allclose(fr.axes @ fr.axes.T, np.eye(3), atol=1e-9)
    assert np.linalg.det(fr.axes) == pytest.approx(1.0)
    assert _same_atom_sets(sym, ref.to_body(xyz), fr.to_body(moved))


def test_kinds() -> None:
    kinds = {n: canonical_body_frame(*MOLS[n]).kind for n in MOLS}
    assert kinds["NO"] == "linear"
    assert kinds["CO2"] == "linear"
    assert kinds["H2O"] == "asymmetric"
    assert kinds["NH3"] == "symmetric_top"
    assert kinds["CH4"] == "spherical"
    assert kinds["CHFClBr"] == "asymmetric"


def test_h2o_z_is_plane_normal() -> None:
    sym, xyz = MOLS["H2O"]
    fr = canonical_body_frame(sym, _random_pose(7, xyz))
    moved = _random_pose(7, xyz)
    normal = np.cross(moved[1] - moved[0], moved[2] - moved[0])
    normal /= np.linalg.norm(normal)
    assert abs(float(fr.axes[2] @ normal)) == pytest.approx(1.0)


@pytest.mark.parametrize("name", ["H2O", "NH3", "CH4", "CHFClBr", "CO2"])
def test_atom_order_permutation_gives_same_frame_atoms(name: str) -> None:
    sym, xyz = MOLS[name]
    perm = np.random.default_rng(5).permutation(len(sym))
    psym = [sym[i] for i in perm]
    a = canonical_body_frame(sym, xyz)
    b = canonical_body_frame(psym, xyz[perm])
    assert a.kind == b.kind
    assert _same_atom_sets(psym, a.to_body(xyz)[perm], b.to_body(xyz[perm]))
    if name == "CHFClBr":  # no symmetry: frames identical, not just equivalent
        assert np.allclose(a.axes, b.axes, atol=1e-9)


def test_ch4_is_deterministic() -> None:
    sym, xyz = MOLS["CH4"]
    a = canonical_body_frame(sym, xyz)
    b = canonical_body_frame(sym, xyz)
    assert np.array_equal(a.axes, b.axes)
    assert isinstance(a, BodyFrame)


def test_euler_round_trip_and_relative_rotation() -> None:
    rot = np.array([0.3, -0.5, 1.1])
    R = pose(np.zeros(3), rot).R
    assert np.allclose(np.radians(euler_xyz_deg(R)), rot, atol=1e-9)
    fr = BodyFrame(origin=np.zeros(3), axes=R.T, kind="asymmetric")
    assert np.allclose(np.radians(relative_rotation(fr)), rot, atol=1e-9)
    assert relative_rotation(fr, R.T) == pytest.approx((0.0, 0.0, 0.0), abs=1e-9)
