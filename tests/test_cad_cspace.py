"""precis.cad.cspace — the conservative-advancement walk, on analytic gaps."""

from __future__ import annotations

import pytest

from precis.cad.cspace import sweep_pair

_RES = 1e-6


def _gap_window(lo: float, hi: float):
    """Signed gap that is negative exactly on (lo, hi); slope magnitude 1."""

    def gap_at(q: float) -> tuple[float, float]:
        return max(lo - q, q - hi), _RES

    return gap_at


def test_window_edges_found_and_far_pair_is_cheap():
    res = sweep_pair(
        _gap_window(1.0, 1.2), 0.0, 3.0, 1.0, tol=1e-3, eval_cap=25
    )
    assert not res.capped and not res.persistent_contact
    (iv,) = res.intervals
    assert iv.lo == pytest.approx(1.0, abs=0.05)
    assert iv.hi == pytest.approx(1.2, abs=0.05)
    assert res.evaluations < 25


def test_clear_pair_reports_nothing():
    res = sweep_pair(lambda q: (5.0, _RES), 0.0, 3.0, 1.0, tol=1e-3, eval_cap=9)
    assert res.intervals == () and not res.persistent_contact


def test_touching_throughout_is_persistent_contact_not_a_collision():
    res = sweep_pair(lambda q: (0.0, _RES), 0.0, 3.0, 1.0, tol=1e-3, eval_cap=9)
    assert res.intervals == () and res.persistent_contact
    assert res.evaluations <= 9


def test_collision_open_at_both_ends_covers_the_range():
    res = sweep_pair(lambda q: (-1.0, _RES), 0.0, 3.0, 1.0, tol=1e-3, eval_cap=9)
    (iv,) = res.intervals
    assert (iv.lo, iv.hi) == (0.0, 3.0)


def test_walk_never_exceeds_eval_cap_when_a_gap_hugs_zero():
    res = sweep_pair(lambda q: (2e-7, _RES), 0.0, 3.0, 100.0, tol=1e-3, eval_cap=9)
    assert not res.capped and res.evaluations <= 9


def test_zero_speed_bound_falls_back_to_floor_steps():
    res = sweep_pair(lambda q: (5.0, _RES), 0.0, 3.0, 0.0, tol=1e-3, eval_cap=9)
    assert res.intervals == () and res.evaluations <= 9
