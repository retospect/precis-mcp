"""Discrete curvature of a sampled centre line, and the bend-radius rule a
stiff polymer imposes on it.

The estimator is the **circumscribed circle of consecutive sample triples**:
for ``p[i-1], p[i], p[i+1]`` the curvature is ``4 * area / (|a| |b| |c|)``
with ``a``, ``b``, ``c`` the triangle's side lengths — the exact reciprocal
circumradius. It is exact on a circle at any sampling, second-order accurate
on a general curve, and needs no tangent or derivative estimate, so it is
independent of how the path's tangents were produced.

Endpoints have no triple. Both are filled by **copying the nearest interior
value** (documented choice; the spec leaves it open). That keeps every
returned array ``(N,)`` and index-aligned with the path's points, so
:func:`min_bend_radius_violations` returns indices straight into
``path.points``, and it makes a uniformly-curved sampled arc report its true
curvature at the ends rather than a spurious zero. A path of fewer than 3
samples has no curvature information at all and reports zeros.

Units: curvature is 1/length and radius is length, in whatever unit the
points carry.
"""

from __future__ import annotations

import numpy as np

#: Curvature at or below this (in the caller's 1/length unit) is reported as a
#: straight line — :func:`bend_radius` returns ``inf`` there instead of a huge
#: finite number that a ``>`` comparison would pass by accident.
STRAIGHT_CURVATURE = 1e-12


def discrete_curvature(points: np.ndarray) -> np.ndarray:
    """``(N,)`` curvature at each sample of ``points`` ``(N, 3)``.

    Interior samples use the circumscribed circle of their triple; the two
    endpoints copy their nearest interior neighbour (see the module
    docstring). Collinear or coincident triples give 0.
    """
    pts = np.asarray(points, dtype=float)
    if pts.ndim != 2 or pts.shape[1] != 3:
        raise ValueError(f"points must be (N, 3), got {pts.shape}")
    n = pts.shape[0]
    if n < 3:
        return np.zeros(n)
    p_prev = pts[:-2]
    p_mid = pts[1:-1]
    p_next = pts[2:]
    e_a = p_mid - p_prev
    e_b = p_next - p_mid
    e_c = p_next - p_prev
    len_a = np.linalg.norm(e_a, axis=1)
    len_b = np.linalg.norm(e_b, axis=1)
    len_c = np.linalg.norm(e_c, axis=1)
    twice_area = np.linalg.norm(np.cross(e_a, e_b), axis=1)
    denom = len_a * len_b * len_c
    interior = np.where(
        denom > 0.0, 2.0 * twice_area / np.where(denom > 0.0, denom, 1.0), 0.0
    )
    out = np.empty(n)
    out[1:-1] = interior
    out[0] = interior[0]
    out[-1] = interior[-1]
    return out


def bend_radius(points: np.ndarray) -> np.ndarray:
    """``(N,)`` radius of curvature at each sample — the reciprocal of
    :func:`discrete_curvature`, with ``numpy.inf`` wherever the curvature is
    at or below :data:`STRAIGHT_CURVATURE`."""
    kappa = discrete_curvature(points)
    out = np.full(kappa.shape, np.inf)
    bendy = kappa > STRAIGHT_CURVATURE
    out[bendy] = 1.0 / kappa[bendy]
    return out


def min_bend_radius_violations(points: np.ndarray, r_min: float) -> np.ndarray:
    """Sample indices where the centre line bends tighter than ``r_min``.

    Returns a ``(K,)`` integer array of indices into ``points`` — empty when
    the whole path is slacker than the bound. The comparison is strict
    (``bend_radius < r_min``), so a path bent to exactly ``r_min`` is
    reported as *acceptable*: the bound is a floor the design may sit on.

    ``r_min`` must be positive; a bound of 0 or less would be vacuous and is
    refused rather than silently returning an empty array.
    """
    if r_min <= 0.0:
        raise ValueError(
            f"r_min must be positive (it is a length, in the points' unit), got {r_min}"
        )
    radii = bend_radius(points)
    return np.flatnonzero(radii < r_min).astype(int)
