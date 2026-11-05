"""Crossings between taut wires (G1): where two branches' polylines swap
sides, which edge-index brackets the crossing sits in on each, and which
pairs of crossings bound an empty bigon the wires can slide apart through.

The one place that judges it, for both users: ``realise.assign_layers``
(the finished :class:`~.wires.Wires`) and ``wires.build_wires`` (each net
against the nets built before it, G2e). Everything takes a plain
``Sequence[WirePath]``, so neither needs the other's container. This
module imports ``wires`` for typing only: ``wires`` imports it.
"""

from __future__ import annotations

import bisect
import itertools
import math
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import numpy as np
import shapely
from shapely.geometry import LineString, Polygon

from precis.pcb.topoplace.mesh import TopoMesh

if TYPE_CHECKING:
    from precis.pcb.topoplace.wires import WirePath

CROSS_EPS = 1e-7  # mm: a point this close to a segment lies ON it
ARM_TOL = 1e-9  # rad: arms closer than this are collinear, not interleaved

Pt = tuple[float, float]

#: A crossing: arc positions on branch ``a`` and ``b``, and its bracket.
Cross = tuple[float, float, tuple[int, int, int, int]]

#: A branch's polyline, its segment lengths and the arc position of each
#: crossed edge on it (:func:`branch_geom`).
Geom = tuple[list[Pt], list[float], list[float]]


def edge_xy(mesh: TopoMesh, edge: int, t: float) -> tuple[float, float]:
    """World point at parameter ``t`` along mesh edge ``edge``."""
    u, v = mesh.edges[edge]
    a, b_ = mesh.verts[int(u)], mesh.verts[int(v)]
    return (float(a[0] + t * (b_[0] - a[0])), float(a[1] + t * (b_[1] - a[1])))


def seg_dist(xy: Pt, p: Pt, q: Pt) -> float:
    """Point-to-segment distance — ``xy`` to ``p→q``."""
    dx, dy = q[0] - p[0], q[1] - p[1]
    d2 = dx * dx + dy * dy
    f = (
        0.0
        if d2 == 0
        else min(1.0, max(0.0, ((xy[0] - p[0]) * dx + (xy[1] - p[1]) * dy) / d2))
    )
    return math.dist(xy, (p[0] + f * dx, p[1] + f * dy))


def poly_project(pts: Sequence[Pt], seg: Sequence[float], q: Pt) -> float:
    """Arc length along the polyline of the point nearest ``q``."""
    best_d, best_dist, acc = 0.0, math.inf, 0.0
    for i in range(len(seg)):
        (x0, y0), (x1, y1) = pts[i], pts[i + 1]
        dx, dy = x1 - x0, y1 - y0
        l2 = dx * dx + dy * dy
        u = (
            0.0
            if l2 <= 0.0
            else min(max(((q[0] - x0) * dx + (q[1] - y0) * dy) / l2, 0.0), 1.0)
        )
        dd = math.dist((x0 + u * dx, y0 + u * dy), q)
        if dd < best_dist:
            best_dist, best_d = dd, acc + u * seg[i]
        acc += seg[i]
    return best_d


def arm_angles(pts: Sequence[Pt], p: Pt) -> list[float]:
    """Directions of the polyline's arms leaving ``p``: one at a pad
    endpoint, two at a vertex or a segment interior."""
    out: list[float] = []
    for q0, q1 in itertools.pairwise(pts):
        if math.dist(q0, q1) <= CROSS_EPS or seg_dist(p, q0, q1) > CROSS_EPS:
            continue
        out += [
            math.atan2(q[1] - p[1], q[0] - p[0])
            for q in (q0, q1)
            if math.dist(q, p) > CROSS_EPS
        ]
    return out


def interleaved(a: Sequence[float], b: Sequence[float]) -> bool:
    """Whether arm pair ``b`` straddles arm pair ``a`` around their
    common point — the two polylines swap sides there. A ``b`` arm
    collinear with an ``a`` arm is a touch, not a crossing (a same-side
    vertex touch likewise leaves both ``b`` arms on one side)."""
    span = (a[1] - a[0]) % math.tau
    if span < ARM_TOL or math.tau - span < ARM_TOL:
        return False
    inside = []
    for x in b:
        off = (x - a[0]) % math.tau
        if min(off, math.tau - off, abs(off - span)) < ARM_TOL:
            return False
        inside.append(off < span)
    return inside[0] != inside[1]


def _lateral(pts: Sequence[Pt], p: Pt, into: float, ref: float) -> float | None:
    """Lateral offset of a wire at the end ``p`` of a shared run, as the
    sign of its side of the run's travel ``ref`` times how far its one
    arm off the run bends away from the line: ``pi - theta`` for ``theta``
    the angle between the arm and ``into`` (the direction from ``p``
    into the run). A wire running straight on through the end sits on
    the line (0); of two wires joining on one side, the arm more acute to
    the run is the outer one. ``None`` when the wire has no single arm
    off the run (a pad endpoint)."""
    arms = [
        x
        for x in arm_angles(pts, p)
        if abs((x - into + math.pi) % math.tau - math.pi) > ARM_TOL
    ]
    if len(arms) != 1:
        return None
    theta = abs((arms[0] - into + math.pi) % math.tau - math.pi)
    if math.pi - theta <= ARM_TOL:
        return 0.0
    return math.copysign(math.pi - theta, math.sin(arms[0] - ref))


def _overlap_swaps(
    pts_a: Sequence[Pt], pts_b: Sequence[Pt], pieces: Sequence[tuple[Pt, Pt]]
) -> list[Pt]:
    """Where two polylines that share copper along a collinear run swap
    lateral order. ``pieces`` are the overlapped sub-segments; chained
    end to end they form runs, and a run is a crossing iff the wires'
    lateral order (:func:`_lateral`) at its entry is the opposite of
    that at its exit, both strictly ordered. This is the one-vertex
    touch (:func:`interleaved`) stretched along a run, so the two agree
    when the run shrinks to a point. A run ending at a pad, where a wire
    has no arm, leaves the order open: not a crossing. Returns one point
    per crossing run."""

    def key(p: Pt) -> tuple[int, int]:
        return (round(p[0] * 1e6), round(p[1] * 1e6))

    nbr: dict[tuple[int, int], list[Pt]] = {}
    node: dict[tuple[int, int], Pt] = {}
    for p, q in pieces:
        for u, v in ((p, q), (q, p)):
            nbr.setdefault(key(u), []).append(v)
            node[key(u)] = u

    out: list[Pt] = []
    seen: set[tuple[int, int]] = set()
    for k0, nxt in nbr.items():
        if len(nxt) != 1 or k0 in seen:
            continue
        path = [node[k0], nxt[0]]
        prev, cur = k0, key(nxt[0])
        while len(nbr[cur]) == 2 and len(path) <= len(pieces):
            step = next(v for v in nbr[cur] if key(v) != prev)
            prev, cur = cur, key(step)
            path.append(step)
        if len(nbr[cur]) != 1:
            continue
        seen.add(cur)
        t0 = math.atan2(path[1][1] - path[0][1], path[1][0] - path[0][0])
        t1 = math.atan2(path[-1][1] - path[-2][1], path[-1][0] - path[-2][0])
        ea, eb = (_lateral(pts, path[0], t0, t0) for pts in (pts_a, pts_b))
        xa, xb = (_lateral(pts, path[-1], t1 + math.pi, t1) for pts in (pts_a, pts_b))
        if ea is None or eb is None or xa is None or xb is None:
            continue
        d0, d1 = ea - eb, xa - xb
        if abs(d0) > ARM_TOL and abs(d1) > ARM_TOL and d0 * d1 < 0:
            out.append(
                ((path[0][0] + path[1][0]) / 2.0, (path[0][1] + path[1][1]) / 2.0)
            )
    return out


def arc_bracket(marks: Sequence[float], s: float) -> tuple[int, int]:
    """``(lo, hi)`` edge-index bracket of arc position ``s`` on a branch
    whose crossed edges sit at arc positions ``marks``: the piece
    between edge marks ``k-1`` and ``k`` is run index ``k``, so a
    crossing there brackets ``(k-1, k]``. A pad stub before the first
    mark brackets ``(-1, 0]``; one past the last mark takes the last
    addressable piece, ``(E-2, E-1]`` — a run has no index past
    ``E-1``."""
    n = len(marks)
    k = bisect.bisect_right(marks, s)
    if k <= 0 or n <= 1:
        return (-1, 0)
    return (min(k, n - 1) - 1, min(k, n - 1))


def branch_geom(mesh: TopoMesh, br: WirePath) -> Geom:
    """A branch's polyline, its segment lengths and the arc position of
    each crossed edge on it — what :func:`crossing_events` reads per
    branch, kept by a caller that tests one branch against many."""
    pts = [(float(x), float(y)) for x, y in br.points]
    seg = [math.dist(p, q) for p, q in itertools.pairwise(pts)]
    marks = [
        poly_project(pts, seg, edge_xy(mesh, int(e), float(t)))
        for e, t in zip(br.edges, br.t)
    ]
    return pts, seg, marks


def crossing_events(
    mesh: TopoMesh,
    branches: Sequence[WirePath],
    geom: Sequence[Geom] | None = None,
    first_only: bool = False,
) -> dict[tuple[int, int], list[Cross]]:
    """Every crossing between taut polylines (G1), by branch pair
    ``a < b``: two branches cross where their ``points`` intersect and
    the polylines swap sides there. A proper segment crossing always
    does; at a shared interior vertex or a vertex lying on the other's
    segment the arms must interleave around the point
    (:func:`interleaved`), so a same-side touch is none. A shared pad
    endpoint is no crossing — an endpoint has one arm. A collinear
    overlap is judged over the whole shared run
    (:func:`_overlap_swaps`).

    Each crossing brackets ``(lo, hi]`` on each branch's own edge-index
    space (:func:`arc_bracket`). Same-net pairs stay — the caller
    filters. ``geom`` is :func:`branch_geom` per branch where the caller
    kept it; ``first_only`` reads only the pairs ``(0, j)`` — branch 0
    against the rest, one search tree for all of them."""
    pts_of: list[list[Pt]] = []
    seg_of: list[list[float]] = []
    marks_of: list[list[float]] = []
    segs: list[Any] = []
    owner: list[int] = []
    for bi, br in enumerate(branches):
        pts, seg, marks = geom[bi] if geom is not None else branch_geom(mesh, br)
        pts_of.append(pts)
        seg_of.append(seg)
        marks_of.append(marks)
        for p, q in itertools.pairwise(pts):
            if math.dist(p, q) > CROSS_EPS:
                segs.append(LineString([p, q]))
                owner.append(bi)
    events: dict[tuple[int, int], list[Cross]] = {}
    if not segs:
        return events
    # per branch pair: contact points (keyed by their 1e-6 grid cell) and
    # collinear-overlap pieces
    touch: dict[tuple[int, int], dict[tuple[int, int], Pt]] = {}
    shared: dict[tuple[int, int], list[tuple[Pt, Pt]]] = {}
    tree = shapely.STRtree(segs)
    if first_only:
        mine = np.asarray([i for i, o in enumerate(owner) if o == 0], dtype=int)
        if not len(mine):
            return events
        q_in, ti = tree.query([segs[i] for i in mine], predicate="intersects")
        qi = mine[q_in]
    else:
        qi, ti = tree.query(segs, predicate="intersects")
    for i, j in zip(qi.tolist(), ti.tolist()):
        a, b = owner[i], owner[j]
        if a >= b:
            continue
        inter = segs[i].intersection(segs[j])
        if inter.geom_type == "Point":
            touch.setdefault((a, b), {})[
                (round(inter.x * 1e6), round(inter.y * 1e6))
            ] = (inter.x, inter.y)
        elif inter.length > CROSS_EPS:
            c = list(inter.coords)
            shared.setdefault((a, b), []).append((c[0], c[-1]))
    for pair in sorted(touch.keys() | shared.keys()):
        a, b = pair
        at: list[Pt] = []
        for p in touch.get(pair, {}).values():
            arms_a, arms_b = arm_angles(pts_of[a], p), arm_angles(pts_of[b], p)
            if len(arms_a) == len(arms_b) == 2 and interleaved(arms_a, arms_b):
                at.append(p)
        if pair in shared:
            at += _overlap_swaps(pts_of[a], pts_of[b], shared[pair])
        evs: list[Cross] = []
        for p in at:
            sa = poly_project(pts_of[a], seg_of[a], p)
            sb = poly_project(pts_of[b], seg_of[b], p)
            reg = (*arc_bracket(marks_of[a], sa), *arc_bracket(marks_of[b], sb))
            if not any(e[2] == reg and abs(e[0] - sa) < 1e-6 for e in evs):
                evs.append((sa, sb, reg))
        if evs:
            events[pair] = evs
    return events


def bigon_empty(
    pts_a: Sequence[Pt],
    pts_b: Sequence[Pt],
    x: Cross,
    y: Cross,
    obstacles: np.ndarray,
) -> bool:
    """Whether the region the two wires bound between crossings ``x`` and
    ``y`` holds no obstacle point strictly inside. Anything doubtful — a
    self-touching or zero-area region — reads as not empty."""
    from shapely.ops import substring

    def line(pts: Sequence[Pt]) -> Any:
        return LineString([p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]])

    arc_a = substring(line(pts_a), x[0], y[0])
    arc_b = substring(line(pts_b), y[1], x[1])
    if arc_a.geom_type != "LineString" or arc_b.geom_type != "LineString":
        return False
    poly = Polygon([*arc_a.coords, *arc_b.coords])
    if not poly.is_valid or poly.area <= 1e-9:
        return False
    inner = poly.buffer(-1e-6)
    if inner.is_empty or not len(obstacles):
        return True
    return not bool(shapely.contains_xy(inner, obstacles[:, 0], obstacles[:, 1]).any())


def resolve_crossings(
    mesh: TopoMesh,
    branches: Sequence[WirePath],
    obstacles: np.ndarray,
    events: dict[tuple[int, int], list[Cross]] | None = None,
) -> tuple[
    dict[tuple[int, int], list[tuple[int, int, int, int]]],
    list[tuple[int, int, Cross, Cross]],
]:
    """The live crossing regions, and the cancelled empty bigons as
    ``(a, b, x, y)`` with the two crossings' arc positions on each
    branch. Two crossings of a pair that are consecutive along BOTH
    wires bound a region, and when no obstacle point (``obstacles``,
    ``(n, 2)`` world xy) lies strictly inside it the wires can slide
    apart through it, so both crossings cancel; repeated until stable (a
    removal can make others consecutive), an odd count keeping its last
    crossing. A homotopy test on the polylines, not an order comparison.
    ``events`` is :func:`crossing_events`' result when the caller already
    has it. Regions: ``{(a, b): [(ia1, ia2, ib1, ib2), ...]}``, ``a < b``."""
    regions: dict[tuple[int, int], list[tuple[int, int, int, int]]] = {}
    cancelled: list[tuple[int, int, Cross, Cross]] = []
    if events is None:
        events = crossing_events(mesh, branches)
    for (a, b), evs in events.items():
        pts_a = [(float(x), float(y)) for x, y in branches[a].points]
        pts_b = [(float(x), float(y)) for x, y in branches[b].points]
        while len(evs) >= 2:
            by_a = sorted(range(len(evs)), key=lambda i: evs[i][0])
            rank_b = {
                i: r
                for r, i in enumerate(sorted(range(len(evs)), key=lambda i: evs[i][1]))
            }
            gone = next(
                (
                    (i, j)
                    for i, j in itertools.pairwise(by_a)
                    if abs(rank_b[i] - rank_b[j]) == 1
                    and bigon_empty(pts_a, pts_b, evs[i], evs[j], obstacles)
                ),
                None,
            )
            if gone is None:
                break
            cancelled.append((a, b, evs[gone[0]], evs[gone[1]]))
            evs = [e for k, e in enumerate(evs) if k not in gone]
        if evs:
            regions[(a, b)] = [e[2] for e in evs]
    return regions, cancelled
