"""precis_chain.frames — RMF holonomy, planar-curve exactness, twist totals.

The three theorems, all recomputed from the returned ``(N, 3, 3)`` arrays:

- On a helix ``(r cos t, r sin t, c t)`` the rotation-minimizing frame comes
  back from one full turn rolled by ``-2 pi c / sqrt(r^2 + c^2)`` relative to
  where it started — the negative of the Frenet frame's torsion integral, since
  the RMF is exactly the Frenet frame with the torsional roll removed. Wrapped
  into ``[0, 2 pi)`` that is ``2 pi (1 - c / sqrt(r^2 + c^2))``.
- On a **planar** curve there is no torsion, so start-to-end twist is zero —
  and the double-reflection step maps the plane to itself exactly, so it is zero
  to machine precision, not just to the sampling's accuracy.
- Twist accumulated per step sums to the authored total (two turns over 21
  units at 34.286 deg, three turns over 32 at 33.75 deg); a single start-to-end
  reading cannot, because it only ever sees the residue mod ``2 pi``.
"""

from __future__ import annotations

import itertools
import math

import numpy as np
import pytest

from precis_chain.frames import (
    accumulated_twist,
    apply_twist,
    rmf_double_reflection,
    twist_between,
)
from precis_chain.path import polyline

_HELIX_R = 1.0
_HELIX_C = 0.2


def _helix(samples_per_turn: int, turns: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """Analytic helix points and unit tangents — analytic so the test measures
    the frame propagation, not a finite-difference tangent."""
    n = round(samples_per_turn * turns)
    t = np.linspace(0.0, 2.0 * math.pi * turns, n + 1)
    points = np.stack(
        [_HELIX_R * np.cos(t), _HELIX_R * np.sin(t), _HELIX_C * t], axis=1
    )
    tangents = np.stack(
        [-_HELIX_R * np.sin(t), _HELIX_R * np.cos(t), np.full_like(t, _HELIX_C)], axis=1
    )
    return points, tangents / np.linalg.norm(tangents, axis=1)[:, None]


def _planar_s_curve(
    n: int, amplitude: float = 2.0, length: float = 10.0
) -> tuple[np.ndarray, np.ndarray]:
    """Points and the exact in-plane normals of ``y = A sin(2 pi x / L)``."""
    x = np.linspace(0.0, length, n + 1)
    k = 2.0 * math.pi / length
    points = np.stack([x, amplitude * np.sin(k * x), np.zeros_like(x)], axis=1)
    tangents = np.stack(
        [np.ones_like(x), amplitude * k * np.cos(k * x), np.zeros_like(x)], axis=1
    )
    tangents /= np.linalg.norm(tangents, axis=1)[:, None]
    normals = np.stack([-tangents[:, 1], tangents[:, 0], np.zeros_like(x)], axis=1)
    return points, normals


def test_frames_are_proper_orthonormal_with_columns_t_n_b() -> None:
    points, tangents = _helix(64)
    frames = rmf_double_reflection(points, tangents, np.array([0.0, 0.0, 1.0]))
    assert frames.shape == (65, 3, 3)
    assert np.allclose(frames[:, :, 0], tangents, atol=1e-12)
    for f in frames:
        assert np.allclose(f.T @ f, np.eye(3), atol=1e-12)
        assert np.linalg.det(f) == pytest.approx(1.0, abs=1e-12)
        assert np.allclose(np.cross(f[:, 0], f[:, 1]), f[:, 2], atol=1e-12)


def test_rmf_holonomy_over_one_helix_turn() -> None:
    points, tangents = _helix(64)
    frames = rmf_double_reflection(points, tangents, np.array([0.0, 0.0, 1.0]))
    # The tangent has returned to its start, so the residual roll is a holonomy.
    assert np.allclose(frames[0, :, 0], frames[-1, :, 0], atol=1e-12)
    holonomy = twist_between(frames[0], frames[-1]) % (2.0 * math.pi)
    slope = _HELIX_C / math.hypot(_HELIX_R, _HELIX_C)
    expected = (2.0 * math.pi * (1.0 - slope)) % (2.0 * math.pi)
    assert holonomy == pytest.approx(expected, abs=1e-3)


def test_rmf_holonomy_is_independent_of_the_seed_vector() -> None:
    points, tangents = _helix(64)
    seeds = (
        np.array([0.0, 0.0, 1.0]),
        np.array([1.0, 1.0, 0.0]),
        np.array([0.3, -2.0, 0.7]),
    )
    values = [
        twist_between(
            rmf_double_reflection(points, tangents, s)[0],
            rmf_double_reflection(points, tangents, s)[-1],
        )
        % (2.0 * math.pi)
        for s in seeds
    ]
    assert max(values) - min(values) < 1e-9


def test_planar_curve_has_zero_start_to_end_twist() -> None:
    points, normals = _planar_s_curve(120)
    path = polyline(points)
    frames = rmf_double_reflection(path.points, path.tangents, normals[0])
    assert abs(twist_between(frames[0], frames[-1])) < 1e-6
    # The binormal stays the plane normal all the way along.
    assert np.allclose(np.abs(frames[:, 2, 2]), 1.0, atol=1e-12)


def test_planar_frame_error_shrinks_at_least_threefold_per_refinement() -> None:
    errors = []
    for n in (50, 100, 200, 400):
        points, normals = _planar_s_curve(n)
        path = polyline(points)
        frames = rmf_double_reflection(path.points, path.tangents, normals[0])
        cos = np.clip(np.einsum("ij,ij->i", frames[:, :, 1], normals), -1.0, 1.0)
        errors.append(float(np.max(np.arccos(cos))))
    assert errors[0] > 0.0
    for coarse, fine in itertools.pairwise(errors):
        assert coarse / fine >= 3.0


@pytest.mark.parametrize(
    ("n_units", "twist_deg", "total_deg"),
    [(21, 34.286, 720.0), (32, 33.75, 1080.0)],
)
def test_apply_twist_accumulates_the_authored_number_of_turns(
    n_units: int, twist_deg: float, total_deg: float
) -> None:
    rise = 0.334
    twist = math.radians(twist_deg)
    s = np.arange(n_units + 1) * rise
    path = polyline(np.stack([np.zeros_like(s), np.zeros_like(s), s], axis=1))
    frames = rmf_double_reflection(
        path.points, path.tangents, np.array([1.0, 0.0, 0.0])
    )
    rolled = apply_twist(frames, twist / rise, path.s)
    assert accumulated_twist(rolled) == pytest.approx(math.radians(total_deg), abs=0.1)
    # Unrolled, the same frame field carries no twist at all.
    assert abs(accumulated_twist(frames)) < 1e-12
    # Tangents survive the roll.
    assert np.allclose(rolled[:, :, 0], frames[:, :, 0])


def test_twist_between_is_signed_and_wrapped_half_open() -> None:
    t = np.array([1.0, 0.0, 0.0])
    n = np.array([0.0, 1.0, 0.0])
    b = np.cross(t, n)
    base = np.stack([t, n, b], axis=1)
    for angle in (0.3, -0.3, 2.0, -2.0):
        rolled = apply_twist(base[None], 1.0, np.array([angle]))[0]
        assert twist_between(base, rolled) == pytest.approx(angle, abs=1e-12)
    # +pi wraps to -pi, so the range has no duplicated endpoint.
    half = apply_twist(base[None], 1.0, np.array([math.pi]))[0]
    assert twist_between(base, half) == pytest.approx(-math.pi, abs=1e-12)


def test_reference_parallel_to_the_first_tangent_is_refused() -> None:
    points = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0]])
    path = polyline(points)
    with pytest.raises(ValueError, match="parallel to the first tangent"):
        rmf_double_reflection(path.points, path.tangents, np.array([1.0, 0.0, 0.0]))


def test_repeated_samples_do_not_poison_the_frame_field() -> None:
    points = np.array(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0]]
    )
    tangents = np.tile([1.0, 0.0, 0.0], (4, 1))
    frames = rmf_double_reflection(points, tangents, np.array([0.0, 1.0, 0.0]))
    assert np.all(np.isfinite(frames))
    assert np.allclose(frames[:, :, 1], np.tile([0.0, 1.0, 0.0], (4, 1)))
