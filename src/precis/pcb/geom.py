"""Pure 2D geometry for the PCB eyes (ADR 0042 §8) — segment crossing and
distance. No dependencies; unit-testable in isolation.
"""

from __future__ import annotations

import math

Point = tuple[float, float]


def dist(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _orient(a: Point, b: Point, c: Point) -> float:
    """Signed area ×2 of triangle abc; >0 ccw, <0 cw, ~0 collinear."""
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _same(a: Point, b: Point, eps: float) -> bool:
    return abs(a[0] - b[0]) <= eps and abs(a[1] - b[1]) <= eps


def shares_endpoint(
    p1: Point, p2: Point, p3: Point, p4: Point, *, eps: float = 1e-6
) -> bool:
    """True if segment (p1,p2) and (p3,p4) share an endpoint.

    Ratsnest airwires fan out from a common pin / component centroid; two
    wires meeting at that shared point are **not** a crossing, so the crossing
    test excludes this case.
    """
    return (
        _same(p1, p3, eps)
        or _same(p1, p4, eps)
        or _same(p2, p3, eps)
        or _same(p2, p4, eps)
    )


def segments_cross(
    p1: Point, p2: Point, p3: Point, p4: Point, *, eps: float = 1e-9
) -> bool:
    """True iff segments (p1,p2) and (p3,p4) *properly* cross.

    Shared-endpoint and collinear/touch-only cases return False — for the
    ratsnest crossing metric we want genuine X-crossings, not wires that
    merely meet at a pin. (ADR 0042 §8.1.)
    """
    if shares_endpoint(p1, p2, p3, p4):
        return False
    d1 = _orient(p3, p4, p1)
    d2 = _orient(p3, p4, p2)
    d3 = _orient(p1, p2, p3)
    d4 = _orient(p1, p2, p4)
    straddle_a = (d1 > eps and d2 < -eps) or (d1 < -eps and d2 > eps)
    straddle_b = (d3 > eps and d4 < -eps) or (d3 < -eps and d4 > eps)
    return straddle_a and straddle_b


def bbox(p1: Point, p2: Point) -> tuple[float, float, float, float]:
    """Axis-aligned bounding box (minx, miny, maxx, maxy) of a segment."""
    return (
        min(p1[0], p2[0]),
        min(p1[1], p2[1]),
        max(p1[0], p2[0]),
        max(p1[1], p2[1]),
    )


def bboxes_disjoint(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> bool:
    """True if two AABBs cannot overlap — the cheap pre-filter before the
    exact segment-cross test (ADR 0042 §12)."""
    return a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1]
