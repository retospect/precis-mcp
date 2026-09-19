"""``precis.cad.vec`` — the float64 vec3 + rigid-transform kernel.

Covers :func:`~precis.cad.vec.axis_angle_from_matrix` (R2, docs/backlog/
port-rotation-and-lever-composition.md): the no-rotation and near-π
degenerate regimes named in its docstring, plus a generic round-trip
through :func:`~precis.cad.vec.rotation`.
"""

from __future__ import annotations

import math

import numpy as np

from precis.cad.vec import axis_angle_from_matrix, rotation


def test_identity_has_no_axis() -> None:
    axis, angle = axis_angle_from_matrix(np.eye(3))
    assert axis is None
    assert angle == 0.0


def test_ninety_degrees_about_z() -> None:
    R = rotation(0.0, 0.0, math.pi / 2.0).R
    axis, angle = axis_angle_from_matrix(R)
    assert axis is not None
    assert math.isclose(angle, math.pi / 2.0, abs_tol=1e-9)
    assert math.isclose(axis[0], 0.0, abs_tol=1e-9)
    assert math.isclose(axis[1], 0.0, abs_tol=1e-9)
    assert math.isclose(axis[2], 1.0, abs_tol=1e-9)


def test_one_eighty_degrees_about_x_hits_the_degenerate_branch() -> None:
    R = rotation(math.pi, 0.0, 0.0).R
    axis, angle = axis_angle_from_matrix(R)
    assert axis is not None
    assert math.isclose(angle, math.pi, abs_tol=1e-6)
    # The x-axis, sign made consistent (first nonzero component positive)
    # since a pure-π rotation's antisymmetric part is ~zero.
    assert axis[0] > 0.9
    assert math.isclose(axis[1], 0.0, abs_tol=1e-6)
    assert math.isclose(axis[2], 0.0, abs_tol=1e-6)


def test_generic_rotation_round_trips_through_rotation() -> None:
    rx, ry, rz = 0.3, -0.7, 1.1
    R = rotation(rx, ry, rz).R
    axis, angle = axis_angle_from_matrix(R)
    assert axis is not None
    axis_arr = np.asarray(axis, dtype=np.float64)
    assert math.isclose(float(np.linalg.norm(axis_arr)), 1.0, abs_tol=1e-9)
    # The axis is R's own fixed point.
    assert np.allclose(R @ axis_arr, axis_arr, atol=1e-9)
    # Rodrigues' formula, rebuilt from (axis, angle), reproduces R exactly.
    k = axis_arr
    K = np.array(
        [[0.0, -k[2], k[1]], [k[2], 0.0, -k[0]], [-k[1], k[0], 0.0]],
        dtype=np.float64,
    )
    rebuilt = np.eye(3) + math.sin(angle) * K + (1.0 - math.cos(angle)) * (K @ K)
    assert np.allclose(rebuilt, R, atol=1e-9)


def test_near_pi_within_tolerance_uses_the_degenerate_branch() -> None:
    # pi - 5e-7 is inside the 1e-6 window around pi.
    R = rotation(0.0, math.pi - 5e-7, 0.0).R
    axis, angle = axis_angle_from_matrix(R)
    assert axis is not None
    assert math.isclose(angle, math.pi, abs_tol=1e-5)
    assert axis[1] > 0.9  # about +y, sign following the tiny antisymmetric part
