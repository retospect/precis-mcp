"""Capsule-capsule clearance, and the broad phase that makes a thousand-segment
design checkable.

Two numbers matter. The **gap** between capsules ``A`` and ``B`` is the
segment-to-segment distance minus the two radii: negative means the tubes
interpenetrate, zero means they touch. A **clash** is a gap below a caller's
tolerance, which is how a design's minimum inter-helix gap is expressed — pass
``tol = min_gap`` and every pair closer than that is reported.

The narrow phase is Ericson's closest-point-between-segments solve (*Real-Time
Collision Detection*, §5.1.9), vectorised over pair arrays: the kernel never
evaluates one pair at a time in a loop, because the relax inner loop
re-evaluates its whole neighbour list every iteration.

The broad phase is a uniform grid over the capsules' radius-expanded axis-
aligned bounding boxes. It is **exact, not approximate**: every capsule is
rasterised into every cell its expanded box touches, so two boxes that overlap
always share a cell, and a pair whose gap is below ``tol`` always has
overlapping boxes. :func:`clashes` therefore returns exactly what an
``O(n^2)`` sweep would — the grid only skips work, never answers.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import numpy as np

from precis_chain.envelope import Capsule

_EPS = 1e-12

#: Squared-length floor below which a segment is treated as a point. Squared,
#: so it is far tighter than :data:`_EPS`: a 1e-6-long segment has squared
#: length 1e-12 and must still be solved as a segment.
_LEN2_EPS = 1e-18

#: Relative floor on the Gram determinant ``a*e - b*b`` below which two
#: segments count as parallel (the solve for ``s`` is then indeterminate and
#: Ericson's fallback ``s = 0`` plus a ``t`` clamp is used instead).
_PARALLEL_RTOL = 1e-12

#: Below this many capsules the grid's bookkeeping costs more than the pairs it
#: saves, and :func:`clashes` sweeps every pair directly.
_BRUTE_FORCE_BELOW = 32


def segment_closest_points(
    p1: np.ndarray, q1: np.ndarray, p2: np.ndarray, q2: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Closest point pair between segment ``p1->q1`` and segment ``p2->q2``.

    All four arguments are ``(M, 3)`` (or ``(3,)``, broadcast to ``M = 1``);
    returns two ``(M, 3)`` arrays. Degenerate (zero-length) segments are
    handled — a point-vs-segment or point-vs-point query gives the right
    answer rather than dividing by zero. Parallel segments have a whole
    interval of closest pairs; the middle of that interval is returned (see
    the note at the parallel branch).
    """
    a1 = np.atleast_2d(np.asarray(p1, dtype=float))
    b1 = np.atleast_2d(np.asarray(q1, dtype=float))
    a2 = np.atleast_2d(np.asarray(p2, dtype=float))
    b2 = np.atleast_2d(np.asarray(q2, dtype=float))
    d1 = b1 - a1
    d2 = b2 - a2
    r = a1 - a2
    a = np.einsum("ij,ij->i", d1, d1)
    e = np.einsum("ij,ij->i", d2, d2)
    f = np.einsum("ij,ij->i", d2, r)
    c = np.einsum("ij,ij->i", d1, r)
    b = np.einsum("ij,ij->i", d1, d2)

    a_ok = a > _LEN2_EPS
    e_ok = e > _LEN2_EPS
    a_safe = np.where(a_ok, a, 1.0)
    e_safe = np.where(e_ok, e, 1.0)

    denom = a * e - b * b
    denom_ok = denom > _PARALLEL_RTOL * np.maximum(a * e, _LEN2_EPS)
    # Parallel segments have no unique closest pair — an interval of them. Take
    # the MIDDLE of the overlapping parameter range rather than Ericson's
    # ``s = 0``: the distance is the same either way, but the midpoint is
    # symmetric, so a caller applying a force at the contact point (the relax
    # pass's excluded volume) gets no spurious torque out of two bodies lying
    # alongside each other. Empty overlap collapses to the nearer end, which is
    # the right answer there.
    overlap_lo = np.clip(np.minimum(-c / a_safe, (b - c) / a_safe), 0.0, 1.0)
    overlap_hi = np.clip(np.maximum(-c / a_safe, (b - c) / a_safe), 0.0, 1.0)
    s = np.where(
        denom_ok,
        np.clip((b * f - c * e) / np.where(denom_ok, denom, 1.0), 0.0, 1.0),
        0.5 * (overlap_lo + overlap_hi),
    )
    t_raw = np.where(e_ok, (b * s + f) / e_safe, 0.0)
    t = np.clip(t_raw, 0.0, 1.0)

    # t clamped to an end: re-solve s against that fixed t (Ericson's branches).
    s_at_t0 = np.clip(-c / a_safe, 0.0, 1.0)
    s_at_t1 = np.clip((b - c) / a_safe, 0.0, 1.0)
    s = np.where(t_raw < 0.0, s_at_t0, s)
    s = np.where(t_raw > 1.0, s_at_t1, s)
    # Degenerate second segment: t is pinned at 0, so minimise over s alone.
    s = np.where(e_ok, s, s_at_t0)
    # Degenerate first segment: there is no s to choose.
    s = np.where(a_ok, s, 0.0)

    return a1 + s[:, None] * d1, a2 + t[:, None] * d2


def segment_distance(
    p1: np.ndarray, q1: np.ndarray, p2: np.ndarray, q2: np.ndarray
) -> np.ndarray:
    """``(M,)`` distance between segment pairs — the norm of
    :func:`segment_closest_points`' difference."""
    c1, c2 = segment_closest_points(p1, q1, p2, q2)
    return np.linalg.norm(c1 - c2, axis=1)


def capsule_distance(a: Capsule, b: Capsule) -> float:
    """The signed gap between two capsules: segment-segment distance minus
    ``a.r + b.r``.

    Positive = clear by that much, 0 = touching, negative = interpenetrating
    by that much. Two parallel capsules of radius ``r`` whose axes are ``d``
    apart give ``d - 2r``; two crossing ones give ``-2r``.
    """
    gap = float(segment_distance(a.a, a.b, b.a, b.b)[0])
    return gap - (a.r + b.r)


def capsule_gaps(caps: Sequence[Capsule], pairs: np.ndarray) -> np.ndarray:
    """``(M,)`` gaps for the capsule index ``pairs`` ``(M, 2)`` — the
    vectorised form of :func:`capsule_distance`, and the only form the relax
    inner loop uses. An empty ``pairs`` returns an empty array."""
    idx = np.asarray(pairs, dtype=int).reshape(-1, 2)
    if idx.size == 0:
        return np.zeros(0)
    starts = np.array([c.a for c in caps])
    ends = np.array([c.b for c in caps])
    radii = np.array([c.r for c in caps])
    i, j = idx[:, 0], idx[:, 1]
    gap = segment_distance(starts[i], ends[i], starts[j], ends[j])
    return gap - (radii[i] + radii[j])


def _expanded_boxes(
    caps: Sequence[Capsule], tol: float
) -> tuple[np.ndarray, np.ndarray]:
    """Per-capsule AABB expanded by ``r + tol/2``, as ``(lo, hi)`` ``(N, 3)``.

    The half-``tol`` split is what makes the broad phase exact: two capsules
    whose gap is below ``tol`` have segment distance below
    ``r_i + r_j + tol``, which is exactly the sum of the two expansions, so
    their boxes must overlap.
    """
    starts = np.array([c.a for c in caps])
    ends = np.array([c.b for c in caps])
    pad = np.array([c.r for c in caps]) + 0.5 * tol
    lo = np.minimum(starts, ends) - pad[:, None]
    hi = np.maximum(starts, ends) + pad[:, None]
    return lo, hi


def candidate_pairs(caps: Sequence[Capsule], tol: float = 0.0) -> np.ndarray:
    """``(M, 2)`` capsule index pairs (``i < j``) whose expanded boxes overlap
    — the broad phase, a superset of the true clashes at ``tol``.

    Below :data:`_BRUTE_FORCE_BELOW` capsules this is every pair. Above it, a
    uniform grid whose cell edge is the largest expanded box extent, so each
    box rasterises into at most 8 cells.
    """
    n = len(caps)
    if n < 2:
        return np.zeros((0, 2), dtype=int)
    if n < _BRUTE_FORCE_BELOW:
        i, j = np.triu_indices(n, k=1)
        return np.stack([i, j], axis=1)

    lo, hi = _expanded_boxes(caps, max(tol, 0.0))
    cell = float(np.max(hi - lo))
    if cell < _EPS:
        i, j = np.triu_indices(n, k=1)
        return np.stack([i, j], axis=1)
    lo_cell = np.floor(lo / cell).astype(np.int64)
    hi_cell = np.floor(hi / cell).astype(np.int64)

    buckets: dict[tuple[int, int, int], list[int]] = {}
    for idx in range(n):
        for cx in range(lo_cell[idx, 0], hi_cell[idx, 0] + 1):
            for cy in range(lo_cell[idx, 1], hi_cell[idx, 1] + 1):
                for cz in range(lo_cell[idx, 2], hi_cell[idx, 2] + 1):
                    buckets.setdefault((cx, cy, cz), []).append(idx)

    seen: set[tuple[int, int]] = set()
    for members in buckets.values():
        for a_pos in range(len(members)):
            for b_pos in range(a_pos + 1, len(members)):
                first, second = members[a_pos], members[b_pos]
                seen.add((first, second) if first < second else (second, first))
    if not seen:
        return np.zeros((0, 2), dtype=int)
    pairs = np.array(sorted(seen), dtype=int)
    # Drop cell-mates whose boxes do not actually overlap.
    left, right = pairs[:, 0], pairs[:, 1]
    overlap = np.all((lo[left] <= hi[right]) & (lo[right] <= hi[left]), axis=1)
    return pairs[overlap]


def clashes(
    caps: Sequence[Capsule],
    skip_pairs: Iterable[tuple[int, int]] | None = None,
    tol: float = 0.0,
) -> list[tuple[int, int]]:
    """Capsule index pairs whose gap is below ``tol``, sorted and ``i < j``.

    ``tol`` is the required clearance in the caller's length unit: 0 reports
    only genuine interpenetration, a positive ``tol`` reports anything closer
    than that gap. The comparison is strict (``gap < tol``), matching
    :func:`precis_chain.curvature.min_bend_radius_violations` — a design
    sitting exactly on its stated minimum gap is acceptable.

    ``skip_pairs`` is the sanctioned-contact list: pairs that are *supposed*
    to touch (consecutive segments of one helix, two segments joined by a
    crossover). Order within a pair does not matter.
    """
    if not caps:
        return []
    skip: set[tuple[int, int]] = set()
    for i, j in skip_pairs or ():
        skip.add((i, j) if i < j else (j, i))
    pairs = candidate_pairs(caps, tol=max(tol, 0.0))
    if pairs.size == 0:
        return []
    gaps = capsule_gaps(caps, pairs)
    hits = pairs[gaps < tol]
    return [(int(i), int(j)) for i, j in hits if (int(i), int(j)) not in skip]
