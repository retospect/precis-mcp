"""1-D interval algebra over the ray parameter ``t``.

A ray probe reduces every primitive to a set of disjoint closed
intervals ``[(t_in, t_out), ...]`` (sorted, non-overlapping) along the
ray where the ray is *inside* the solid. The boolean fold (ADR 0041 §6)
then combines these per-node interval sets with union / subtract /
intersect to produce the material-vs-void spans the LLM reads.

Intervals may carry ``±inf`` bounds (an unbounded half-space chamfer).
``merge_intervals`` coalesces touching / overlapping spans within the
linear epsilon so a fused pair reads as one solid run.
"""

from __future__ import annotations

import math

from precis.cad.vec import LINEAR_EPS

#: ``(t_in, t_out)`` with ``t_in <= t_out``.
Interval = tuple[float, float]
Intervals = list[Interval]

NEG_INF = -math.inf
POS_INF = math.inf


def merge_intervals(spans: Intervals, *, eps: float = LINEAR_EPS) -> Intervals:
    """Sort and coalesce overlapping / touching intervals."""
    if not spans:
        return []
    ordered = sorted(spans, key=lambda s: s[0])
    out: Intervals = [ordered[0]]
    for lo, hi in ordered[1:]:
        plo, phi = out[-1]
        if lo <= phi + eps:
            out[-1] = (plo, max(phi, hi))
        else:
            out.append((lo, hi))
    return out


def intersect(a: Intervals, b: Intervals) -> Intervals:
    """Set intersection of two interval lists."""
    a = merge_intervals(a)
    b = merge_intervals(b)
    out: Intervals = []
    i = j = 0
    while i < len(a) and j < len(b):
        lo = max(a[i][0], b[j][0])
        hi = min(a[i][1], b[j][1])
        if lo <= hi:
            out.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return merge_intervals(out)


def union(a: Intervals, b: Intervals) -> Intervals:
    """Set union of two interval lists."""
    return merge_intervals(a + b)


def complement(a: Intervals, *, lo: float = NEG_INF, hi: float = POS_INF) -> Intervals:
    """Complement of ``a`` within ``[lo, hi]``."""
    a = merge_intervals([s for s in a if s[1] >= lo and s[0] <= hi])
    out: Intervals = []
    cursor = lo
    for s_lo, s_hi in a:
        s_lo = max(s_lo, lo)
        s_hi = min(s_hi, hi)
        if s_lo > cursor:
            out.append((cursor, s_lo))
        cursor = max(cursor, s_hi)
    if cursor < hi:
        out.append((cursor, hi))
    return out


def subtract(a: Intervals, b: Intervals) -> Intervals:
    """``a`` minus ``b`` (set difference)."""
    if not a:
        return []
    if not b:
        return merge_intervals(a)
    return intersect(a, complement(b))


def quadratic_le(a: float, b: float, c: float, *, eps: float = LINEAR_EPS) -> Intervals:
    """Intervals of ``t`` satisfying ``a·t² + b·t + c <= 0``.

    Handles the degenerate linear (``a≈0``) and constant cases. When
    ``a > 0`` the solution is the closed interval between the roots (or
    empty); when ``a < 0`` it is the two unbounded tails outside the
    roots; with no real roots the answer is all-of-line or empty per the
    sign of the leading behaviour.
    """
    if abs(a) <= eps:
        if abs(b) <= eps:
            return [(NEG_INF, POS_INF)] if c <= eps else []
        root = -c / b
        # b·t + c <= 0  →  t <= root (b>0) or t >= root (b<0)
        return [(NEG_INF, root)] if b > 0 else [(root, POS_INF)]
    disc = b * b - 4 * a * c
    if disc < 0:
        # No real roots: the quadratic keeps the sign of ``a``.
        return [] if a > 0 else [(NEG_INF, POS_INF)]
    sq = math.sqrt(disc)
    r1 = (-b - sq) / (2 * a)
    r2 = (-b + sq) / (2 * a)
    lo, hi = (r1, r2) if r1 <= r2 else (r2, r1)
    if a > 0:
        return [(lo, hi)]
    return [(NEG_INF, lo), (hi, POS_INF)]


def total_length(spans: Intervals) -> float:
    """Sum of finite interval lengths (ignores unbounded tails)."""
    out = 0.0
    for lo, hi in merge_intervals(spans):
        if math.isinf(lo) or math.isinf(hi):
            continue
        out += hi - lo
    return out
