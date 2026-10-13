"""Tile-capacity global routing plan — an experiment probe, not wired in.

**Why this module exists.** :func:`precis.pcb.realize._realize_maze` routes
nets sequentially, shortest-first, on a hard-occupancy grid: the first net
to claim a corridor keeps it, and no mechanism expresses "leave room for
the net that has nowhere else to go". On the EWOD dogfood-6 replay board
that ceiling is 51/55. The hypothesis under test
(``docs/backlog/pcb-global-plan-probe.md``) is that the residual is a
missing *capacity-aware global plan* — not grid pitch or lane count — so
this module measures whether a feasible coarse plan exists at all, and
whether handing it to the maze as corridors beats 51/55.

**Three stages, all pure functions over the same static copper the maze
stamps** (the probe test captures :func:`precis.pcb.realize._route_pass`'s
arguments, so the plan reads exactly what a real pass would route over):

1. :func:`build_tile_graph` — per routable layer, ``tile_mm``-square
   tiles. Each tile edge's capacity is the number of escape-class tracks
   whose centrelines could cross that boundary: cells of *foreign* copper
   dilated by the track's keep-out radius split the boundary into free
   runs, and each run contributes ``floor(run_mm / (track + clearance))``
   lanes. Claims owned by a *participant* net are exempt from this mask —
   every planned net must be able to leave its own endpoint tiles — but
   NOT from the via mask below: a through-via must clear every net's
   copper, its own included. Each tile's via capacity counts how many
   ``via_dia + clearance``-spaced envelopes fit in the cells that are via-
   legal on EVERY stackup layer (the router's own ``r_via`` disk plus the
   pad keep-out mask), packed greedily in scan order.
2. :func:`plan_routes` — negotiated congestion (PathFinder, McMurchie &
   Ebeling 1995, VPR schedule) over the tile graph. Nodes are
   ``(tile, layer)``; in-plane edges cost ``tile_mm`` times the layer's
   off-axis penalty for a non-preferred direction; via edges cost
   ``maze.VIA_COST_MM`` and consume the tile's via capacity. Each
   iteration rips up and re-routes every net against present-plus-history
   cost; converged means every edge and via within capacity. With
   ``free_sinks`` each net may end at ANY candidate pad node and the
   one-pad-per-net matching is re-solved each iteration by
   :func:`precis.pcb.pinswap._hungarian` on the negotiated distances.
3. :func:`corridor_route` — commits the plan to the real
   ``OccupancyGrid`` by restricting each net's search to its corridor
   tiles dilated by one tile, implemented by temporarily stamping
   :data:`~precis.pcb.maze.CONTESTED` on out-of-corridor cells (the
   router reads CONTESTED as foreign to every net, endpoints excepted —
   same semantics as a ``mask`` argument, with zero changes to
   :mod:`precis.pcb.maze`). Leftover nets then get the ordinary
   rip-up-and-reorder treatment with the corridor results held fixed as
   proposals (:func:`precis.pcb.realize._route_pass`'s ``preferred=``
   seam, which commits a path verbatim when it is still legal).

Nothing here is a default, a job param or a handler: the probe exists to
produce numbers for the results note. Everything is deterministic —
fixed iteration orders, ``(cost, seq, node)`` heap keys, and
:func:`precis.pcb.pinswap._hungarian`'s deterministic matching.
"""

from __future__ import annotations

import heapq
import itertools
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from precis.pcb import maze, pinswap, realize
from precis.pcb.geom import dist
from precis.pcb.ir import PcbIR

#: Default tile side for the probe (the task's parameter).
DEFAULT_TILE_MM = 0.5

#: PathFinder schedule (VPR defaults — same constants the fine-grid
#: :class:`precis.pcb.maze.Negotiation` uses, mirrored so the plan's price
#: curve is the same shape as the router's own negotiation).
PRES_FAC0 = maze.NEGOTIATE_PRES_FAC
PRES_GROWTH = maze.NEGOTIATE_PRES_GROWTH
HIST_FAC = maze.NEGOTIATE_HIST_FAC

#: Cap on PathFinder iterations. Convergence on a small tile graph is
#: tens of iterations at most; a loop still open at the cap is reported
#: as not converged, which is itself the measurement.
MAX_PLAN_ITERATIONS = 200

#: Corridor dilation, in tiles, for the detailed route: the plan's own
#: tiles plus one ring. The task's "dilate the corridor tiles by one
#: tile" — enough slack for a via envelope or a detour inside the lane
#: without letting the search wander back into contested space.
CORRIDOR_DILATE_TILES = 1


def _dilate_disk(mask: np.ndarray, r_cells: int) -> np.ndarray:
    """Boolean dilation by a Euclidean disk of ``r_cells`` — the same
    disk :func:`~precis.pcb.maze._disk_hits_foreign` tests, computed as
    ORs of shifted copies (a cell ``r`` from a blocked one reads blocked).
    Used for the capacity masks only; routing legality stays with the
    occupancy grid."""
    if r_cells <= 0 or not mask.any():
        return mask.copy()
    ny, nx = mask.shape
    half = maze._disk_half_chords(r_cells)
    out = mask.copy()
    for dy in range(-r_cells, r_cells + 1):
        w = int(half[dy + r_cells])
        y_lo = max(0, dy)
        y_hi = min(ny, ny + dy)
        if y_lo >= y_hi:
            continue
        src = mask[y_lo - dy : y_hi - dy]
        row = np.zeros_like(src)
        for dx in range(-w, w + 1):
            x_lo = max(0, dx)
            x_hi = min(nx, nx + dx)
            row[:, x_lo:x_hi] |= src[:, x_lo - dx : x_hi - dx]
        out[y_lo:y_hi] |= row
    return out


def _run_capacity(free: np.ndarray, cell_mm: float, lane_mm: float) -> int:
    """Track lanes through the contiguous FREE runs of one boundary scan:
    ``floor(run_mm / lane_mm)`` per run, summed. Per-run flooring, not a
    floor over the total — two stubs of half a lane do not make a lane."""
    cap = 0
    run = 0
    for f in free:
        if f:
            run += 1
        else:
            cap += math.floor(run * cell_mm / lane_mm) if run else 0
            run = 0
    if run:
        cap += math.floor(run * cell_mm / lane_mm)
    return cap


@dataclass(frozen=True, slots=True)
class TileGraph:
    """The coarse routing graph for one board state.

    ``cap_x[li, ty, tx]`` is the track capacity of the boundary between
    tiles ``(tx, ty)`` and ``(tx + 1, ty)`` on ``layers[li]``; ``cap_y``
    the same for the ``(tx, ty)``/``(tx, ty + 1)`` boundary.
    ``via_cap[ty, tx]`` is the number of through-via envelopes that fit in
    the tile's free area on all layers. ``layers`` are stackup indices;
    ``ox``/``oy`` is the tile grid's origin in board mm (the maze grid's
    own origin — tile edges need not coincide with cell centres)."""

    spec: maze.GridSpec
    tile_mm: float
    layers: tuple[int, ...]
    ntx: int
    nty: int
    ox: float
    oy: float
    cap_x: np.ndarray
    cap_y: np.ndarray
    via_cap: np.ndarray
    row_report: tuple[str, ...]

    def tile_of(self, x: float, y: float) -> tuple[int, int]:
        tx = math.floor((x - self.ox) / self.tile_mm)
        ty = math.floor((y - self.oy) / self.tile_mm)
        return (min(max(tx, 0), self.ntx - 1), min(max(ty, 0), self.nty - 1))

    def node(self, li: int, tx: int, ty: int) -> int:
        return (li * self.nty + ty) * self.ntx + tx

    def node_parts(self, node: int) -> tuple[int, int, int]:
        li, rem = divmod(node, self.ntx * self.nty)
        ty, tx = divmod(rem, self.ntx)
        return li, tx, ty


def build_tile_graph(
    grid: maze.OccupancyGrid,
    *,
    layers: Sequence[int],
    track_mm: float,
    clearance_mm: float,
    via_dia_mm: float,
    tile_mm: float = DEFAULT_TILE_MM,
    participant_nets: Iterable[int] = (),
) -> TileGraph:
    """Measure the tile graph's capacities off ``grid``'s stamped claims.

    ``participant_nets`` are the nets being planned (the escape class):
    their own claims are exempt from the *track* capacity mask so a net
    can leave the tile its pad or via sits in. Via capacity uses every
    claim — a drilled via clears copper net-blind — folded with
    ``grid``'s own pad keep-out mask, so a tile under a pad row reports
    zero rather than a nominal count the router could never use.
    """
    spec = grid.spec
    owner = grid.owner
    xs = spec.x0 + np.arange(spec.nx) * spec.pitch
    ys = spec.y0 + np.arange(spec.ny) * spec.pitch
    ox, oy = spec.x0, spec.y0
    ntx = math.ceil(spec.nx * spec.pitch / tile_mm)
    nty = math.ceil(spec.ny * spec.pitch / tile_mm)
    layer_list = list(layers)
    participants = np.isin(owner, np.asarray(sorted(participant_nets), np.int32))
    foreign = (owner != maze.FREE) & ~participants
    # The track keep-out radius mirrors `_route_in`: half the track width
    # plus one cell of discretisation slack.
    r_track = math.ceil((track_mm / 2.0) / spec.pitch) + 1
    lane_mm = track_mm + clearance_mm
    tx_of = np.clip(((xs - ox) / tile_mm).astype(int), 0, ntx - 1)
    ty_of = np.clip(((ys - oy) / tile_mm).astype(int), 0, nty - 1)
    cap_x = np.zeros((len(layer_list), nty, ntx - 1), dtype=np.int32)
    cap_y = np.zeros((len(layer_list), nty - 1, ntx), dtype=np.int32)
    for li, layer in enumerate(layer_list):
        free = ~_dilate_disk(foreign[layer], r_track)
        for tx in range(ntx - 1):
            bx = ox + (tx + 1) * tile_mm
            ix_l = int(np.searchsorted(xs, bx, side="left")) - 1
            ix_r = ix_l + 1
            if ix_l < 0 or ix_r >= spec.nx:
                continue
            both = free[:, ix_l] & free[:, ix_r]
            for ty in range(nty):
                cap_x[li, ty, tx] = _run_capacity(
                    both[ty_of == ty], spec.pitch, lane_mm
                )
        for ty in range(nty - 1):
            by = oy + (ty + 1) * tile_mm
            iy_l = int(np.searchsorted(ys, by, side="left")) - 1
            iy_r = iy_l + 1
            if iy_l < 0 or iy_r >= spec.ny:
                continue
            both = free[iy_l, :] & free[iy_r, :]
            for tx in range(ntx):
                cap_y[li, ty, tx] = _run_capacity(
                    both[tx_of == tx], spec.pitch, lane_mm
                )
    # Via capacity: a through-via needs its envelope on EVERY layer, plus
    # the router's pad keep-out. All claims count (participants included).
    r_via = math.ceil((via_dia_mm / 2.0) / spec.pitch) + 1
    via_free = np.ones((spec.ny, spec.nx), dtype=bool)
    for layer in range(spec.n_layers):
        via_free &= ~_dilate_disk(owner[layer] != maze.FREE, r_via)
    via_free &= ~grid._pad_keepout_mask(via_dia_mm / 2.0)
    via_pitch = via_dia_mm + clearance_mm
    via_cap = np.zeros((nty, ntx), dtype=np.int32)
    for ty in range(nty):
        rows = np.nonzero(ty_of == ty)[0]
        for tx in range(ntx):
            cols = np.nonzero(tx_of == tx)[0]
            cells = np.argwhere(via_free[np.ix_(rows, cols)])
            placed: list[tuple[float, float]] = []
            for ry, rx in cells:
                x, y = xs[cols[rx]], ys[rows[ry]]
                if all(dist((x, y), p) >= via_pitch - 1e-9 for p in placed):
                    placed.append((x, y))
            via_cap[ty, tx] = len(placed)
    lines: list[str] = []
    for li, layer in enumerate(layer_list):
        lines.append(f"layer {layer} ({li}): tile rows y-capacity / x-capacity / vias")
        for ty in range(nty):
            lines.append(
                f"  row {ty:2d}: cap_x sum={int(cap_x[li, ty].sum()):3d} "
                f"min={int(cap_x[li, ty].min()):2d} | "
                f"cap_y "
                + (
                    f"sum={int(cap_y[li, ty].sum()):3d} "
                    f"min={int(cap_y[li, ty].min()):2d}"
                    if ty < nty - 1
                    else "   -   "
                )
                + f" | vias={int(via_cap[ty].sum()):3d}"
            )
    return TileGraph(
        spec,
        tile_mm,
        tuple(layer_list),
        ntx,
        nty,
        ox,
        oy,
        cap_x,
        cap_y,
        via_cap,
        tuple(lines),
    )


@dataclass(frozen=True, slots=True)
class PlanNet:
    """One connection to plan: ``sources`` are the node ids of its fixed
    terminal's tile on every planned layer; ``sink`` is the fixed sink
    node (pad tile on its pad layer) or ``None`` when ``sinks`` holds the
    admissible set for the folded-in assignment."""

    seg_id: int
    net_id: int
    name: str
    sources: tuple[int, ...]
    sink: int | None
    sinks: tuple[int, ...] = ()
    sink_pins: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class PlannedPath:
    """One net's tile path: node ids in order plus its via tiles
    (``ty * ntx + tx`` per layer change). ``layers``/``tiles`` derive from
    ``nodes``."""

    nodes: tuple[int, ...]
    cost_mm: float


@dataclass(frozen=True, slots=True)
class Saturated:
    """One resource at or over capacity at plan end (``use >= cap``)."""

    kind: str  # "x", "y" or "via"
    layer: int  # stackup index, or -1 for via
    tx: int
    ty: int
    use: int
    cap: int


@dataclass(frozen=True, slots=True)
class PlanResult:
    """What one :func:`plan_routes` run produced. ``converged`` = every
    edge and every via tile within capacity AND every net routed.
    ``assignment`` maps ``seg_id`` to the sink pin the last iteration's
    matching chose (empty for fixed-sink runs)."""

    converged: bool
    iterations: int
    conflicted_per_iter: tuple[int, ...]
    total_length_mm: float
    via_count: int
    unrouted: tuple[int, ...]
    saturated: tuple[Saturated, ...]
    paths: dict[int, PlannedPath]
    assignment: dict[int, int] = field(default_factory=dict)


def _off_axis(pref: str | None, dx: int) -> float:
    """Step multiplier for a tile edge along +x (``dx`` true) or +y."""
    if pref == maze.PREF_H:
        return 1.0 if dx else maze.OFF_AXIS_PENALTY
    if pref == maze.PREF_V:
        return maze.OFF_AXIS_PENALTY if dx else 1.0
    if pref == maze.PREF_DIAG:
        # A 4-adjacency tile graph has no diagonal edges; both axes pay
        # the "not what this layer is for" price uniformly.
        return maze.DIAGONAL_PENALTY
    return 1.0


def plan_routes(
    graph: TileGraph,
    nets: Sequence[PlanNet],
    *,
    layer_prefs: dict[int, str] | None = None,
    free_sinks: bool = False,
    max_iterations: int = MAX_PLAN_ITERATIONS,
    pres_fac0: float = PRES_FAC0,
    pres_growth: float = PRES_GROWTH,
    hist_fac: float = HIST_FAC,
    via_cost_mm: float = maze.VIA_COST_MM,
) -> PlanResult:
    """PathFinder over the tile graph. Iteration 0 routes every net; later
    iterations rip up and re-route only the nets that ended the previous
    one in conflict (touching an over-capacity resource, or unrouted) —
    the same unit-of-work choice ``realize._negotiate`` makes on the fine
    grid. Edge cost is ``base * (1 + pres * over) + hist_fac * hist`` with
    ``over = use - cap + 1`` the prospective excess; over-capacity
    resources accrue one unit of history each iteration they stay hot.

    Deterministic: fixed net order, ``(cost, seq, node)`` heap keys, and a
    Hungarian pad matching for ``free_sinks`` (each iteration re-solves
    the one-pad-per-net assignment over the pads not held by
    non-conflicted nets, on the distances the same iteration's searches
    produced)."""
    ntx, nty = graph.ntx, graph.nty
    n_nodes = len(graph.layers) * ntx * nty
    use_x = np.zeros(graph.cap_x.shape, dtype=np.int32)
    use_y = np.zeros(graph.cap_y.shape, dtype=np.int32)
    use_v = np.zeros(graph.via_cap.shape, dtype=np.int32)
    hx = np.zeros(graph.cap_x.shape, dtype=np.float64)
    hy = np.zeros(graph.cap_y.shape, dtype=np.float64)
    hv = np.zeros(graph.via_cap.shape, dtype=np.float64)
    pres = pres_fac0
    pref = {li: (layer_prefs or {}).get(layer) for li, layer in enumerate(graph.layers)}
    cost_x = np.array(
        [graph.tile_mm * _off_axis(pref[li], True) for li in range(len(graph.layers))]
    )
    cost_y = np.array(
        [graph.tile_mm * _off_axis(pref[li], False) for li in range(len(graph.layers))]
    )

    def price(use: int, cap: int, hist: float) -> float:
        over = max(0, use - cap + 1)
        return 1.0 + pres * over + hist_fac * hist

    def dijkstra(
        net: PlanNet, targets: frozenset[int] | None
    ) -> tuple[np.ndarray, np.ndarray]:
        """Min-cost tree from ``net.sources`` over current usage.
        ``(dist, came)``; early-exits once every member of ``targets``
        is closed (``None`` = run to exhaustion)."""
        dist = np.full(n_nodes, math.inf)
        came = np.full(n_nodes, -1, dtype=np.int64)
        closed = np.zeros(n_nodes, dtype=bool)
        want = 0 if targets is None else len(targets)
        heap: list[tuple[float, int, int]] = []
        seq = 0
        for s in net.sources:
            if dist[s] > 0.0:
                dist[s] = 0.0
                heapq.heappush(heap, (0.0, seq, s))
                seq += 1
        while heap:
            d, _s, node = heapq.heappop(heap)
            if closed[node]:
                continue
            closed[node] = True
            if targets is not None and node in targets:
                want -= 1
                if want == 0:
                    break
            li, tx, ty = graph.node_parts(node)
            if tx + 1 < ntx and graph.cap_x[li, ty, tx] > 0:
                c = d + cost_x[li] * price(
                    int(use_x[li, ty, tx]),
                    int(graph.cap_x[li, ty, tx]),
                    float(hx[li, ty, tx]),
                )
                if c < dist[node + 1]:
                    dist[node + 1] = c
                    came[node + 1] = node
                    heapq.heappush(heap, (c, seq, node + 1))
                    seq += 1
            if tx > 0 and graph.cap_x[li, ty, tx - 1] > 0:
                c = d + cost_x[li] * price(
                    int(use_x[li, ty, tx - 1]),
                    int(graph.cap_x[li, ty, tx - 1]),
                    float(hx[li, ty, tx - 1]),
                )
                if c < dist[node - 1]:
                    dist[node - 1] = c
                    came[node - 1] = node
                    heapq.heappush(heap, (c, seq, node - 1))
                    seq += 1
            if ty + 1 < nty and graph.cap_y[li, ty, tx] > 0:
                c = d + cost_y[li] * price(
                    int(use_y[li, ty, tx]),
                    int(graph.cap_y[li, ty, tx]),
                    float(hy[li, ty, tx]),
                )
                nb = node + ntx
                if c < dist[nb]:
                    dist[nb] = c
                    came[nb] = node
                    heapq.heappush(heap, (c, seq, nb))
                    seq += 1
            if ty > 0 and graph.cap_y[li, ty - 1, tx] > 0:
                c = d + cost_y[li] * price(
                    int(use_y[li, ty - 1, tx]),
                    int(graph.cap_y[li, ty - 1, tx]),
                    float(hy[li, ty - 1, tx]),
                )
                nb = node - ntx
                if c < dist[nb]:
                    dist[nb] = c
                    came[nb] = node
                    heapq.heappush(heap, (c, seq, nb))
                    seq += 1
            if graph.via_cap[ty, tx] > 0:
                for lj in range(len(graph.layers)):
                    if lj == li:
                        continue
                    nb = node + (lj - li) * ntx * nty
                    c = d + via_cost_mm * price(
                        int(use_v[ty, tx]),
                        int(graph.via_cap[ty, tx]),
                        float(hv[ty, tx]),
                    )
                    if c < dist[nb]:
                        dist[nb] = c
                        came[nb] = node
                        heapq.heappush(heap, (c, seq, nb))
                        seq += 1
        return dist, came

    def path_nodes(came: np.ndarray, sink: int) -> tuple[int, ...]:
        out = [sink]
        while came[out[-1]] >= 0:
            out.append(int(came[out[-1]]))
        out.reverse()
        return tuple(out)

    def add(nodes: tuple[int, ...], delta: int) -> None:
        exs, eys, vts = _resources(graph, nodes)
        for li, ty, tx in exs:
            use_x[li, ty, tx] += delta
        for li, ty, tx in eys:
            use_y[li, ty, tx] += delta
        for vt in vts:
            ty_, tx_ = divmod(vt, ntx)
            use_v[ty_, tx_] += delta

    paths: dict[int, tuple[int, ...]] = {}
    assignment: dict[int, int] = {}
    all_sinks = sorted({s for n in nets for s in n.sinks})
    trail: list[int] = []
    converged = False
    iterations = 0
    conflicted = {net.seg_id for net in nets}
    for iteration in range(max_iterations):
        iterations = iteration + 1
        if free_sinks:
            # Re-solve one-pad-per-net for the conflicted nets over the
            # pads the settled nets do not already hold.
            held = {assignment[s] for s in assignment if s not in conflicted}
            free = [s for s in all_sinks if s not in held]
            todo = [n for n in nets if n.seg_id in conflicted]
            dist_rows: list[np.ndarray] = []
            cames: list[np.ndarray] = []
            for net in todo:
                if net.seg_id in paths:
                    add(paths[net.seg_id], -1)
                dist, came = dijkstra(net, frozenset(free))
                dist_rows.append(dist)
                cames.append(came)
            big = 1e12
            cost = [
                [
                    float(dist_rows[i][s]) if math.isfinite(dist_rows[i][s]) else big
                    for s in free
                ]
                for i in range(len(todo))
            ]
            assign = pinswap._hungarian(cost) if todo else []
            for i, net in enumerate(todo):
                sink = free[assign[i]]
                assignment[net.seg_id] = sink
                if not math.isfinite(dist_rows[i][sink]):
                    paths.pop(net.seg_id, None)
                    continue
                nodes = path_nodes(cames[i], sink)
                paths[net.seg_id] = nodes
                add(nodes, 1)
        else:
            for net in nets:
                if net.seg_id not in conflicted:
                    continue
                if net.seg_id in paths:
                    add(paths[net.seg_id], -1)
                dist, came = dijkstra(
                    net, frozenset({net.sink}) if net.sink is not None else None
                )
                if net.sink is None or not math.isfinite(dist[net.sink]):
                    paths.pop(net.seg_id, None)
                    continue
                nodes = path_nodes(came, net.sink)
                paths[net.seg_id] = nodes
                add(nodes, 1)
        over_x = np.argwhere(use_x > graph.cap_x)
        over_y = np.argwhere(use_y > graph.cap_y)
        over_v = np.argwhere(use_v > graph.via_cap)
        for li, ty, tx in over_x:
            hx[li, ty, tx] += 1.0
        for li, ty, tx in over_y:
            hy[li, ty, tx] += 1.0
        for ty, tx in over_v:
            hv[ty, tx] += 1.0
        over_x_set = {(int(a), int(b), int(c)) for a, b, c in over_x}
        over_y_set = {(int(a), int(b), int(c)) for a, b, c in over_y}
        over_v_set = {int(a * ntx + b) for a, b in over_v}
        conflicted = set()
        for net in nets:
            planned = paths.get(net.seg_id)
            if planned is None:
                conflicted.add(net.seg_id)
                continue
            exs, eys, vts = _resources(graph, planned)
            if (
                any(e in over_x_set for e in exs)
                or any(e in over_y_set for e in eys)
                or any(v in over_v_set for v in vts)
            ):
                conflicted.add(net.seg_id)
        trail.append(len(conflicted))
        if not conflicted:
            converged = True
            break
        pres *= pres_growth
    # A resource is saturated when it is USED to its limit; a cap-0 edge
    # or tile nobody uses is a wall, not a saturated cut (2553 of them on
    # the dogfood-6 replay would otherwise read as "tight").
    saturated: list[Saturated] = []
    for li, ty, tx in np.argwhere((use_x >= graph.cap_x) & (graph.cap_x > 0)):
        saturated.append(
            Saturated(
                "x",
                int(graph.layers[li]),
                int(tx),
                int(ty),
                int(use_x[li, ty, tx]),
                int(graph.cap_x[li, ty, tx]),
            )
        )
    for li, ty, tx in np.argwhere((use_y >= graph.cap_y) & (graph.cap_y > 0)):
        saturated.append(
            Saturated(
                "y",
                int(graph.layers[li]),
                int(tx),
                int(ty),
                int(use_y[li, ty, tx]),
                int(graph.cap_y[li, ty, tx]),
            )
        )
    for ty, tx in np.argwhere((use_v >= graph.via_cap) & (graph.via_cap > 0)):
        saturated.append(
            Saturated(
                "via",
                -1,
                int(tx),
                int(ty),
                int(use_v[ty, tx]),
                int(graph.via_cap[ty, tx]),
            )
        )
    total_len = 0.0
    via_count = 0
    final_paths: dict[int, PlannedPath] = {}
    for net in nets:
        planned = paths.get(net.seg_id)
        if planned is None:
            continue
        exs, eys, vts = _resources(graph, planned)
        # Report geometric cost, not the negotiated price: tile_mm per
        # in-plane step, one via per layer change.
        total_len += (len(exs) + len(eys)) * graph.tile_mm
        via_count += len(vts)
        geom_cost = (len(exs) + len(eys)) * graph.tile_mm + len(vts) * via_cost_mm
        final_paths[net.seg_id] = PlannedPath(planned, geom_cost)
    return PlanResult(
        converged=converged,
        iterations=iterations,
        conflicted_per_iter=tuple(trail),
        total_length_mm=total_len,
        via_count=via_count,
        unrouted=tuple(n.seg_id for n in nets if n.seg_id not in final_paths),
        saturated=tuple(saturated),
        paths=final_paths,
        assignment=assignment,
    )


def corridor_tiles(
    graph: TileGraph, path: PlannedPath, *, dilate: int = CORRIDOR_DILATE_TILES
) -> dict[int, set[tuple[int, int]]]:
    """``{stackup_layer: {(tx, ty)}}`` the path touches, dilated by
    ``dilate`` tiles. A via's tile counts on both endpoint layers (the
    transition touches both)."""
    out: dict[int, set[tuple[int, int]]] = {}
    for node in path.nodes:
        li, tx, ty = graph.node_parts(node)
        out.setdefault(graph.layers[li], set()).add((tx, ty))
    grown: dict[int, set[tuple[int, int]]] = {}
    for layer, tiles in out.items():
        spread: set[tuple[int, int]] = set()
        for tx, ty in tiles:
            for dx in range(-dilate, dilate + 1):
                for dy in range(-dilate, dilate + 1):
                    nx_, ny_ = tx + dx, ty + dy
                    if 0 <= nx_ < graph.ntx and 0 <= ny_ < graph.nty:
                        spread.add((nx_, ny_))
        grown[layer] = spread
    return grown


def corridor_mask(
    graph: TileGraph,
    tiles_by_layer: dict[int, set[tuple[int, int]]],
    *,
    extra: Iterable[tuple[int, float, float]] = (),
) -> np.ndarray:
    """``(n_layers, ny, nx)`` bool — True where the corridor covers the
    cell's centre on that layer. ``extra`` ``(layer, x, y)`` points force
    the cell under an endpoint into the mask even if its tile was not on
    the plan path (the source via's tile on the hard route's start
    layer)."""
    spec = graph.spec
    xs = spec.x0 + np.arange(spec.nx) * spec.pitch
    ys = spec.y0 + np.arange(spec.ny) * spec.pitch
    tx_of = np.clip(((xs - graph.ox) / graph.tile_mm).astype(int), 0, graph.ntx - 1)
    ty_of = np.clip(((ys - graph.oy) / graph.tile_mm).astype(int), 0, graph.nty - 1)
    mask = np.zeros((spec.n_layers, spec.ny, spec.nx), dtype=bool)
    for layer, tiles in tiles_by_layer.items():
        if not (0 <= layer < spec.n_layers):
            continue
        table = np.zeros((graph.nty, graph.ntx), dtype=bool)
        for tx, ty in tiles:
            table[ty, tx] = True
        mask[layer] = table[ty_of[:, None], tx_of[None, :]]
    for layer, x, y in extra:
        ix, iy = spec.to_cell(x, y)
        if 0 <= layer < spec.n_layers:
            mask[layer, iy, ix] = True
    return mask


def commit_searched_path(
    grid: maze.OccupancyGrid,
    ir: PcbIR,
    req: realize._SegRequest,
    path: maze.RoutePath,
    config: realize.RealizeConfig,
    spec: maze.GridSpec,
    *,
    ink_field: Any = None,
) -> tuple[list[Any], list[Any], maze.RoutePath]:
    """The post-search half of ``_route_pass``'s per-segment body —
    snap to pads, straighten, stamp, emit via group and tracks — factored
    so a corridor-guided search lands the same copper an unrestricted one
    would. ``(tracks, vias, committed_path)``."""
    net_id, rules, n_vias = req.net_id, req.rules, req.n_vias
    group_extent = req.group_extent
    path = realize._snap_to_pads(path, req.start, req.end, spec.pitch)
    if config.straighten:
        path = realize._straighten(
            path,
            grid,
            net_id,
            rules.track_width_mm,
            shove_vias=config.shove_vias,
            via_group_extent_mm=group_extent,
            via_shove_radius_mm=config.via_shove_radius_mm,
            ink_field=ink_field,
        )
    grid.stamp_path(path, rules.track_width_mm)
    vias: list[Any] = []
    tracks: list[Any] = []
    via_r = (
        grid.core_radius_mm(rules.via_dia_mm) if rules.via_dia_mm is not None else 0.0
    )
    for vx, vy, lo, hi in path.vias:
        if rules.via_dia_mm is None or rules.via_drill_mm is None:
            continue
        pitch = rules.via_dia_mm + grid.clearance_mm
        for k in range(n_vias):
            gx = vx + (k - (n_vias - 1) / 2.0) * pitch
            grid.stamp_disk(range(0, spec.n_layers), gx, vy, via_r, net_id)
            grid.register_via(gx, vy, rules.via_dia_mm / 2.0)
            vias.append(
                realize.RealizedVia(
                    seg_id=req.seg_id,
                    net_id=net_id,
                    x=gx,
                    y=vy,
                    dia_mm=rules.via_dia_mm,
                    drill_mm=rules.via_drill_mm,
                    layer_lo=lo,
                    layer_hi=hi,
                    endpoint="a",
                )
            )
        if n_vias > 1 and rules.via_dia_mm is not None:
            half = (n_vias - 1) / 2.0 * (rules.via_dia_mm + grid.clearance_mm)
            for layer in (lo, hi):
                tracks += realize._track_from_run(
                    req.seg_id,
                    net_id,
                    layer,
                    [(vx - half, vy), (vx + half, vy)],
                    rules.via_dia_mm,
                )
    tracks.extend(
        realize._tracks_from_path(
            req.seg_id,
            net_id,
            path,
            rules.track_width_mm,
            fillet_radius_mm=config.fillet_radius_tracks * rules.track_width_mm,
        )
    )
    return tracks, vias, path


def corridor_route(
    grid: maze.OccupancyGrid,
    ir: PcbIR,
    reqs: dict[int, realize._SegRequest],
    order: Sequence[int],
    corridors: dict[int, np.ndarray],
    corridor_layers: dict[int, list[int]],
    config: realize.RealizeConfig,
    spec: maze.GridSpec,
    *,
    ink_field: Any = None,
    soft_penalty: float | None = None,
) -> tuple[list[Any], list[Any], list[int], dict[int, maze.RoutePath], list[int]]:
    """Route ``order`` on ``grid`` (static copper already claimed), each
    segment confined to ``corridors[seg_id]``'s mask on
    ``corridor_layers[seg_id]`` (the plan's layers, intersected with the
    request's own allowed set). Returns ``(tracks, vias, unrouted,
    paths, exhausted)`` — ``paths`` holds the committed RoutePath per
    routed segment for later verbatim re-stamping; ``exhausted`` is the
    subset of ``unrouted`` whose search hit ``max_expansions`` rather
    than draining the open set (a budget artefact, not proof the corridor
    has no path).

    ``soft_penalty`` (slice 0b of docs/backlog/pcb-global-plan-router.md)
    makes the corridor a COST instead of a wall: no ``mask=``; every
    off-corridor cell carries a static :class:`maze.Negotiation` history
    of ``soft_penalty`` with usage zero, so a step there costs
    ``pitch * (1 + soft_penalty)`` and the search may still leave the
    corridor when the corridor is wrong. No new router code — the
    negotiation seam prices it."""
    tracks: list[Any] = []
    vias: list[Any] = []
    unrouted: list[int] = []
    paths: dict[int, maze.RoutePath] = {}
    exhausted: list[int] = []
    for seg_id in order:
        req = reqs[seg_id]
        allowed = req.route_kwargs["layers"]
        plan_layers = [l for l in corridor_layers[seg_id] if l in set(allowed)]
        for need in (req.start_layer, req.goal_layer):
            if need not in plan_layers:
                plan_layers.append(need)
        kwargs = dict(req.route_kwargs)
        kwargs["layers"] = sorted(plan_layers)
        # The corridor is a per-cell track mask on the search itself
        # (``OccupancyGrid.route(mask=)``): the via disk may overhang it.
        # The earlier CONTESTED-stamping restriction (the 2026-10-13 probe
        # numbers' caveat) forbade in-corridor vias as a side effect.
        if soft_penalty is None:
            kwargs["mask"] = corridors[seg_id]
        else:
            neg = maze.Negotiation(spec, pres_fac=0.0, hist_fac=1.0)
            neg.hist[~corridors[seg_id]] = float(soft_penalty)
            kwargs["negotiation"] = neg
        path = grid.route(req.net_id, req.eff_start, req.eff_goal, **kwargs)
        spent = grid.last_route_exhausted
        if path is None or len(path.points) < 2:
            unrouted.append(seg_id)
            if spent:
                exhausted.append(seg_id)
            continue
        t, v, committed = commit_searched_path(
            grid, ir, req, path, config, spec, ink_field=ink_field
        )
        tracks += t
        vias += v
        paths[seg_id] = committed
    return tracks, vias, unrouted, paths, exhausted


def plan_order(
    graph: TileGraph,
    result: PlanResult,
    order: Sequence[int],
) -> list[int]:
    """Most-constrained-first ordering over the plan's paths: a net's
    score is the smallest residual capacity (cap − use) of any resource
    its path touches; ties keep ``order``'s arrangement. Nets with no
    path sort last, in ``order``."""
    use_x = np.zeros(graph.cap_x.shape, dtype=np.int32)
    use_y = np.zeros(graph.cap_y.shape, dtype=np.int32)
    use_v = np.zeros(graph.via_cap.shape, dtype=np.int32)
    residual: dict[int, float] = {}
    for seg_id, path in result.paths.items():
        exs, eys, vts = _resources(graph, path.nodes)
        for li, ty, tx in exs:
            use_x[li, ty, tx] += 1
        for li, ty, tx in eys:
            use_y[li, ty, tx] += 1
        for vt in vts:
            ty, tx = divmod(vt, graph.ntx)
            use_v[ty, tx] += 1
    for seg_id, path in result.paths.items():
        exs, eys, vts = _resources(graph, path.nodes)
        best = math.inf
        for li, ty, tx in exs:
            best = min(best, int(graph.cap_x[li, ty, tx]) - int(use_x[li, ty, tx]))
        for li, ty, tx in eys:
            best = min(best, int(graph.cap_y[li, ty, tx]) - int(use_y[li, ty, tx]))
        for vt in vts:
            ty, tx = divmod(vt, graph.ntx)
            best = min(best, int(graph.via_cap[ty, tx]) - int(use_v[ty, tx]))
        residual[seg_id] = best
    indexed = {seg_id: i for i, seg_id in enumerate(order)}
    return sorted(
        order,
        key=lambda s: (residual.get(s, math.inf), indexed[s]),
    )


def _resources(graph: TileGraph, nodes: tuple[int, ...]):
    exs: list[tuple[int, int, int]] = []
    eys: list[tuple[int, int, int]] = []
    vts: list[int] = []
    for a, b in itertools.pairwise(nodes):
        la, xa, ya = graph.node_parts(a)
        lb, xb, yb = graph.node_parts(b)
        if la == lb:
            if xb == xa + 1:
                exs.append((la, ya, xa))
            elif xb == xa - 1:
                exs.append((la, ya, xb))
            elif yb == ya + 1:
                eys.append((la, ya, xa))
            elif yb == ya - 1:
                eys.append((la, yb, xa))
        elif xa == xb and ya == yb:
            vts.append(ya * graph.ntx + xa)
    return exs, eys, vts


__all__ = [
    "CORRIDOR_DILATE_TILES",
    "DEFAULT_TILE_MM",
    "PlanNet",
    "PlanResult",
    "PlannedPath",
    "Saturated",
    "TileGraph",
    "build_tile_graph",
    "commit_searched_path",
    "corridor_mask",
    "corridor_route",
    "corridor_tiles",
    "plan_order",
    "plan_routes",
]
