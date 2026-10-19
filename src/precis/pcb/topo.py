"""CDT region graph and physical portal capacities — a measurement probe,
not wired into routing (``docs/backlog/pcb-vector-router.md`` slice 5);
``view='congestion'`` prints its per-layer portal map.

The tile plan (:mod:`precis.pcb.globalplan`) prices fixed square tiles,
so a braid inside one tile is invisible to it. Here free space on a layer
is triangulated with every obstacle boundary as a constraint
(``shapely.constrained_delaunay_triangles``), so each primal edge between
two free triangles — a *portal* — is a gap between two real obstacles (or
an obstacle and the board edge). The dual graph has one node per free
triangle and one edge per portal.

**Capacity is physical** (Liu et al., D-PCDT, doi
10.1109/TCAD.2008.2009154): ``n`` tracks of width ``w`` cross a portal of
length ``L`` when ``n·w + (n + 1)·s ≤ L`` — clearance ``s`` to each end
obstacle and between neighbours — so :func:`portal_capacity` is
``⌊(L − s)/(w + s)⌋``. ``s`` is one class figure here; the per-pair
``required()`` of the vector router's slice 1 replaces it when it lands.

**Obstacles are drawn outward.** Copper shapes come from
:func:`precis.pcb.drc._copper_item_polygon` (the one shape function DRC
uses), then :func:`outward` simplifies each at ``δ/2`` and buffers it by
``δ/2`` with mitred joins: the result contains the exact shape and widens
it by at most ``δ`` (Decision 7: polygons outside the kernel are drawn
outward, ``δ`` = 5 µm), so the triangulation can narrow a portal by the
tolerance and never open a false one.

**What the probe does not model.** No via edges between layers (the via
sites are the vector router's step 2); own-net copper other than a
connection's two terminals is an obstacle like any other (conservative);
a triangle's interior is not capacity-checked, only its portals.
"""

from __future__ import annotations

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

from precis.pcb.drc import _clearance_items, _copper_item_polygon, _via_layer_names

#: Outward polygonisation tolerance, mm (Decision 6/7).
DELTA_MM = 0.005
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
    """One copper item on one layer, drawn outward."""

    net: str
    kind: str
    poly: Polygon


def board_obstacles(
    model: dict[str, Any], layer: str, *, delta_mm: float = DELTA_MM
) -> list[Obstacle]:
    """Every copper item of a gerber-shaped ``model`` (pads, tracks, vias,
    pours) that has copper on ``layer``. A drilled pad and a via span
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
        geom = _copper_item_polygon(item)
        if geom is None or geom.is_empty or geom.area <= 0:
            continue
        out.append(
            Obstacle(str(item.get("net") or ""), str(ctype), outward(geom, delta_mm))
        )
    return out


def portal_capacity(length_mm: float, track_mm: float, clearance_mm: float) -> int:
    """Tracks of width ``track_mm`` that fit across a gap of
    ``length_mm`` with ``clearance_mm`` to both ends and between each
    other: ``⌊(L − s)/(w + s)⌋``, never negative. A 1 nm allowance keeps
    an exact fit (a gap drawn at the rule) from reading one short."""
    pitch = track_mm + clearance_mm
    if pitch <= 0:
        raise ValueError(f"track + clearance must be positive, got {pitch!r}")
    return max(0, math.floor((length_mm - clearance_mm + 1e-6) / pitch))


@dataclass(frozen=True, slots=True)
class Portal:
    """A primal edge shared by free triangles ``a`` and ``b``. ``ends``
    are the obstacle indices its two vertices lie on (:data:`OUTLINE` for
    the board edge)."""

    a: int
    b: int
    length_mm: float
    capacity: int
    ends: tuple[int, int]


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
