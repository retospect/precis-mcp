"""precis_chain.envelope — capsule splitting and the CAD pose.

Theorems:

- A straight path of length ``L`` at ``max_seg_len = L/4`` gives exactly 4
  capsules, each of length ``L/4``, and every path sample lies inside their
  union.
- A circular arc of bend radius ``R`` split at ``max_turn_rad = 5 deg`` puts
  every path sample within ``R (1 - cos 2.5 deg)`` of its capsule's chord — the
  sagitta of a 5-degree chord — plus the sampled polyline's own inscribed error,
  which the test computes explicitly rather than hiding in a fudge factor.
- ``capsule_pose``'s Euler triple, recomposed as ``Rz @ Ry @ Rx``, maps local
  ``+z`` onto the capsule axis, and its origin is the capsule's ``a`` end.
"""

from __future__ import annotations

import itertools
import math

import numpy as np
import pytest

from precis_chain.clash import segment_distance
from precis_chain.envelope import Capsule, capsule_pose, capsules_along, total_turning
from precis_chain.path import polyline

_L = 8.0


def _straight(n: int = 99) -> np.ndarray:
    return np.stack([np.linspace(0.0, _L, n), np.zeros(n), np.zeros(n)], axis=1)


def _arc(radius: float, sweep: float, n: int = 400) -> np.ndarray:
    t = np.linspace(0.0, sweep, n)
    return np.stack([radius * np.cos(t), radius * np.sin(t), np.zeros_like(t)], axis=1)


def _rot_zyx(rx: float, ry: float, rz: float) -> np.ndarray:
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    r_x = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]], dtype=float)
    r_y = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]], dtype=float)
    r_z = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]], dtype=float)
    return r_z @ r_y @ r_x


def test_straight_path_splits_into_exactly_four_capsules() -> None:
    path = polyline(_straight())
    caps = capsules_along(path, 1.0, _L / 4.0)
    assert len(caps) == 4
    for cap in caps:
        assert cap.length == pytest.approx(_L / 4.0)
        assert cap.r == 1.0
    # Consecutive capsules share an endpoint exactly.
    for lo, hi in itertools.pairwise(caps):
        assert np.allclose(lo.b, hi.a)
    assert np.allclose(caps[0].a, path.points[0])
    assert np.allclose(caps[-1].b, path.points[-1])


def test_every_path_sample_is_inside_the_capsule_union() -> None:
    path = polyline(_straight())
    caps = capsules_along(path, 0.5, _L / 4.0)
    for sample in path.points:
        nearest = min(
            float(segment_distance(sample, sample, cap.a, cap.b)[0]) for cap in caps
        )
        assert nearest <= 0.5 + 1e-12


def test_sampling_density_does_not_change_the_capsule_count() -> None:
    for n in (5, 33, 99, 1000):
        caps = capsules_along(polyline(_straight(n)), 1.0, _L / 4.0)
        assert len(caps) == 4


def test_arc_sagitta_is_bounded_by_the_turn_limit() -> None:
    radius, sweep = 10.0, math.pi / 2.0
    n = 400
    path = polyline(_arc(radius, sweep, n))
    max_turn = math.radians(5.0)
    caps = capsules_along(path, 1.0, 1e9, max_turn)
    assert len(caps) == math.ceil(sweep / max_turn)
    bound = radius * (1.0 - math.cos(max_turn / 2.0))
    # The sampled polyline is itself inscribed in the true arc, so its own
    # sagitta over one sample step rides on top of the capsule's.
    sample_sag = radius * (1.0 - math.cos(sweep / (n - 1) / 2.0))
    # Every sample is within the sagitta of SOME capsule's chord — the claim
    # that makes the capsule chain a faithful stand-in for the curve.
    per_capsule = np.stack(
        [
            segment_distance(
                path.points,
                path.points,
                np.tile(cap.a, (len(path), 1)),
                np.tile(cap.b, (len(path), 1)),
            )
            for cap in caps
        ]
    )
    worst = float(per_capsule.min(axis=0).max())
    assert worst > 0.0
    assert worst <= bound + 2.0 * sample_sag


def test_max_seg_len_and_max_turn_both_bind() -> None:
    path = polyline(_arc(10.0, math.pi / 2.0))
    length_only = capsules_along(path, 1.0, path.length / 3.0)
    assert len(length_only) == 3
    both = capsules_along(path, 1.0, path.length / 3.0, math.radians(5.0))
    assert len(both) == math.ceil((math.pi / 2.0) / math.radians(5.0))


def test_total_turning_is_zero_straight_and_the_sweep_on_an_arc() -> None:
    assert total_turning(polyline(_straight())) == pytest.approx(0.0, abs=1e-12)
    path = polyline(_arc(4.0, math.pi, 1000))
    assert total_turning(path) == pytest.approx(math.pi, rel=2e-3)


def test_capsule_pose_origin_is_the_a_end_and_local_z_is_the_axis() -> None:
    for a, b in (
        (np.array([1.0, 2.0, 3.0]), np.array([1.0, 2.0, 7.0])),
        (np.array([0.0, 0.0, 0.0]), np.array([3.0, 4.0, 12.0])),
        (np.array([-2.0, 5.0, 1.0]), np.array([-2.0, -5.0, 1.0])),
    ):
        cap = Capsule(a, b, 0.5)
        origin, euler, length = capsule_pose(cap)
        assert np.allclose(origin, a)
        assert length == pytest.approx(float(np.linalg.norm(b - a)))
        rot = _rot_zyx(*euler)
        assert np.allclose(rot @ np.array([0.0, 0.0, length]), b - a, atol=1e-9)
        assert np.allclose(rot.T @ rot, np.eye(3), atol=1e-12)


def test_zero_length_capsule_poses_as_the_identity() -> None:
    cap = Capsule(np.array([1.0, 1.0, 1.0]), np.array([1.0, 1.0, 1.0]), 0.7)
    origin, euler, length = capsule_pose(cap)
    assert length == 0.0
    assert np.allclose(origin, [1.0, 1.0, 1.0])
    assert np.allclose(_rot_zyx(*euler), np.eye(3))


def test_capsule_and_split_validation() -> None:
    path = polyline(_straight())
    with pytest.raises(ValueError, match="max_seg_len"):
        capsules_along(path, 1.0, 0.0)
    with pytest.raises(ValueError, match="max_turn_rad"):
        capsules_along(path, 1.0, 1.0, 0.0)
    with pytest.raises(ValueError, match="radius must be"):
        capsules_along(path, -1.0, 1.0)
    with pytest.raises(ValueError, match=">= 2 samples"):
        capsules_along(polyline(np.zeros((1, 3))), 1.0, 1.0)
    with pytest.raises(ValueError, match="non-zero arc length"):
        capsules_along(polyline(np.zeros((4, 3))), 1.0, 1.0)
    with pytest.raises(ValueError, match="Capsule.r"):
        Capsule(np.zeros(3), np.ones(3), -1.0)
