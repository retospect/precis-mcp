"""Edge-mating parts: a side-insertion connector must sit ON the board edge.

A USB receptacle, card-edge, many JST/FFC parts and barrel jacks mate by
SIDE insertion: the cable reaches the part only if the part sits on the
board outline with its mating face pointing OUTWARD (gr451356). A USB-C
10mm inside the board is not a tight fit, it is an unbuildable board. Nothing
else in the model knows this — ``board_edge_clearance`` / ``outline_
containment`` keep copper AWAY from / INSIDE the edge, and the placer's cost
terms have no edge affinity — so this module owns the one rule, three ways:

1. **The attribute** (:func:`parse_edge_mate`): an instance's ``edge_mate``
   (``pcb_instances.meta``, authored on the component put) is ``{"face":
   <direction>, "require": "required"|"preferred"}``. ``face`` is the
   part's OWN mating-face direction in the footprint-local frame —
   ``"N"|"E"|"S"|"W"`` (``N`` = local +y, ``E`` = local +x) or degrees
   clockwise from +y — so it survives the placer's 90-degree ROTATE moves
   and a bottom-side mirror (:func:`outward_vector` runs it through the
   same transform as a pad offset). It is AUTHORED, not inferred: EasyEDA
   footprints carry no mating direction, and guessing one from pad
   geometry would be a silent wrong answer on exactly the parts where it
   matters. ``require`` defaults to ``required``.
2. **The placer** (:func:`snap_to_edges`): after the seed, each such part
   is rotated so its face looks along the nearest outline edge's outward
   normal, slid flush onto that edge, and LOCKED (``inst_fixed_*``) so the
   anneal cannot walk it back inside. A part the author already pinned
   (``fixed``) or put in a rigid group is left alone — the DRC rule below
   is then what reports it.
3. **The check** (:func:`evaluate`, wired as ``drc.check_edge_mating``): the
   mating face's centre must lie within :data:`EDGE_TOL_MM` of an outline
   edge whose outward normal is within :data:`FACING_TOL_DEG` of the face
   direction.

Geometry is plain Python (shapely only for the pairwise overlap probe in the
slide), so :mod:`precis.pcb.ir` can import :func:`parse_edge_mate` without
pulling the geometry stack.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from precis.pcb import pose

if TYPE_CHECKING:
    from precis.pcb.ir import PcbIR

#: How far (mm) the mating-face centre may sit from the outline and still
#: count as "on the edge". The snap lands 1e-4 inside; this is the slack for
#: a hand-placed part, not for a part that is merely near.
EDGE_TOL_MM = 0.25

#: Max angle between the face direction and the edge's outward normal for
#: the part to count as facing out (a part 90 degrees off faces sideways).
FACING_TOL_DEG = 20.0

#: The snap parks the mating face this far inside the outline, so the
#: courtyard stays ``covered`` by it (``outline_containment``) despite float
#: noise.
_SNAP_INSET_MM = 1e-4

_COMPASS = {"N": 0.0, "E": 90.0, "S": 180.0, "W": 270.0}
_REQUIRE = ("required", "preferred")

Point = tuple[float, float]


@dataclass(frozen=True, slots=True)
class EdgeMateSpec:
    """A part's authored edge requirement: ``face_deg`` is the mating-face
    direction in the footprint-local frame (degrees clockwise from +y)."""

    require: str
    face_deg: float


@dataclass(frozen=True, slots=True)
class EdgeMateRecord:
    """One placed edge-mating part, in board space, ready for the check."""

    refdes: str
    require: str
    polygon: list[Point]  # placed courtyard
    outward: Point  # unit vector, board frame


def parse_edge_mate(raw: Any) -> EdgeMateSpec | None:
    """The authored ``edge_mate`` value -> :class:`EdgeMateSpec`, or ``None``
    when absent/``none``/malformed (an unreadable constraint degrades to
    "no constraint", like :func:`precis.pcb.ir._parse_instance_groups`)."""
    if not isinstance(raw, dict):
        return None
    face = raw.get("face")
    if isinstance(face, str) and face.strip().upper() in _COMPASS:
        deg = _COMPASS[face.strip().upper()]
    elif face is None:
        return None
    else:
        try:
            deg = float(face)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(deg):
            return None
    require = str(raw.get("require") or "required").strip().lower()
    if require not in _REQUIRE:
        return None
    return EdgeMateSpec(require=require, face_deg=deg % 360.0)


def outward_vector(
    spec: EdgeMateSpec, rot_deg: float, *, mirrored: bool = False
) -> Point:
    """The mating-face direction in board space for a pose: the local
    direction through the SAME mirror-then-rotate-clockwise transform a pad
    offset takes (:func:`precis.pcb.pose.rel_to_world`)."""
    a = math.radians(spec.face_deg)
    return pose.rel_to_world(math.sin(a), math.cos(a), rot_deg, mirrored=mirrored)


def _angle_cw(v: Point) -> float:
    """Degrees clockwise from +y — the board frame's heading convention."""
    return math.degrees(math.atan2(v[0], v[1])) % 360.0


Edge = tuple[Point, Point, Point]  # (a, b, outward unit normal)


def outline_edges(outline: Any) -> list[Edge]:
    """Each outline segment with its OUTWARD unit normal (signed area picks
    the winding, so either ring direction works)."""
    ring = [(float(p[0]), float(p[1])) for p in outline or []]
    if len(ring) > 1 and ring[0] == ring[-1]:
        ring = ring[:-1]
    if len(ring) < 3:
        return []
    area2 = sum(
        ring[i][0] * ring[(i + 1) % len(ring)][1]
        - ring[(i + 1) % len(ring)][0] * ring[i][1]
        for i in range(len(ring))
    )
    sign = 1.0 if area2 > 0 else -1.0  # CCW: outward is the right-hand side
    edges: list[Edge] = []
    for i, a in enumerate(ring):
        b = ring[(i + 1) % len(ring)]
        dx, dy = b[0] - a[0], b[1] - a[1]
        n = math.hypot(dx, dy)
        if n < 1e-9:
            continue
        edges.append((a, b, (sign * dy / n, -sign * dx / n)))
    return edges


def _seg_distance(p: Point, a: Point, b: Point) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def mating_face(poly: list[Point], d: Point) -> tuple[Point, float]:
    """``(centre, width)`` of the polygon's face looking along ``d``: the
    vertices extremal along ``d`` and the polygon's extent across it."""
    s = max(p[0] * d[0] + p[1] * d[1] for p in poly)
    face = [p for p in poly if p[0] * d[0] + p[1] * d[1] >= s - 1e-6]
    centre = (
        sum(p[0] for p in face) / len(face),
        sum(p[1] for p in face) / len(face),
    )
    across = [p[0] * -d[1] + p[1] * d[0] for p in poly]
    return centre, max(across) - min(across)


@dataclass(frozen=True, slots=True)
class EdgeVerdict:
    """``state``: ``"ok"`` | ``"interior"`` (face not near any edge) |
    ``"inward"`` (on an edge, facing the wrong way). ``distance_mm`` is the
    face centre's distance to the nearest edge; ``angle_deg`` the angle
    between the face direction and that edge's outward normal."""

    state: str
    distance_mm: float
    angle_deg: float


def evaluate(rec: EdgeMateRecord, edges: list[Edge]) -> EdgeVerdict:
    """Is ``rec`` on the outline with its face pointing out? Considers every
    edge within tolerance and passes if ANY faces the right way (a face
    centre at a corner is on two edges)."""
    centre, _w = mating_face(rec.polygon, rec.outward)
    best = min(edges, key=lambda e: _seg_distance(centre, e[0], e[1]))
    dist = _seg_distance(centre, best[0], best[1])
    near = [e for e in edges if _seg_distance(centre, e[0], e[1]) <= EDGE_TOL_MM]
    if not near:
        return EdgeVerdict("interior", dist, _angle_between(rec.outward, best[2]))
    angles = [_angle_between(rec.outward, e[2]) for e in near]
    if min(angles) <= FACING_TOL_DEG:
        return EdgeVerdict("ok", dist, min(angles))
    return EdgeVerdict("inward", dist, min(angles))


def _angle_between(u: Point, v: Point) -> float:
    c = max(-1.0, min(1.0, u[0] * v[0] + u[1] * v[1]))
    return math.degrees(math.acos(c))


def records(ir: PcbIR) -> list[EdgeMateRecord]:
    """Every PLACED edge-mating instance of ``ir`` as a board-space record
    (placed courtyard + outward vector) — the input of
    ``drc.check_edge_mating``. ``[]`` when no instance declares one."""
    specs = _specs(ir)
    if not any(s is not None for s in specs):
        return []
    from precis.pcb.cost import COURTYARD_MIN_SEPARATION_MM
    from precis.pcb.ir import COURTYARD_CLEARANCE_MM, instance_courtyard_polygons
    from precis.pcb.landpattern import place_points

    local = instance_courtyard_polygons(
        ir,
        clearance_mm=COURTYARD_CLEARANCE_MM,
        fallback_half_extent_mm=COURTYARD_MIN_SEPARATION_MM / 2.0,
    )
    out: list[EdgeMateRecord] = []
    for i, spec in enumerate(specs):
        if spec is None:
            continue
        x, y = float(ir.inst_x[i]), float(ir.inst_y[i])
        if not (math.isfinite(x) and math.isfinite(y)) or len(local[i]) < 3:
            continue
        rot = _rot(ir, i)
        mirrored = bool(ir.inst_bottom[i])
        out.append(
            EdgeMateRecord(
                refdes=str(ir.instance_refdes[i]),
                require=spec.require,
                polygon=place_points(
                    local[i], cx=x, cy=y, rot_deg=rot, mirrored=mirrored
                ),
                outward=outward_vector(spec, rot, mirrored=mirrored),
            )
        )
    return out


def _specs(ir: PcbIR) -> list[EdgeMateSpec | None]:
    raw = getattr(ir, "inst_edge_mate", None)
    if raw is None:
        return [None] * ir.n_instances
    return [raw[i] for i in range(ir.n_instances)]


def _rot(ir: PcbIR, i: int) -> float:
    r = float(ir.inst_rot[i])
    return 0.0 if math.isnan(r) else r


def snap_to_edges(ir: PcbIR) -> list[str]:
    """Place every unpinned edge-mating part flush on its nearest outline
    edge, face outward, and lock it. Returns the refdes list it moved.

    Per part: pick the nearest edge wide enough for the mating face (and,
    if the author locked the rotation, one that already faces the right
    way); rotate so the face direction equals that edge's outward normal;
    slide the face centre onto the edge (:data:`_SNAP_INSET_MM` inside);
    clamp along the edge so the part does not hang past a corner; step
    along the edge past any earlier snapped part's courtyard. No outline,
    no spec, an authored ``fixed`` position or a rigid group member: left
    alone (``drc.check_edge_mating`` then judges it as it stands).
    """
    edges = outline_edges(ir.outline)
    specs = _specs(ir)
    if not edges or not any(s is not None for s in specs):
        return []
    from shapely.geometry import Polygon

    from precis.pcb.cost import COURTYARD_MIN_SEPARATION_MM
    from precis.pcb.ir import COURTYARD_CLEARANCE_MM, instance_courtyard_polygons
    from precis.pcb.landpattern import place_points

    local = instance_courtyard_polygons(
        ir,
        clearance_mm=COURTYARD_CLEARANCE_MM,
        fallback_half_extent_mm=COURTYARD_MIN_SEPARATION_MM / 2.0,
    )
    xs = [p[0] for e in edges for p in (e[0], e[1])]
    ys = [p[1] for e in edges for p in (e[0], e[1])]
    mid = ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0)
    taken: list[Any] = []  # shapely polygons of parts already on the edge
    moved: list[str] = []
    for i, spec in enumerate(specs):
        if spec is None or len(local[i]) < 3:
            continue
        if bool(ir.inst_fixed_xy[i]) or int(ir.inst_group[i]) >= 0:
            continue
        mirrored = bool(ir.inst_bottom[i])
        x, y = float(ir.inst_x[i]), float(ir.inst_y[i])
        if not (math.isfinite(x) and math.isfinite(y)):
            x, y = mid
        rot_locked = bool(ir.inst_fixed_rot[i])
        base = outward_vector(spec, 0.0, mirrored=mirrored)
        _c, width = mating_face(local[i], base)
        cands = [e for e in edges if math.dist(e[0], e[1]) >= width] or edges
        if rot_locked:
            cur = outward_vector(spec, _rot(ir, i), mirrored=mirrored)
            cands = [e for e in cands if _angle_between(cur, e[2]) <= FACING_TOL_DEG]
            if not cands:
                continue
        a, b, n = min(cands, key=lambda e: _seg_distance((x, y), e[0], e[1]))
        rot = _rot(ir, i)
        if not rot_locked:
            rot = round((_angle_cw(n) - _angle_cw(base)) % 360.0, 6) % 360.0
        d = outward_vector(spec, rot, mirrored=mirrored)
        poly = place_points(local[i], cx=x, cy=y, rot_deg=rot, mirrored=mirrored)
        centre, width = mating_face(poly, d)
        sd = (centre[0] - a[0]) * n[0] + (centre[1] - a[1]) * n[1]
        shift = -_SNAP_INSET_MM - sd
        x, y = x + shift * n[0], y + shift * n[1]
        length = math.dist(a, b)
        t = ((b[0] - a[0]) / length, (b[1] - a[1]) / length)
        u = (centre[0] + shift * n[0] - a[0]) * t[0] + (
            centre[1] + shift * n[1] - a[1]
        ) * t[1]
        lo, hi = width / 2.0, length - width / 2.0
        u_goal = max(lo, min(hi, u)) if lo <= hi else length / 2.0
        step = width + 1.0
        for k in range(0, 2 * int(length / step) + 2):
            off = ((k + 1) // 2) * step * (1 if k % 2 else -1)
            u_try = u_goal + off
            if lo <= hi and not (lo <= u_try <= hi):
                continue
            tx, ty = x + (u_try - u) * t[0], y + (u_try - u) * t[1]
            cand = Polygon(
                place_points(local[i], cx=tx, cy=ty, rot_deg=rot, mirrored=mirrored)
            )
            if not any(cand.intersects(o) for o in taken):
                x, y = tx, ty
                break
        else:
            x, y = x + (u_goal - u) * t[0], y + (u_goal - u) * t[1]
        ir.move_instance(i, x=x, y=y, rot=rot)
        ir.lock_instance(i, xy=True, rot=True)
        taken.append(
            Polygon(place_points(local[i], cx=x, cy=y, rot_deg=rot, mirrored=mirrored))
        )
        moved.append(str(ir.instance_refdes[i]))
    return moved
