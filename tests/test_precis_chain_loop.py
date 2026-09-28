"""precis_chain.loop — the (n+1)-bond contour convention and its consequences.

The convention is the point of this module, so it is tested as arithmetic
rather than as a property: a loop of ``n`` units spans ``n + 1`` backbone bonds,
so at ``c = 0.63`` a 3-unit loop reaches ``4 * 0.63 = 2.52`` and cannot bridge
3.0, while a 4-unit loop reaches ``5 * 0.63 = 3.15`` and can. A 0-unit loop —
a crossover — still has one bond of reach.
"""

from __future__ import annotations

import itertools

import numpy as np
import pytest

from precis_chain.loop import (
    contour,
    loop_curve,
    loop_feasible,
    loop_slack_energy,
    min_units,
)

_C = 0.63  # per-unit backbone contour, test fixture (nm-scale ssDNA-like)
_P = np.array([0.0, 0.0, 0.0])
_Q = np.array([3.0, 0.0, 0.0])  # exactly 3.0 apart


def test_contour_counts_n_plus_one_bonds() -> None:
    for n in range(6):
        assert contour(n, _C) == pytest.approx((n + 1) * _C)
    assert contour(0, _C) == pytest.approx(_C)
    assert contour(2, _C) == pytest.approx(1.89)


def test_three_units_cannot_bridge_three_nanometres_but_four_can() -> None:
    assert contour(3, _C) == pytest.approx(2.52)
    assert contour(4, _C) == pytest.approx(3.15)
    assert not loop_feasible(_P, _Q, 3, _C)
    assert loop_feasible(_P, _Q, 4, _C)


def test_zero_unit_crossover_reaches_exactly_one_bond() -> None:
    assert loop_feasible(_P, np.array([_C, 0.0, 0.0]), 0, _C)
    assert not loop_feasible(_P, np.array([_C * 1.001, 0.0, 0.0]), 0, _C)
    # ... and the tolerance is what lets a measured exit pair through.
    assert loop_feasible(_P, np.array([_C * 1.001, 0.0, 0.0]), 0, _C, tol=0.01)


def test_feasibility_is_inclusive_at_the_contour() -> None:
    assert loop_feasible(_P, np.array([contour(4, _C), 0.0, 0.0]), 4, _C)


def test_min_units_inverts_feasibility() -> None:
    for dist in (0.0, 0.3, _C, 1.0, 3.0, 3.15, 7.7):
        q = np.array([dist, 0.0, 0.0])
        n = min_units(_P, q, _C)
        assert loop_feasible(_P, q, n, _C)
        assert n == 0 or not loop_feasible(_P, q, n - 1, _C)
    assert min_units(_P, _Q, _C) == 4
    assert min_units(_P, _P, _C) == 0


def test_loop_slack_energy_is_monotone_decreasing_in_n() -> None:
    energies = [loop_slack_energy(_P, _Q, n, _C) for n in range(1, 25)]
    assert all(hi > lo for hi, lo in itertools.pairwise(energies))
    # The closed form, recomputed: 3 d^2 / (2 (n+1) c^2).
    for n in (0, 1, 5, 20):
        assert loop_slack_energy(_P, _Q, n, _C) == pytest.approx(
            3.0 * 9.0 / (2.0 * (n + 1) * _C * _C)
        )
    # Zero extension costs nothing, whatever the loop length.
    assert loop_slack_energy(_P, _P, 3, _C) == 0.0


def test_loop_slack_energy_grows_with_extension_at_fixed_n() -> None:
    values = [
        loop_slack_energy(_P, np.array([d, 0.0, 0.0]), 4, _C)
        for d in (0.5, 1.0, 2.0, 3.0)
    ]
    assert all(lo < hi for lo, hi in itertools.pairwise(values))


def test_loop_curve_leaves_and_arrives_along_the_given_tangents() -> None:
    tp = np.array([0.0, 1.0, 0.0])
    tq = np.array([0.0, -1.0, 0.0])
    path = loop_curve(_P, tp, _Q, tq, 4, _C, samples=64)
    assert np.allclose(path.points[0], _P)
    assert np.allclose(path.points[-1], _Q)
    assert np.allclose(path.tangents[0], tp)
    assert np.allclose(path.tangents[-1], tq)
    # A slack loop bows out; the arc length exceeds the straight distance and
    # is in the neighbourhood of the contour it was scaled by.
    straight = float(np.linalg.norm(_Q - _P))
    assert path.length > straight
    assert 0.5 * contour(4, _C) < path.length < 2.0 * contour(4, _C)


def test_loop_curve_slack_makes_a_longer_detour() -> None:
    tp = np.array([0.0, 1.0, 0.0])
    tq = np.array([0.0, -1.0, 0.0])
    lengths = [
        loop_curve(_P, tp, _Q, tq, n, _C, samples=128).length for n in (2, 6, 12)
    ]
    assert all(lo < hi for lo, hi in itertools.pairwise(lengths))


def test_loop_validation() -> None:
    with pytest.raises(ValueError, match="must be >= 0"):
        contour(-1, _C)
    with pytest.raises(ValueError, match="must be positive"):
        contour(2, 0.0)
    with pytest.raises(ValueError, match="must be positive"):
        min_units(_P, _Q, -1.0)
    with pytest.raises(ValueError, match="non-zero exit tangents"):
        loop_curve(_P, np.zeros(3), _Q, np.ones(3), 2, _C)
