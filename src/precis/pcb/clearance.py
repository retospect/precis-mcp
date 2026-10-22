"""The exact clearance kernel (``docs/backlog/pcb-vector-router.md`` step 1,
slice 2): one definition of "how far apart are these two pieces of copper",
built once per board from the same :func:`precis.pcb.realize.to_gerber_model`
dict :mod:`precis.pcb.drc` reads.

**Bodies, not polygons.** Every copper item becomes a :class:`Body`: a
*core* (a shapely point, line string or polygon holding the item's straight
geometry), a list of circular *arcs* (an arc track's centrelines), and a
radius ``r`` swept around both. A via or round pad is a point core with
``r`` = its radius; an obround pad a two-point line with ``r`` = half its
short side; a track its line runs plus its arcs with ``r`` = half its
width; a rect/polygon pad or a pour a polygon core with ``r = 0``. The
edge-to-edge gap of two bodies is the distance between their cores and
arcs minus both radii, and every piece of that is exact:

- core to core: shapely's ``distance`` between points, line strings and
  polygons (exact on straight edges; nothing is buffered);
- anything to an arc: closed forms (point–arc, segment–arc, arc–arc, with
  the candidate sets of ``perplexity-research:491941`` §3), a polygon
  being its edges plus a containment test.

So the verdict carries no polygon tolerance: the 15° chord flattening of
``drc._arc_points`` and the inscribed 64-segment buffers are gone from the
clearance path. Polygons remain only where a consumer needs them, and then
they are drawn outward (:func:`outward_circle`, Decision 7: 5 µm sagitta).

**Index.** One STRtree per layer over each body's bounding box, grown by
its radius. A box is a superset of the body, so pruning never drops a pair
the exact test would keep.

The independent check of this module is
:func:`precis.pcb.drc.clearance_violations_naive` (no shapely), asserted
equal to it over randomized layouts in ``tests/test_pcb_clearance.py``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import shapely
from shapely import STRtree, box
from shapely.geometry import LineString, MultiLineString, Point, Polygon
from shapely.geometry.base import BaseGeometry

from precis.pcb.padplace import pad_world_rot
from precis.pcb.pose import rotate_cw as _rotate_cw

Coord = tuple[float, float]

#: Angular slack when deciding whether a direction lies inside an arc's
#: sweep: a point on the arc's own endpoint must count as on the arc.
_ANG_EPS = 1e-12
#: Decision 6: the router's margin and the outward polygonisation sagitta.
DELTA_MM = 0.005


@dataclass(frozen=True, slots=True)
class Arc:
    """A circular arc: centre, radius, start angle and SIGNED sweep
    (radians; positive is counter-clockwise, gerber G03)."""

    cx: float
    cy: float
    radius: float
    a0: float
    sweep: float

    def contains_angle(self, theta: float) -> bool:
        if abs(self.sweep) >= math.tau - _ANG_EPS:
            return True
        if self.sweep >= 0:
            d = (theta - self.a0) % math.tau
            return d <= self.sweep + _ANG_EPS or d >= math.tau - _ANG_EPS
        d = (self.a0 - theta) % math.tau
        return d <= -self.sweep + _ANG_EPS or d >= math.tau - _ANG_EPS

    def point_at(self, theta: float) -> Coord:
        return (
            self.cx + self.radius * math.cos(theta),
            self.cy + self.radius * math.sin(theta),
        )

    def endpoints(self) -> tuple[Coord, Coord]:
        return self.point_at(self.a0), self.point_at(self.a0 + self.sweep)

    def bounds(self) -> tuple[float, float, float, float]:
        (sx, sy), (ex, ey) = self.endpoints()
        xs, ys = [sx, ex], [sy, ey]
        for k in range(4):
            theta = k * math.pi / 2
            if self.contains_angle(theta):
                px, py = self.point_at(theta)
                xs.append(px)
                ys.append(py)
        return min(xs), min(ys), max(xs), max(ys)


def arc_from_segment(seg: dict[str, Any]) -> Arc:
    """A gerber-shaped ``{"shape": "arc", start, end, center, cw}`` segment
    as an :class:`Arc` — the sweep convention of ``drc._arc_points``
    (``cw`` taken literally, defaulting to clockwise)."""
    cx, cy = float(seg["center"][0]), float(seg["center"][1])
    sx, sy = float(seg["start"][0]), float(seg["start"][1])
    ex, ey = float(seg["end"][0]), float(seg["end"][1])
    a1 = math.atan2(sy - cy, sx - cx)
    a2 = math.atan2(ey - cy, ex - cx)
    diff = (a2 - a1) % math.tau
    sweep = diff if not seg.get("cw", True) else diff - math.tau
    return Arc(cx, cy, math.hypot(sx - cx, sy - cy), a1, sweep)


# ── closed-form distances (centreline to centreline) ─────────────────────


def point_segment(p: Coord, a: Coord, b: Coord) -> float:
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    den = dx * dx + dy * dy
    if den == 0.0:
        return math.hypot(p[0] - ax, p[1] - ay)
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / den))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def point_arc(p: Coord, arc: Arc) -> float:
    dx, dy = p[0] - arc.cx, p[1] - arc.cy
    rho = math.hypot(dx, dy)
    if rho > 0.0 and arc.contains_angle(math.atan2(dy, dx)):
        return abs(rho - arc.radius)
    if rho == 0.0:
        return arc.radius
    (sx, sy), (ex, ey) = arc.endpoints()
    return min(math.hypot(p[0] - sx, p[1] - sy), math.hypot(p[0] - ex, p[1] - ey))


def segment_arc(a: Coord, b: Coord, arc: Arc) -> float:
    """Exact distance between segment ``ab`` and ``arc``. Candidates: each
    end of either against the other; the foot of the perpendicular from
    the centre (the only interior–interior critical point); and the
    segment's crossings of the circle, which are 0 when on the arc."""
    best = min(point_arc(a, arc), point_arc(b, arc))
    s, e = arc.endpoints()
    best = min(best, point_segment(s, a, b), point_segment(e, a, b))
    dx, dy = b[0] - a[0], b[1] - a[1]
    den = dx * dx + dy * dy
    if den == 0.0:
        return best
    fx, fy = arc.cx - a[0], arc.cy - a[1]
    t = (fx * dx + fy * dy) / den
    if 0.0 < t < 1.0:
        px, py = a[0] + t * dx, a[1] + t * dy
        best = min(best, point_arc((px, py), arc))
    # Crossings: |a + u d - c|^2 = R^2.
    qa = den
    qb = -2.0 * (fx * dx + fy * dy)
    qc = fx * fx + fy * fy - arc.radius * arc.radius
    disc = qb * qb - 4.0 * qa * qc
    if disc >= 0.0:
        root = math.sqrt(disc)
        for u in ((-qb - root) / (2 * qa), (-qb + root) / (2 * qa)):
            if 0.0 <= u <= 1.0:
                px, py = a[0] + u * dx, a[1] + u * dy
                if arc.contains_angle(math.atan2(py - arc.cy, px - arc.cx)):
                    return 0.0
    return best


def arc_arc(p: Arc, q: Arc) -> float:
    """Exact distance between two arcs: endpoints of each against the
    other, the points of each on the line of centres (the interior–interior
    critical points), and the circles' crossings (0 when on both arcs)."""
    best = min(point_arc(pt, q) for pt in p.endpoints())
    best = min(best, *(point_arc(pt, p) for pt in q.endpoints()))
    dx, dy = q.cx - p.cx, q.cy - p.cy
    d = math.hypot(dx, dy)
    if d == 0.0:
        # Concentric: the gap is |R_p - R_q| wherever the sweeps overlap.
        if any(q.contains_angle(t) for t in (p.a0, p.a0 + p.sweep)) or any(
            p.contains_angle(t) for t in (q.a0, q.a0 + q.sweep)
        ):
            return min(best, abs(p.radius - q.radius))
        return best
    toward = math.atan2(dy, dx)
    for theta in (toward, toward + math.pi):
        if p.contains_angle(theta):
            best = min(best, point_arc(p.point_at(theta), q))
        if q.contains_angle(theta):
            best = min(best, point_arc(q.point_at(theta), p))
    # Circle–circle crossings.
    if abs(p.radius - q.radius) <= d <= p.radius + q.radius:
        along = (p.radius**2 - q.radius**2 + d * d) / (2 * d)
        h = math.sqrt(max(0.0, p.radius**2 - along * along))
        mx, my = p.cx + along * dx / d, p.cy + along * dy / d
        for sign in (1.0, -1.0):
            ix, iy = mx - sign * h * dy / d, my + sign * h * dx / d
            if p.contains_angle(math.atan2(iy - p.cy, ix - p.cx)) and q.contains_angle(
                math.atan2(iy - q.cy, ix - q.cx)
            ):
                return 0.0
    return best


def _edge_boxes(core: BaseGeometry) -> np.ndarray:
    """Every straight edge of ``core`` as an ``(n, 4)`` array of
    ``x0, y0, x1, y1``."""
    if isinstance(core, Polygon):
        rings = [core.exterior, *core.interiors]
    elif isinstance(core, MultiLineString):
        rings = list(core.geoms)
    else:
        rings = [core]
    parts = []
    for ring in rings:
        xy = shapely.get_coordinates(ring)
        if len(xy) >= 2:
            parts.append(np.hstack([xy[:-1], xy[1:]]))
    return np.vstack(parts) if parts else np.empty((0, 4))


def core_arc(core: BaseGeometry, arc: Arc) -> float:
    """Exact distance between a shapely core and an arc.

    A pour has thousands of edges, so they are culled first, exactly: the
    arc's start point gives an upper bound ``ub`` (a distance the pair
    really achieves), and an edge whose box lies farther than ``ub`` from
    the arc's box cannot hold a closer point. The closed form runs on the
    survivors only."""
    start, _ = arc.endpoints()
    if isinstance(core, Point):
        return point_arc((core.x, core.y), arc)
    if isinstance(core, Polygon) and core.covers(Point(start)):
        return 0.0
    ub = float(core.distance(Point(start)))
    edges = _edge_boxes(core)
    if not len(edges):
        return math.inf
    ax0, ay0, ax1, ay1 = arc.bounds()
    ex0 = np.minimum(edges[:, 0], edges[:, 2])
    ex1 = np.maximum(edges[:, 0], edges[:, 2])
    ey0 = np.minimum(edges[:, 1], edges[:, 3])
    ey1 = np.maximum(edges[:, 1], edges[:, 3])
    dx = np.maximum(0.0, np.maximum(ex0 - ax1, ax0 - ex1))
    dy = np.maximum(0.0, np.maximum(ey0 - ay1, ay0 - ey1))
    near = edges[np.hypot(dx, dy) <= ub + 1e-12]
    best = ub
    for x0, y0, x1, y1 in near.tolist():
        best = min(best, segment_arc((x0, y0), (x1, y1), arc))
        if best <= 0.0:
            return 0.0
    return best


# ── bodies ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Body:
    """One copper item as the kernel measures it (module docstring)."""

    core: BaseGeometry | None
    arcs: tuple[Arc, ...]
    r: float

    def bounds(self) -> tuple[float, float, float, float]:
        boxes = [a.bounds() for a in self.arcs]
        if self.core is not None and not self.core.is_empty:
            x0, y0, x1, y1 = self.core.bounds
            boxes.append((x0, y0, x1, y1))
        x0 = min(b[0] for b in boxes) - self.r
        y0 = min(b[1] for b in boxes) - self.r
        x1 = max(b[2] for b in boxes) + self.r
        y1 = max(b[3] for b in boxes) + self.r
        return x0, y0, x1, y1


def gap(a: Body, b: Body) -> float:
    """Edge-to-edge gap between two bodies, 0 when they touch or overlap."""
    return max(0.0, signed_gap(a, b))


def signed_gap(a: Body, b: Body) -> float:
    """Core-and-arc distance minus both radii, unclamped: negative by the
    overlap depth while the cores stay apart (two discs, a disc and a
    track), 0 minus the radii once the cores touch."""
    centre = math.inf
    if a.core is not None and b.core is not None:
        centre = float(a.core.distance(b.core))
    for arc in b.arcs:
        if centre <= 0.0:
            break
        if a.core is not None:
            centre = min(centre, core_arc(a.core, arc))
        for other in a.arcs:
            centre = min(centre, arc_arc(other, arc))
    if a.arcs and b.core is not None:
        for arc in a.arcs:
            if centre <= 0.0:
                break
            centre = min(centre, core_arc(b.core, arc))
    return centre - a.r - b.r


def _track_body(item: dict[str, Any]) -> Body | None:
    runs: list[list[Coord]] = []
    arcs: list[Arc] = []
    run: list[Coord] = []
    for seg in item.get("segments") or []:
        start = (float(seg["start"][0]), float(seg["start"][1]))
        end = (float(seg["end"][0]), float(seg["end"][1]))
        if seg.get("shape") == "arc":
            if len(run) >= 2:
                runs.append(run)
            run = []
            arcs.append(arc_from_segment(seg))
            continue
        if not run or run[-1] != start:
            if len(run) >= 2:
                runs.append(run)
            run = [start]
        run.append(end)
    if len(run) >= 2:
        runs.append(run)
    if not runs and not arcs:
        return None
    core: BaseGeometry | None
    if not runs:
        core = None
    elif len(runs) == 1:
        core = LineString(runs[0])
    else:
        core = MultiLineString(runs)
    return Body(core, tuple(arcs), float(item.get("width_mm", 0.0)) / 2.0)


def body_of(item: dict[str, Any]) -> Body | None:
    """The kernel body of one ``model`` item — a ``copper`` row (track,
    via, pour) or a pad tagged ``ctype='pad'`` — or ``None`` when it is
    degenerate (no length, no size). Shapes match
    ``drc._copper_item_polygon`` exactly except that nothing is buffered."""
    ctype = item.get("ctype")
    if ctype == "track":
        return _track_body(item)
    if ctype == "via":
        r = float(item.get("dia_mm", 0.0)) / 2.0
        if r <= 0:
            return None
        return Body(Point(float(item["x"]), float(item["y"])), (), r)
    if ctype == "pour":
        poly = item.get("polygon") or []
        if len(poly) < 3:
            return None
        holes = [
            [(float(p[0]), float(p[1])) for p in hole]
            for hole in item.get("holes") or []
            if len(hole) >= 3
        ]
        return Body(Polygon([(float(p[0]), float(p[1])) for p in poly], holes), (), 0.0)
    if ctype == "pad":
        shape = item.get("shape", "circle")
        if shape == "polygon":
            poly = item.get("poly") or []
            if len(poly) < 3:
                return None
            return Body(Polygon([(float(p[0]), float(p[1])) for p in poly]), (), 0.0)
        x, y = float(item["x"]), float(item["y"])
        w = float(item.get("w", 0.0))
        h = float(item.get("h", w))
        if w <= 0 or h <= 0:
            return None
        if shape == "circle" or (shape == "obround" and w == h):
            return Body(Point(x, y), (), w / 2.0)
        # ``oblique_rot`` is the pad's recorded total rotation (instance +
        # pad, CW-from-north degrees — ``padplace.place_footprint_pads``
        # writes it when a placed land no longer lands axis-aligned). The
        # unrotated w×h box is NOT the pad: its corners reach past the true
        # rotated outline (a rotated exposed pad firing a phantom
        # pad-vs-pad clearance hit against its own part's pins was the
        # seed-2 nano refusal) while its edges under-cover the true land.
        # Board-frame angle via `pad_world_rot` (bottom: instance - pad,
        # gr504558), the same reader the DRC shape function uses.
        rot = pad_world_rot(item)

        def _rp(px: float, py: float) -> tuple[float, float]:
            rx, ry = _rotate_cw(px, py, rot)
            return x + rx, y + ry

        if shape == "rect":
            hw, hh = w / 2.0, h / 2.0
            return Body(
                Polygon(
                    [
                        _rp(px, py)
                        for px, py in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))
                    ]
                ),
                (),
                0.0,
            )
        if shape == "obround":
            if w > h:
                half = (w - h) / 2.0
                ends = [(-half, 0.0), (half, 0.0)]
                r = h / 2.0
            else:
                half = (h - w) / 2.0
                ends = [(0.0, -half), (0.0, half)]
                r = w / 2.0
            return Body(LineString([_rp(px, py) for px, py in ends]), (), r)
        raise ValueError(f"unknown pad shape {shape!r}")
    return None


# ── the per-layer index ──────────────────────────────────────────────────


@dataclass
class LayerIndex:
    """Bodies on one layer and their box tree; ``entries`` are the
    caller's item indices, parallel to ``bodies``."""

    entries: list[int] = field(default_factory=list)
    nets: list[str] = field(default_factory=list)
    bodies: list[Body] = field(default_factory=list)
    _tree: STRtree | None = None

    def tree(self) -> STRtree:
        if self._tree is None:
            self._tree = STRtree([box(*b.bounds()) for b in self.bodies])
        return self._tree


def different_net_pairs(
    items: Sequence[dict[str, Any]],
    layers_of: Sequence[Sequence[str]],
    *,
    within_mm: float,
    every_layer: bool = False,
) -> list[tuple[int, int, float, str]]:
    """``(i, j, gap_mm, layer)`` for every different-net pair of ``items``
    sharing a layer with an exact gap below ``within_mm`` (``i < j``).
    ``layers_of[k]`` is item k's layer list (a via or drilled pad has
    several). Without ``every_layer`` a pair is reported once, on the
    first layer (in layer order of first appearance) where it falls short;
    with it, once per layer — what a caller with per-layer rules needs.
    Output is sorted by layer of first appearance, then ``(i, j)``."""
    index: dict[str, LayerIndex] = {}
    bodies: list[Body | None] = [body_of(item) for item in items]
    for k, body in enumerate(bodies):
        if body is None:
            continue
        net = str(items[k].get("net", ""))
        for layer in layers_of[k]:
            li = index.setdefault(str(layer), LayerIndex())
            li.entries.append(k)
            li.nets.append(net)
            li.bodies.append(body)
    out: list[tuple[int, int, float, str]] = []
    seen: set[tuple[int, int]] = set()
    for layer, li in index.items():
        if len(li.entries) < 2:
            continue
        tree = li.tree()
        boxes = [box(*b.bounds()) for b in li.bodies]
        left, right = tree.query(boxes, predicate="dwithin", distance=within_mm)
        found: list[tuple[int, int, float]] = []
        for p, q in zip(left.tolist(), right.tolist(), strict=True):
            if p >= q or li.nets[p] == li.nets[q]:
                continue
            i, j = li.entries[p], li.entries[q]
            if i == j:
                continue
            key = (min(i, j), max(i, j))
            if key in seen and not every_layer:
                continue
            g = gap(li.bodies[p], li.bodies[q])
            if g < within_mm - 1e-9:
                found.append((key[0], key[1], g))
        for i, j, g in sorted(found):
            if (i, j) in seen and not every_layer:
                continue
            seen.add((i, j))
            out.append((i, j, g, layer))
    return out


# ── outward polygonisation (Decision 7) ──────────────────────────────────


def outward_segments_per_quadrant(radius: float, sagitta: float = DELTA_MM) -> int:
    """Segments per quarter circle so that a circumscribed polygon of a
    circle of ``radius`` stays within ``sagitta`` of it."""
    if radius <= sagitta:
        return 1
    # Circumscribed regular polygon with n sides: vertex radius R/cos(pi/n),
    # deviation R(1/cos(pi/n) - 1) <= sagitta.
    half_angle = math.acos(radius / (radius + sagitta))
    return max(1, math.ceil(math.pi / (4 * half_angle)))


def outward_circle(
    x: float, y: float, radius: float, sagitta: float = DELTA_MM
) -> Polygon:
    """The circumscribed polygon of a circle: every point of the circle
    lies inside it, and no vertex lies more than ``sagitta`` outside it.
    For consumers that only take polygons (CDT input, pour booleans,
    views) — never for a clearance verdict."""
    q = outward_segments_per_quadrant(radius, sagitta)
    n = 4 * q
    vertex = radius / math.cos(math.pi / n)
    return Polygon(
        [
            (
                x + vertex * math.cos(2 * math.pi * (k + 0.5) / n),
                y + vertex * math.sin(2 * math.pi * (k + 0.5) / n),
            )
            for k in range(n)
        ]
    )


def outward_polygon(
    body: Body, grow: float = 0.0, sagitta: float = DELTA_MM
) -> BaseGeometry:
    """A polygon holding every point within ``body.r + grow`` of the body's
    core and arcs, and reaching no more than ``sagitta`` past that — what a
    pour antipad is cut with, so a pour is generated legal rather than
    flagged. Each arc is flattened with chord error ``e = sagitta / 4``
    and the polyline buffered ``e`` further, so it holds the true arc; on
    the inner side of a bend that costs ``2e``. The round buffer's facets
    are circumscribed (as :func:`outward_circle`) within another ``e``,
    and a last ``e`` absorbs GEOS's own buffer rounding and input
    simplification (measured at tens of nm): at most ``sagitta`` past the
    grown body anywhere."""
    e = sagitta / 4.0
    parts: list[BaseGeometry] = []
    if body.core is not None and not body.core.is_empty:
        parts.append(body.core)
    for arc in body.arcs:
        if arc.radius <= e:
            step = math.pi
        else:
            step = 2.0 * math.acos(1.0 - e / arc.radius)
        n = max(1, math.ceil(abs(arc.sweep) / step))
        parts.append(
            LineString([arc.point_at(arc.a0 + arc.sweep * k / n) for k in range(n + 1)])
        )
    reach = body.r + grow + e + (e if body.arcs else 0.0)
    q = outward_segments_per_quadrant(reach, e)
    return shapely.union_all(parts).buffer(
        reach / math.cos(math.pi / (4 * q)), quad_segs=q
    )


__all__ = [
    "DELTA_MM",
    "Arc",
    "Body",
    "arc_arc",
    "arc_from_segment",
    "body_of",
    "core_arc",
    "different_net_pairs",
    "gap",
    "outward_circle",
    "outward_polygon",
    "outward_segments_per_quadrant",
    "point_arc",
    "point_segment",
    "segment_arc",
    "signed_gap",
]
