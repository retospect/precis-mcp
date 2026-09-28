"""precis_chain.clash — closed-form capsule clearance and an exact broad phase.

Theorems:

- Two parallel capsules of radius ``r`` with axes ``d`` apart have gap ``d - 2r``;
  two perpendicular crossing ones have gap ``-2r``.
- The uniform-grid broad phase is *exact*: on 500 random capsules,
  ``clashes`` returns byte-for-byte what an ``O(n^2)`` sweep of
  ``capsule_distance`` returns, at several tolerances.
"""

from __future__ import annotations

import numpy as np
import pytest

from precis_chain.clash import (
    candidate_pairs,
    capsule_distance,
    capsule_gaps,
    clashes,
    segment_closest_points,
    segment_distance,
)
from precis_chain.envelope import Capsule


def _random_capsules(n: int, seed: int = 7) -> list[Capsule]:
    rng = np.random.default_rng(seed)
    out: list[Capsule] = []
    for _ in range(n):
        a = rng.uniform(-20.0, 20.0, 3)
        out.append(
            Capsule(a, a + rng.uniform(-3.0, 3.0, 3), float(rng.uniform(0.2, 1.0)))
        )
    return out


def _brute(caps: list[Capsule], tol: float) -> list[tuple[int, int]]:
    """Every pair, no broad phase — the reference ``clashes`` must reproduce.

    Vectorised through :func:`capsule_gaps` rather than a Python double loop
    only for speed: it still evaluates all ``n (n-1) / 2`` pairs and shares no
    code with the grid.
    """
    i, j = np.triu_indices(len(caps), k=1)
    pairs = np.stack([i, j], axis=1)
    gaps = capsule_gaps(caps, pairs)
    return sorted((int(a), int(b)) for a, b in pairs[gaps < tol])


@pytest.mark.parametrize("spacing", [0.5, 2.0, 3.0, 11.0])
def test_parallel_capsules_have_gap_spacing_minus_two_radii(spacing: float) -> None:
    r = 1.0
    a = Capsule(np.array([0.0, 0.0, 0.0]), np.array([5.0, 0.0, 0.0]), r)
    b = Capsule(np.array([0.0, spacing, 0.0]), np.array([5.0, spacing, 0.0]), r)
    assert capsule_distance(a, b) == pytest.approx(spacing - 2.0 * r)


def test_perpendicular_crossing_capsules_have_gap_minus_two_radii() -> None:
    r = 1.0
    a = Capsule(np.array([0.0, 0.0, 0.0]), np.array([5.0, 0.0, 0.0]), r)
    b = Capsule(np.array([2.5, -2.0, 0.0]), np.array([2.5, 2.0, 0.0]), r)
    assert capsule_distance(a, b) == pytest.approx(-2.0 * r)
    # ... and skew-crossing at a z offset picks the offset up exactly.
    c = Capsule(np.array([2.5, -2.0, 4.0]), np.array([2.5, 2.0, 4.0]), r)
    assert capsule_distance(a, c) == pytest.approx(4.0 - 2.0 * r)


def test_parallel_closest_points_are_symmetric_not_an_arbitrary_end() -> None:
    a = Capsule(np.array([0.0, 0.0, 0.0]), np.array([5.0, 0.0, 0.0]), 1.0)
    b = Capsule(np.array([0.0, 3.0, 0.0]), np.array([5.0, 3.0, 0.0]), 1.0)
    c1, c2 = segment_closest_points(a.a, a.b, b.a, b.b)
    assert np.allclose(c1[0], [2.5, 0.0, 0.0])
    assert np.allclose(c2[0], [2.5, 3.0, 0.0])
    # Partial overlap: the middle of the OVERLAPPING range, not of the segment.
    d = Capsule(np.array([3.0, 3.0, 0.0]), np.array([9.0, 3.0, 0.0]), 1.0)
    c1, _c2 = segment_closest_points(a.a, a.b, d.a, d.b)
    assert np.allclose(c1[0], [4.0, 0.0, 0.0])


def test_collinear_and_degenerate_segments() -> None:
    a = Capsule(np.array([0.0, 0.0, 0.0]), np.array([5.0, 0.0, 0.0]), 1.0)
    end_to_end = Capsule(np.array([7.0, 0.0, 0.0]), np.array([12.0, 0.0, 0.0]), 1.0)
    assert capsule_distance(a, end_to_end) == pytest.approx(0.0)
    point = Capsule(np.array([2.5, 4.0, 0.0]), np.array([2.5, 4.0, 0.0]), 0.5)
    assert capsule_distance(a, point) == pytest.approx(4.0 - 1.5)
    assert capsule_distance(point, a) == pytest.approx(4.0 - 1.5)
    assert capsule_distance(point, point) == pytest.approx(-1.0)


def test_segment_distance_is_symmetric_on_random_pairs() -> None:
    rng = np.random.default_rng(11)
    p1, q1, p2, q2 = (rng.uniform(-5.0, 5.0, (300, 3)) for _ in range(4))
    forward = segment_distance(p1, q1, p2, q2)
    backward = segment_distance(p2, q2, p1, q1)
    assert np.allclose(forward, backward, atol=1e-12)
    # ... and never exceeds any particular pair of points on the two segments.
    for frac in (0.0, 0.25, 0.5, 1.0):
        s1 = p1 + frac * (q1 - p1)
        s2 = p2 + (1.0 - frac) * (q2 - p2)
        assert np.all(forward <= np.linalg.norm(s1 - s2, axis=1) + 1e-12)


@pytest.mark.parametrize("tol", [0.0, 0.25, 1.0])
def test_grid_broad_phase_equals_brute_force_on_500_capsules(tol: float) -> None:
    caps = _random_capsules(500)
    assert clashes(caps, tol=tol) == _brute(caps, tol)


def test_candidate_pairs_is_a_superset_of_the_true_clashes() -> None:
    caps = _random_capsules(500)
    candidates = {(int(i), int(j)) for i, j in candidate_pairs(caps, tol=0.25)}
    assert set(_brute(caps, 0.25)) <= candidates
    # ... and a real saving over the 124 750 pairs a full sweep would test.
    assert len(candidates) < 0.2 * (500 * 499 // 2)


def test_skip_pairs_removes_sanctioned_contacts_in_either_order() -> None:
    caps = [
        Capsule(np.array([0.0, 0.0, 0.0]), np.array([5.0, 0.0, 0.0]), 1.0),
        Capsule(np.array([5.0, 0.0, 0.0]), np.array([10.0, 0.0, 0.0]), 1.0),
        Capsule(np.array([0.0, 1.0, 0.0]), np.array([5.0, 1.0, 0.0]), 1.0),
    ]
    assert clashes(caps) == [(0, 1), (0, 2), (1, 2)]
    assert clashes(caps, skip_pairs=[(1, 0)]) == [(0, 2), (1, 2)]
    assert clashes(caps, skip_pairs=[(0, 1), (2, 0), (2, 1)]) == []


def test_touching_exactly_is_not_a_clash() -> None:
    a = Capsule(np.array([0.0, 0.0, 0.0]), np.array([5.0, 0.0, 0.0]), 1.0)
    b = Capsule(np.array([0.0, 2.0, 0.0]), np.array([5.0, 2.0, 0.0]), 1.0)
    assert capsule_distance(a, b) == pytest.approx(0.0)
    assert clashes([a, b]) == []
    assert clashes([a, b], tol=1e-9) == [(0, 1)]


def test_capsule_gaps_matches_the_scalar_form() -> None:
    caps = _random_capsules(40, seed=3)
    pairs = candidate_pairs(caps, tol=1.0)
    gaps = capsule_gaps(caps, pairs)
    assert gaps.shape == (pairs.shape[0],)
    for (i, j), gap in zip(pairs, gaps):
        assert gap == pytest.approx(capsule_distance(caps[i], caps[j]))
    assert capsule_gaps(caps, np.zeros((0, 2), dtype=int)).shape == (0,)


def test_empty_and_singleton_inputs() -> None:
    assert clashes([]) == []
    assert clashes([Capsule(np.zeros(3), np.ones(3), 1.0)]) == []
    assert candidate_pairs([]).shape == (0, 2)
