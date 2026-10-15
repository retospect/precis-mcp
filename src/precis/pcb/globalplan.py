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
   pad keep-out mask), packed greedily in scan order. ``strict=True``
   (slice 0b(a)) drops the participant exemption — a net's own copper is
   transparent only on the boundaries it narrows (``exit_cap``) — and
   charges each planned through-via lanes on its tile's boundaries.
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
   ``OccupancyGrid``: each net's track search is confined to its corridor
   tiles (dilated by one tile) through ``OccupancyGrid.route(mask=)`` —
   the via disk may overhang the mask — or, with ``soft_penalty``, priced
   off-corridor through a static :class:`~precis.pcb.maze.Negotiation`
   history instead of walled. Leftover nets then get the ordinary
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
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numba
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


#: Neighbour offsets for :func:`_disjoint_paths`, ordered so direction
#: ``k``'s opposite is ``7 - k`` — the maze's own 8-connected step set
#: (``maze._STEPS``), so a flow path is legal centreline motion exactly
#: where the router could put one.
_FLOW_DIRS: tuple[tuple[int, int], ...] = (
    (-1, -1),
    (-1, 0),
    (-1, 1),
    (0, -1),
    (0, 1),
    (1, -1),
    (1, 0),
    (1, 1),
)


@numba.njit(cache=False, nogil=True)
def _disjoint_paths(
    free: np.ndarray,
    h: int,
    w: int,
    src: np.ndarray,
    snk: np.ndarray,
    limit: int,
) -> int:
    """Vertex-disjoint max-flow on a small cell region (Edmonds-Karp).

    ``free``/``src``/``snk`` are flat ``h * w`` masks over a rectangular
    cell window. Every cell is a unit-capacity node (standard in/out
    split); 8-connected edges between free cells carry the paths. The
    returned count is the number of cell-disjoint paths from any ``src``
    cell to any ``snk`` cell — the connectivity the fine grid actually
    offers, before the caller rescales cells to track lanes. Stops at
    ``limit`` paths (the caller's upper bound; nothing above it can
    change the lane count)."""
    n = h * w
    source = 2 * n
    target = 2 * n + 1
    cap_io = free.astype(np.int8)
    f_io = np.zeros(n, dtype=np.int8)
    f_uv = np.zeros((n, 8), dtype=np.int8)
    f_s = np.zeros(n, dtype=np.int8)
    f_t = np.zeros(n, dtype=np.int8)
    prev = np.empty(2 * n + 2, dtype=np.int64)
    queue = np.empty(2 * n + 2, dtype=np.int64)
    dy = np.empty(8, dtype=np.int64)
    dx = np.empty(8, dtype=np.int64)
    for k in range(8):
        dy[k], dx[k] = _FLOW_DIRS[k]
    total = 0
    while total < limit:
        for i in range(2 * n + 2):
            prev[i] = -1
        head = 0
        tail = 0
        queue[tail] = source
        tail += 1
        prev[source] = source
        while head < tail and prev[target] < 0:
            u = queue[head]
            head += 1
            if u == source:
                for c in range(n):
                    if src[c] and f_s[c] == 0 and prev[2 * c] < 0:
                        prev[2 * c] = u
                        queue[tail] = 2 * c
                        tail += 1
            elif u % 2 == 0:
                c = u // 2
                if cap_io[c] > f_io[c] and prev[u + 1] < 0:
                    prev[u + 1] = u
                    queue[tail] = u + 1
                    tail += 1
                # Residual arcs BACK to an out-node: undo a path that
                # entered c from neighbour p (flow on out(p) -> in(c)).
                cy, cx = divmod(c, w)
                for k in range(8):
                    py, px = cy + dy[k], cx + dx[k]
                    if 0 <= py < h and 0 <= px < w:
                        p = py * w + px
                        v = 2 * p + 1
                        if f_uv[p, 7 - k] == 1 and prev[v] < 0:
                            prev[v] = u
                            queue[tail] = v
                            tail += 1
            else:
                c = (u - 1) // 2
                cy, cx = divmod(c, w)
                for k in range(8):
                    ny_, nx_ = cy + dy[k], cx + dx[k]
                    if 0 <= ny_ < h and 0 <= nx_ < w:
                        nb = ny_ * w + nx_
                        v = 2 * nb
                        if free[nb] and f_uv[c, k] == 0 and prev[v] < 0:
                            prev[v] = u
                            queue[tail] = v
                            tail += 1
                if f_io[c] == 1 and prev[2 * c] < 0:
                    prev[2 * c] = u
                    queue[tail] = 2 * c
                    tail += 1
                if snk[c] and f_t[c] == 0 and prev[target] < 0:
                    prev[target] = u
                    queue[tail] = target
                    tail += 1
        if prev[target] < 0:
            break
        v = target
        while prev[v] != v:
            u = prev[v]
            if u == source:
                f_s[v // 2] = 1
            elif v == target:
                f_t[u // 2] = 1
            elif u % 2 == 0:
                if v // 2 == u // 2:
                    f_io[u // 2] = 1
                else:
                    # in(c) -> out(p): a backward traversal of out(p) ->
                    # in(c); remove that unit of flow.
                    p = v // 2
                    k = _flow_dir_index((u // 2) // w - p // w, (u // 2) % w - p % w)
                    f_uv[p, k] = 0
            else:
                c = (u - 1) // 2
                if v // 2 == c:
                    f_io[c] = 0
                else:
                    nb = v // 2
                    k = _flow_dir_index(nb // w - c // w, nb % w - c % w)
                    f_uv[c, k] = 1
            v = u
        total += 1
    return total


@numba.njit(cache=False, nogil=True, inline="always")
def _flow_dir_index(dy: int, dx: int) -> int:
    """``(dy, dx)`` unit offset -> its slot in :data:`_FLOW_DIRS`."""
    k = (dy + 1) * 3 + (dx + 1)
    return k if k < 4 else k - 1


def _flow_cap(
    free: np.ndarray,
    r0: int,
    r1: int,
    c0: int,
    c1: int,
    split: int,
    axis: str,
    pitch: float,
    lane_mm: float,
) -> int:
    """Max lanes across one tile boundary, from the cells themselves.

    The window is the two-tile strip ``free[r0:r1, c0:c1]``; ``split``
    is the boundary's position inside it (columns or rows of the first
    tile, ``axis`` ``"x"``/``"y"``). The answer is the tighter of two
    fine-grid cuts:

    * the BOUNDARY FACE itself — the run capacity of the free cell
      pairs astride the split, the same measure ``capacity="runs"``
      takes (a crossing track must occupy one of those pairs);
    * the strip's interior connectivity — ``M`` vertex-disjoint paths
      from ANY free cell of the first tile to the second tile's free
      cells on its three OUTER faces (``axis`` ``"x"``: its far column
      and top/bottom rows). Sources are the whole first tile rather
      than its outer faces because a plan edge prices only the
      crossing: how a track got into the first tile is the previous
      edge's question, not this strip's — charging the approach here
      under-reads exactly where the fan is already tight.

    ``M`` cell-paths pack to ``1 + floor((M - 1) * pitch / lane_mm)``
    lane-separated centrelines — the first track needs a legal cell,
    each next one a lane's spacing, not a fresh lane's width. Interior
    walls that spare the face (a via row mid-tile) only show up in the
    second term; boundary slits too narrow for a lane only show up in
    the first."""
    h, w = r1 - r0, c1 - c0
    sub = free[r0:r1, c0:c1]
    yy, xx = np.mgrid[0:h, 0:w]
    if axis == "x":
        face = sub[:, split - 1] & sub[:, split]
        src = sub & (xx < split)
        snk = (xx >= split) & ((xx == w - 1) | (yy == 0) | (yy == h - 1))
    else:
        face = sub[split - 1, :] & sub[split, :]
        src = sub & (yy < split)
        snk = (yy >= split) & ((yy == h - 1) | (xx == 0) | (xx == w - 1))
    snk &= sub
    face_cap = _run_capacity(face, pitch, lane_mm)
    if face_cap == 0:
        return 0
    limit = int(min(src.sum(), snk.sum()))
    if limit == 0:
        return 0
    m = _disjoint_paths(
        np.ascontiguousarray(sub.reshape(-1)),
        h,
        w,
        np.ascontiguousarray(src.reshape(-1)),
        np.ascontiguousarray(snk.reshape(-1)),
        limit,
    )
    if m == 0:
        return 0
    return min(face_cap, 1 + math.floor((m - 1) * pitch / lane_mm + 1e-9))


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
    #: ``strict`` graphs only (slice 0b(a)): ``{(kind, li, ty, tx):
    #: {net_id: cap}}`` — the capacity a participant net sees on a
    #: boundary its OWN copper narrows, where that copper is transparent
    #: to it. Every other net sees ``cap_x``/``cap_y``.
    exit_cap: dict[tuple[str, int, int, int], dict[int, int]] = field(
        default_factory=dict
    )
    #: Track lanes one planned through-via takes from each boundary of its
    #: tile on every planned layer (0 = vias uncharged, the original model).
    via_lanes: int = 0
    #: How ``cap_x``/``cap_y`` were measured — ``"runs"`` (boundary free
    #: span) or ``"flow"`` (vertex-disjoint paths across the two-tile
    #: strip, :func:`build_tile_graph`).
    capacity: str = "runs"

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
    strict: bool = False,
    capacity: str = "runs",
) -> TileGraph:
    """Measure the tile graph's capacities off ``grid``'s stamped claims.

    ``participant_nets`` are the nets being planned (the escape class).
    By default their claims are exempt from the *track* capacity mask so
    a net can leave the tile its pad or via sits in — which also reads
    every OTHER participant's plaza via as free lanes (the probe's main
    overestimate). ``strict`` (slice 0b(a)) blocks lanes on every claim
    and instead records, per participant, the boundaries its own copper
    narrows with that copper transparent (``exit_cap``), and charges each
    planned through-via ``via_lanes`` lanes on its tile's boundaries.
    Via capacity uses every claim either way — a drilled via clears
    copper net-blind — folded with ``grid``'s own pad keep-out mask, so a
    tile under a pad row reports zero rather than a nominal count the
    router could never use.

    ``capacity="runs"`` (the default, slices 0–0b) prices each boundary
    by the free span along the boundary itself — which over-reads
    wherever the free face connects to a region a track cannot actually
    get THROUGH (the 2026-10-13 open/strict bracket: open 55 >= 51 >=
    strict 38, neither reading the fine grid's own connectivity).
    ``capacity="flow"`` (slice 1) instead counts vertex-disjoint paths
    across the two-tile strip on the free-centreline cells
    (:func:`_disjoint_paths`/:func:`_flow_cap`): the boundary's capacity
    is the strip's narrowest interior waist, so a via row or keep-out
    INSIDE a tile subtracts lanes the run model still reports free.
    """
    if capacity not in ("runs", "flow"):
        raise ValueError(f"capacity must be 'runs' or 'flow', got {capacity!r}")
    spec = grid.spec
    owner = grid.owner
    xs = spec.x0 + np.arange(spec.nx) * spec.pitch
    ys = spec.y0 + np.arange(spec.ny) * spec.pitch
    ox, oy = spec.x0, spec.y0
    ntx = math.ceil(spec.nx * spec.pitch / tile_mm)
    nty = math.ceil(spec.ny * spec.pitch / tile_mm)
    layer_list = list(layers)
    participant_list = sorted(participant_nets)
    participants = np.isin(owner, np.asarray(participant_list, np.int32))
    foreign = owner != maze.FREE
    if not strict:
        foreign &= ~participants
    # The track keep-out radius mirrors `_route_in`: half the track width
    # plus one cell of discretisation slack.
    r_track = math.ceil((track_mm / 2.0) / spec.pitch) + 1
    lane_mm = track_mm + clearance_mm
    tx_of = np.clip(((xs - ox) / tile_mm).astype(int), 0, ntx - 1)
    ty_of = np.clip(((ys - oy) / tile_mm).astype(int), 0, nty - 1)
    cap_x = np.zeros((len(layer_list), nty, ntx - 1), dtype=np.int32)
    cap_y = np.zeros((len(layer_list), nty - 1, ntx), dtype=np.int32)
    # Cell columns/rows either side of each tile boundary (-1 = none).
    bx_cells = [
        int(np.searchsorted(xs, ox + (tx + 1) * tile_mm, side="left")) - 1
        for tx in range(ntx - 1)
    ]
    by_cells = [
        int(np.searchsorted(ys, oy + (ty + 1) * tile_mm, side="left")) - 1
        for ty in range(nty - 1)
    ]
    # Each tile's own cell block (the flow model's strip is two of these).
    cols_of = [np.nonzero(tx_of == tx)[0] for tx in range(ntx)]
    rows_of = [np.nonzero(ty_of == ty)[0] for ty in range(nty)]

    def boundary_cap(free: np.ndarray, kind: str, ty: int, tx: int) -> int:
        if capacity == "flow":
            if kind == "x":
                rows = rows_of[ty]
                ca, cb = cols_of[tx], cols_of[tx + 1]
                return _flow_cap(
                    free,
                    int(rows[0]),
                    int(rows[-1]) + 1,
                    int(ca[0]),
                    int(cb[-1]) + 1,
                    int(cb[0]) - int(ca[0]),
                    "x",
                    spec.pitch,
                    lane_mm,
                )
            cols = cols_of[tx]
            ra, rb = rows_of[ty], rows_of[ty + 1]
            return _flow_cap(
                free,
                int(ra[0]),
                int(rb[-1]) + 1,
                int(cols[0]),
                int(cols[-1]) + 1,
                int(rb[0]) - int(ra[0]),
                "y",
                spec.pitch,
                lane_mm,
            )
        if kind == "x":
            i = bx_cells[tx]
            if i < 0 or i + 1 >= spec.nx:
                return 0
            both = free[:, i] & free[:, i + 1]
            return _run_capacity(both[ty_of == ty], spec.pitch, lane_mm)
        i = by_cells[ty]
        if i < 0 or i + 1 >= spec.ny:
            return 0
        both = free[i, :] & free[i + 1, :]
        return _run_capacity(both[tx_of == tx], spec.pitch, lane_mm)

    free_by_layer: list[np.ndarray] = []
    for li, layer in enumerate(layer_list):
        free = ~_dilate_disk(foreign[layer], r_track)
        free_by_layer.append(free)
        for tx in range(ntx - 1):
            for ty in range(nty):
                cap_x[li, ty, tx] = boundary_cap(free, "x", ty, tx)
        for ty in range(nty - 1):
            for tx in range(ntx):
                cap_y[li, ty, tx] = boundary_cap(free, "y", ty, tx)
    exit_cap: dict[tuple[str, int, int, int], dict[int, int]] = {}
    if strict:
        # Per participant: the boundaries of every tile its own (dilated)
        # copper reaches, re-measured with that copper transparent.
        for net_id in participant_list:
            for li, layer in enumerate(layer_list):
                own = owner[layer] == net_id
                if not own.any():
                    continue
                own_free = ~_dilate_disk(foreign[layer] & ~own, r_track)
                reach = _dilate_disk(own, r_track)
                iys, ixs = np.nonzero(reach)
                tiles = set(zip(ty_of[iys].tolist(), tx_of[ixs].tolist(), strict=True))
                edges: set[tuple[str, int, int]] = set()
                for ty, tx in tiles:
                    if tx + 1 < ntx:
                        edges.add(("x", ty, tx))
                    if tx > 0:
                        edges.add(("x", ty, tx - 1))
                    if ty + 1 < nty:
                        edges.add(("y", ty, tx))
                    if ty > 0:
                        edges.add(("y", ty - 1, tx))
                for kind, ty, tx in sorted(edges):
                    base = int((cap_x if kind == "x" else cap_y)[li, ty, tx])
                    mine = boundary_cap(own_free, kind, ty, tx)
                    if mine > base:
                        exit_cap.setdefault((kind, li, ty, tx), {})[net_id] = mine
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
        exit_cap,
        math.floor((via_dia_mm + clearance_mm) / lane_mm) if strict else 0,
        capacity,
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
    #: Additional acceptable end nodes in the FIXED-sink mode (island
    #: terminals, a drilled pad's other layers) — the search ends at
    #: whichever of ``{sink} ∪ alt_sinks`` it reaches cheapest. Unused
    #: by ``free_sinks`` (``sinks`` is the candidate set there).
    alt_sinks: tuple[int, ...] = ()
    #: Graph layer indices this net may plan on (a net-class layer lock);
    #: empty = every planned layer.
    layers: tuple[int, ...] = ()


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
    produced).

    On a ``strict`` graph a net prices a boundary its own copper narrows
    at its ``exit_cap`` entry, and a boundary is over capacity when its
    use exceeds the largest capacity any of its current users sees there
    (the strict capacity when none holds an exit entry). Each planned
    via also takes ``graph.via_lanes`` lanes on every boundary of its tile
    on every planned layer except the ones its own net's track crosses
    (:func:`_via_charge`), clamped at the boundary's strict capacity — a
    via fills its tile's lanes, it never overfills them by itself, so the
    charge conflicts only with ANOTHER net crossing that tile."""
    ntx, nty = graph.ntx, graph.nty
    n_nodes = len(graph.layers) * ntx * nty
    net_of = {net.seg_id: net.net_id for net in nets}
    exit_by_net: dict[int, dict[tuple[str, int, int, int], int]] = {}
    for key, caps in graph.exit_cap.items():
        for net_id, cap in caps.items():
            exit_by_net.setdefault(net_id, {})[key] = cap
    use_x = np.zeros(graph.cap_x.shape, dtype=np.int32)
    use_y = np.zeros(graph.cap_y.shape, dtype=np.int32)
    use_v = np.zeros(graph.via_cap.shape, dtype=np.int32)
    # Via lane charges, kept apart from track use so they can be clamped.
    vc_x = np.zeros(graph.cap_x.shape, dtype=np.int32)
    vc_y = np.zeros(graph.cap_y.shape, dtype=np.int32)
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

    def ux(li: int, ty: int, tx: int) -> int:
        return int(use_x[li, ty, tx]) + min(
            int(vc_x[li, ty, tx]), int(graph.cap_x[li, ty, tx])
        )

    def uy(li: int, ty: int, tx: int) -> int:
        return int(use_y[li, ty, tx]) + min(
            int(vc_y[li, ty, tx]), int(graph.cap_y[li, ty, tx])
        )

    def price(use: int, cap: int, hist: float) -> float:
        over = max(0, use - cap + 1)
        return 1.0 + pres * over + hist_fac * hist

    def dijkstra(
        net: PlanNet, targets: frozenset[int] | None
    ) -> tuple[np.ndarray, np.ndarray]:
        """Min-cost tree from ``net.sources`` over current usage.
        ``(dist, came)``; early-exits once every member of ``targets``
        is closed (``None`` = run to exhaustion). ``net.layers`` (graph
        layer indices, empty = all) gates the via transitions — a net
        locked to a layer subset never plans a hop off it."""
        mine = exit_by_net.get(net.net_id, {})
        allowed = frozenset(net.layers)

        def cx(li: int, ty: int, tx: int) -> int:
            return mine.get(("x", li, ty, tx), int(graph.cap_x[li, ty, tx]))

        def cy(li: int, ty: int, tx: int) -> int:
            return mine.get(("y", li, ty, tx), int(graph.cap_y[li, ty, tx]))

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
            if tx + 1 < ntx and cx(li, ty, tx) > 0:
                c = d + cost_x[li] * price(
                    ux(li, ty, tx),
                    cx(li, ty, tx),
                    float(hx[li, ty, tx]),
                )
                if c < dist[node + 1]:
                    dist[node + 1] = c
                    came[node + 1] = node
                    heapq.heappush(heap, (c, seq, node + 1))
                    seq += 1
            if tx > 0 and cx(li, ty, tx - 1) > 0:
                c = d + cost_x[li] * price(
                    ux(li, ty, tx - 1),
                    cx(li, ty, tx - 1),
                    float(hx[li, ty, tx - 1]),
                )
                if c < dist[node - 1]:
                    dist[node - 1] = c
                    came[node - 1] = node
                    heapq.heappush(heap, (c, seq, node - 1))
                    seq += 1
            if ty + 1 < nty and cy(li, ty, tx) > 0:
                c = d + cost_y[li] * price(
                    uy(li, ty, tx),
                    cy(li, ty, tx),
                    float(hy[li, ty, tx]),
                )
                nb = node + ntx
                if c < dist[nb]:
                    dist[nb] = c
                    came[nb] = node
                    heapq.heappush(heap, (c, seq, nb))
                    seq += 1
            if ty > 0 and cy(li, ty - 1, tx) > 0:
                c = d + cost_y[li] * price(
                    uy(li, ty - 1, tx),
                    cy(li, ty - 1, tx),
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
                    if lj == li or (allowed and lj not in allowed):
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
        cxs, cys = _via_charge(graph, exs, eys, vts)
        for li, ty, tx in cxs:
            vc_x[li, ty, tx] += delta * graph.via_lanes
        for li, ty, tx in cys:
            vc_y[li, ty, tx] += delta * graph.via_lanes
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
                targets = (
                    frozenset({net.sink} | set(net.alt_sinks))
                    if net.sink is not None
                    else frozenset(net.alt_sinks)
                )
                if not targets:
                    paths.pop(net.seg_id, None)
                    continue
                dist, came = dijkstra(net, targets)
                best = min(targets, key=lambda t: dist[t])
                if not math.isfinite(dist[best]):
                    paths.pop(net.seg_id, None)
                    continue
                nodes = path_nodes(came, best)
                paths[net.seg_id] = nodes
                add(nodes, 1)
        eff_x, eff_y = _effective_caps(graph, paths, net_of)
        tot_x = use_x + np.minimum(vc_x, graph.cap_x)
        tot_y = use_y + np.minimum(vc_y, graph.cap_y)
        over_x = np.argwhere(tot_x > eff_x)
        over_y = np.argwhere(tot_y > eff_y)
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
            cxs, cys = _via_charge(graph, exs, eys, vts)
            exs, eys = exs + cxs, eys + cys
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
    eff_x, eff_y = _effective_caps(graph, paths, net_of)
    tot_x = use_x + np.minimum(vc_x, graph.cap_x)
    tot_y = use_y + np.minimum(vc_y, graph.cap_y)
    for li, ty, tx in np.argwhere((tot_x >= eff_x) & (eff_x > 0)):
        saturated.append(
            Saturated(
                "x",
                int(graph.layers[li]),
                int(tx),
                int(ty),
                int(tot_x[li, ty, tx]),
                int(eff_x[li, ty, tx]),
            )
        )
    for li, ty, tx in np.argwhere((tot_y >= eff_y) & (eff_y > 0)):
        saturated.append(
            Saturated(
                "y",
                int(graph.layers[li]),
                int(tx),
                int(ty),
                int(tot_y[li, ty, tx]),
                int(eff_y[li, ty, tx]),
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
    max_expansions: int | None = None,
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
    negotiation seam prices it. A priced search expands more cells than
    a plain one (the heuristic stays the unpriced distance), so
    ``max_expansions`` overrides the request's own budget."""
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
        if max_expansions is not None:
            kwargs["max_expansions"] = max_expansions
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


def _effective_caps(
    graph: TileGraph, paths: dict[int, tuple[int, ...]], net_of: dict[int, int]
) -> tuple[np.ndarray, np.ndarray]:
    """``cap_x``/``cap_y`` raised, on each boundary with ``exit_cap``
    entries, to the largest entry among the nets whose track crosses it
    now. Equal to the stored capacities on a non-strict graph."""
    if not graph.exit_cap:
        return graph.cap_x, graph.cap_y
    eff_x = graph.cap_x.copy()
    eff_y = graph.cap_y.copy()
    users: dict[tuple[str, int, int, int], set[int]] = {}
    for seg_id, nodes in paths.items():
        exs, eys, _vts = _resources(graph, nodes)
        for li, ty, tx in exs:
            users.setdefault(("x", li, ty, tx), set()).add(net_of[seg_id])
        for li, ty, tx in eys:
            users.setdefault(("y", li, ty, tx), set()).add(net_of[seg_id])
    for key, caps in graph.exit_cap.items():
        held = [caps[n] for n in users.get(key, ()) if n in caps]
        if not held:
            continue
        kind, li, ty, tx = key
        eff = eff_x if kind == "x" else eff_y
        eff[li, ty, tx] = max(int(eff[li, ty, tx]), max(held))
    return eff_x, eff_y


def _via_charge(
    graph: TileGraph,
    exs: list[tuple[int, int, int]],
    eys: list[tuple[int, int, int]],
    vts: list[int],
) -> tuple[list[tuple[int, int, int]], list[tuple[int, int, int]]]:
    """The boundaries a path's vias take lanes from: every boundary of each
    via tile on every planned layer, minus the ones the path's own track
    crosses (its own track reaches the via through them). One entry per
    via per boundary; the caller weights each by ``graph.via_lanes``.
    Empty on a graph with ``via_lanes == 0``."""
    if graph.via_lanes <= 0 or not vts:
        return [], []
    own_x, own_y = set(exs), set(eys)
    cxs: list[tuple[int, int, int]] = []
    cys: list[tuple[int, int, int]] = []
    for vt in vts:
        ty, tx = divmod(vt, graph.ntx)
        for li in range(len(graph.layers)):
            for e in ([(li, ty, tx)] if tx + 1 < graph.ntx else []) + (
                [(li, ty, tx - 1)] if tx > 0 else []
            ):
                if e not in own_x:
                    cxs.append(e)
            for e in ([(li, ty, tx)] if ty + 1 < graph.nty else []) + (
                [(li, ty - 1, tx)] if ty > 0 else []
            ):
                if e not in own_y:
                    cys.append(e)
    return cxs, cys


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


def plan_nets(
    ir: PcbIR,
    reqs: dict[int, realize._SegRequest],
    graph: TileGraph,
    pad_geoms: Sequence[Any] = (),
) -> list[PlanNet]:
    """One :class:`PlanNet` per resolved segment — the generic sibling of
    the probe test's escape-class builder. Sources: the start's tile on
    its start layer, every ``extra_start_terminals`` tile on its own
    layer, and — for a THROUGH start (a drilled pad, or a via island
    terminal: copper that lands on every layer) — the start tile on
    every layer the net may use. Sinks mirror that on the goal side:
    the goal tile on its goal layer, plus alternate terminal tiles and
    the through-goal's other layers as :attr:`PlanNet.alt_sinks`. A
    segment locked to layers outside the graph's set is skipped (it
    stays the ordinary passes' problem, never a phantom corridor)."""
    layer_li = {layer: i for i, layer in enumerate(graph.layers)}
    nets: list[PlanNet] = []
    for seg_id, req in reqs.items():
        allowed_li = tuple(
            sorted({layer_li[l] for l in req.route_kwargs["layers"] if l in layer_li})
        )
        if (
            not allowed_li
            or req.start_layer not in layer_li
            or req.goal_layer not in layer_li
        ):
            continue
        a, b = int(ir.seg_pin_a[seg_id]), int(ir.seg_pin_b[seg_id])
        term_a, term_b = req.terms if req.terms else ((), ())
        sources = {
            graph.node(layer_li[req.start_layer], *graph.tile_of(*req.eff_start))
        }
        a_through = (a < len(pad_geoms) and bool(pad_geoms[a].drill_mm)) or any(
            t.kind == "via" for t, _l in term_a
        )
        if a_through:
            tx, ty = graph.tile_of(*req.eff_start)
            sources |= {graph.node(li, tx, ty) for li in allowed_li}
        for t, layer in term_a:
            if layer in layer_li:
                sources.add(graph.node(layer_li[layer], *graph.tile_of(*t.point)))
        sinks = {graph.node(layer_li[req.goal_layer], *graph.tile_of(*req.eff_goal))}
        b_through = (b < len(pad_geoms) and bool(pad_geoms[b].drill_mm)) or any(
            t.kind == "via" for t, _l in term_b
        )
        if b_through:
            tx, ty = graph.tile_of(*req.eff_goal)
            sinks |= {graph.node(li, tx, ty) for li in allowed_li}
        for t, layer in term_b:
            if layer in layer_li:
                sinks.add(graph.node(layer_li[layer], *graph.tile_of(*t.point)))
        sink = graph.node(layer_li[req.goal_layer], *graph.tile_of(*req.eff_goal))
        nets.append(
            PlanNet(
                seg_id=seg_id,
                net_id=req.net_id,
                name=str(ir.net_name[req.net_id]),
                sources=tuple(sorted(sources)),
                sink=sink,
                alt_sinks=tuple(sorted(sinks - {sink})),
                layers=allowed_li,
            )
        )
    return nets


@dataclass(frozen=True, slots=True)
class CorridorPlan:
    """What :func:`corridor_attempt` produced: ``preferred`` proposals
    for ``_route_pass``'s verbatim-commit seam plus the route order
    (most-constrained planned segments first, then everything else in
    the pass's own order). ``plan`` is the negotiated result, for the
    caller's report (``None`` for :func:`plaza_attempt`, whose corridors
    come from the angular assignment, not a negotiated plan)."""

    preferred: dict[int, realize._Proposal]
    order: list[int]
    plan: PlanResult | None


def _route_requests(
    ir: PcbIR, ctx: realize._RouteContext, config: realize.RealizeConfig
) -> dict[int, realize._SegRequest]:
    """Every routable segment's request, resolved the way the first pass
    resolves it — shared by :func:`corridor_attempt` and
    :func:`plaza_attempt`."""
    reqs: dict[int, realize._SegRequest] = {}
    for seg_id in ctx.order:
        req = realize._seg_request(
            ir,
            seg_id,
            config,
            ctx.rules_by_net,
            ctx.clearance,
            ctx.signal_layers,
            ctx.island_terminals,
            ctx.net_layers,
        )
        if req is not None and not req.failed:
            reqs[seg_id] = req
    return reqs


def corridor_attempt(
    ir: PcbIR,
    ctx: realize._RouteContext,
    config: realize.RealizeConfig,
) -> CorridorPlan | None:
    """Corridors-as-cost on a strict flow-capacity tile plan (slice 1,
    ``docs/backlog/pcb-global-plan-router.md``), the opt-in half of
    ``RealizeConfig.corridor_penalty``.

    Builds the static grid the first route pass would see, measures tile
    capacities as fine-grid connectivity (``capacity="flow"``, strict:
    participant copper blocks lanes, planned through-vias charged),
    negotiates one PathFinder plan over every routed segment, then lets
    each planned segment search with its corridor dilated one tile and
    every off-corridor cell priced ``corridor_penalty`` — a cost, not a
    wall, so a wrong corridor still finds its detour. Committed paths
    come back as ``preferred`` proposals for the reorder passes;
    segments the plan left unrouted stay ordinary searches. ``None``
    when there is nothing to plan or the plan produced no paths — the
    caller falls back to plain passes.

    The capacity measurement prices every net at the MODAL
    (track, clearance, via_dia) rules triple — a shared graph cannot
    carry per-net widths; on a mixed-class board the map describes the
    dominant class and other nets' corridors are the approximation.
    """
    reqs = _route_requests(ir, ctx, config)
    if not reqs:
        return None
    grid, _plane_t, _plane_v, _su, _pf = realize._static_route_grid(ir, ctx, config)
    layers = sorted(
        {layer for r in reqs.values() for layer in r.route_kwargs["layers"]}
    )
    if not layers:
        return None
    (track_mm, clearance_mm, via_dia_mm), _count = Counter(
        (r.rules.track_width_mm, r.rules.clearance_mm, r.rules.via_dia_mm)
        for r in reqs.values()
    ).most_common(1)[0]
    tgraph = build_tile_graph(
        grid,
        layers=layers,
        track_mm=track_mm,
        clearance_mm=clearance_mm,
        via_dia_mm=via_dia_mm or 0.45,
        participant_nets={r.net_id for r in reqs.values()},
        strict=True,
        capacity="flow",
    )
    nets = plan_nets(ir, reqs, tgraph, ctx.pad_geoms)
    if not nets:
        return None
    plan = plan_routes(
        tgraph,
        nets,
        layer_prefs=(
            realize._layer_preferences(ir, ctx.signal_layers)
            if config.preferred_directions
            else None
        ),
    )
    if not plan.paths:
        return None
    corridors: dict[int, np.ndarray] = {}
    clayers: dict[int, list[int]] = {}
    for seg_id, path in plan.paths.items():
        req = reqs[seg_id]
        corridors[seg_id] = corridor_mask(
            tgraph,
            corridor_tiles(tgraph, path),
            extra=(
                (req.start_layer, req.eff_start[0], req.eff_start[1]),
                (req.goal_layer, req.eff_goal[0], req.eff_goal[1]),
            ),
        )
        clayers[seg_id] = sorted(
            {tgraph.layers[tgraph.node_parts(n)[0]] for n in path.nodes}
        )
    guided = plan_order(tgraph, plan, [s for s in ctx.order if s in plan.paths])
    route_grid, _pt, _pv, _su2, _pf2 = realize._static_route_grid(ir, ctx, config)
    _t, _v, _unrouted, paths, _exhausted = corridor_route(
        route_grid,
        ir,
        reqs,
        guided,
        corridors,
        clayers,
        config,
        ctx.spec,
        ink_field=ctx.ink_field,
        soft_penalty=config.corridor_penalty,
    )
    preferred = {s: realize._Proposal(p, p) for s, p in paths.items()}
    order = [s for s in guided if s in paths] + [
        s for s in ctx.order if s not in set(paths)
    ]
    return CorridorPlan(preferred, order, plan)


# ── plaza packing (slice 2) ─────────────────────────────────────────────
#
# The tile plan's measured wall is the tile abstraction itself: a dense
# pad row (a QFP ring, the EWOD driver pads) needs its escaping nets
# BRAIDED, which per-boundary capacities cannot express (slice-1
# decisions log: strict flow admits the same 35 as strict runs while the
# fine grid routes 51). Reto's ruling of 2026-10-14 — plaza packing,
# targeted for tough spots: plan the escape the way a human does, ONLY
# where the capacity map says the row cannot carry the demand. Order the
# escaping pads by angle around the package, take each outward on an
# inner layer past the pad row to a via on a packed margin lattice at
# the via envelope, run a short stub back to the pad — the assignment
# is planar (no crossings in the escape region) by construction, and the
# wedge corridors hand it to the maze as a cost, not a wall.

#: Minimum routed pads on a ring before the plaza treatment applies —
#: below it the ordinary search is not losing to the braid.
PLAZA_MIN_ESCAPE = 8
#: A pad row's sorted pad angles must cover the circle — the largest
#: angular gap allowed between neighbours before the row reads as an
#: open edge, not a closed loop (a straight connector row, a two-sided
#: DIP edge: no wrap, no plaza).
_PLAZA_MAX_GAP_DEG = 120.0
#: A group is "tough" (the capacity map says the row cannot carry the
#: demand) when this fraction of its ring-crossing boundaries read
#: zero lanes in the strict flow map.
_PLAZA_TOUGH_FRACTION = 0.5
#: Second staggered via row's offset, as a fraction of the envelope —
#: the hex-packing step.
_VIA_STAGGER = 0.8


@dataclass(frozen=True, slots=True)
class PlazaGroup:
    """One pad row the angular escape is assigned around: ``segs`` in
    pad-angle order, ``centre`` the row's hub, ``pad_out`` each pad's
    own radial half-extent (aligned with ``segs``). ``b_pad`` is the
    subset whose row pin is the segment's pin B — the row can sit at
    either end (the QFP member's packages are the far pin)."""

    instance: int
    segs: tuple[int, ...]
    centre: tuple[float, float]
    pad_out: tuple[float, ...]
    b_pad: frozenset[int] = frozenset()


def _pad_row(
    points: list[tuple[float, float]],
) -> tuple[float, float] | None:
    """``(cx, cy)`` when ``points`` form a CLOSED pad row wrapping the
    centroid — sorted angles cover the circle with no gap over
    ``_PLAZA_MAX_GAP_DEG`` — else ``None``. Shape-agnostic: a square
    QFP ring and a round connector ring both qualify; the radius is a
    per-pad fact, not a group constant."""
    if len(points) < PLAZA_MIN_ESCAPE:
        return None
    cx = sum(p[0] for p in points) / len(points)
    cy = sum(p[1] for p in points) / len(points)
    angles = sorted(math.atan2(p[1] - cy, p[0] - cx) for p in points)
    gaps = [angles[(i + 1) % len(angles)] - angles[i] for i in range(len(angles))]
    gaps[-1] += 2 * math.pi
    if max(gaps) > math.radians(_PLAZA_MAX_GAP_DEG):
        return None
    return cx, cy


def _plaza_groups(
    ir: PcbIR,
    reqs: dict[int, realize._SegRequest],
    pad_geoms: Sequence[Any] = (),
) -> list[PlazaGroup]:
    """Pad rows among the routed segments: group each segment by EITHER
    pin's instance (the row can be the near or the far end — the QFP
    member's packages are pin B), keep instances whose routed pads wrap
    the centroid in at least ``PLAZA_MIN_ESCAPE`` pads. The anchor is
    the pin's own point — ``eff_start``/``eff_goal`` can sit at an
    island terminal off the row."""
    by_inst: dict[int, list[tuple[int, bool, tuple[float, float], float]]] = {}
    for seg_id, req in reqs.items():
        a = int(ir.seg_pin_a[seg_id])
        b = int(ir.seg_pin_b[seg_id])
        for pin, is_b, pt in ((a, False, req.start), (b, True, req.end)):
            inst = int(ir.pin_instance[pin])
            geom = pad_geoms[pin] if pin < len(pad_geoms) else None
            pad_extent = max(geom.w_mm, geom.h_mm) / 2.0 if geom is not None else 0.3
            by_inst.setdefault(inst, []).append((seg_id, is_b, pt, pad_extent))
    groups: list[PlazaGroup] = []
    for inst, members in by_inst.items():
        row = _pad_row([pt for _s, _b, pt, _e in members])
        if row is None:
            continue
        cx, cy = row
        # One seg could land on this instance through both pins — keep
        # its pin-A entry (appended first) so a group never carries it
        # twice.
        by_seg: dict[int, tuple[int, bool, tuple[float, float], float]] = {}
        for m in members:
            if m[0] not in by_seg or not m[1]:
                by_seg[m[0]] = m
        deduped = sorted(
            by_seg.values(), key=lambda m: math.atan2(m[2][1] - cy, m[2][0] - cx)
        )
        groups.append(
            PlazaGroup(
                instance=inst,
                segs=tuple(m[0] for m in deduped),
                centre=(cx, cy),
                pad_out=tuple(m[3] for m in deduped),
                b_pad=frozenset(m[0] for m in deduped if m[1]),
            )
        )
    return groups


def _ring_crossing_cap(
    graph: TileGraph,
    layer: int,
    centre: tuple[float, float],
    point: tuple[float, float],
) -> int:
    """Lanes across the boundary between the pad's tile and the next
    tile OUTWARD along its radial ray — the boundary the escape stub
    must cross, on the pad's layer."""
    li = graph.layers.index(layer)
    tx, ty = graph.tile_of(*point)
    cx, cy = centre
    dx, dy = point[0] - cx, point[1] - cy
    r = math.hypot(dx, dy) or 1.0
    # One full tile outward along the ray.
    nx_, ny_ = point[0] + dx / r * graph.tile_mm, point[1] + dy / r * graph.tile_mm
    tx2, ty2 = graph.tile_of(nx_, ny_)
    if tx2 == tx + 1 and ty2 == ty and tx + 1 < graph.ntx:
        return int(graph.cap_x[li, ty, tx])
    if tx2 == tx - 1 and ty2 == ty and tx > 0:
        return int(graph.cap_x[li, ty, tx - 1])
    if ty2 == ty + 1 and tx2 == tx and ty + 1 < graph.nty:
        return int(graph.cap_y[li, ty, tx])
    if ty2 == ty - 1 and tx2 == tx and ty > 0:
        return int(graph.cap_y[li, ty - 1, tx])
    if tx2 == tx and ty2 == ty:
        # The outward step stayed inside the tile (tile bigger than the
        # pad row's spacing): test the boundary the ray crosses next.
        return 2  # no wall detected on this row — not a tough spot
    return min(
        int(graph.cap_x[li, ty, tx]) if tx2 > tx and tx + 1 < graph.ntx else 99,
        int(graph.cap_x[li, ty, tx - 1]) if tx2 < tx and tx > 0 else 99,
        int(graph.cap_y[li, ty, tx]) if ty2 > ty and ty + 1 < graph.nty else 99,
        int(graph.cap_y[li, ty - 1, tx]) if ty2 < ty and ty > 0 else 99,
    )


def _inner_layer(req: realize._SegRequest, pad_layer: int) -> int | None:
    """The inner layer the escape's radial run drops to: the allowed
    layer second-closest to the far stackup side — a pad on the bottom
    gets ``In2`` (the EWOD plan's own choice), a pad on top gets the
    first inner layer. ``None`` when the net has no second layer."""
    allowed = sorted(set(req.route_kwargs["layers"]))
    if len(allowed) < 2:
        return None
    if pad_layer == allowed[-1]:
        return allowed[-2]
    if pad_layer == allowed[0]:
        return allowed[1]
    for layer in allowed:
        if layer != pad_layer:
            return layer
    return None


def _margin_slots(
    angles: Sequence[float],
    base: Sequence[float],
    envelope: float,
) -> list[float]:
    """The packed margin lattice's slot radii (callers add the centre):
    entry ``i`` sits at ``base[i]`` on the ray at ``angles[i]`` — the
    margin follows the row's outline, not a circle. When any pair of
    angular neighbours (cyclic) lands closer than the via ``envelope``
    the odd entries stagger out one hex row (``_VIA_STAGGER * envelope``)."""
    n = len(angles)
    if n == 0:
        return []
    chords = [
        math.hypot(
            base[i] * math.cos(angles[i])
            - base[(i + 1) % n] * math.cos(angles[(i + 1) % n]),
            base[i] * math.sin(angles[i])
            - base[(i + 1) % n] * math.sin(angles[(i + 1) % n]),
        )
        for i in range(n)
    ]
    slots = list(base)
    if min(chords) < envelope:
        for i in range(1, n, 2):
            slots[i] += _VIA_STAGGER * envelope
    return slots


def _seg_cells(
    spec: maze.GridSpec,
    a: tuple[float, float],
    b: tuple[float, float],
    half_w: float,
    out: np.ndarray,
) -> None:
    """OR the cells within ``half_w`` of segment ``a``→``b`` into
    ``out`` — a flat ``spec.nx * spec.ny`` boolean view of one layer."""
    xs = spec.x0 + np.arange(spec.nx) * spec.pitch
    ys = spec.y0 + np.arange(spec.ny) * spec.pitch
    gx, gy = np.meshgrid(xs, ys)
    ax, ay = a
    vx, vy = b[0] - ax, b[1] - ay
    ll = vx * vx + vy * vy
    if ll == 0:
        dist2 = (gx - ax) ** 2 + (gy - ay) ** 2
    else:
        t = np.clip(((gx - ax) * vx + (gy - ay) * vy) / ll, 0.0, 1.0)
        dist2 = (ax + t * vx - gx) ** 2 + (ay + t * vy - gy) ** 2
    out |= (dist2 <= half_w * half_w).reshape(-1)


def plaza_mask(
    spec: maze.GridSpec,
    *,
    pad_layer: int,
    inner_layer: int | None,
    pad_point: tuple[float, float],
    via_point: tuple[float, float],
    source_point: tuple[float, float],
    half_w: float,
    via_r: float,
    extra: Sequence[tuple[int, float, float]] = (),
) -> np.ndarray:
    """One segment's wedge corridor on the fine grid: a ``half_w``-wide
    lane for the stub (pad → via slot, on the pad's layer) and for the
    inner run (via slot → source, on the inner layer), plus a via-sized
    disk at the slot on every layer — the lattice cell the drop is
    planned into. ``extra`` are ``(layer, x, y)`` cells always inside
    (endpoints, island terminals)."""
    mask = np.zeros((spec.n_layers, spec.ny, spec.nx), dtype=bool)
    _seg_cells(spec, pad_point, via_point, half_w, mask[pad_layer].reshape(-1))
    if inner_layer is not None:
        _seg_cells(spec, via_point, source_point, half_w, mask[inner_layer].reshape(-1))
    xs = spec.x0 + np.arange(spec.nx) * spec.pitch
    ys = spec.y0 + np.arange(spec.ny) * spec.pitch
    gx, gy = np.meshgrid(xs, ys)
    disk = (gx - via_point[0]) ** 2 + (gy - via_point[1]) ** 2 <= via_r * via_r
    mask |= disk[None, :, :]
    for layer, x, y in extra:
        iy = int((y - spec.y0) / spec.pitch)
        ix = int((x - spec.x0) / spec.pitch)
        if 0 <= layer < spec.n_layers and 0 <= iy < spec.ny and 0 <= ix < spec.nx:
            mask[layer, iy, ix] = True
    return mask


def plaza_corridors(
    ir: PcbIR,
    ctx: realize._RouteContext,
    reqs: dict[int, realize._SegRequest],
    graph: TileGraph,
    groups: list[PlazaGroup],
) -> tuple[dict[int, np.ndarray], dict[int, list[int]], list[int]]:
    """The angular escape assignment for the tough groups: via slots on
    a packed margin lattice at the via envelope (staggered into a second
    row when angular neighbours crowd the envelope), each segment's corridor
    the wedge pad → slot → source. Returns ``(corridors, layers, order)``
    — corridors keyed by seg, the layer pair each runs on, and the
    segments in plan order (angular)."""
    spec = ctx.spec
    corridors: dict[int, np.ndarray] = {}
    clayers: dict[int, list[int]] = {}
    order: list[int] = []
    for group in groups:
        cx, cy = group.centre
        envelope = max(
            float(reqs[s].rules.via_dia_mm or 0.45)
            + 2 * float(reqs[s].rules.clearance_mm)
            for s in group.segs
        )
        # Each slot sits just past its own pad's outer extent on the
        # pad's ray; ``_margin_slots`` staggers crowded neighbours.
        angles = [
            math.atan2(
                (reqs[s].end if s in group.b_pad else reqs[s].start)[1] - cy,
                (reqs[s].end if s in group.b_pad else reqs[s].start)[0] - cx,
            )
            for s in group.segs
        ]
        base = [
            dist(reqs[s].end if s in group.b_pad else reqs[s].start, (cx, cy))
            + group.pad_out[i]
            + envelope / 2.0
            + 0.05
            for i, s in enumerate(group.segs)
        ]
        slots = _margin_slots(angles, base, envelope)
        for i, seg_id in enumerate(group.segs):
            req = reqs[seg_id]
            rules = req.rules
            clearance = float(rules.clearance_mm)
            via = (
                cx + slots[i] * math.cos(angles[i]),
                cy + slots[i] * math.sin(angles[i]),
            )
            is_b = seg_id in group.b_pad
            pad = req.end if is_b else req.start
            pad_layer = req.goal_layer if is_b else req.start_layer
            source = req.eff_start if is_b else req.eff_goal
            source_layer = req.start_layer if is_b else req.goal_layer
            inner = _inner_layer(req, pad_layer)
            layer_set = {pad_layer} | ({inner} if inner is not None else set())
            extra: list[tuple[int, float, float]] = [
                (pad_layer, *(req.eff_goal if is_b else req.eff_start)),
                (source_layer, *source),
            ]
            for t, layer in req.terms[0] if req.terms else ():
                extra.append((layer, *t.point))
            for t, layer in req.terms[1] if req.terms else ():
                extra.append((layer, *t.point))
            mask = plaza_mask(
                spec,
                pad_layer=pad_layer,
                inner_layer=inner,
                pad_point=pad,
                via_point=via,
                source_point=source,
                half_w=max(rules.track_width_mm + clearance, 0.35),
                via_r=envelope / 2.0 + spec.pitch,
                extra=extra,
            )
            corridors[seg_id] = mask
            clayers[seg_id] = sorted(layer_set)
            order.append(seg_id)
    return corridors, clayers, order


def plaza_attempt(
    ir: PcbIR,
    ctx: realize._RouteContext,
    config: realize.RealizeConfig,
) -> CorridorPlan | None:
    """Plaza packing, targeted for tough spots (slice 2,
    ``docs/backlog/pcb-global-plan-router.md``; Reto's 2026-10-14 ruling
    that connectivity capacity is the start and plaza packing targets
    the tough spots). Detects pad rows whose ring-crossing boundaries
    the strict flow map reads as lanes-free (``_plaza_groups`` +
    ``_ring_crossing_cap``), assigns each tough row an angular escape —
    stub outward on the pad's layer, via drop on a packed margin
    lattice, inner-layer run to the source — and hands the wedges to the
    maze as off-corridor costs, the same seam as
    :func:`corridor_attempt`. Rows the map does not flag stay ordinary
    searches; ``None`` when no row is tough (the plain passes take over).
    """
    reqs = _route_requests(ir, ctx, config)
    if not reqs:
        return None
    grid, _pt, _pv, _su, _pf = realize._static_route_grid(ir, ctx, config)
    layers = sorted(
        {layer for r in reqs.values() for layer in r.route_kwargs["layers"]}
    )
    if not layers:
        return None
    (track_mm, clearance_mm, via_dia_mm), _n = Counter(
        (r.rules.track_width_mm, r.rules.clearance_mm, r.rules.via_dia_mm)
        for r in reqs.values()
    ).most_common(1)[0]
    tgraph = build_tile_graph(
        grid,
        layers=layers,
        track_mm=track_mm,
        clearance_mm=clearance_mm,
        via_dia_mm=via_dia_mm or 0.45,
        participant_nets={r.net_id for r in reqs.values()},
        strict=True,
        capacity="flow",
    )
    tough = [
        g
        for g in _plaza_groups(ir, reqs, ctx.pad_geoms)
        if sum(
            _ring_crossing_cap(
                tgraph,
                reqs[s].goal_layer if s in g.b_pad else reqs[s].start_layer,
                g.centre,
                reqs[s].end if s in g.b_pad else reqs[s].start,
            )
            == 0
            for s in g.segs
        )
        / len(g.segs)
        >= _PLAZA_TOUGH_FRACTION
    ]
    if not tough:
        return None
    corridors, clayers, order = plaza_corridors(ir, ctx, reqs, tgraph, tough)
    if not corridors:
        return None
    route_grid, _pt2, _pv2, _su2, _pf2 = realize._static_route_grid(ir, ctx, config)
    penalty = config.corridor_penalty if config.corridor_penalty > 0 else 5.0
    _t, _v, _unrouted, paths, _exhausted = corridor_route(
        route_grid,
        ir,
        reqs,
        order,
        corridors,
        clayers,
        config,
        ctx.spec,
        ink_field=ctx.ink_field,
        soft_penalty=penalty,
    )
    preferred = {s: realize._Proposal(p, p) for s, p in paths.items()}
    route_order = [s for s in order if s in paths] + [
        s for s in ctx.order if s not in set(paths)
    ]
    return CorridorPlan(preferred, route_order, None)


__all__ = [
    "CORRIDOR_DILATE_TILES",
    "DEFAULT_TILE_MM",
    "CorridorPlan",
    "PlanNet",
    "PlanResult",
    "PlannedPath",
    "Saturated",
    "TileGraph",
    "build_tile_graph",
    "commit_searched_path",
    "corridor_attempt",
    "corridor_mask",
    "corridor_route",
    "corridor_tiles",
    "plan_nets",
    "plan_order",
    "plan_routes",
    "plaza_attempt",
    "plaza_corridors",
    "plaza_mask",
]
