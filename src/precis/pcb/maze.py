"""Grid maze router — copper that *cannot* violate clearance.

**Why this module exists.** :func:`precis.pcb.realize.realize_segment`
draws each segment as a straight line, optionally hugging ONE component
courtyard, with no knowledge of any other track. That is a drawing
strategy, not a routing strategy: on the ESP32-C3 reference fixture, 61
independently-drawn tracks on 4 layers crossed each other 159 times, and
every crossing is an exact 0.000mm ``clearance`` error that no post-hoc
pass can repair — you cannot un-cross two straight lines without routing
them differently.

**The inversion.** Here copper is *claimed* on a shared occupancy grid
before it is drawn. A cell claimed by another net is not passable, so two
nets' centrelines can never end up closer than the claim radius. Zero
``clearance`` findings is therefore a property of the algorithm rather
than an outcome to be measured, and what varies instead is **how many
nets get routed at all** — reported honestly as
:attr:`precis.pcb.realize.RealizeResult.unrouted` rather than papered over
with overlapping copper. That trade is the whole point: an unrouted net is
a legible to-do, a shorted one is a scrapped board.

**A claim covers this copper and its own clearance; the QUERY pays for
the querying net.** The obvious alternative — claim ``w_self/2 +
clearance + w_max/2`` so any later net is safe by construction — reserves
the widest net on the board around every pad, which at 0.65mm pitch is
about twice the real requirement and seals the escape corridor. Measured:
58 of 61 connections unrouted, with DRC reading a flawless zero. So
:meth:`OccupancyGrid.route` instead dilates the other-net mask by the
routing net's own half-width, plus one cell for discretisation (a path is
sampled at cell centres, and adjacent centres are up to ``pitch*sqrt(2)``
apart). Same guarantee, evaluated with the widths actually involved.

**What this is not.** There is no rip-up-and-retry and no negotiated
congestion (PathFinder, Ebeling & McMurchie 1995) — nets are routed in
one pass, shortest-first, and a net that finds no path is simply
reported. Adding negotiation on top is a strict improvement to
*routability* and changes nothing about the clearance guarantee, which is
enforced by the occupancy grid rather than by the search order.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np

#: Nothing owns this cell.
FREE = -1
#: Two different nets' claims overlap here, so it belongs to neither. Used
#: only while stamping the static pad/keepout layer: a pad's claim disk is
#: allowed to collide with a neighbouring pad's, and where it does, the
#: honest answer is "no net may route through", not "whoever stamped last".
CONTESTED = -2

#: Cost, in millimetres of equivalent trace length, of one layer change.
#: A via is not free — it costs board area, a drill hit, and reliability —
#: so the search should prefer a moderate detour to a layer change. The
#: figure is a routing *preference*, not the ``via_count`` MONEY term in
#: :mod:`precis.pcb.cost` (which prices vias for the placer); they are
#: separate questions and deliberately not wired together.
VIA_COST_MM = 3.0

#: Fallback pad keep-out radius. Real pad extents live in a footprint's
#: land pattern, which :mod:`precis.pcb.landpattern` synthesises offsets
#: for but not sizes; until that exists this is the honest generic pad
#: half-extent, matching the scale of the 0.65mm-pitch parts the fixture
#: uses. Deliberately NOT the 1.0mm courtyard radius: a courtyard is the
#: component body (which copper may pass *under* on another layer), a pad
#: is copper (which it may not).
PAD_RADIUS_MM = 0.2

#: Cap on A* node expansions for a single segment. A blocked net should
#: fail in milliseconds and be reported, never spin: the grid is finite so
#: the search always terminates, but "always" can mean after every cell on
#: four layers, which is not a useful amount of time to spend proving one
#: net is boxed in.
MAX_EXPANSIONS = 120_000

#: Heuristic inflation for weighted A*. Paths may be up to this factor
#: longer than optimal; expansions drop by roughly an order of magnitude
#: on open board. PCB routes are not shortest-path-critical — a 15% longer
#: trace is invisible, a 60-second route is not.
HEURISTIC_WEIGHT = 1.15

_SQRT2 = math.sqrt(2.0)
#: (dx, dy) in-plane steps and their per-step length in grid units.
_STEPS: tuple[tuple[int, int, float], ...] = (
    (1, 0, 1.0),
    (-1, 0, 1.0),
    (0, 1, 1.0),
    (0, -1, 1.0),
    (1, 1, _SQRT2),
    (1, -1, _SQRT2),
    (-1, 1, _SQRT2),
    (-1, -1, _SQRT2),
)

#: What a step costs when it disagrees with its layer's preferred
#: direction. A multiplier on the step, not a veto: a hard constraint would
#: strand every connection whose two pads are simply not aligned that way,
#: and the point of preferred directions is to shape the *bulk* of the
#: routing, not to forbid a turn.
#:
#: **Why have them at all.** Unstructured routing on every layer fragments
#: the remaining free space into islands too small to route through and too
#: awkward to pour — the standard VLSI reason for assigning each layer an
#: axis. Traces that agree on a direction leave corridors between them;
#: traces that wander leave slivers.
OFF_AXIS_PENALTY = 1.6
#: Diagonals are half-penalised on an H or V layer: they are the natural
#: way to make progress toward a pad that is off-axis, and taxing them as
#: hard as a full cross-grain run just produces staircases instead.
DIAGONAL_PENALTY = 1.25

#: Layer preference tokens accepted by :meth:`OccupancyGrid.route`.
PREF_H = "h"
PREF_V = "v"
PREF_DIAG = "d"


def _step_penalty(pref: str | None, dx: int, dy: int) -> float:
    """Cost multiplier for one grid step on a layer with this preference."""
    if pref is None:
        return 1.0
    diagonal = dx != 0 and dy != 0
    if pref == PREF_DIAG:
        return 1.0 if diagonal else OFF_AXIS_PENALTY
    on_axis = (dy == 0) if pref == PREF_H else (dx == 0)
    if on_axis:
        return 1.0
    return DIAGONAL_PENALTY if diagonal else OFF_AXIS_PENALTY


def preferred_directions(layers: list[int]) -> dict[int, str]:
    """Assign each routable layer an axis: H, V, H, V, ... by stackup order.

    Alternating is the whole point — two adjacent layers sharing a
    direction cannot hand off to each other, so a via between them buys
    nothing. Three or more layers get a diagonal in third place, which
    absorbs the connections that neither axis serves without forcing them
    to staircase across an H or V layer.
    """
    cycle = (PREF_H, PREF_V, PREF_DIAG)
    return {layer: cycle[i % len(cycle)] for i, layer in enumerate(sorted(layers))}


@dataclass(frozen=True, slots=True)
class GridSpec:
    """The routing grid's placement in board coordinates. ``(x0, y0)`` is
    the centre of cell ``(0, 0)``; cell ``(ix, iy)`` is centred at
    ``(x0 + ix*pitch, y0 + iy*pitch)``."""

    x0: float
    y0: float
    pitch: float
    nx: int
    ny: int
    n_layers: int

    @property
    def n_cells(self) -> int:
        return self.nx * self.ny * self.n_layers

    def to_cell(self, x: float, y: float) -> tuple[int, int]:
        """Nearest cell to a board point, clamped into the grid."""
        ix = round((x - self.x0) / self.pitch)
        iy = round((y - self.y0) / self.pitch)
        return (min(max(ix, 0), self.nx - 1), min(max(iy, 0), self.ny - 1))

    def to_point(self, ix: int, iy: int) -> tuple[float, float]:
        return (self.x0 + ix * self.pitch, self.y0 + iy * self.pitch)


def grid_for(
    points: list[tuple[float, float]],
    *,
    n_layers: int,
    margin_mm: float = 2.0,
    bounds: tuple[float, float, float, float] | None = None,
    target_cells_per_axis: int = 400,
    min_pitch_mm: float = 0.05,
) -> GridSpec:
    """A grid covering ``points`` plus ``margin_mm``, clipped to
    ``bounds``, with the pitch chosen so neither axis exceeds
    ``target_cells_per_axis``.

    The extent comes from the *pads*, not from the board outline. On this
    project's reference fixture the outline is a deliberately oversized
    300x300mm placeholder while the parts occupy ~50mm — gridding the
    outline would spend 97% of the cells, and all of the time, on empty
    board.

    ``bounds`` is the outline's own usable rectangle, and it is a CLIP,
    not the extent. Without it the pad hull plus margin can reach outside
    the board: a part legally placed 0.5mm from the edge has 1.5mm of
    routable grid hanging off it, and the router will happily use it —
    measured as 2 ``board_edge_clearance`` errors on an otherwise clean
    board. The grid is where copper may go, so it has to end where the
    board does.
    """
    if not points:
        return GridSpec(0.0, 0.0, 1.0, 1, 1, max(1, n_layers))
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x0, x1 = min(xs) - margin_mm, max(xs) + margin_mm
    y0, y1 = min(ys) - margin_mm, max(ys) + margin_mm
    if bounds is not None:
        bx0, by0, bx1, by1 = bounds
        # Clip, but never below the pads themselves — a pad outside the
        # bounds is a placement problem, and dropping it from the grid
        # would turn it into a silent routing failure instead.
        x0, y0 = min(max(x0, bx0), min(xs)), min(max(y0, by0), min(ys))
        x1, y1 = max(min(x1, bx1), max(xs)), max(min(y1, by1), max(ys))
    span = max(x1 - x0, y1 - y0, 1e-6)
    pitch = max(min_pitch_mm, span / target_cells_per_axis)
    nx = math.ceil((x1 - x0) / pitch) + 1
    ny = math.ceil((y1 - y0) / pitch) + 1
    return GridSpec(x0, y0, pitch, nx, ny, max(1, n_layers))


@dataclass(frozen=True, slots=True)
class RoutePath:
    """One routed connection: a polyline per layer plus the via points
    where it changes layer. ``points`` is the full 3-D cell path collapsed
    to ``(x, y, layer)`` board coordinates with collinear runs merged."""

    net_id: int
    points: tuple[tuple[float, float, int], ...]
    length_mm: float
    #: True when the search started on this net's OWN already-routed copper
    #: (attach-to-own-copper) rather than at the ``start`` pad. The caller
    #: cannot infer this from the geometry: on a star decomposition every
    #: connection of a net shares one hub pin, so the trunk runs right past
    #: that pad and "the head is near the pad" is true either way. A caller
    #: that guesses will eventually drag a branch off its trunk and onto a
    #: pad it never came from — severing the net at the exact point it
    #: meant to join it.
    attached: bool = False

    @property
    def vias(self) -> tuple[tuple[float, float, int, int], ...]:
        """``(x, y, layer_lo, layer_hi)`` for each layer change."""
        out = []
        for a, b in zip(self.points, self.points[1:], strict=False):
            if a[2] != b[2]:
                out.append((a[0], a[1], min(a[2], b[2]), max(a[2], b[2])))
        return tuple(out)


def _dilate(mask: np.ndarray, r_cells: int) -> np.ndarray:
    """Chebyshev (8-neighbour) dilation by ``r_cells``, per layer.

    Chebyshev over-dilates diagonally by up to ``sqrt(2)`` versus a
    Euclidean disk. That is the safe direction — it costs a little
    routability and never a clearance violation — and it is four array
    ORs per step instead of a distance transform.
    """
    out = mask
    for _ in range(max(0, r_cells)):
        acc = out.copy()
        acc[:, :-1, :] |= out[:, 1:, :]
        acc[:, 1:, :] |= out[:, :-1, :]
        acc[:, :, :-1] |= out[:, :, 1:]
        acc[:, :, 1:] |= out[:, :, :-1]
        out = acc
    return out


class OccupancyGrid:
    """The shared claim map: which net's copper CORE covers each cell.

    **A cell's claim is the copper plus that copper's own clearance —
    not plus anyone else's.** The obvious alternative (claim
    ``w_self/2 + clearance + w_max/2`` so any later net is safe by
    construction) was tried first and is quietly disastrous at fine
    pitch: it reserves the widest net's half-width around *every* pad,
    which on a 0.65mm-pitch land pattern is roughly twice the real
    requirement and seals the pad's escape corridor entirely — 58 of 61
    connections went unrouted while DRC read a perfect zero, which is
    the exact "clean because it did nothing" failure a clearance
    guarantee makes so easy to ship.

    Instead the *query* pays for the querying net: :meth:`route` dilates
    the other-net core mask by that net's own half-width before
    searching. Same guarantee, evaluated with the width that is actually
    involved rather than the worst one on the board.
    """

    def __init__(self, spec: GridSpec, *, clearance_mm: float):
        self.spec = spec
        self.clearance_mm = clearance_mm
        self._owner = np.full((spec.n_layers, spec.ny, spec.nx), FREE, dtype=np.int32)
        self._flat = self._owner.reshape(-1)
        #: Per net: flat cell index -> the EXACT centreline coordinate that
        #: claimed it. These are the legal attach points for that net's next
        #: connection. Kept separate from ``_owner`` because ``_owner`` also
        #: holds the static pad claims, and a pad is NOT an attach point:
        #: the segment's own destination pad is already owned by its net, so
        #: sourcing from "any cell this net owns" would let every connection
        #: terminate instantly on its own goal and report a fully-routed
        #: board with no copper on it.
        #:
        #: **The value is the whole point.** A cell index alone answers "may
        #: this net start here", which is a clearance question; it does not
        #: answer "where is the copper", which is a connectivity one. A
        #: branch starting from the cell CENTRE begins up to half a cell
        #: diagonal off the trunk it means to join, and a 0.1mm branch can
        #: miss a 0.1mm trunk entirely — the net is severed while DRC reads
        #: clean and nothing is reported unrouted. Storing the coordinate
        #: that actually claimed the cell lets :meth:`route` emit a first
        #: point that lies ON the trunk.
        self._routed_cells: dict[int, dict[int, tuple[float, float]]] = {}

    @property
    def owner(self) -> np.ndarray:
        return self._owner

    def core_radius_mm(self, width_mm: float) -> float:
        """The radius one piece of copper claims for itself: its own
        half-width plus its own clearance. What another net owes on top
        of this is that net's half-width, applied at query time."""
        return width_mm / 2.0 + self.clearance_mm

    def stamp_disk(
        self,
        layers: Iterable[int],
        x: float,
        y: float,
        radius_mm: float,
        net_id: int,
        *,
        contest: bool = False,
    ) -> None:
        """Claim every cell whose centre is within ``radius_mm`` of
        ``(x, y)`` on each of ``layers``. With ``contest=True`` a cell
        already owned by a DIFFERENT net becomes :data:`CONTESTED`
        (passable by nobody) instead of being overwritten — the correct
        resolution for two pads whose keep-outs overlap."""
        spec = self.spec
        r_cells = math.ceil(radius_mm / spec.pitch)
        cx, cy = spec.to_cell(x, y)
        lo_x, hi_x = max(0, cx - r_cells), min(spec.nx - 1, cx + r_cells)
        lo_y, hi_y = max(0, cy - r_cells), min(spec.ny - 1, cy + r_cells)
        if lo_x > hi_x or lo_y > hi_y:
            return
        ix = np.arange(lo_x, hi_x + 1)
        iy = np.arange(lo_y, hi_y + 1)
        dx = spec.x0 + ix * spec.pitch - x
        dy = spec.y0 + iy * spec.pitch - y
        d2 = dy[:, None] ** 2 + dx[None, :] ** 2
        inside = d2 <= radius_mm**2
        if not inside.any():
            # A disk smaller than half a cell diagonal can miss every cell
            # centre. Claiming nothing is the wrong answer for a PAD: its
            # own net then has no owned cell to start a route from and the
            # net is silently unroutable. Claim the single nearest cell —
            # the honest discretisation of "there is copper here".
            iy_hit, ix_hit = np.unravel_index(int(np.argmin(d2)), d2.shape)
            inside = np.zeros_like(inside)
            inside[iy_hit, ix_hit] = True
        for layer in layers:
            window = self._owner[layer, lo_y : hi_y + 1, lo_x : hi_x + 1]
            if contest:
                clash = inside & (window != FREE) & (window != net_id)
                window[inside] = net_id
                window[clash] = CONTESTED
            else:
                window[inside] = net_id

    def disk_is_free(
        self, layers: Iterable[int], x: float, y: float, radius_mm: float, net_id: int
    ) -> bool:
        """Could ``net_id`` claim this disk without touching another net?

        The query :meth:`stamp_disk` does not make. ``stamp_disk``
        overwrites unconditionally, which is right for a pad (the pad IS
        there) and wrong for anything the engine gets to *place* — a drop
        via stamped without asking produced real overlapping copper, and
        the occupancy grid's whole guarantee is that copper is claimed
        before it is drawn. Callers that choose a position must ask first.
        """
        spec = self.spec
        r_cells = math.ceil(radius_mm / spec.pitch)
        cx, cy = spec.to_cell(x, y)
        lo_x, hi_x = max(0, cx - r_cells), min(spec.nx - 1, cx + r_cells)
        lo_y, hi_y = max(0, cy - r_cells), min(spec.ny - 1, cy + r_cells)
        if lo_x > hi_x or lo_y > hi_y:
            return False
        ix = np.arange(lo_x, hi_x + 1)
        iy = np.arange(lo_y, hi_y + 1)
        dx = spec.x0 + ix * spec.pitch - x
        dy = spec.y0 + iy * spec.pitch - y
        inside = (dy[:, None] ** 2 + dx[None, :] ** 2) <= radius_mm**2
        for layer in layers:
            if not (0 <= layer < spec.n_layers):
                return False
            window = self._owner[layer, lo_y : hi_y + 1, lo_x : hi_x + 1]
            if bool(((window != FREE) & (window != net_id) & inside).any()):
                return False
        return True

    def stamp_path(self, path: RoutePath, width_mm: float) -> None:
        """Claim a routed path's corridor. Sampling every point of the
        (already collinear-merged) polyline is not enough — merged runs
        skip intermediate cells — so each span is re-sampled at half-pitch
        steps before stamping."""
        radius = self.core_radius_mm(width_mm)
        step = self.spec.pitch / 2.0
        spec = self.spec
        plane = spec.nx * spec.ny
        attach = self._routed_cells.setdefault(path.net_id, {})
        for a, b in zip(path.points, path.points[1:], strict=False):
            if a[2] != b[2]:  # a via: claim it on every layer it spans
                lo, hi = min(a[2], b[2]), max(a[2], b[2])
                self.stamp_disk(range(lo, hi + 1), a[0], a[1], radius, path.net_id)
                for layer in range(lo, hi + 1):
                    ix, iy = spec.to_cell(a[0], a[1])
                    attach[layer * plane + iy * spec.nx + ix] = (a[0], a[1])
                continue
            seg_len = math.hypot(b[0] - a[0], b[1] - a[1])
            n = max(1, math.ceil(seg_len / step))
            for k in range(n + 1):
                t = k / n
                px = a[0] + (b[0] - a[0]) * t
                py = a[1] + (b[1] - a[1]) * t
                self.stamp_disk(
                    (a[2],),
                    px,
                    py,
                    radius,
                    path.net_id,
                )
                ix, iy = spec.to_cell(px, py)
                attach[a[2] * plane + iy * spec.nx + ix] = (px, py)

    # -- the search ----------------------------------------------------
    def route(
        self,
        net_id: int,
        start: tuple[float, float],
        goal: tuple[float, float],
        *,
        layers: list[int],
        width_mm: float,
        via_dia_mm: float | None = None,
        pad_layer: int | None = None,
        attach: bool = True,
        via_cost_mm: float = VIA_COST_MM,
        max_expansions: int = MAX_EXPANSIONS,
        layer_prefs: dict[int, str] | None = None,
    ) -> RoutePath | None:
        """Weighted-A* from ``start`` to ``goal`` for a trace of
        ``width_mm``, through cells this net's centreline may legally
        occupy. ``None`` when no path exists within ``max_expansions`` —
        an honest "unrouted", never a path drawn through someone else's
        copper.

        The passable set is computed once per call: every other net's
        core, dilated by this net's own half-width (plus one cell of
        discretisation slack). See :class:`OccupancyGrid` for why the
        dilation belongs here and not in the claim."""
        spec = self.spec
        if not layers:
            return None
        sx, sy = spec.to_cell(*start)
        gx, gy = spec.to_cell(*goal)
        plane = spec.nx * spec.ny
        allowed = sorted(layers)
        layer_set = set(allowed)
        # Both endpoints are pads, and a pad lives on exactly one layer —
        # the search enters and leaves there, and buys a via (twice) if it
        # wants an inner layer in between.
        entry = allowed[0] if pad_layer is None else pad_layer
        if entry not in layer_set:
            return None
        start_layer = goal_layer = entry

        foreign = (self._owner != FREE) & (self._owner != net_id)
        r_cells = math.ceil((width_mm / 2.0) / spec.pitch) + 1
        blocked = _dilate(foreign, r_cells).reshape(-1)
        # A via is not a track. It is wider (an annulus, not a trace) and
        # it exists on every layer it spans, so a cell the TRACK may
        # legally occupy is routinely a cell the via may not — the search
        # planned corridors at track width, dropped via-sized copper into
        # them, and put back 21 clearance errors an otherwise sound
        # occupancy grid had just eliminated. Layer changes are therefore
        # gated on their own, wider mask, collapsed across layers because
        # a through via has to clear copper on all of them.
        if via_dia_mm is None:
            via_blocked = None
        else:
            via_r_cells = math.ceil((via_dia_mm / 2.0) / spec.pitch) + 1
            via_blocked = _dilate(foreign, via_r_cells).any(axis=0).reshape(-1)

        def passable(idx: int) -> bool:
            return not blocked[idx]

        def via_ok(ix: int, iy: int) -> bool:
            if via_blocked is None:
                return False  # no via geometry resolved -- never place one
            return not via_blocked[iy * spec.nx + ix]

        start_idx = start_layer * plane + sy * spec.nx + sx
        goal_idx = goal_layer * plane + gy * spec.nx + gx
        if start_idx == goal_idx:
            x, y = spec.to_point(sx, sy)
            return RoutePath(net_id, ((x, y, start_layer),), 0.0)
        if not passable(start_idx) or not passable(goal_idx):
            return None

        pitch = spec.pitch

        def heuristic(ix: int, iy: int, layer: int) -> float:
            dx, dy = abs(ix - gx), abs(iy - gy)
            octile = pitch * (max(dx, dy) + (_SQRT2 - 1.0) * min(dx, dy))
            if layer != goal_layer:
                octile += via_cost_mm
            return octile * HEURISTIC_WEIGHT

        # Multi-source: this net's own already-routed copper is a legal
        # place to start, because connecting to a net means reaching ANY
        # point of it, not one designated pad. Without this the segment
        # decomposition's hub pad has to carry every one of its net's
        # connections through its own escape corridor — 26 GND segments
        # radiating from one pin, of which about two fit.
        g_score: dict[int, float] = {start_idx: 0.0}
        came: dict[int, int] = {}
        closed: set[int] = set()
        heap: list[tuple[float, int]] = [(heuristic(sx, sy, start_layer), start_idx)]
        # cell -> the exact copper coordinate that source represents, so the
        # reconstructed path can BEGIN on the trunk instead of near it.
        anchors: dict[int, tuple[float, float]] = {}
        if attach:
            for src, at in self._routed_cells.get(net_id, {}).items():
                if src == goal_idx or src in g_score or not passable(src):
                    continue
                g_score[src] = 0.0
                anchors[src] = at
                s_layer, s_rem = divmod(src, plane)
                s_iy, s_ix = divmod(s_rem, spec.nx)
                heapq.heappush(heap, (heuristic(s_ix, s_iy, s_layer), src))
        expansions = 0

        while heap:
            _f, cur = heapq.heappop(heap)
            if cur in closed:
                continue
            closed.add(cur)
            if cur == goal_idx:
                return self._reconstruct(came, cur, net_id, g_score[cur], anchors)
            expansions += 1
            if expansions > max_expansions:
                return None
            layer, rem = divmod(cur, plane)
            iy, ix = divmod(rem, spec.nx)
            base = g_score[cur]
            pref = None if layer_prefs is None else layer_prefs.get(layer)
            for dx, dy, weight in _STEPS:
                nxi, nyi = ix + dx, iy + dy
                if not (0 <= nxi < spec.nx and 0 <= nyi < spec.ny):
                    continue
                nidx = layer * plane + nyi * spec.nx + nxi
                if nidx in closed or not passable(nidx):
                    continue
                tentative = base + pitch * weight * _step_penalty(pref, dx, dy)
                if tentative < g_score.get(nidx, math.inf):
                    g_score[nidx] = tentative
                    came[nidx] = cur
                    heapq.heappush(heap, (tentative + heuristic(nxi, nyi, layer), nidx))
            # ANY allowed layer, not just layer +/- 1. A via is a plated
            # hole through the stackup, not a step between neighbours:
            # this board's signal layers are 0 and 3 (1 and 2 are planes),
            # so an adjacency-only transition made every layer change
            # unreachable and the router silently single-layer — 8 nets
            # unrouted with three empty layers underneath them.
            if not via_ok(ix, iy):
                continue
            for other in allowed:
                if other == layer:
                    continue
                nidx = other * plane + iy * spec.nx + ix
                if nidx in closed or not passable(nidx):
                    continue
                tentative = base + via_cost_mm
                if tentative < g_score.get(nidx, math.inf):
                    g_score[nidx] = tentative
                    came[nidx] = cur
                    heapq.heappush(heap, (tentative + heuristic(ix, iy, other), nidx))
        return None

    def _reconstruct(
        self,
        came: dict[int, int],
        goal: int,
        net_id: int,
        length: float,
        anchors: dict[int, tuple[float, float]] | None = None,
    ) -> RoutePath:
        spec = self.spec
        plane = spec.nx * spec.ny
        cells: list[tuple[int, int, int]] = []
        cur = goal
        while True:
            layer, rem = divmod(cur, plane)
            iy, ix = divmod(rem, spec.nx)
            cells.append((ix, iy, layer))
            if cur not in came:
                break
            cur = came[cur]
        source = cur
        cells.reverse()
        points = _merge_collinear(cells, spec)
        # The path started on this net's own copper: begin it at the exact
        # coordinate that copper occupies, not at the centre of the cell the
        # coordinate happened to fall in. Half a cell diagonal is nothing
        # next to a pad and everything next to a 0.1mm trunk. The move stays
        # inside the one cell of slack the query dilation already carries,
        # so it cannot walk the polyline out of its cleared corridor.
        attached = bool(anchors) and source in (anchors or {})
        if attached and points:
            ax, ay = (anchors or {})[source]
            points = ((ax, ay, points[0][2]), *points[1:])
        return RoutePath(net_id, points, length, attached)


def _merge_collinear(
    cells: list[tuple[int, int, int]], spec: GridSpec
) -> tuple[tuple[float, float, int], ...]:
    """Collapse runs of cells that continue in the same direction on the
    same layer. Exact, never a tolerance-based simplification: a
    Douglas-Peucker pass would let the polyline drift off the corridor the
    search just proved clear, which is the one thing this module
    guarantees."""
    if not cells:
        return ()
    kept: list[tuple[int, int, int]] = [cells[0]]
    for i in range(1, len(cells) - 1):
        # Direction is measured against the IMMEDIATE predecessor, not
        # against `kept[-1]`: after a few cells are dropped the latter is
        # a multi-cell delta that can never equal the next unit step, so
        # every run would stop collapsing after one point.
        px, py, pl = cells[i - 1]
        cx, cy, cl = cells[i]
        nx_, ny_, nl = cells[i + 1]
        if pl == cl == nl and (cx - px, cy - py) == (nx_ - cx, ny_ - cy):
            continue  # same direction, same layer -- an interior point
        kept.append(cells[i])
    if len(cells) > 1:
        kept.append(cells[-1])
    return tuple((*spec.to_point(ix, iy), layer) for ix, iy, layer in kept)


__all__ = [
    "CONTESTED",
    "FREE",
    "MAX_EXPANSIONS",
    "PAD_RADIUS_MM",
    "VIA_COST_MM",
    "GridSpec",
    "OccupancyGrid",
    "RoutePath",
    "grid_for",
]
