"""Pure 2D geometry for the PCB eyes — segment crossing and
distance, corner filleting, and gerber-unit quantization. Its only
non-stdlib dependency is :mod:`precis.pcb.gerber`'s fixed-point unit
constant (:data:`GERBER_UNIT_MM`, imported rather than restated — see
:func:`quantize`); ``gerber.py`` itself imports nothing from this
package, so that import cannot cycle back here. Otherwise
unit-testable in isolation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from precis.pcb.gerber import _UNITS_PER_MM as _GERBER_UNITS_PER_MM

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
    merely meet at a pin.
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


def dist_point_to_segment(p: Point, a: Point, b: Point) -> float:
    """Shortest distance from ``p`` to the closed segment ``(a, b)``."""
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 < 1e-12:
        return dist(p, a)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return dist(p, (ax + t * dx, ay + t * dy))


def point_in_polygon(p: Point, poly: list[Point]) -> bool:
    """Ray-cast point-in-polygon test; ``poly`` vertices in order, any winding."""
    x, y = p
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            x_at_y = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < x_at_y:
                inside = not inside
    return inside


def convex_polygons_overlap(poly_a: list[Point], poly_b: list[Point]) -> bool:
    """Separating Axis Theorem for two CONVEX polygons — exact, and the ONE
    polygon-overlap primitive in this package.

    Three consumers ask the same question about the same shapes and must
    not answer it differently: :mod:`precis.pcb.silk` (does this silk
    stroke's box hit that pad), :mod:`precis.pcb.optimize` (may these two
    parts' courtyards be placed here) and :mod:`precis.pcb.drc` (do any two
    courtyards on the finished board overlap). A placer that thought a
    placement legal while DRC called it a violation is the drift
    ``docs/backlog/pcb-courtyard-polygon.md`` exists to close, and two
    implementations of one predicate is how that drift starts.

    **Convex only, and the name says so.** SAT tests the edge normals of
    both polygons as candidate separating axes, which is complete for
    convex shapes and silently WRONG for a concave one (two shapes can be
    disjoint while no edge normal separates them, and vice versa). Every
    caller here passes something convex by construction — a rotated
    rectangle, a pad, or a courtyard (:func:`precis.pcb.ir.
    instance_courtyard_polygon` is a convex hull offset outward, which
    stays convex).

    Touching counts as overlapping: the test is a strict ``<`` on the
    projection gap, so two polygons sharing exactly one edge or vertex
    return ``True``. That verdict is pinned by test rather than left to
    float luck — a shared boundary is measure-zero and would otherwise
    never be exercised.

    A closed ring (first vertex repeated last) is accepted: the duplicate
    contributes a zero-length edge whose normal is ``(0, 0)``, which
    projects everything to 0 and can never separate, so it changes no
    answer.
    """
    for poly in (poly_a, poly_b):
        n = len(poly)
        for i in range(n):
            x1, y1 = poly[i]
            x2, y2 = poly[(i + 1) % n]
            nx, ny = -(y2 - y1), (x2 - x1)
            a_vals = [nx * px + ny * py for px, py in poly_a]
            b_vals = [nx * px + ny * py for px, py in poly_b]
            if max(a_vals) < min(b_vals) or max(b_vals) < min(a_vals):
                return False
    return True


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
    exact segment-cross test."""
    return a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1]


def sweep_line_crossings(segments: list[tuple[int, Point, Point]]) -> int:
    """Count genuine crossings among ``segments`` — ``(group_id, p1, p2)``
    triples, e.g. a net id, so two segments sharing a ``group_id`` (spokes
    of the same star hub) NEVER count against each other, regardless of
    geometry: real board segments only fan out from a shared point by
    construction, not by a routing conflict (this is the primary
    "shared-endpoint" degenerate case — the same-``group_id`` check is a
    stronger, exact version of :func:`shares_endpoint`'s coordinate-based
    one, kept as a belt-and-suspenders second line of defense inside
    :func:`segments_cross` for segments of DIFFERENT groups that happen to
    land on the same coordinate, e.g. two components seeded on top of each
    other).

    **This is a plane sweep with x-interval active-set pruning, not a full
    Bentley-Ottmann y-ordered-neighbor sweep** — an explicit complexity
    tradeoff, stated rather than left implicit: the active set is a plain
    Python list (O(active size) insert/remove, not O(log n) via a balanced
    BST), and every newly-active segment is compared against every OTHER
    currently-active segment (an AABB check first, then the exact
    orientation test), not just its immediate y-neighbors. That gives
    ``O(n log n + m)`` where ``m`` is the number of x-interval-overlapping
    segment PAIRS — equal to the true crossing count ``k`` (the task's
    target complexity) whenever geometry is spatially local, which is the
    ordinary case for a placed board (few segments span the whole board
    width at once); it degrades toward ``O(n^2)`` only for a pathological
    layout where most segments overlap in x for most of the sweep (e.g.
    many long near-full-width parallel traces). A full event-driven
    Bentley-Ottmann (with intersection-swap events and a balanced status
    structure) would hold the tighter bound unconditionally, at the cost
    of real numerical-robustness risk (near-vertical segments, concurrent
    events at one x, exact overlaps) for a board-scale problem — few
    hundred to low thousands of segments per layer — where that risk isn't
    worth the payoff. Chosen and stated, not silently assumed away.

    Degenerate cases (see also :func:`segments_cross`, reused unchanged
    rather than re-decided here):
    - collinear overlap and touch-without-crossing are NOT counted — only
      genuine transversal 'X' crossings are.
    - two segments sharing an endpoint COORDINATE never count, even across
      different groups.
    """
    # events: (x, kind, index) — kind 0 = segment start (left x), 1 = end
    # (right x); starts sort before ends at an identical x so two segments
    # that only just touch at one x still get compared once while both are
    # briefly "active" together (the exact test below is what decides
    # whether that touch is a real crossing, not this ordering).
    events: list[tuple[float, int, int]] = []
    boxes: list[tuple[float, float, float, float]] = []
    for i, (_gid, p1, p2) in enumerate(segments):
        boxes.append(bbox(p1, p2))
        x0, x1 = sorted((p1[0], p2[0]))
        events.append((x0, 0, i))
        events.append((x1, 1, i))
    events.sort()

    active: list[int] = []
    count = 0
    for _x, kind, i in events:
        if kind == 0:
            gid_i, p1i, p2i = segments[i]
            box_i = boxes[i]
            for j in active:
                gid_j, p1j, p2j = segments[j]
                if gid_i == gid_j:
                    continue
                if bboxes_disjoint(box_i, boxes[j]):
                    continue
                if segments_cross(p1i, p2i, p1j, p2j):
                    count += 1
            active.append(i)
        else:
            active.remove(i)
    return count


# ── gerber-unit quantization ─────────────────────────────────────────────

#: The gerber writer's own fixed-point resolution, in mm —
#: ``1 / precis.pcb.gerber._UNITS_PER_MM``. ``gerber.py``'s ``%FSLAX46Y46*%``
#: header declares 6 decimal digits (currently 1e-6 mm, one nanometre);
#: this is imported from there rather than re-declared here so the two can
#: never drift out of the same unit (a second copy of that literal is
#: exactly the kind of defect this repo keeps paying for — see
#: :func:`quantize`).
GERBER_UNIT_MM = 1.0 / _GERBER_UNITS_PER_MM


def quantize(value: float) -> float:
    """Snap ``value`` (mm) to the gerber writer's own fixed-point unit
    (:data:`GERBER_UNIT_MM`).

    ``gerber.py``'s ``_u()`` already does ``round(mm * _UNITS_PER_MM)`` at
    EMISSION time, on every coordinate it writes. Today's model carries
    float mm computed upstream of that (routing, filleting, ...), so that
    ``round()`` can move a coordinate by up to half a unit (5e-7 mm at the
    current resolution) — the artefact on disk is then not exactly the
    geometry the router computed, only close to it. Quantizing a
    coordinate as soon as it is COMPUTED, using the exact same rounding
    rule against the exact same unit, means ``_u()``'s own ``round()`` at
    export time is a no-op on it (see the round-trip test in
    ``tests/test_pcb_geom.py``, which asserts this directly rather than
    trusting it) — the file matches the model exactly, not approximately.

    Idempotent: ``quantize(x)`` is (up to float representation) an integer
    multiple ``k`` of :data:`GERBER_UNIT_MM`; re-quantizing recovers the
    same ``k`` because ``k`` is well within a ``float``'s exact-integer
    range (53 bits) for any realistic board coordinate (millimetre-scale
    values times a 1e6 unit stay far below ``2**53``).
    """
    return round(value * _GERBER_UNITS_PER_MM) / _GERBER_UNITS_PER_MM


# ── corner filleting ──────────────────────────────────────────────────────

#: Interior-angle thresholds below/above which a corner is left sharp
#: rather than filleted — see :func:`fillet_polyline`'s docstring for what
#: each guards against and why 2 degrees. Exposed as the functions' own
#: default keyword values (not just module constants) so a caller can
#: override per-call without a second code path.
DEFAULT_REVERSAL_EPS_RAD = math.radians(2.0)
DEFAULT_COLLINEAR_EPS_RAD = math.radians(2.0)


@dataclass(frozen=True, slots=True)
class _CornerFillet:
    """One interior vertex's resolved fillet geometry — the shared
    computation :func:`fillet_polyline` (which turns it into segments) and
    :func:`max_inward_deviation` (which only needs ``deviation_mm``) both
    call, so the two can never resolve a different effective radius for
    the same corner (:func:`_corner_fillet` returns ``None`` instead of an
    instance for a corner that should stay sharp)."""

    t1: Point  #: tangent point on the incoming leg (toward `prev`)
    t2: Point  #: tangent point on the outgoing leg (toward `next`)
    center: Point
    cw: bool
    radius_mm: float  #: the CLAMPED radius actually used at this corner
    deviation_mm: float  #: this corner's :func:`max_inward_deviation` term
    #: Half the interior angle, in radians. Carried rather than recomputed
    #: because :func:`max_radius_for_deviation` needs the angle and NOT the
    #: resolved geometry: re-deriving ``theta`` from the same three points
    #: in a second function is this subsystem's named defect generator (one
    #: rule, two call sites, then drift), and the two would disagree the
    #: moment either eps threshold moved.
    half_angle_rad: float


def _sub(a: Point, b: Point) -> Point:
    return (a[0] - b[0], a[1] - b[1])


def _unit(v: Point) -> Point:
    n = math.hypot(v[0], v[1])
    return (v[0] / n, v[1] / n)


def _corner_fillet(
    prev: Point,
    cur: Point,
    nxt: Point,
    radius_mm: float,
    *,
    reversal_eps_rad: float,
    collinear_eps_rad: float,
) -> _CornerFillet | None:
    """One vertex's fillet, or ``None`` when it should stay a sharp
    corner — ``radius_mm <= 0``, a zero-length adjoining leg (no leg
    direction to fillet against), or a near-collinear/near-reversal
    interior angle (see :func:`fillet_polyline`).

    **The trig.** Let ``e1 = prev - cur`` and ``e2 = next - cur`` (both
    pointing AWAY from the vertex, back along the incoming leg and
    forward along the outgoing one) and ``theta`` the angle between them
    — the corner's interior angle: ``theta == pi`` is dead straight
    (``e1``/``e2`` opposite), ``theta == 0`` is a full reversal
    (``e1``/``e2`` parallel). For the right triangle (vertex, tangent
    point, arc centre) with the right angle at the tangent point and
    angle ``theta/2`` at the vertex: the tangent setback is
    ``t = r / tan(theta/2)`` (adjacent = r / tan of the vertex angle) and
    the vertex-to-centre distance is ``r / sin(theta/2)`` (hypotenuse).
    The centre sits on the interior bisector, ``normalize(e1 + e2)``,
    at that distance.

    **Sign convention, matched to** :func:`precis.pcb.realize.
    tangent_arc_path` **exactly, not re-derived**: the arc runs from
    ``t1`` (on the incoming leg, i.e. first in path-traversal order) to
    ``t2`` (outgoing), and ``cw`` is the sign of the shorter atan2 sweep
    from ``t1`` to ``t2`` around the centre — positive (ccw, increasing
    angle) is ``cw=False``, negative is ``cw=True``, same as ``realize.
    py``'s ``_signed_shorter_sweep``/``tangent_arc_path``. This always
    picks the SHORT way (the fillet's own central angle is ``pi - theta``,
    which stays under ``pi`` for every non-degenerate ``theta`` this
    function accepts), so unlike ``tangent_arc_path`` there is no 4-way
    pairing ambiguity to resolve — the two tangent points and their
    traversal order are already fixed by construction.
    """
    if radius_mm <= 0:
        return None
    len_in = dist(prev, cur)
    len_out = dist(cur, nxt)
    if len_in < 1e-9 or len_out < 1e-9:
        return None
    u1 = _unit(_sub(prev, cur))
    u2 = _unit(_sub(nxt, cur))
    cos_theta = max(-1.0, min(1.0, u1[0] * u2[0] + u1[1] * u2[1]))
    theta = math.acos(cos_theta)
    if theta > math.pi - collinear_eps_rad:
        return None  # near-straight: nothing to round
    if theta < reversal_eps_rad:
        return None  # near-reversal: a fillet is meaningless here
    half = theta / 2.0
    t_wanted = radius_mm / math.tan(half)
    t_max = min(len_in, len_out) / 2.0
    if t_wanted > t_max:
        # Clamp: the setback must not exceed half of either adjoining leg,
        # or two adjacent fillets on the same leg would overrun each other
        # and the path would self-intersect. Reduce r for THIS corner
        # (rather than failing) by solving the same relation backwards.
        t = t_max
        r_eff = t * math.tan(half)
    else:
        t = t_wanted
        r_eff = radius_mm
    t1 = (cur[0] + u1[0] * t, cur[1] + u1[1] * t)
    t2 = (cur[0] + u2[0] * t, cur[1] + u2[1] * t)
    bisector = _unit((u1[0] + u2[0], u1[1] + u2[1]))
    center_dist = r_eff / math.sin(half)
    center = (
        cur[0] + bisector[0] * center_dist,
        cur[1] + bisector[1] * center_dist,
    )
    a1 = math.atan2(t1[1] - center[1], t1[0] - center[0])
    a2 = math.atan2(t2[1] - center[1], t2[0] - center[0])
    sweep = (a2 - a1) % (2 * math.pi)
    if sweep > math.pi:
        sweep -= 2 * math.pi
    cw = sweep < 0
    # Sagitta of the arc measured from the CHORD joining t1/t2 (a bevel/
    # chamfer cut between the same two tangent points) -- see
    # max_inward_deviation()'s docstring for the full derivation of why
    # this, and not the vertex-to-arc distance, is the right "how far did
    # the round-over intrude past the original mitered corner" figure.
    deviation = r_eff * (1.0 - math.sin(half))
    return _CornerFillet(t1, t2, center, cw, r_eff, deviation, half)


def fillet_polyline(
    points: list[Point],
    radius_mm: float,
    *,
    reversal_eps_rad: float = DEFAULT_REVERSAL_EPS_RAD,
    collinear_eps_rad: float = DEFAULT_COLLINEAR_EPS_RAD,
) -> list[dict[str, Any]]:
    """Replace each interior corner of ``points`` with a tangent arc of
    radius ``radius_mm``, in the exact segment-chain shape
    :func:`precis.pcb.realize.tangent_arc_path` already returns (and
    :mod:`precis.pcb.gerber` already reads): ``[{"shape": "line", "start":
    [...], "end": [...]}, {"shape": "arc", "start": [...], "end": [...],
    "center": [...], "cw": bool}, ...]``.

    A 2-point polyline has no interior vertex at all — it is returned as
    ONE unchanged line segment, endpoints exact (no vertex means no corner
    to round, so this is not a degenerate case of the loop below, it is
    the loop having nothing to do).

    Each interior vertex is independently resolved by
    :func:`_corner_fillet`, which:

    - **clamps the radius per corner** so the tangent setback never
      exceeds half of either adjoining leg (else two adjacent fillets on
      one leg would overrun each other and self-intersect) — reducing
      ``radius_mm`` for that corner rather than raising;
    - **skips near-collinear corners** (interior angle within
      ``collinear_eps_rad`` of ``pi`` — default 2 degrees): at 178-182
      degrees there is nothing worth rounding, and right at 180 degrees
      the construction is singular (the two legs' directions are
      opposite, so no bisector exists to place a centre on);
    - **skips near-reversal corners** (interior angle under
      ``reversal_eps_rad`` — default 2 degrees): at that point the two
      legs nearly double back on each other and a "corner" is not a
      meaningful shape to round (the arc's own central angle would be
      near ``pi``, in the limit a near-full circle sitting almost on top
      of the vertex).

    A skipped corner is passed straight through unmodified — the vertex
    stays a sharp point, exactly as if this function had never touched
    it — and every OTHER corner is filleted independently; one skip does
    not suppress the rest of the polyline.

    See :func:`max_inward_deviation` for the copper-side consequence of
    the radius this function actually used per corner (which, after
    clamping, may be smaller than the ``radius_mm`` asked for).
    """
    if len(points) < 2:
        raise ValueError("fillet_polyline needs at least 2 points")
    if len(points) == 2:
        return [{"shape": "line", "start": list(points[0]), "end": list(points[1])}]

    corners = [
        _corner_fillet(
            points[i - 1],
            points[i],
            points[i + 1],
            radius_mm,
            reversal_eps_rad=reversal_eps_rad,
            collinear_eps_rad=collinear_eps_rad,
        )
        for i in range(1, len(points) - 1)
    ]

    segments: list[dict[str, Any]] = []
    cursor = points[0]
    for i, corner in enumerate(corners):
        vertex = points[i + 1]
        if corner is None:
            if dist(cursor, vertex) > 1e-9:
                segments.append(
                    {"shape": "line", "start": list(cursor), "end": list(vertex)}
                )
            cursor = vertex
            continue
        if dist(cursor, corner.t1) > 1e-9:
            segments.append(
                {"shape": "line", "start": list(cursor), "end": list(corner.t1)}
            )
        segments.append(
            {
                "shape": "arc",
                "start": list(corner.t1),
                "end": list(corner.t2),
                "center": list(corner.center),
                "cw": corner.cw,
            }
        )
        cursor = corner.t2
    if dist(cursor, points[-1]) > 1e-9:
        segments.append(
            {"shape": "line", "start": list(cursor), "end": list(points[-1])}
        )
    return segments


def max_inward_deviation(
    points: list[Point],
    radius_mm: float,
    *,
    reversal_eps_rad: float = DEFAULT_REVERSAL_EPS_RAD,
    collinear_eps_rad: float = DEFAULT_COLLINEAR_EPS_RAD,
) -> float:
    """The largest distance, over every corner :func:`fillet_polyline`
    would actually round, that its arc bulges past the ORIGINAL mitered
    (sharp-corner) path — new copper on the concave side of a turn, where
    a pad or via may already sit. ``0.0`` for a polyline with no interior
    vertex, an untouched ``radius_mm <= 0``, or a polyline whose every
    corner is skipped (near-collinear/near-reversal, same thresholds as
    :func:`fillet_polyline` — pass the SAME keyword overrides to both if
    you override either).

    **Derivation.** Put a vertex at the origin with its interior bisector
    along the +x axis, half-angle ``b = theta/2`` between the bisector and
    each leg. From :func:`_corner_fillet`: the arc centre sits at
    ``(d, 0)`` with ``d = r/sin(b)``, and the point on the arc CLOSEST to
    the vertex along the bisector is ``M = (d - r, 0)``. ``M`` is also the
    farthest the arc gets from either leg (by the symmetry of the
    construction — the two legs are equidistant from any point on the
    bisector, and the arc's deviation from a leg only grows moving from
    the tangent point, where it is zero, toward ``M``). The perpendicular
    distance from ``M`` to the leg through the origin at angle ``b`` is
    ``|M| * sin(b) = (d - r) * sin(b) = (r/sin(b) - r) * sin(b) = r * (1 -
    sin(b))`` — this function's return value per corner, maximized over
    every corner that actually gets an arc.

    This is EXACTLY the sagitta of the arc measured from the chord
    joining its own two tangent points (a bevel/chamfer cut between the
    same two points): ``sagitta = r * (1 - cos(central_angle / 2))``, and
    ``central_angle = pi - theta`` (this module's own arc, see
    :func:`_corner_fillet`) makes ``cos(central_angle/2) ==
    cos(pi/2 - theta/2) == sin(theta/2)`` — the same expression, reached
    two ways, which is the cross-check ``tests/test_pcb_geom.py`` runs
    (an independent sagitta formula against this derivation) rather than
    trusting either alone. At a right-angle corner (``theta = pi/2``):
    ``r * (1 - sin(45deg)) = r * (1 - 0.70710...) ~= 0.2929 * r``.

    Uses the SAME clamped, per-corner radius :func:`fillet_polyline` would
    actually draw (:func:`_corner_fillet` is the one shared computation,
    called here too) — a corner whose requested ``radius_mm`` got reduced
    by the setback clamp reports the deviation of the radius that would
    really be drawn, not the one that was asked for.
    """
    if len(points) < 3:
        return 0.0
    best = 0.0
    for i in range(1, len(points) - 1):
        corner = _corner_fillet(
            points[i - 1],
            points[i],
            points[i + 1],
            radius_mm,
            reversal_eps_rad=reversal_eps_rad,
            collinear_eps_rad=collinear_eps_rad,
        )
        if corner is not None:
            best = max(best, corner.deviation_mm)
    return best


def max_radius_for_deviation(
    points: list[Point],
    budget_mm: float,
    *,
    reversal_eps_rad: float = DEFAULT_REVERSAL_EPS_RAD,
    collinear_eps_rad: float = DEFAULT_COLLINEAR_EPS_RAD,
) -> float:
    """The largest ``radius_mm`` for which :func:`fillet_polyline` on
    ``points`` is guaranteed to keep every corner's inward bulge
    (:func:`max_inward_deviation`) within ``budget_mm``. ``math.inf`` when
    no corner is filleted at all, so a caller can always ``min()`` this
    against its own preferred radius.

    **Why this is not "scale the radius by budget/worst".** That was the
    original approach here and it is WRONG whenever the worst corner is
    setback-clamped. :func:`_corner_fillet` clamps the tangent setback to
    half the shorter adjoining leg and back-solves ``r_eff = t_max *
    tan(theta/2)`` — a value that does not depend on the requested radius
    at all. Scaling the request down therefore changes nothing at exactly
    the corner that was over budget: measured on a 60-degree corner with
    legs 4x the track width and the default 1.5x-width radius, the
    proportional step reduced the deviation by 0.000000000 mm and it
    stayed 0.1443mm against a 0.125mm budget. The linearity the old
    docstring appealed to holds only for UNCLAMPED corners.

    **The bound.** Per corner the drawn radius is ``r_eff = min(requested,
    t_max * tan(b))`` with ``b = theta/2``, so ``r_eff <= requested``
    unconditionally. Deviation is ``r_eff * (1 - sin(b))``
    (:func:`max_inward_deviation`), monotone in ``r_eff``. Hence
    ``requested <= budget / (1 - sin(b))`` forces ``deviation <= budget``
    whether or not the clamp fires. Taking the min over corners gives a
    single radius safe for the whole polyline, in one pass and with no
    iteration or convergence question — and it is TIGHT: at a corner that
    is not setback-clamped the resulting deviation equals ``budget_mm``
    exactly.

    Only corners :func:`fillet_polyline` would really round are counted
    (same skip rules, same eps thresholds — pass the SAME keyword
    overrides to both if you override either). A skipped corner stays a
    sharp vertex and adds no copper, so constraining the radius on its
    behalf would shrink every other corner's fillet for nothing.
    """
    if len(points) < 3 or budget_mm <= 0.0:
        return math.inf
    cap = math.inf
    for i in range(1, len(points) - 1):
        corner = _corner_fillet(
            points[i - 1],
            points[i],
            points[i + 1],
            # Any positive radius identifies which corners get rounded and
            # at what half-angle: the skip rules are angle/leg-length tests,
            # never radius tests (beyond ``radius_mm > 0``), and the clamp
            # cannot change the interior angle. The value is a probe, not a
            # proposal -- nothing about the returned geometry is used here.
            1.0,
            reversal_eps_rad=reversal_eps_rad,
            collinear_eps_rad=collinear_eps_rad,
        )
        if corner is None:
            continue
        shrink = 1.0 - math.sin(corner.half_angle_rad)
        if shrink <= 0.0:
            # theta == pi exactly: a straight run, no arc, no deviation.
            # Unreachable through the default collinear eps, kept so a
            # caller passing collinear_eps_rad=0 divides by nothing.
            continue
        cap = min(cap, budget_mm / shrink)
    return cap
