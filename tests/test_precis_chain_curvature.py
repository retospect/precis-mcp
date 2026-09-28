"""precis_chain.curvature — closed-form curvature and the bend-radius rule.

Theorems recomputed from the returned arrays: a helix ``(r cos t, r sin t,
c t)`` has curvature ``r / (r^2 + c^2)`` and radius of curvature
``(r^2 + c^2) / r`` everywhere, and a circle of radius ``R`` violates a
minimum-bend-radius bound of ``1.01 R`` at every sample while satisfying
``0.99 R`` at none.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from precis_chain.curvature import (
    bend_radius,
    discrete_curvature,
    min_bend_radius_violations,
)


def _helix(r: float, c: float, samples_per_turn: int = 64) -> np.ndarray:
    t = np.linspace(0.0, 2.0 * math.pi, samples_per_turn + 1)
    return np.stack([r * np.cos(t), r * np.sin(t), c * t], axis=1)


def _circle(radius: float, samples: int = 64) -> np.ndarray:
    t = np.linspace(0.0, 2.0 * math.pi, samples + 1)
    return np.stack([radius * np.cos(t), radius * np.sin(t), np.zeros_like(t)], axis=1)


@pytest.mark.parametrize(("r", "c"), [(1.0, 0.2), (2.5, 1.0), (10.0, 0.05)])
def test_helix_curvature_and_bend_radius_at_64_samples_per_turn(
    r: float, c: float
) -> None:
    points = _helix(r, c)
    kappa_exact = r / (r * r + c * c)
    kappa = discrete_curvature(points)
    assert kappa.shape == (points.shape[0],)
    assert np.max(np.abs(kappa - kappa_exact)) / kappa_exact < 1e-3
    radii = bend_radius(points)
    assert np.max(np.abs(radii - (r * r + c * c) / r)) / ((r * r + c * c) / r) < 1e-3


def test_curvature_is_exact_on_a_circle_at_any_sampling() -> None:
    for samples in (8, 17, 200):
        kappa = discrete_curvature(_circle(3.0, samples))
        assert np.allclose(kappa, 1.0 / 3.0, rtol=1e-12)


def test_straight_line_has_zero_curvature_and_infinite_bend_radius() -> None:
    points = np.stack([np.linspace(0.0, 9.0, 10), np.zeros(10), np.zeros(10)], axis=1)
    assert np.allclose(discrete_curvature(points), 0.0)
    assert np.all(np.isinf(bend_radius(points)))


def test_min_bend_radius_violations_brackets_the_circle_radius() -> None:
    radius = 12.0
    points = _circle(radius, 128)
    tight = min_bend_radius_violations(points, 1.01 * radius)
    slack = min_bend_radius_violations(points, 0.99 * radius)
    assert tight.size == points.shape[0]  # every sample is too tight
    assert slack.size == 0
    assert tight.dtype == np.dtype(int)


def test_violations_index_into_points_and_find_only_the_kinked_region() -> None:
    # A long straight run with one tight quarter-circle spliced into the middle.
    straight_in = np.stack(
        [np.linspace(-20.0, 0.0, 40), np.zeros(40), np.zeros(40)], axis=1
    )
    t = np.linspace(0.0, math.pi / 2, 30)[1:]
    bend = np.stack(
        [np.sin(t) * 1.0, (1.0 - np.cos(t)) * 1.0, np.zeros_like(t)], axis=1
    )
    points = np.concatenate([straight_in, bend])
    hits = min_bend_radius_violations(points, 5.0)
    assert hits.size > 0
    assert hits.min() >= 38  # nothing in the straight run is reported
    assert np.all(
        np.linalg.norm(points[hits] - np.array([0.0, 1.0, 0.0]), axis=1) < 1.5
    )


def test_bend_bound_is_a_floor_a_design_may_sit_on() -> None:
    """The comparison is strict, so a path bent to exactly the bound passes.

    Tested a relative epsilon below the true radius rather than at it: the
    circumcircle estimate of a sampled circle lands on ``R`` only to floating
    point, so roughly half the samples come out a few ULP under and an equality
    test would be a coin flip. The epsilon is the measurement's noise floor,
    not slack in the rule.
    """
    points = _circle(7.0, 256)
    assert min_bend_radius_violations(points, 7.0 * (1.0 - 1e-9)).size == 0
    assert min_bend_radius_violations(points, 7.0 * (1.0 + 1e-6)).size == 257


def test_short_paths_and_bad_bounds() -> None:
    assert discrete_curvature(np.zeros((2, 3))).shape == (2,)
    assert np.allclose(discrete_curvature(np.zeros((2, 3))), 0.0)
    with pytest.raises(ValueError, match="must be positive"):
        min_bend_radius_violations(_circle(1.0), 0.0)
    with pytest.raises(ValueError, match=r"must be \(N, 3\)"):
        discrete_curvature(np.zeros((4, 2)))
