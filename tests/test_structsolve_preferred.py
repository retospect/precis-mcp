"""precis.structsolve.preferred — the preferred-number wells (slice 1,
docs/backlog/preferred-number-term.md). One test per acceptance
criterion; grids stay tiny (three tiers over ~87 units).
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from precis.structsolve.preferred import (
    default_coefficients,
    penalty,
    pull_ratio,
    scale_A,
    tiers_for,
    well,
)

L = 87.0
TIERS = tiers_for(L)  # (10.0, 2.0, 1.0)
COEFF = default_coefficients(TIERS)


def _pen(m, coeffs=COEFF):
    return penalty(
        m, coeffs.tiers, A=coeffs.A, B=coeffs.B, eps=coeffs.eps, beta=coeffs.beta
    )


def _penalty_max(m):
    """The note's rejected combination: ``max_p A_p·h(d_p)`` with
    log-scaled ``A_p`` over the doc's counterexample set {2.5, 5, 10,
    100} — a coarser tier whose sawtooth is NOT at rest at m = 10 is
    what makes max drag a round value away."""
    tiers = (2.5, 5.0, 10.0, 100.0)
    p_min = min(tiers)
    vals, grads = [], []
    for p in tiers:
        a = 1.0 + math.log(p / p_min)
        v, g = well(m, p, A=a, B=0.0, eps=1e-4 * p_min)
        vals.append(v)
        grads.append(g)
    i = int(np.argmax(vals))
    return vals[i], grads[i]


# criterion 1 — analytic gradient matches central finite difference


def test_c1_gradient_fd():
    rng = np.random.default_rng(0)
    ms = rng.uniform(0.0, 3 * L, 1000)
    # plus points inside the eps caps of wells bottoms and peaks
    for p in TIERS:
        for k in range(1, 4):
            ms = np.append(
                ms,
                [
                    k * p + 0.5 * COEFF.eps,
                    k * p - 0.3 * COEFF.eps,
                    (k + 0.5) * p - 0.5 * COEFF.eps,
                    (k + 0.5) * p + 0.7 * COEFF.eps,
                ],
            )
    h = 1e-5
    _, g = _pen(ms)
    fp, _ = _pen(ms + h)
    fm, _ = _pen(ms - h)
    fd = (fp - fm) / (2 * h)
    # penalty is piecewise quadratic, so central FD is exact away from
    # the kinks; a draw within h of a kink (d = eps, d = p/2 − eps)
    # sees the f'' jump and is excluded.
    keep = np.ones(ms.shape, bool)
    for p in TIERS:
        d = np.abs(ms - p * np.rint(ms / p))
        keep &= ~(np.abs(d - COEFF.eps) < 2 * h)
        keep &= ~(np.abs(d - (p / 2 - COEFF.eps)) < 2 * h)
    tol = 1e-6 * np.maximum(1.0, np.abs(g[keep]))
    assert np.all(np.abs(fd[keep] - g[keep]) <= tol), np.max(np.abs(fd[keep] - g[keep]))


# criterion 2 — value and derivative continuous across the kinks and
# tier tie surfaces


def test_c2_continuity():
    delta = 1e-7
    points = []
    for p in TIERS:
        for k in range(0, int(L // p) + 1):
            points += [k * p + COEFF.eps, k * p - COEFF.eps]  # d = eps
            if (k + 0.5) * p <= L:
                points += [
                    (k + 0.5) * p + COEFF.eps,
                    (k + 0.5) * p - COEFF.eps,
                ]  # d = p/2 − eps
    # tier tie surfaces: scan for m where the two smallest wells are
    # nearly equal
    grid = np.arange(0.0, L, 1e-3)
    B = COEFF.B
    for i in range(len(TIERS)):
        for j in range(i + 1, len(TIERS)):
            vi, _ = well(grid, TIERS[i], A=COEFF.A, B=B[i], eps=COEFF.eps)
            vj, _ = well(grid, TIERS[j], A=COEFF.A, B=B[j], eps=COEFF.eps)
            near = grid[np.abs(vi - vj) < 1e-3]
            points += list(near[:: max(1, len(near) // 5)])
    ms = np.asarray(points)
    fp, gp = _pen(ms + delta)
    fm, gm = _pen(ms - delta)
    # a true jump adds a constant independent of delta; smooth
    # variation is bounded by slope·2δ (≤ A) and curvature·2δ
    # (≤ 1/eps) respectively.
    assert np.all(np.abs(fp - fm) <= 2 * delta * COEFF.A + 1e-9)
    assert np.all(np.abs(gp - gm) <= 2 * delta / COEFF.eps + 1e-9)


# criterion 3 — every grid point of every tier is a strict local minimum


def test_c3_rest_points():
    delta = 10 * COEFF.eps
    for p in TIERS:
        for k in range(0, int(L // p) + 1):
            m = k * p
            _, gm = _pen(m - delta)
            _, gp = _pen(m + delta)
            assert gm < 0.0 < gp, f"tier {p}, m={m}: {gm}, {gp}"


# criterion 4 — min rests at 10; max pulls away


def test_c4_counterexample():
    g10 = _pen(10.0)[1]
    assert g10 == pytest.approx(0.0, abs=1e-9)
    v10, _ = _pen(10.0)
    for dm in (-0.3, 0.3):
        v, _ = _pen(10.0 + dm)
        assert v10 < v
    vmax, gmax = _penalty_max(10.0)
    assert abs(gmax) > 0.1  # max does not rest at a round 10 mm
    vmax_dn, _ = _penalty_max(10.0 - 0.3)
    assert vmax_dn < vmax  # downhill leads away from 10


# criterion 5 — coarser rest points are deeper


def test_c5_coarse_preference():
    v10, _ = _pen(10.0)
    v8, _ = _pen(8.0)
    v7, _ = _pen(7.0)
    assert v10 < v8 < v7


# criterion 6 — fixed-step descent settles on the nearest tier point


def test_c6_descent_demo():
    # step below eps so the quadratic well floor contracts instead of
    # oscillating: inside eps, d_new = d·(1 − step/eps).
    step = 0.1 * COEFF.eps
    m = 8.3586
    for _ in range(200_000):
        _, g = _pen(m)
        m = m - step * g
        if abs(m - 8.0) < COEFF.eps:
            break
    assert abs(m - 8.0) < COEFF.eps


# criterion 7 — nearest 1-2-5 in log space, deduped


def test_c7_tiers_for():
    t = tiers_for(87.0)
    assert len(t) == 3
    for got, want in zip(t, (10.0, 2.0, 1.0)):
        assert math.isclose(got, want, rel_tol=1e-12)
    t = tiers_for(0.0347)
    assert len(t) == 2
    for got, want in zip(t, (0.005, 0.0005)):
        assert math.isclose(got, want, rel_tol=1e-12)


# criterion 8 — the A0 rule hits rho* exactly


def test_c8_scale_a():
    rng = np.random.default_rng(1)
    gp = rng.normal(size=7)
    gj = rng.normal(size=7)
    s = scale_A(gp, gj, rho_star=0.1)
    assert pull_ratio(s * gp, gj) == pytest.approx(0.1, rel=1e-12)
    with pytest.raises(ValueError):
        scale_A(gp, np.zeros(7))
    with pytest.raises(ValueError):
        pull_ratio(gp, np.zeros(7))
