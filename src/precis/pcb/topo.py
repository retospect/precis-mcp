"""CDT region graph, physical portal capacities and the taut-band funnel
domain (``docs/backlog/pcb-vector-router.md`` slice 5);
``view='congestion'`` prints its per-layer portal map.

The tile plan (:mod:`precis.pcb.globalplan`) prices fixed square tiles,
so a braid inside one tile is invisible to it. Here free space on a layer
is triangulated with every obstacle boundary as a constraint
(``shapely.constrained_delaunay_triangles``), so each primal edge between
two free triangles — a *portal* — is a gap between two real obstacles (or
an obstacle and the board edge). The dual graph has one node per free
triangle and one edge per portal. Two domains share the machinery:

- **The portal graph** (copper level, shared by every net): each copper
  item's exact kernel :class:`~precis.pcb.clearance.Body` drawn outward
  by at most ``δ`` = 5 µm (:func:`precis.pcb.clearance.outward_polygon`,
  Decision 7), so a portal is a physical gap and :func:`portal_capacity`
  prices it: ``n`` tracks of width ``w`` cross when
  ``n·w + s_a + s_b + (n−1)·s ≤ L`` — the end clearances priced per pair
  by :func:`precis.pcb.rules.required`
  (:meth:`RegionGraph.capacity_for`; Liu et al., D-PCDT, doi
  10.1109/TCAD.2008.2009154).
- **The net domain** (one per ``(net, layer)``): every foreign obstacle
  inflated by ``w/2 + required(net, obstacle, layer).warn + δ`` — the
  centreline configuration space a taut band runs in
  (:func:`net_region_graph`). This is the funnel domain the unified
  spec's bands pull taut in, the insertion search's alternative to the
  tangent graph, and where :func:`side_swap_candidates` finds the sleeve
  on the other side of a portal
  (``docs/backlog/pcb-unified-place-route.md`` §Taut bands,
  §Composition row "5 CDT portals").

**What this does not model.** No via edges between layers (the via
sites are the vector router's step 2); own-net copper drops out of the
net domain entirely (same-net merge — the band may touch it) but stays
an obstacle on the shared graph; a triangle's interior is not
capacity-checked, only its portals.
"""

from __future__ import annotations

import heapq
import itertools
import math
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import shapely
from shapely.geometry import Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.strtree import STRtree

from precis.pcb import clearance as kernel
from precis.pcb import rules
from precis.pcb.drc import _clearance_items, _via_layer_names

#: Outward polygonisation tolerance, mm (Decision 6/7).
DELTA_MM = kernel.DELTA_MM
#: Vertex-to-obstacle match distance, mm: CDT vertices are the obstacle
#: polygons' own vertices, so anything above float noise is a miss.
_TOUCH_MM = 1e-6
#: Ends of a portal that lie on the board outline rather than an obstacle.
OUTLINE = -1


def outward(geom: BaseGeometry, delta_mm: float = DELTA_MM) -> Polygon:
    """``geom`` as a polygon that contains it and lies within ``delta_mm``
    of it: simplified at ``δ/2`` (every original point within ``δ/2`` of
    the simplified boundary), then mitre-buffered by ``δ/2`` (adds no arc
    vertices). A single copper item is connected, so the result is one
    polygon; anything else is refused rather than silently dropped."""
    half = delta_mm / 2.0
    out = geom.simplify(half, preserve_topology=True).buffer(
        half, join_style="mitre", mitre_limit=2.0
    )
    if not isinstance(out, Polygon):
        raise ValueError(f"outward() of one copper item gave a {out.geom_type}")
    return out


@dataclass(frozen=True, slots=True)
class Obstacle:
    """One copper item on one layer, drawn outward. ``body`` is the exact
    kernel body when the obstacle came from :func:`board_obstacles`
    (``None`` for a hand-made one); the net domain re-inflates it."""

    net: str
    kind: str
    poly: BaseGeometry
    body: kernel.Body | None = None


def board_obstacles(
    model: dict[str, Any], layer: str, *, delta_mm: float = DELTA_MM
) -> list[Obstacle]:
    """Every copper item of a gerber-shaped ``model`` (pads, tracks, vias,
    pours) that has copper on ``layer``, as its exact kernel
    :class:`~precis.pcb.clearance.Body` plus that body's outward polygon
    (``delta_mm`` sagitta, Decision 7). A drilled pad and a via span
    their layers (the rule :func:`precis.pcb.drc.clearance_pairs_indexed`
    applies); a pour keeps its antipad holes."""
    layers = [str(x) for x in model.get("layers") or []]
    out: list[Obstacle] = []
    for item in _clearance_items(model):
        ctype = item.get("ctype")
        if ctype == "via":
            on = layer in _via_layer_names(item, layers)
        elif ctype == "pad" and item.get("drill"):
            on = True
        else:
            on = item.get("layer") == layer
        if not on:
            continue
        body = kernel.body_of(item)
        if body is None:
            continue
        poly = kernel.outward_polygon(body, sagitta=delta_mm)
        if poly.is_empty or poly.area <= 0:
            continue
        out.append(Obstacle(str(item.get("net") or ""), str(ctype), poly, body))
    return out


def portal_capacity(
    length_mm: float,
    track_mm: float,
    clearance_mm: float,
    *,
    end_a_mm: float | None = None,
    end_b_mm: float | None = None,
    between_mm: float | None = None,
) -> int:
    """Tracks of width ``track_mm`` that fit across a gap of
    ``length_mm``: ``n·w + s_a + s_b + (n−1)·s ≤ L`` with ``s_a``/``s_b``
    the clearances owed the two end obstacles and ``s`` the clearance
    between neighbouring tracks, each defaulting to ``clearance_mm`` —
    with one figure for all three this is the probe's
    ``⌊(L − s)/(w + s)⌋``. Never negative. A 1 nm allowance keeps an
    exact fit (a gap drawn at the rule) from reading one short."""
    end_a = clearance_mm if end_a_mm is None else end_a_mm
    end_b = clearance_mm if end_b_mm is None else end_b_mm
    between = clearance_mm if between_mm is None else between_mm
    pitch = track_mm + between
    if pitch <= 0:
        raise ValueError(f"track + clearance must be positive, got {pitch!r}")
    return max(0, math.floor((length_mm - end_a - end_b + between + 1e-6) / pitch))


@dataclass(frozen=True, slots=True)
class Portal:
    """A primal edge shared by free triangles ``a`` and ``b``. ``ends``
    are the obstacle indices its two vertices lie on (:data:`OUTLINE` for
    the board edge); ``uv`` are those vertices' coordinates."""

    a: int
    b: int
    length_mm: float
    capacity: int
    ends: tuple[int, int]
    uv: tuple[kernel.Coord, kernel.Coord]


@dataclass(frozen=True)
class RegionGraph:
    """One layer's free-space triangulation and its dual graph."""

    layer: str
    track_mm: float
    clearance_mm: float
    obstacles: tuple[Obstacle, ...]
    #: ``(n, 3, 2)`` triangle vertex coordinates, mm.
    triangles: np.ndarray
    portals: tuple[Portal, ...]
    #: Triangle index -> indices into ``portals``.
    adjacency: tuple[tuple[int, ...], ...]
    #: Obstacle index -> triangles with a vertex on it (its access faces).
    access: dict[int, tuple[int, ...]] = field(default_factory=dict)

    def capacity_histogram(self) -> dict[int, int]:
        """``{capacity: portal count}``."""
        hist: dict[int, int] = {}
        for p in self.portals:
            hist[p.capacity] = hist.get(p.capacity, 0) + 1
        return dict(sorted(hist.items()))

    def capacity_for(
        self,
        portal: Portal,
        net: str,
        ctx: rules.RuleContext,
        *,
        edge_mm: float | None = None,
    ) -> int:
        """Lanes of ``net``'s track across ``portal``, priced by
        :func:`precis.pcb.rules.required`: each end owes the requirement
        against the obstacle there — an own-net end owes nothing
        (same-net merge), ``OUTLINE`` takes ``edge_mm`` or the graph's
        figure (the board-edge kind rule is not in ``required()`` yet) —
        and neighbouring tracks owe the net's own per-layer tier."""
        s = []
        for e in portal.ends:
            if e == OUTLINE:
                s.append(edge_mm if edge_mm is not None else self.clearance_mm)
            else:
                s.append(_warn_required(net, self.obstacles[e], self.layer, ctx))
        nr = ctx.net_rules.get(net) if ctx.net_rules else None
        between = (
            nr.tiers_on(
                self.layer, outer=ctx.is_outer(self.layer), coated=ctx.coated
            ).clearance_mm
            if nr is not None
            else self.clearance_mm
        )
        return portal_capacity(
            portal.length_mm,
            self.track_mm,
            self.clearance_mm,
            end_a_mm=s[0],
            end_b_mm=s[1],
            between_mm=between,
        )

    def centroids(self) -> np.ndarray:
        """``(m, 2)`` triangle centroids — the dual path's edge costs."""
        if not len(self.triangles):
            return np.zeros((0, 2))
        return self.triangles.mean(axis=1)

    def locate(self, x: float, y: float, *, eps: float = 1e-9) -> int | None:
        """Index of the free triangle holding ``(x, y)`` (a boundary hit
        counts), or ``None`` — the point sits inside an obstacle or off
        the board."""
        t = self.triangles
        if not len(t):
            return None
        a, b, c = t[:, 0], t[:, 1], t[:, 2]
        v0x, v0y = c[:, 0] - a[:, 0], c[:, 1] - a[:, 1]
        v1x, v1y = b[:, 0] - a[:, 0], b[:, 1] - a[:, 1]
        v2x, v2y = x - a[:, 0], y - a[:, 1]
        den = v0x * v1y - v0y * v1x
        with np.errstate(divide="ignore", invalid="ignore"):
            u = (v2x * v1y - v2y * v1x) / den
            v = (v0x * v2y - v0y * v2x) / den
        inside = (u >= -eps) & (v >= -eps) & (u + v <= 1.0 + eps)
        hit = np.nonzero(inside)[0]
        return int(hit[0]) if len(hit) else None

    def sleeve_polygon(self, tris: Iterable[int]) -> BaseGeometry:
        """The union of the named triangles — the channel a band claims."""
        return unary_union([Polygon(self.triangles[i]) for i in tris])

    def obstacle_at(
        self, net: str, x: float, y: float, *, near_mm: float = 0.05
    ) -> int | None:
        """The obstacle of ``net`` under ``(x, y)``, else the nearest one
        of that net within ``near_mm``; ``None`` when there is neither."""
        pt = Point(x, y)
        best: tuple[float, int] | None = None
        for i, ob in enumerate(self.obstacles):
            if ob.net != net:
                continue
            d = ob.poly.distance(pt)
            if d <= near_mm and (best is None or d < best[0]):
                best = (d, i)
        return None if best is None else best[1]

    def shorted_by(self, idx: int) -> tuple[int, ...]:
        """Indices of other-net obstacles whose copper overlaps obstacle
        ``idx``. A terminal covered like this has no free-space access
        face of its own (its vertices lie inside the union the CDT
        carves out), so no portal sequence can reach it: the board has a
        copper short there, not a routing cut."""
        mine = self.obstacles[idx]
        return tuple(
            i
            for i, ob in enumerate(self.obstacles)
            if i != idx and ob.net != mine.net and ob.poly.intersects(mine.poly)
        )

    def portal_path(
        self, src: int, dst: int, *, min_capacity: int = 1
    ) -> list[int] | None:
        """Shortest (fewest portals) sequence of portals with capacity ≥
        ``min_capacity`` leading from an access face of obstacle ``src``
        to one of ``dst``; ``[]`` when they share a face, ``None`` when no
        such sequence exists (or either has no access face)."""
        starts = self.access.get(src, ())
        goals = set(self.access.get(dst, ()))
        if not starts or not goals:
            return None
        prev: dict[int, tuple[int, int] | None] = {t: None for t in starts}
        queue = deque(starts)
        while queue:
            tri = queue.popleft()
            if tri in goals:
                path: list[int] = []
                step = prev[tri]
                while step is not None:
                    path.append(step[1])
                    step = prev[step[0]]
                return path[::-1]
            for pi in self.adjacency[tri]:
                portal = self.portals[pi]
                if portal.capacity < min_capacity:
                    continue
                nxt = portal.b if portal.a == tri else portal.a
                if nxt not in prev:
                    prev[nxt] = (tri, pi)
                    queue.append(nxt)
        return None


def build_region_graph(
    obstacles: Sequence[Obstacle],
    outline: Sequence[Sequence[float]],
    layer: str,
    *,
    track_mm: float,
    clearance_mm: float,
) -> RegionGraph:
    """Triangulate ``outline`` minus ``obstacles`` with every boundary
    constrained, then price each shared edge by :func:`portal_capacity`.
    Obstacles that overlap are unioned for the triangulation only; every
    vertex still maps back to every obstacle it lies on."""
    obs = tuple(obstacles)
    board = Polygon([(float(p[0]), float(p[1])) for p in outline])
    free = board.difference(unary_union([o.poly for o in obs])) if obs else board
    tris = [
        g
        for g in shapely.constrained_delaunay_triangles(free).geoms
        if g.geom_type == "Polygon" and g.area > 0
    ]
    coords = (
        np.array([np.asarray(t.exterior.coords)[:3] for t in tris], dtype=float)
        if tris
        else np.zeros((0, 3, 2))
    )
    # Vertex -> obstacles: CDT vertices are the input rings' own vertices.
    # A vertex where obstacles touch gives access to every one of them; a
    # portal end names the lowest index.
    vkey: dict[tuple[float, float], tuple[int, ...]] = {}
    tree = STRtree([o.poly for o in obs]) if obs else None

    def vertex_owners(x: float, y: float) -> tuple[int, ...]:
        key = (round(x, 9), round(y, 9))
        hit = vkey.get(key)
        if hit is None:
            hit = ()
            if tree is not None:
                cand = tree.query(Point(x, y), predicate="dwithin", distance=_TOUCH_MM)
                hit = tuple(sorted(int(c) for c in cand))
            vkey[key] = hit
        return hit

    owners = [[vertex_owners(float(x), float(y)) for x, y in tri] for tri in coords]
    access: dict[int, set[int]] = {}
    edges: dict[
        tuple[tuple[float, float], tuple[float, float]], list[tuple[int, int, int]]
    ] = {}
    for ti, tri in enumerate(coords):
        ends = [own[0] if own else OUTLINE for own in owners[ti]]
        for k in range(3):
            for owner in owners[ti][k]:
                access.setdefault(owner, set()).add(ti)
            j = (k + 1) % 3
            pa = (round(float(tri[k][0]), 9), round(float(tri[k][1]), 9))
            pb = (round(float(tri[j][0]), 9), round(float(tri[j][1]), 9))
            key = (pa, pb) if pa <= pb else (pb, pa)
            edges.setdefault(key, []).append((ti, ends[k], ends[j]))
    portals: list[Portal] = []
    adjacency: list[list[int]] = [[] for _ in range(len(coords))]
    for (pa, pb), sides in edges.items():
        if len(sides) != 2:
            continue  # an obstacle or outline boundary edge
        (ta, ea, eb), (tb, _, _) = sides
        length = math.hypot(pb[0] - pa[0], pb[1] - pa[1])
        pi = len(portals)
        portals.append(
            Portal(
                ta,
                tb,
                length,
                portal_capacity(length, track_mm, clearance_mm),
                (ea, eb),
                (pa, pb),
            )
        )
        adjacency[ta].append(pi)
        adjacency[tb].append(pi)
    return RegionGraph(
        layer=layer,
        track_mm=track_mm,
        clearance_mm=clearance_mm,
        obstacles=obs,
        triangles=coords,
        portals=tuple(portals),
        adjacency=tuple(tuple(a) for a in adjacency),
        access={k: tuple(sorted(v)) for k, v in access.items()},
    )


@dataclass(frozen=True, slots=True)
class Terminal:
    """One end of a connection: a point on copper of ``net`` and the
    layers that copper is on."""

    x: float
    y: float
    layers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Connection:
    net: str
    a: Terminal
    b: Terminal


def connection_layers(conn: Connection, graphs: dict[str, RegionGraph]) -> list[str]:
    """Layers with a region graph that both terminals' copper is on."""
    return [lay for lay in graphs if lay in conn.a.layers and lay in conn.b.layers]


def connection_reach(
    conn: Connection, graphs: dict[str, RegionGraph], *, min_capacity: int = 1
) -> dict[str, list[int] | None]:
    """Per shared layer, the portal sequence joining the connection's two
    terminals (:meth:`RegionGraph.portal_path`), or ``None``."""
    out: dict[str, list[int] | None] = {}
    for lay in connection_layers(conn, graphs):
        g = graphs[lay]
        src = g.obstacle_at(conn.net, conn.a.x, conn.a.y)
        dst = g.obstacle_at(conn.net, conn.b.x, conn.b.y)
        out[lay] = (
            None
            if src is None or dst is None
            else g.portal_path(src, dst, min_capacity=min_capacity)
        )
    return out


def _buried(conn: Connection, graphs: dict[str, RegionGraph]) -> bool:
    """True when a terminal's copper is overlapped by another net's copper
    on a shared layer (:meth:`RegionGraph.shorted_by`): an unreachable
    connection of this kind is a board short, not a portal cut."""
    for lay in connection_layers(conn, graphs):
        g = graphs[lay]
        for t in (conn.a, conn.b):
            idx = g.obstacle_at(conn.net, t.x, t.y)
            if idx is not None and g.shorted_by(idx):
                return True
    return False


def summarize(
    graphs: dict[str, RegionGraph], connections: Iterable[Connection]
) -> dict[str, Any]:
    """The probe's measurement for one board: per layer the triangle and
    portal counts and the capacity histogram; over ``connections``, how
    many have a portal sequence on at least one shared layer, and which
    unreachable nets have a terminal buried in foreign copper."""
    conns = list(connections)
    reach = [connection_reach(c, graphs) for c in conns]
    return {
        "layers": {
            lay: {
                "triangles": len(g.triangles),
                "portals": len(g.portals),
                "zero_cap": sum(1 for p in g.portals if p.capacity == 0),
                "cap_hist": g.capacity_histogram(),
            }
            for lay, g in graphs.items()
        },
        "connections": len(conns),
        "reachable": sum(1 for r in reach if any(v is not None for v in r.values())),
        "unreachable": sorted(
            {
                c.net
                for c, r in zip(conns, reach, strict=True)
                if not any(v is not None for v in r.values())
            }
        ),
        "buried": sorted(
            {
                c.net
                for c, r in zip(conns, reach, strict=True)
                if not any(v is not None for v in r.values()) and _buried(c, graphs)
            }
        ),
    }


def _warn_required(net: str, ob: Obstacle, layer: str, ctx: rules.RuleContext) -> float:
    """The warn-tier clearance net ``net`` owes obstacle ``ob`` on
    ``layer`` (its error floor when there is no warn figure — same-net's
    ``none`` carries one, giving 0)."""
    req = rules.required(rules.Owner(net), rules.Owner(ob.net, ob.kind), layer, ctx)
    return float(req.warn_mm if req.warn_mm is not None else req.error_mm)


# ── the net domain: one band's free space ────────────────────────────────


def net_obstacles(
    obstacles: Sequence[Obstacle],
    net: str,
    track_mm: float,
    layer: str,
    ctx: rules.RuleContext,
    *,
    delta_mm: float = DELTA_MM,
) -> list[Obstacle]:
    """The layer's obstacles as net ``net``'s band sees them: every
    FOREIGN item inflated by ``track_mm/2 + required(net, item, layer).
    warn + δ`` — the region the centreline must stay out of — drawn
    outward within ``delta_mm`` (the same margin and sagitta convention
    ``vroute`` uses). Own-net copper drops out: same-net merge means the
    band may touch it, and its pads are where terminals sit."""
    half = track_mm / 2.0
    out: list[Obstacle] = []
    for ob in obstacles:
        if ob.net == net:
            continue
        grow = half + _warn_required(net, ob, layer, ctx) + DELTA_MM
        body = ob.body or kernel.Body(ob.poly, (), 0.0)
        poly = kernel.outward_polygon(body, grow=grow, sagitta=delta_mm)
        if poly.is_empty or poly.area <= 0:
            continue
        out.append(Obstacle(ob.net, ob.kind, poly, body))
    return out


def net_region_graph(
    obstacles: Sequence[Obstacle],
    outline: Sequence[Sequence[float]],
    layer: str,
    *,
    net: str,
    track_mm: float,
    ctx: rules.RuleContext,
    delta_mm: float = DELTA_MM,
) -> RegionGraph:
    """The CDT of the free space net ``net``'s band runs in — the funnel
    domain of ``docs/backlog/pcb-unified-place-route.md`` §Taut bands.
    Portal ``capacity`` in a net domain is centreline room
    (``clearance_mm`` = 0: the inflation already priced the ends), not
    the physical figure the shared portal graph reports."""
    return build_region_graph(
        net_obstacles(obstacles, net, track_mm, layer, ctx, delta_mm=delta_mm),
        outline,
        layer,
        track_mm=track_mm,
        clearance_mm=0.0,
    )


# ── sleeves, the funnel and bands ─────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Band:
    """A taut band: the shortest centreline polyline through a triangle
    sleeve, plus the sleeve and portal sequence that pin its homotopy
    class (what SIDE_SWAP/ORDER_SWAP candidates compare against)."""

    points: tuple[kernel.Coord, ...]
    sleeve: tuple[int, ...]
    portals: tuple[int, ...]
    length_mm: float


def dual_path(
    graph: RegionGraph,
    a_tri: int,
    b_tri: int,
    *,
    banned: Iterable[int] = (),
    min_capacity: int = 0,
) -> tuple[list[int], list[int]] | None:
    """Shortest sleeve between two free triangles: Dijkstra over the dual
    with centroid-to-centroid edge costs, crossing only portals not in
    ``banned`` whose ``capacity`` is ≥ ``min_capacity``. Returns
    ``(tri_ids, portal_ids)`` — the triangle sequence and the portal
    crossed into each — or ``None``."""
    if a_tri == b_tri:
        return ([a_tri], [])
    cen = graph.centroids()
    banned_set = set(banned)
    dist = {a_tri: 0.0}
    prev: dict[int, tuple[int, int]] = {}
    heap: list[tuple[float, int]] = [(0.0, a_tri)]
    while heap:
        d, t = heapq.heappop(heap)
        if d > dist.get(t, math.inf) + 1e-12:
            continue
        if t == b_tri:
            tris, pids = [t], []
            while t in prev:
                t, pi = prev[t]
                tris.append(t)
                pids.append(pi)
            return tris[::-1], pids[::-1]
        for pi in graph.adjacency[t]:
            if pi in banned_set:
                continue
            portal = graph.portals[pi]
            if portal.capacity < min_capacity:
                continue
            nxt = portal.b if portal.a == t else portal.a
            nd = d + math.dist(cen[t], cen[nxt])
            if nd < dist.get(nxt, math.inf) - 1e-12:
                dist[nxt] = nd
                prev[nxt] = (t, pi)
                heapq.heappush(heap, (nd, nxt))
    return None


def sleeve_between(
    graph: RegionGraph,
    a: kernel.Coord,
    b: kernel.Coord,
    *,
    banned: Iterable[int] = (),
    min_capacity: int = 0,
) -> tuple[list[int], list[int]] | None:
    """``dual_path`` between the free triangles holding ``a`` and ``b``;
    ``None`` when either point sits inside an obstacle (in a net domain,
    a terminal buried under a foreign item's inflation)."""
    ta, tb = graph.locate(*a), graph.locate(*b)
    if ta is None or tb is None:
        return None
    return dual_path(graph, ta, tb, banned=banned, min_capacity=min_capacity)


def _area2(a: kernel.Coord, b: kernel.Coord, c: kernel.Coord) -> float:
    """Mononen's ``triarea2`` — ``(c−a) × (b−a)``, NEGATIVE of the usual
    ``cross(b−a, c−a)``, so a positive value means ``c`` is to the RIGHT
    of ``a→b``. The funnel's comparisons are written against this sign."""
    return (c[0] - a[0]) * (b[1] - a[1]) - (b[0] - a[0]) * (c[1] - a[1])


def _same(a: kernel.Coord, b: kernel.Coord) -> bool:
    return math.dist(a, b) <= 1e-9


def _third_vertex(tri: np.ndarray, u: kernel.Coord, v: kernel.Coord) -> kernel.Coord:
    """The vertex of ``tri`` that is neither portal endpoint — the apex
    the portal is crossed toward."""
    best: kernel.Coord = (float(tri[0][0]), float(tri[0][1]))
    for p in tri:
        c = (float(p[0]), float(p[1]))
        if math.dist(c, u) > 1e-9 and math.dist(c, v) > 1e-9:
            return c
    return best


def _funnel(portals: list[tuple[kernel.Coord, kernel.Coord]]) -> list[kernel.Coord]:
    """Mononen's "simple stupid" funnel: ``portals`` is the sleeve's
    ordered ``(left, right)`` endpoint pairs (left as seen walking
    start→goal), degenerate ``(s, s)``/``(g, g)`` at the ends; returns
    the shortest polyline through the sleeve, bending only at its reflex
    vertices."""
    apex = portals[0][0]
    goal = portals[-1][0]
    left_v = apex
    right_v = apex
    apex_i = left_i = right_i = 0
    path = [apex]
    i = 1
    n = len(portals)
    while i < n:
        lp, rp = portals[i]
        # Update the right side of the funnel.
        if _area2(apex, right_v, rp) <= 0.0:
            if _same(apex, right_v) or _area2(apex, left_v, rp) > 0.0:
                right_v, right_i = rp, i
            else:
                # Right crossed left: the left vertex is a bend; restart there.
                path.append(left_v)
                apex, apex_i = left_v, left_i
                left_v = right_v = apex
                left_i = right_i = apex_i
                i = apex_i + 1
                continue
        # Update the left side.
        if _area2(apex, left_v, lp) >= 0.0:
            if _same(apex, left_v) or _area2(apex, right_v, lp) < 0.0:
                left_v, left_i = lp, i
            else:
                path.append(right_v)
                apex, apex_i = right_v, right_i
                left_v = right_v = apex
                left_i = right_i = apex_i
                i = apex_i + 1
                continue
        i += 1
    if not _same(path[-1], goal):
        path.append(goal)
    return path


def band_in_sleeve(
    graph: RegionGraph,
    sleeve: tuple[list[int], list[int]],
    a: kernel.Coord,
    b: kernel.Coord,
) -> Band:
    """The taut band through ``sleeve`` = ``(tri_ids, portal_ids)`` from
    ``a`` to ``b``: the funnel over the sleeve's portals, each oriented
    by the triangle it enters."""
    tris, pids = sleeve
    lr: list[tuple[kernel.Coord, kernel.Coord]] = []
    for k, pi in enumerate(pids):
        u, v = graph.portals[pi].uv
        apex_next = _third_vertex(graph.triangles[tris[k + 1]], u, v)
        # The endpoint on the walker's left is ``u`` when the triangle
        # being entered lies left of ``u→v`` (``cross(v−u, apex−u) > 0``,
        # i.e. negative under ``_area2``'s sign).
        lr.append((u, v) if _area2(u, v, apex_next) < 0.0 else (v, u))
    pts = _funnel([((a[0], a[1]), (a[0], a[1]))] + lr + [((b[0], b[1]), (b[0], b[1]))])
    pts = [p for i, p in enumerate(pts) if i == 0 or not _same(p, pts[i - 1])]
    length = sum(math.dist(p, q) for p, q in itertools.pairwise(pts))
    return Band(tuple(pts), tuple(tris), tuple(pids), length)


def funnel_path(
    graph: RegionGraph,
    a: kernel.Coord,
    b: kernel.Coord,
    *,
    banned: Iterable[int] = (),
    min_capacity: int = 0,
) -> Band | None:
    """The taut band from ``a`` to ``b`` in ``graph`` — the insertion
    search's CDT alternative: locate both ends, take the shortest dual
    sleeve, pull the funnel taut. ``None`` when an end is buried or no
    sleeve exists."""
    sleeve = sleeve_between(graph, a, b, banned=banned, min_capacity=min_capacity)
    if sleeve is None:
        return None
    return band_in_sleeve(graph, sleeve, a, b)


def side_swap_candidates(
    graph: RegionGraph,
    base_sleeve: tuple[list[int], list[int]],
    *,
    k: int = 8,
    min_capacity: int = 0,
) -> list[tuple[list[int], list[int]]]:
    """Alternative sleeves for the base band's terminals, one per portal
    of its sleeve banned — the unified spec's SIDE_SWAP candidates: each
    distinct result threads at least one portal differently, which is a
    band re-threaded on the other side of whatever bounds that portal."""
    tris, pids = base_sleeve
    out: list[tuple[list[int], list[int]]] = []
    seen = {tuple(pids)}
    for pi in pids:
        alt = dual_path(
            graph, tris[0], tris[-1], banned={pi}, min_capacity=min_capacity
        )
        if alt is None:
            continue
        key = tuple(alt[1])
        if key in seen:
            continue
        seen.add(key)
        out.append(alt)
        if len(out) >= k:
            break
    return out
