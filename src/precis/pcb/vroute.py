"""The single-net vector router (``docs/backlog/pcb-vector-router.md``
step 2, slice 3): gridless, octilinear, measured by the exact clearance
kernel (:mod:`precis.pcb.clearance`) instead of a grid's cells.

**Why.** A grid router quantises space into cells (the old maze router did — it was removed 2026-10-20). At 0.5 mm pad pitch a legal
exit between two pads (gr489923: a 0.3 mm track clears its neighbours by
0.2 mm against a 0.152 mm rule) can have no free cell, so the segment fails
``no_path`` though copper fits. Here a track is a centreline polyline and
legality is the kernel's distance test, so a corridor is open exactly when
the copper fits.

**Graph.** Every foreign obstacle on a layer (one :class:`Obstacle` per pad,
via, track segment or track arc) is inflated by ``w/2 + clearance + δ`` and
circumscribed by an axis-and-diagonal octagon, computed exactly from the
obstacle's support values (a polygon's vertices, an arc's extreme points).
The octagon's eight corners are the graph's nodes, with the two terminals;
a node is kept only if a track end fits there. Two nodes connect by an
octilinear connector — one 0/45/90° segment, or a 45° leg and an axis leg
in either order — and an edge exists only when every leg clears every
foreign obstacle by the kernel. Through-only vias (gr495289 c9) join the
layers at nodes where a via clears every copper layer. A* runs over
``(node, layer)`` with the octile distance as heuristic (admissible: no
octilinear path is shorter).

**Not a guarantee of optimality**: the node set is the octagon corners, not
the exact tangent graph of step 2, so a path that only exists through a gap
no corner falls into is missed. It is complete for the cases that motivate
the slice (a straight escape along a pad's axis between neighbours, then
around obstacles), and every path it returns is legal by the kernel.
"""

from __future__ import annotations

import dataclasses
import heapq
import itertools
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import shapely
from shapely import STRtree, box
from shapely.geometry import LineString, Point

from precis.pcb import clearance as kernel
from precis.pcb.clearance import Body, Coord

#: Decision 6: margin added to every inflated obstacle, so a path built on
#: an octagon corner never sits exactly on the clearance limit.
DELTA_MM = kernel.DELTA_MM
_SQRT2 = math.sqrt(2.0)
#: Unit normals of the octagon's eight half-planes, counter-clockwise.
_DIRS = tuple(
    (math.cos(k * math.pi / 4.0), math.sin(k * math.pi / 4.0)) for k in range(8)
)
#: Distances along each of a terminal's eight octilinear rays where an
#: escape node is offered (the axis component, so a diagonal node lands on
#: the 45° line): enough to clear a 1.55 mm fine-pitch pad row and reach
#: the octagons beyond it.
_ESCAPE_STEPS_MM = (0.4, 0.8, 1.2, 1.6, 2.0, 2.5, 3.0)


@dataclass(frozen=True, slots=True)
class Obstacle:
    """One piece of copper: its exact body, net, the copper layers it
    occupies, and whether a new via must keep clear of it whatever its net:
    every pad (no via-in-pad, Reto gr496850; the old grid's
    via_clears_pads``'s rule) and every via (the hole-to-hole rule holds
    between one net's own vias too). A ``track_only`` obstacle is not
    copper but an area reserved for ``net``'s pour (``realize.
    _track_reserved_regions``): it keeps foreign TRACKS out and never
    blocks a via, as the old grid's ``reserve_track_cells`` did)."""

    body: Body
    net: str
    layers: frozenset[int]
    blocks_vias: bool = False
    track_only: bool = False
    #: Blocks VIA SITES, invisible to tracks (``realize``'s silk-outline
    #: keep-out — the port of the grid's ``add_via_keepout_line``): tented
    #: copper under ink is fine, a drilled annulus is not.
    via_only: bool = False


@dataclass(frozen=True, slots=True)
class Terminal:
    x: float
    y: float
    layers: frozenset[int]


@dataclass(frozen=True, slots=True)
class ViaSpec:
    dia_mm: float
    drill_mm: float


@dataclass(frozen=True, slots=True)
class Request:
    """One connection to route. ``bounds`` is the box a track centreline
    must stay inside (the outline inset by the edge rule and half the
    track); ``via_layers`` the copper layers a through via occupies.
    ``layers`` is the hard set a TRACK may run on; a terminal may sit on
    a layer outside it (a surface pad's mount side under a layer
    assignment) — that layer then admits only pad→via-site stub legs, the
    old grid's ``start_layer``/``goal_layer`` escape.

    ``via_body_zones`` are soft cost regions (part courtyards, realized as
    shapely polygons): a via site inside one pays
    ``via_body_cost_mm`` on top of ``via_cost_mm`` — both in wire-length
    millimetres, so the search prefers a detour of up to that much extra
    wire over a via under a body (gr502369; Reto 2026-10-21). A soft
    cost, never a keep-out; ``0.0`` prices every via site the same."""

    net: str
    width_mm: float
    layers: tuple[int, ...]
    start: Terminal
    goal: Terminal
    bounds: tuple[float, float, float, float]
    via: ViaSpec | None = None
    via_layers: tuple[int, ...] = ()
    via_cost_mm: float = 1.0
    via_body_cost_mm: float = 0.0
    via_body_zones: tuple[Any, ...] = ()
    bend_cost_mm: float = 0.05
    window_mm: float = 3.0
    max_expansions: int = 6000


@dataclass
class VPath:
    """A routed connection: per-layer point runs, through-via sites."""

    runs: list[tuple[int, list[Coord]]]
    vias: list[Coord]
    length_mm: float
    expansions: int = 0


@dataclass
class _Layer:
    obstacles: list[Obstacle] = field(default_factory=list)
    _tree: STRtree | None = None
    _cores: np.ndarray | None = None
    _radii: np.ndarray | None = None
    _arcs: np.ndarray | None = None
    _nets: np.ndarray | None = None
    _pads: np.ndarray | None = None
    _track_only: np.ndarray | None = None
    _via_only: np.ndarray | None = None
    #: Each obstacle's own clearance on this layer (filled by :class:`Space`).
    _cl: np.ndarray | None = None

    def invalidate(self) -> None:
        self._tree = None

    def build(self) -> None:
        bodies = [o.body for o in self.obstacles]
        self._tree = STRtree([box(*b.bounds()) for b in bodies])
        self._cores = np.array(
            [b.core if b.core is not None else Point() for b in bodies], dtype=object
        )
        self._radii = np.array([b.r for b in bodies], dtype=float)
        self._arcs = np.array([bool(b.arcs) for b in bodies], dtype=bool)
        self._nets = np.array([o.net for o in self.obstacles], dtype=object)
        self._pads = np.array([o.blocks_vias for o in self.obstacles], dtype=bool)
        self._track_only = np.array([o.track_only for o in self.obstacles], dtype=bool)
        self._via_only = np.array([o.via_only for o in self.obstacles], dtype=bool)


class Space:
    """Foreign copper indexed per layer, and the clearance each net owes.

    ``clearance(net, layer)`` is a net's own routing clearance on a layer
    (the maze's per-(net, layer) figure, slice 1 P5); a pair owes the larger
    of the two. Pair rules stay DRC-only, as on the grid."""

    def __init__(
        self,
        obstacles: Iterable[Obstacle],
        clearance: Callable[[str, int], float],
    ) -> None:
        self._clearance = clearance
        self._cache: dict[tuple[str, int], float] = {}
        self._layers: dict[int, _Layer] = {}
        self._reach: dict[int, float] = {}
        for ob in obstacles:
            self.add(ob)

    def cl(self, net: str, layer: int) -> float:
        key = (net, layer)
        if key not in self._cache:
            self._cache[key] = float(self._clearance(net, layer))
        return self._cache[key]

    def add(self, ob: Obstacle) -> None:
        for layer in ob.layers:
            li = self._layers.setdefault(layer, _Layer())
            li.obstacles.append(ob)
            li.invalidate()
            self._reach[layer] = max(
                self._reach.get(layer, 0.0), self.cl(ob.net, layer)
            )

    def mark(self) -> dict[int, int]:
        """Obstacle counts per layer, for :meth:`rollback`."""
        return {layer: len(li.obstacles) for layer, li in self._layers.items()}

    def rollback(self, mark: dict[int, int]) -> None:
        """Drop every obstacle added since :meth:`mark` (a net whose
        segments did not all route keeps none of its new copper)."""
        for layer, li in self._layers.items():
            keep = mark.get(layer, 0)
            if len(li.obstacles) > keep:
                del li.obstacles[keep:]
                li.invalidate()

    def _layer(self, layer: int) -> _Layer | None:
        li = self._layers.get(layer)
        if li is None or not li.obstacles:
            return None
        if li._tree is None:
            li.build()
            li._cl = np.array(
                [self.cl(o.net, layer) for o in li.obstacles], dtype=float
            )
        return li

    def obstacles_in(
        self, layer: int, window: tuple[float, float, float, float]
    ) -> list[Obstacle]:
        li = self._layer(layer)
        if li is None:
            return []
        assert li._tree is not None
        return [li.obstacles[int(i)] for i in li._tree.query(box(*window))]

    def clears(
        self, body: Body, net: str, layer: int, *, via_site: bool = False
    ) -> bool:
        """Does ``body`` (copper of ``net``) clear every foreign obstacle on
        ``layer`` by the pair's clearance? With ``via_site`` (``body`` is
        a new via) every pad and via counts, its own net's included, and no
        ``track_only`` area does."""
        li = self._layer(layer)
        if li is None:
            return True
        assert li._tree is not None and li._nets is not None
        assert li._cores is not None and li._radii is not None
        assert li._arcs is not None and li._pads is not None
        assert li._track_only is not None
        assert li._via_only is not None
        assert li._pads is not None
        own = self.cl(net, layer)
        reach = max(own, self._reach.get(layer, 0.0))
        cand = li._tree.query(box(*body.bounds()), predicate="dwithin", distance=reach)
        if not len(cand):
            return True
        foreign = li._nets[cand] != net
        if via_site:
            foreign |= li._pads[cand] | li._via_only[cand]
            foreign &= ~li._track_only[cand]
        else:
            foreign &= ~li._via_only[cand]
        cand = cand[foreign]
        if not len(cand):
            return True
        req = np.array(
            [max(own, self.cl(str(n), layer)) for n in li._nets[cand]], dtype=float
        )
        flat = ~li._arcs[cand]
        if body.core is not None and not body.arcs and flat.any():
            d = shapely.distance(body.core, li._cores[cand[flat]])
            if np.any(d - li._radii[cand[flat]] - body.r < req[flat] - 1e-9):
                return False
            rest = cand[~flat]
            rest_req = req[~flat]
        else:
            rest, rest_req = cand, req
        for i, need in zip(rest.tolist(), rest_req.tolist(), strict=True):
            if kernel.gap(body, li.obstacles[i].body) < need - 1e-9:
                return False
        return True

    def clears_legs(
        self, legs: Sequence[tuple[Coord, Coord]], half: float, net: str, layer: int
    ) -> np.ndarray:
        """:meth:`clears` for many straight track legs of half-width
        ``half`` at once — the same predicate, one tree query and one
        vectorised distance instead of a Python call per leg (the search
        tests ~100 legs per expansion). A leg's candidates are the
        obstacles whose box lies within ``reach + half`` of its centreline,
        a superset of those within ``reach`` of its capsule."""
        ok = np.ones(len(legs), dtype=bool)
        li = self._layer(layer)
        if li is None or not legs:
            return ok
        assert li._tree is not None and li._nets is not None
        assert li._cores is not None and li._radii is not None
        assert li._arcs is not None and li._cl is not None
        own = self.cl(net, layer)
        reach = max(own, self._reach.get(layer, 0.0))
        coords = np.array([[a, b] for a, b in legs], dtype=float)
        geoms = shapely.linestrings(coords)
        # A zero-length leg (a track end) as a point: GEOS treats a
        # degenerate line string as empty in the tree query, so it would
        # find no candidates and pass a node that does not fit.
        zero = np.all(coords[:, 0] == coords[:, 1], axis=1)
        if zero.any():
            geoms[zero] = shapely.points(coords[zero, 0])
        assert li._via_only is not None
        left, right = li._tree.query(geoms, predicate="dwithin", distance=reach + half)
        foreign = (li._nets[right] != net) & ~li._via_only[right]
        left, right = left[foreign], right[foreign]
        if not len(left):
            return ok
        req = np.maximum(own, li._cl[right])
        flat = ~li._arcs[right]
        if flat.any():
            d = shapely.distance(geoms[left[flat]], li._cores[right[flat]])
            bad = d - li._radii[right[flat]] - half < req[flat] - 1e-9
            ok[left[flat][bad]] = False
        arc_left, arc_right, arc_req = left[~flat], right[~flat], req[~flat]
        for k, i, need in zip(
            arc_left.tolist(), arc_right.tolist(), arc_req.tolist(), strict=True
        ):
            if not ok[k]:
                continue
            leg = Body(LineString([legs[k][0], legs[k][1]]), (), half)
            if kernel.gap(leg, li.obstacles[i].body) < need - 1e-9:
                ok[k] = False
        return ok


# ── geometry helpers ─────────────────────────────────────────────────────


def support(body: Body, ux: float, uy: float) -> float:
    """``max p·u`` over the body (``u`` a unit vector), exactly: a core's
    vertices (the extreme of a linear function over a polygon or line is at
    a vertex), an arc's endpoints and its extreme point when the direction
    lies inside the sweep, plus the swept radius."""
    best = -math.inf
    if body.core is not None and not body.core.is_empty:
        xy = shapely.get_coordinates(body.core)
        best = float(np.max(xy[:, 0] * ux + xy[:, 1] * uy))
    for arc in body.arcs:
        for px, py in arc.endpoints():
            best = max(best, px * ux + py * uy)
        if arc.contains_angle(math.atan2(uy, ux)):
            best = max(best, arc.cx * ux + arc.cy * uy + arc.radius)
    return best + body.r


def octagon_corners(body: Body, grow: float) -> list[Coord]:
    """The eight corners of the axis-and-diagonal octagon circumscribing
    ``body`` grown by ``grow``: the intersections of consecutive
    half-planes ``n_k · p <= support_k + grow``."""
    s = [support(body, ux, uy) + grow for ux, uy in _DIRS]
    out: list[Coord] = []
    for k in range(8):
        (a1, b1), (a2, b2) = _DIRS[k], _DIRS[(k + 1) % 8]
        det = a1 * b2 - a2 * b1
        c1, c2 = s[k], s[(k + 1) % 8]
        out.append(((c1 * b2 - c2 * b1) / det, (a1 * c2 - a2 * c1) / det))
    return out


def is_octilinear(a: Coord, b: Coord, tol: float = 1e-9) -> bool:
    dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
    return dx <= tol or dy <= tol or abs(dx - dy) <= tol


def connectors(a: Coord, b: Coord) -> list[list[Coord]]:
    """Octilinear polylines from ``a`` to ``b``: the straight segment when
    it is octilinear, else a 45° leg then an axis leg, and the reverse."""
    if is_octilinear(a, b):
        return [[a, b]]
    dx, dy = b[0] - a[0], b[1] - a[1]
    m = min(abs(dx), abs(dy))
    sx, sy = math.copysign(1.0, dx), math.copysign(1.0, dy)
    diag_first = (a[0] + sx * m, a[1] + sy * m)
    axis_first = (b[0] - sx * m, b[1] - sy * m)
    return [[a, diag_first, b], [a, axis_first, b]]


def octile(a: Coord, b: Coord) -> float:
    dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
    return max(dx, dy) + (_SQRT2 - 1.0) * min(dx, dy)


def _length(pts: Sequence[Coord]) -> float:
    return sum(math.dist(p, q) for p, q in itertools.pairwise(pts))


def _inside(p: Coord, bounds: tuple[float, float, float, float]) -> bool:
    return bounds[0] <= p[0] <= bounds[2] and bounds[1] <= p[1] <= bounds[3]


def _simplify(pts: list[Coord]) -> list[Coord]:
    """Drop repeated and collinear interior points."""
    out: list[Coord] = []
    for p in pts:
        if out and math.dist(out[-1], p) <= 1e-9:
            continue
        if len(out) >= 2:
            (ax, ay), (bx, by) = out[-2], out[-1]
            cross = (bx - ax) * (p[1] - by) - (by - ay) * (p[0] - bx)
            dot = (bx - ax) * (p[0] - bx) + (by - ay) * (p[1] - by)
            if abs(cross) <= 1e-12 and dot >= 0:
                out[-1] = p
                continue
        out.append(p)
    return out


# ── the search ───────────────────────────────────────────────────────────


def route(
    space: Space,
    req: Request,
    *,
    stats: dict[str, Any] | None = None,
) -> VPath | None:
    """Route ``req`` through ``space``; ``None`` when no path is found in
    the window (grown once, ×3) within ``req.max_expansions``, shared by
    both windows. Bounded by work, never by the clock, so a result does
    not depend on the machine. ``stats``, when given, receives
    ``expansions`` (both windows), the last window's ``nodes`` per layer
    and ``stop`` (``found``, ``exhausted``, ``budget``, ``no_layer``)."""
    if stats is None:
        stats = {}
    window = req.window_mm
    used = 0
    path = None
    for _attempt in range(2):
        left = req.max_expansions - used
        if left <= 0:
            break
        attempt = dataclasses.replace(req, max_expansions=left)
        path = _route_in_window(space, attempt, window, stats)
        used += int(stats.get("expansions", 0))
        if path is not None:
            break
        window *= 3.0
    stats["expansions"] = used
    return path


def _route_in_window(
    space: Space,
    req: Request,
    margin: float,
    stats: dict[str, Any],
) -> VPath | None:
    sx, sy, gx, gy = req.start.x, req.start.y, req.goal.x, req.goal.y
    win = (
        max(req.bounds[0], min(sx, gx) - margin),
        max(req.bounds[1], min(sy, gy) - margin),
        min(req.bounds[2], max(sx, gx) + margin),
        min(req.bounds[3], max(sy, gy) + margin),
    )
    half = req.width_mm / 2.0
    start, goal = (sx, sy), (gx, gy)

    # Nodes per layer: 0 = start, 1 = goal, then the terminals' escape rays
    # and the octagon corners, each kept only where a track end fits.
    # ``term_only`` layers carry a TERMINAL (a pad's mount side) but no
    # routing — the old grid's ``start_layer``/``goal_layer`` outside
    # ``layers``: the pad is entered on its own layer, escapes at most a
    # dogbone-length stub to a via site, and every real track leg runs on
    # ``req.layers`` (leg_clear below refuses escape↔escape and
    # terminal↔terminal legs there, so no through-route can sneak on).
    term_only = sorted((req.start.layers | req.goal.layers) - set(req.layers))
    term_only_set = frozenset(term_only)
    #: layer -> (start's escape rays, goal's escape rays). Disjoint on
    #: purpose: leg_clear admits only terminal↔own-ray legs on a term-only
    #: layer, so no path can chain start→ray→goal into a whole track there.
    term_rays: dict[int, tuple[frozenset[Coord], frozenset[Coord]]] = {}
    nodes: dict[int, list[Coord]] = {}
    for layer in itertools.chain(req.layers, term_only):
        if layer in term_only_set:
            ray_a = [
                (sx + ux * n_ * reach, sy + uy * n_ * reach)
                for ux, uy in _DIRS
                for n_ in (1.0 if abs(ux) < 1e-9 or abs(uy) < 1e-9 else _SQRT2,)
                for reach in _ESCAPE_STEPS_MM
            ]
            ray_b = [
                (gx + ux * n_ * reach, gy + uy * n_ * reach)
                for ux, uy in _DIRS
                for n_ in (1.0 if abs(ux) < 1e-9 or abs(uy) < 1e-9 else _SQRT2,)
                for reach in _ESCAPE_STEPS_MM
            ]
            cand = [*ray_a, *ray_b]
            term_rays[layer] = (frozenset(ray_a), frozenset(ray_b))
        else:
            cand = [
                (tx + ux * n_ * reach, ty + uy * n_ * reach)
                for tx, ty in (start, goal)
                for ux, uy in _DIRS
                for n_ in (1.0 if abs(ux) < 1e-9 or abs(uy) < 1e-9 else _SQRT2,)
                for reach in _ESCAPE_STEPS_MM
            ]
        for ob in () if layer in term_only_set else space.obstacles_in(layer, win):
            grow = (
                half + max(space.cl(req.net, layer), space.cl(ob.net, layer)) + DELTA_MM
            )
            cand.extend(octagon_corners(ob.body, grow))
        kept: list[Coord] = []
        seen: set[tuple[float, float]] = set()
        for c in cand:
            key = (round(c[0], 6), round(c[1], 6))
            if key in seen or not (_inside(c, win) and _inside(c, req.bounds)):
                continue
            seen.add(key)
            kept.append(c)
        # A track end at ``c`` is a zero-length leg: one batched check.
        fits = space.clears_legs([(c, c) for c in kept], half, req.net, layer)
        nodes[layer] = [start, goal] + [
            c for c, ok in zip(kept, fits.tolist(), strict=True) if ok
        ]

    body_tree = (
        STRtree(req.via_body_zones)
        if req.via_body_cost_mm and req.via_body_zones
        else None
    )
    body_memo: dict[tuple[float, float], bool] = {}

    def under_body(p: Coord) -> bool:
        """Is ``p`` inside a ``via_body_zones`` polygon — a site whose via
        pays ``via_body_cost_mm`` on top of the flat ``via_cost_mm``?"""
        if body_tree is None:
            return False
        key = (round(p[0], 9), round(p[1], 9))
        if key not in body_memo:
            pt = Point(p)
            body_memo[key] = any(
                req.via_body_zones[int(i)].covers(pt) for i in body_tree.query(pt)
            )
        return body_memo[key]

    via_ok: dict[tuple[float, float], bool] = {}

    def via_fits(p: Coord) -> bool:
        if req.via is None or not req.via_layers:
            return False
        key = (round(p[0], 9), round(p[1], 9))
        if key not in via_ok:
            body = Body(Point(p), (), req.via.dia_mm / 2.0)
            # ``bounds`` insets a track centreline by half the TRACK; a via
            # land is wider, so it sits further in.
            extra = max(0.0, req.via.dia_mm - req.width_mm) / 2.0 + DELTA_MM
            inner = (
                req.bounds[0] + extra,
                req.bounds[1] + extra,
                req.bounds[2] - extra,
                req.bounds[3] - extra,
            )
            via_ok[key] = _inside(p, inner) and all(
                space.clears(body, req.net, layer, via_site=True)
                for layer in req.via_layers
            )
        return via_ok[key]

    seg_ok: dict[tuple[int, Coord, Coord], bool] = {}

    def leg_clear(layer: int, a: Coord, b: Coord) -> bool:
        if layer in term_only_set:
            # A terminal-only layer admits exactly a pad→via-site stub:
            # terminal↔terminal is a whole track on the wrong layer,
            # escape↔escape chains are longer stubs, and a leg must land on
            # ITS OWN terminal's rays (partitioning is what forbids the
            # start→ray→goal cheat path across the same escape node).
            sa, sb = term_rays[layer]
            if a in (start, goal) and b in (start, goal):
                return False
            if a not in (start, goal) and b not in (start, goal):
                return False
            term, esc = (a, b) if a in (start, goal) else (b, a)
            if esc not in (sa if term == start else sb):
                return False
        key = (layer, a, b) if a <= b else (layer, b, a)
        if key not in seg_ok:
            if not (_inside(a, req.bounds) and _inside(b, req.bounds)):
                seg_ok[key] = False
            else:
                body = Body(LineString([a, b]), (), half)
                seg_ok[key] = space.clears(body, req.net, layer)
        return seg_ok[key]

    def prefetch(layer: int, a: Coord, targets: list[Coord]) -> None:
        """Fill ``seg_ok`` for every connector leg from ``a`` to each target
        in one batched check (:meth:`Space.clears_legs`); the per-leg
        logic below then only reads the cache."""
        todo: list[tuple[Coord, Coord]] = []
        keys: list[tuple[int, Coord, Coord]] = []
        queued: set[tuple[int, Coord, Coord]] = set()
        for b in targets:
            for poly in connectors(a, b):
                for p, q in itertools.pairwise(poly):
                    key = (layer, p, q) if p <= q else (layer, q, p)
                    if key in seg_ok or key in queued:
                        continue
                    queued.add(key)
                    if not (_inside(p, req.bounds) and _inside(q, req.bounds)):
                        seg_ok[key] = False
                        continue
                    keys.append(key)
                    todo.append((p, q))
        if todo:
            for key, good in zip(
                keys,
                space.clears_legs(todo, half, req.net, layer).tolist(),
                strict=True,
            ):
                seg_ok[key] = bool(good)

    def best_connector(layer: int, a: Coord, b: Coord) -> list[Coord] | None:
        for poly in connectors(a, b):
            if all(leg_clear(layer, p, q) for p, q in itertools.pairwise(poly)):
                return poly
        return None

    arrays = {layer: np.array(pts, dtype=float) for layer, pts in nodes.items()}
    node_at = {
        layer: {(round(p[0], 9), round(p[1], 9)): k for k, p in enumerate(pts)}
        for layer, pts in nodes.items()
    }
    goal_layers = req.goal.layers & set(nodes)
    start_layers = req.start.layers & set(nodes)
    stats.update(nodes={layer: len(p) for layer, p in nodes.items()}, margin=margin)
    if not goal_layers or not start_layers:
        stats["stop"] = "no_layer"
        return None

    State = tuple[int, int]  # (layer, node index)
    g: dict[State, float] = {}
    parent: dict[State, tuple[State, list[Coord], bool]] = {}
    heap: list[tuple[float, float, int, State]] = []
    tie = itertools.count()
    h0 = octile(start, goal)
    for layer in start_layers:
        g[(layer, 0)] = 0.0
        heapq.heappush(heap, (h0, 0.0, next(tie), (layer, 0)))
    expansions = 0
    neighbours_k = 64
    while heap:
        _f, cost, _t, state = heapq.heappop(heap)
        if cost > g.get(state, math.inf) + 1e-12:
            continue
        layer, i = state
        if i == 1 and layer in goal_layers:
            stats.update(stop="found", expansions=expansions)
            return _reconstruct(parent, state, cost, expansions)
        if expansions >= req.max_expansions:
            stats.update(stop="budget", expansions=expansions)
            return None
        expansions += 1
        here = nodes[layer][i]
        d = np.abs(arrays[layer] - np.array(here))
        dist = np.maximum(d[:, 0], d[:, 1]) + (_SQRT2 - 1.0) * np.minimum(
            d[:, 0], d[:, 1]
        )
        order = np.argsort(dist)[: neighbours_k + 1].tolist()
        if 1 not in order:
            order.append(1)
        prefetch(layer, here, [nodes[layer][j] for j in order if j != i and j != 0])
        for j in order:
            if j == i or j == 0:
                continue
            poly = best_connector(layer, here, nodes[layer][j])
            if poly is None:
                continue
            step = _length(poly) + req.bend_cost_mm * (len(poly) - 1)
            nxt = (layer, j)
            new = cost + step
            if new < g.get(nxt, math.inf) - 1e-12:
                g[nxt] = new
                parent[nxt] = (state, poly, False)
                heapq.heappush(
                    heap, (new + octile(nodes[layer][j], goal), new, next(tie), nxt)
                )
        if req.via is not None and i != 1 and via_fits(here):
            # The jump may land on a terminal-only layer too — that is the
            # whole point of one (drop back to the goal pad's own layer).
            for other in itertools.chain(req.layers, term_only):
                if other == layer:
                    continue
                # The same point is a node on the other layer: add it there
                # once (re-adding on every expansion multiplies the states).
                key = (round(here[0], 9), round(here[1], 9))
                j = node_at[other].get(key, -1)
                if j < 0:
                    nodes[other].append(here)
                    arrays[other] = np.vstack([arrays[other], np.array(here)])
                    j = len(nodes[other]) - 1
                    node_at[other][key] = j
                nxt = (other, j)
                new = cost + req.via_cost_mm
                if under_body(here):
                    new += req.via_body_cost_mm
                if new < g.get(nxt, math.inf) - 1e-12:
                    g[nxt] = new
                    parent[nxt] = (state, [here], True)
                    heapq.heappush(
                        heap, (new + octile(here, goal), new, next(tie), nxt)
                    )
    stats.update(stop="exhausted", expansions=expansions)
    return None


def _reconstruct(
    parent: dict[tuple[int, int], tuple[tuple[int, int], list[Coord], bool]],
    state: tuple[int, int],
    cost: float,
    expansions: int,
) -> VPath:
    steps: list[tuple[int, list[Coord], bool]] = []
    while state in parent:
        prev, poly, is_via = parent[state]
        steps.append((state[0], poly, is_via))
        state = prev
    steps.reverse()
    runs: list[tuple[int, list[Coord]]] = []
    vias: list[Coord] = []
    current: list[Coord] = []
    layer = state[0]
    for step_layer, poly, is_via in steps:
        if is_via:
            if len(current) >= 2:
                runs.append((layer, _simplify(current)))
            vias.append(poly[0])
            current = [poly[0]]
            layer = step_layer
            continue
        if not current:
            current = list(poly)
        else:
            current.extend(poly[1:])
    if len(current) >= 2:
        runs.append((layer, _simplify(current)))
    length = sum(_length(pts) for _l, pts in runs)
    return VPath(runs, vias, length, expansions)


# ── the realize post-pass (slice 3) ──────────────────────────────────────


def _row_layers(row: dict[str, Any], index: dict[str, int], n: int) -> frozenset[int]:
    """The copper layers a gerber-model row occupies: a via its explicit
    ``layers`` or ``span`` (else every layer), a drilled pad every layer,
    anything else its ``layer``."""
    ctype = row.get("ctype")
    if ctype == "via":
        if row.get("layers"):
            return frozenset(index[str(x)] for x in row["layers"] if str(x) in index)
        span = row.get("span")
        if span and str(span[0]) in index and str(span[1]) in index:
            lo, hi = sorted((index[str(span[0])], index[str(span[1])]))
            return frozenset(range(lo, hi + 1))
        return frozenset(range(n))
    if ctype == "pad" and row.get("drill"):
        return frozenset(range(n))
    layer = str(row.get("layer", ""))
    return frozenset((index[layer],)) if layer in index else frozenset()


def obstacles_of(row: dict[str, Any], index: dict[str, int], n: int) -> list[Obstacle]:
    """One gerber-model copper row as obstacles: a track one per line
    segment and per arc (so each gets its own tight octagon), anything
    else its kernel body."""
    layers = _row_layers(row, index, n)
    if not layers:
        return []
    net = str(row.get("net", ""))
    if row.get("ctype") == "track":
        r = float(row.get("width_mm", 0.0)) / 2.0
        out: list[Obstacle] = []
        for seg in row.get("segments") or []:
            if seg.get("shape") == "arc":
                body = Body(None, (kernel.arc_from_segment(seg),), r)
            else:
                a = (float(seg["start"][0]), float(seg["start"][1]))
                b = (float(seg["end"][0]), float(seg["end"][1]))
                if a == b:
                    body = Body(Point(a), (), r)
                else:
                    body = Body(LineString([a, b]), (), r)
            out.append(Obstacle(body, net, layers))
        return out
    item = kernel.body_of(row)
    if item is None:
        return []
    return [Obstacle(item, net, layers, blocks_vias=row.get("ctype") in ("pad", "via"))]


@dataclass(frozen=True, slots=True)
class EscapeReport:
    """What one vector route pass did on one board: segments it tried,
    how many it routed, the search work it spent (the budget's unit)
    and, for information only, its wall time."""

    tried: int
    routed: int
    expansions: int
    elapsed_s: float
    out_of_budget: bool = False

    def line(self) -> str | None:
        """The route summary line; ``None`` when nothing was tried."""
        if not self.tried:
            return None
        out = (
            f"vector route: {self.routed} of {self.tried} segments "
            f"routed gridless ({self.expansions} expansions, "
            f"{self.elapsed_s:.1f}s)"
        )
        if self.out_of_budget:
            out += " (stopped at the expansion budget)"
        return out


__all__ = [
    "DELTA_MM",
    "EscapeReport",
    "Obstacle",
    "Request",
    "Space",
    "Terminal",
    "VPath",
    "ViaSpec",
    "connectors",
    "is_octilinear",
    "obstacles_of",
    "octagon_corners",
    "octile",
    "route",
    "support",
]
