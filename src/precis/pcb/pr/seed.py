"""4a topology seed for the slice-1 prototype (spec §Method 4a): each
connection is inserted into a per-layer CDT as a crossing-free path —
a :func:`precis.pcb.topo.funnel_path` band in the net's own centreline
domain — with portal occupancy priced against the SHARED per-layer
region graph (:meth:`RegionGraph.capacity_for`). When the preferred
path is blocked, the wires occupying the saturated portals are ripped,
the connection inserted, and the blockers re-inserted, depth-first under
a per-connection rip budget (``docs/backlog/pcb-place-route-datamodel.md``
"rip up the connections the cheapest path names as blockers").

The two graphs serve different jobs. The **net domain**
(:func:`topo.net_region_graph`, foreign obstacles inflated by
``w/2 + required.warn + δ``) is where the funnel runs: its taut band is
a legal-centreline polyline. The **shared graph** (real obstacles, one
per layer) is where portal identity is stable across nets, so occupancy
is tracked there: a seeded polyline's crossings of shared portal edges
mark its claim. The map between them is geometric — piece ∩ portal
edge — because the two triangulations do not share vertex sets.

Greedy first seed: each connection takes the cheaper layer subject to
capacity (spec: "the first seed puts each connection on its cheapest
layer greedily"). Wires that fit nowhere after rip-reinsert are reported
with the saturated portal as the reason, never silently dropped.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from precis.pcb import topo
from precis.pcb.pr.state import PrProblem

#: Funnel restarts allowed per insertion attempt when a saturated portal
#: is met on the preferred path (each restart bans the net-domain portal
#: nearest the saturated shared one — a different homotopy, not a rip).
_MAX_FUNNEL_ALTS = 4
#: Per-connection rip budget: connections a single insertion may displace.
_RIP_BUDGET = 32
#: Epsilon for the piece∩portal-edge contact test — portal vertices sit on
#: real obstacle corners, net-domain funnel vertices on inflated ones, so
#: a true crossing is decisive well above float noise.
_TOUCH_EPS = 1e-9


def _cross2(
    ax: np.ndarray, ay: np.ndarray, bx: np.ndarray, by: np.ndarray
) -> np.ndarray:
    return ax * by - ay * bx


def crossing_portals(graph: topo.RegionGraph, polyline: np.ndarray) -> list[int]:
    """Shared-graph portal ids the polyline crosses, in order crossed.
    A piece counts a portal when the segments properly intersect or touch
    (a funnel vertex resting on a portal edge still claims the gap)."""
    polyline = np.asarray(polyline, dtype=np.float64)
    if len(polyline) < 2 or not len(graph.portals):
        return []
    u = np.array([p.uv[0] for p in graph.portals])
    v = np.array([p.uv[1] for p in graph.portals])
    e = v - u
    out: list[int] = []
    seen: set[int] = set()
    for i in range(len(polyline) - 1):
        p0, p1 = polyline[i], polyline[i + 1]
        d = p1 - p0
        o1 = _cross2(d[0], d[1], u[:, 0] - p0[0], u[:, 1] - p0[1])
        o2 = _cross2(d[0], d[1], v[:, 0] - p0[0], v[:, 1] - p0[1])
        o3 = _cross2(e[:, 0], e[:, 1], p0[0] - u[:, 0], p0[1] - u[:, 1])
        o4 = _cross2(e[:, 0], e[:, 1], p1[0] - u[:, 0], p1[1] - u[:, 1])
        hit = (o1 * o2 <= _TOUCH_EPS) & (o3 * o4 <= _TOUCH_EPS)
        for pi_ in np.flatnonzero(hit):
            pi_i = int(pi_)
            if pi_i not in seen:
                seen.add(pi_i)
                out.append(pi_i)
    return out


@dataclass(slots=True)
class WireSeed:
    """One wire's seeded topology: its layer, the funnel polyline
    (terminal to terminal), and the shared-graph portals it claims."""

    polyline: np.ndarray  # (m, 2); empty when unrouted
    layer: int
    portals: tuple[int, ...]
    reason: str | None = None  # set when unrouted


@dataclass
class SeedResult:
    """The seeded board: per-wire seeds, the shared graphs they were
    inserted into, and the resulting portal occupancy."""

    seeds: tuple[WireSeed, ...]
    graphs: dict[int, topo.RegionGraph]
    occupancy: dict[int, dict[int, list[int]]]  # layer -> portal -> [wire]
    net_graphs: dict[tuple[int, int], topo.RegionGraph] = field(
        default_factory=dict, repr=False
    )

    @property
    def unrouted(self) -> tuple[int, ...]:
        return tuple(
            i for i, s in enumerate(self.seeds) if s is None or s.polyline.size == 0
        )

    def portals_of(self, wire: int) -> tuple[int, ...]:
        return self.seeds[wire].portals


def _shared_graphs(problem: PrProblem) -> dict[int, topo.RegionGraph]:
    """One physical portal graph per layer over all obstacles."""
    model = {"layers": list(problem.layers), "pads": list(problem.pads)}
    out: dict[int, topo.RegionGraph] = {}
    for li, lname in enumerate(problem.layers):
        obs = topo.board_obstacles(model, lname)
        track = max(w.width_mm for w in problem.wires)
        out[li] = topo.build_region_graph(
            obs,
            problem.outline.tolist(),
            lname,
            track_mm=track,
            clearance_mm=problem.clearance_mm,
        )
    return out


def _obstacles_by_layer(problem: PrProblem) -> dict[int, list[topo.Obstacle]]:
    model = {"layers": list(problem.layers), "pads": list(problem.pads)}
    return {
        li: topo.board_obstacles(model, lname)
        for li, lname in enumerate(problem.layers)
    }


def _net_graph(
    problem: PrProblem,
    obs: dict[int, list[topo.Obstacle]],
    cache: dict[tuple[int, int], topo.RegionGraph],
    wi: int,
    layer: int,
) -> topo.RegionGraph:
    key = (wi, layer)
    g = cache.get(key)
    if g is None:
        w = problem.wires[wi]
        g = topo.net_region_graph(
            obs[layer],
            problem.outline.tolist(),
            problem.layers[layer],
            net=problem.net_name[w.net_id],
            track_mm=w.width_mm,
            ctx=problem.ctx,
        )
        cache[key] = g
    return g


def seed(
    problem: PrProblem,
    *,
    rip_budget: int = _RIP_BUDGET,
    funnel_alts: int = _MAX_FUNNEL_ALTS,
    keep: dict[int, tuple[np.ndarray, int]] | None = None,
    skip: set[int] | None = None,
) -> SeedResult:
    """Insert every wire, cheapest layer first subject to capacity, with
    depth-first rip-reinsert of the blockers a saturated path names.

    ``keep`` maps a wire to its CURRENT ``(polyline, layer)``: those wires
    are committed as they are (their portal claims recorded) before
    anything is inserted, so an outer round re-inserts only the wires
    that have no polyline yet — and a kept wire is ripped and re-funnelled
    only when it blocks a new insertion (spec 4a, the rip-reinsert)."""
    graphs = _shared_graphs(problem)
    obs = _obstacles_by_layer(problem)
    occupancy: dict[int, dict[int, list[int]]] = {li: {} for li in graphs}
    net_graphs: dict[tuple[int, int], topo.RegionGraph] = {}
    seeds: list[WireSeed | None] = [None] * len(problem.wires)
    budget = [rip_budget]

    def capacity(layer: int, portal: int, wi: int) -> int:
        return graphs[layer].capacity_for(
            graphs[layer].portals[portal],
            problem.net_name[problem.wires[wi].net_id],
            problem.ctx,
            edge_mm=problem.edge_mm,
        )

    from shapely.geometry import LineString

    def crosses_committed(layer: int, poly: np.ndarray, wi: int) -> list[int]:
        """Committed same-layer wires ``poly`` properly crosses — a band
        sharing a sleeve can still cross a sibling (portal capacity
        counts lanes, not their order); treat that as a blockage too."""
        mine = LineString(poly)
        out = []
        for wj, s in enumerate(seeds):
            if s is None or s.layer != layer or len(s.polyline) < 2:
                continue
            if mine.crosses(LineString(s.polyline)):
                out.append(wj)
        return out

    def attempt(
        wi: int, layer: int, *, soft_capacity: bool = False
    ) -> tuple[Any, list[int], list[int], list[int]] | None:
        """Funnel on ``layer`` avoiding saturated portals and committed-
        wire crossings where a ban helps; ``(band, crossed, saturated,
        crossing_wires)`` or None when unreachable. ``soft_capacity``
        (the fallback pass) lets a wire claim an already-full portal —
        the over-capacity claim is the recorded debt the layer loop
        works off."""
        w = problem.wires[wi]
        ng = _net_graph(problem, obs, net_graphs, wi, layer)
        banned: set[int] = set()
        last: tuple[Any, list[int], list[int], list[int]] | None = None
        for _ in range(funnel_alts):
            band = topo.funnel_path(ng, w.a, w.b, banned=banned)
            if band is None:
                return last or None
            poly = np.asarray(band.points)
            crossed = crossing_portals(graphs[layer], poly)
            sat = [
                p
                for p in crossed
                if capacity(layer, p, wi) - len(occupancy[layer].get(p, ())) < 1
            ]
            xw = [] if sat else crosses_committed(layer, poly, wi)
            last = (band, crossed, sat, xw)
            if not sat and not xw:
                return last
            if soft_capacity and not xw:
                return last
            # Ban the net-domain portal nearest the offence — a
            # different homotopy, not a rip.
            if sat:
                s_mid = np.mean(np.asarray(graphs[layer].portals[sat[0]].uv), axis=0)
            else:
                xseed = seeds[xw[0]]
                assert xseed is not None
                hit = LineString(poly).intersection(LineString(xseed.polyline))
                s_mid = (
                    np.asarray([hit.x, hit.y])
                    if hit.geom_type == "Point"
                    else np.asarray(hit.centroid.coords[0])
                )
            mids = [np.mean(np.asarray(ng.portals[p].uv), axis=0) for p in band.portals]
            if not mids:
                break
            banned.add(
                band.portals[int(np.argmin([np.linalg.norm(m - s_mid) for m in mids]))]
            )
        return last

    def commit(wi: int, layer: int, band: Any, crossed: list[int]) -> None:
        for p in crossed:
            occupancy[layer].setdefault(p, []).append(wi)
        pts = band if isinstance(band, np.ndarray) else np.asarray(band.points)
        seeds[wi] = WireSeed(polyline=pts, layer=layer, portals=tuple(crossed))

    for wi, (poly, layer) in (keep or {}).items():
        poly = np.asarray(poly, dtype=np.float64)
        if len(poly) < 2 or not (0 <= layer < problem.n_layers):
            continue
        commit(wi, layer, poly, crossing_portals(graphs[layer], poly))

    def rip(wi: int) -> None:
        s = seeds[wi]
        assert s is not None
        for p in s.portals:
            occupancy[s.layer][p].remove(wi)
        seeds[wi] = None

    def insert(wi: int, depth: int) -> bool:
        budget[0] -= 1
        if budget[0] < 0:
            return False
        w = problem.wires[wi]
        # A whole-wire seed layer must carry BOTH terminals' copper.
        shared = [
            l
            for l in range(problem.n_layers)
            if (w.a_lmask >> l) & 1 and (w.b_lmask >> l) & 1
        ]
        routable = [l for l in range(problem.n_layers) if (w.route_lmask >> l) & 1]
        locked = [l for l in shared if l in routable] or shared or routable
        cands = []
        for layer in locked or list(range(problem.n_layers)):
            got = attempt(wi, layer, soft_capacity=depth > 0)
            if got is not None:
                band, crossed, sat, xw = got
                cands.append((band.length_mm, layer, band, crossed, sat, xw))
        free = [c for c in cands if not c[4] and not c[5]]
        if free:
            _, layer, band, crossed, _, _ = min(free, key=lambda c: c[0])
            commit(wi, layer, band, crossed)
            return True
        if not cands or depth >= rip_budget:
            return False
        # Cheapest blocked path names the blockers (spec 4a). A
        # crossing-only blockage rips the crossed wire; capacity
        # blockers are ripped only on the hard pass (depth == 0) —
        # deeper recursion already admits the over-capacity claim.
        _, layer, band, crossed, sat, xw = min(cands, key=lambda c: c[0])
        blockers = sorted(
            set(xw)
            | (
                {bi for p in sat for bi in occupancy[layer].get(p, ())}
                if depth == 0
                else set()
            )
        )
        if not blockers:
            if depth > 0 and not xw:
                # Soft capacity: claim the saturated portals anyway.
                commit(wi, layer, band, crossed)
                return True
            return False
        for bi in blockers:
            rip(bi)
        ok = insert(wi, depth + 1)
        for bi in blockers:
            # Ripped blockers get their reinsertion whether or not the
            # insertion that displaced them landed — losing them quietly
            # is not an option.
            if not insert(bi, depth + 1):
                ok = False
        return ok

    for wi in sorted(skip or ()):
        seeds[wi] = WireSeed(
            polyline=np.zeros((0, 2)), layer=-1, portals=(), reason="skipped on request"
        )
    for wi in range(len(problem.wires)):
        if seeds[wi] is not None:
            continue  # kept from the previous round, or skipped
        budget[0] = rip_budget
        if not insert(wi, 0):
            # Fallback pass: capacity-soft, crossing still hard — the
            # seed must produce a polyline; the over-capacity portals it
            # claims are the layer loop's recorded debt.
            if not insert(wi, 1):
                if seeds[wi] is not None:
                    rip(wi)
                seeds[wi] = WireSeed(
                    polyline=np.zeros((0, 2)),
                    layer=-1,
                    portals=(),
                    reason="no path on any layer",
                )
    # A wire left un-seeded is unrouted, never silently absent.
    for wi, s in enumerate(seeds):
        if s is None:
            seeds[wi] = WireSeed(
                polyline=np.zeros((0, 2)),
                layer=-1,
                portals=(),
                reason="ripped by a failed reinsertion",
            )
    return SeedResult(
        seeds=tuple(s for s in seeds if s is not None),
        graphs=graphs,
        occupancy=occupancy,
        net_graphs=net_graphs,
    )


def refunnel(
    problem: PrProblem,
    seeded: SeedResult,
    wi: int,
    target_layers: np.ndarray,
    piece_mm: float = 2.0,
    base: np.ndarray | None = None,
    prev_layers: np.ndarray | None = None,
) -> np.ndarray:
    """Re-insert the spans of wire ``wi`` whose target layer differs from
    the seed's: each maximal same-layer run is re-funnelled on ITS layer's
    net domain between the boundary points (the via sites, shared by the
    two layers). Runs on the seeded layer keep their funnel geometry.
    ``target_layers`` is per SUBDIVIDED piece (the layout's span grid);
    the returned polyline is the subdivided one. Falls back to the
    seeded geometry for a run whose sub-funnel fails. ``base`` is the
    polyline to start from (default: the seed's — the slice-2 driver
    passes the relaxed one)."""
    seed = seeded.seeds[wi]
    src = seed.polyline if base is None else base
    poly = subdivide(np.asarray(src, dtype=np.float64), piece_mm)
    if poly.size == 0 or len(poly) < 2:
        return poly
    obs = _obstacles_by_layer(problem)
    out: list[tuple[float, float]] = []
    n_pieces = len(poly) - 1
    if len(target_layers) != n_pieces:
        return poly  # span grid changed; keep the seeded geometry
    if prev_layers is not None and len(prev_layers) != n_pieces:
        prev_layers = None
    i = 0
    while i < n_pieces:
        layer = int(target_layers[i])
        j = i
        while j < n_pieces and int(target_layers[j]) == layer:
            j += 1
        # Run [i, j): pieces i..j-1 on `layer`, endpoints poly[i]..poly[j].
        a, b = poly[i], poly[j]
        unchanged = (
            all(int(prev_layers[k]) == layer for k in range(i, j))
            if prev_layers is not None
            else layer == seed.layer
        )
        if unchanged:
            run = [tuple(p) for p in poly[i : j + 1]]
        else:
            ng = _net_graph(problem, obs, seeded.net_graphs, wi, layer)
            band = topo.funnel_path(ng, tuple(a), tuple(b))
            run = (
                [tuple(p) for p in band.points]
                if band is not None
                else [tuple(p) for p in poly[i : j + 1]]
            )
        if out:
            run = run[1:]  # shared boundary point
        out += run
        i = j
    return np.asarray(out)


def subdivide(polyline: np.ndarray, piece_mm: float) -> np.ndarray:
    """Refine a polyline so every piece is ≤ ``piece_mm`` — the span
    granularity 4b/4c work on (spec: funnel vertices per span, pieces
    ≤ 2 mm)."""
    if len(polyline) < 2:
        return polyline
    out = [polyline[0]]
    for i in range(len(polyline) - 1):
        a, b = polyline[i], polyline[i + 1]
        n = max(1, math.ceil(float(np.linalg.norm(b - a)) / piece_mm))
        for k in range(1, n + 1):
            out.append(a + (b - a) * (k / n))
    return np.asarray(out)


def capacity_violations(
    problem: PrProblem,
    seeded: SeedResult,
    polylines: list[np.ndarray],
    layers: list[int],
) -> list[str]:
    """The growth-step capacity check: recount each wire's crossed
    shared-graph portals at its CURRENT polyline and report the first
    portal whose occupancy beats its lanes — the 4c failure reason."""
    use: dict[int, dict[int, set[int]]] = {li: {} for li in seeded.graphs}
    for wi, (poly, layer) in enumerate(zip(polylines, layers, strict=True)):
        if len(poly) < 2:
            continue
        for p in crossing_portals(seeded.graphs[layer], poly):
            use[layer].setdefault(p, set()).add(wi)
    out: list[str] = []
    for layer, per in use.items():
        for p, wset in sorted(per.items()):
            portal = seeded.graphs[layer].portals[p]
            worst = min(
                seeded.graphs[layer].capacity_for(
                    portal,
                    problem.net_name[problem.wires[wi].net_id],
                    problem.ctx,
                    edge_mm=problem.edge_mm,
                )
                for wi in wset
            )
            if len(wset) > worst:
                u, v = portal.uv
                out.append(
                    f"portal {p} on {problem.layers[layer]} "
                    f"(({u[0]:.2f},{u[1]:.2f})-({v[0]:.2f},{v[1]:.2f}), "
                    f"{portal.length_mm:.3f} mm): {len(wset)} wires through "
                    f"{worst} lane(s)"
                )
                return out
    return out
