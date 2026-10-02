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

**Rip-up and retry** is by re-ordering, and it lives in
:func:`precis.pcb.realize._realize_maze` rather than here: the whole pass
is re-run on a fresh grid with the previous attempt's failures moved to
the front. This module stays a single deterministic pass over whatever
order it is given. Nothing about the clearance guarantee depends on that
order — the occupancy grid enforces it, not the search.

**``layers=`` is a hard list, not a preference.** The search may only
enter a layer in it, and that has one non-obvious consequence:
:meth:`OccupancyGrid.stamp_path` registers a via's attach cells on every
layer its barrel passes through (correct — that is where the copper is,
and connectivity depends on it), so :meth:`OccupancyGrid.route` has to
filter those sources back down to ``layers`` before using them. Without
that filter a net owning a through via could start a later connection
inside the barrel on an inner layer and run a trace along it; on the
reference board three traces landed on a PLANE layer, which shorts to the
plane the moment one is poured.

**A via must clear a pad even when the copper would be legal.** Same-net
copper is exempt from the clearance guarantee above by design — that is
how a trace joins a pad — so nothing above stops a via from landing
squarely on a pad it shares a net with. Physically that is not legal
copper, it is a hole drilled through a land you meant to solder to:
:meth:`OccupancyGrid.via_clears_pads` is therefore a SEPARATE, net-blind
question folded into :meth:`OccupancyGrid.route`'s via-candidate mask
(:meth:`OccupancyGrid._pad_keepout_mask`), not an exception carved into
the clearance one — the same relationship :func:`precis.pcb.drc.
check_connectivity` has to :func:`precis.pcb.drc.check_clearance`.
"""

from __future__ import annotations

import heapq
import itertools
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import numba
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

#: Extra cost, ON TOP OF ``VIA_COST_MM``, for a via whose transition cell
#: falls under a placed component's own body (see
#: :class:`OccupancyGrid`'s ``body_mask``/:meth:`OccupancyGrid.
#: set_body_mask`, and :func:`precis.pcb.realize._courtyard_body_mask` for
#: how that mask is built). A via there is not illegal — sometimes it is
#: the only way through — but it is the one a rework has to desolder the
#: part to reach, so reworkability prices it rather than vetoes it: a
#: routing *preference*, not the ``via_count`` MONEY term in
#: :mod:`precis.pcb.cost` (which prices vias for the placer), same
#: framing as ``VIA_COST_MM`` above and deliberately not wired to it.
VIA_UNDER_BODY_COST_MM = 3.0

#: Cap on A* node expansions for a single segment. A blocked net should
#: fail in milliseconds and be reported, never spin: the grid is finite so
#: the search always terminates, but "always" can mean after every cell on
#: four layers, which is not a useful amount of time to spend proving one
#: net is boxed in.
MAX_EXPANSIONS = 120_000

#: :meth:`OccupancyGrid.route`'s first search is confined to the
#: endpoints' bounding box grown by the larger of these: an absolute
#: margin (room to escape a dense part and come back) and a fraction of
#: the span (a long connection's detour scales with its length). A miss
#: inside the window falls back to the whole grid, so these trade speed,
#: never correctness.
WINDOW_MARGIN_MM = 5.0
WINDOW_SPAN_FRACTION = 0.5

#: Memory bound for :func:`grid_for` when a design-rule pitch cap asks
#: for a fine grid on a large board: 4000 x 4000 x 4 layers of int32
#: owner cells is 256 MB.
MAX_CELLS_PER_AXIS = 4000

#: Heuristic inflation for weighted A*. Paths may be up to this factor
#: longer than optimal; expansions drop by roughly an order of magnitude
#: on open board. PCB routes are not shortest-path-critical — a 15% longer
#: trace is invisible, a 60-second route is not.
HEURISTIC_WEIGHT = 1.15

#: :class:`Negotiation`'s price schedule, VPR's defaults (Betz & Rose):
#: the present-congestion factor starts at 0.5 and grows 1.5x per
#: iteration, so early iterations explore and late ones force a split;
#: each iteration a cell spends contested adds one history unit, priced at
#: ``NEGOTIATE_HIST_FAC`` grid steps.
NEGOTIATE_PRES_FAC = 0.5
NEGOTIATE_PRES_GROWTH = 1.5
NEGOTIATE_HIST_FAC = 1.0

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
    max_pitch_mm: float | None = None,
    max_cells_per_axis: int = MAX_CELLS_PER_AXIS,
) -> GridSpec:
    """A grid covering ``points`` plus ``margin_mm``, clipped to
    ``bounds``, with the pitch chosen so neither axis exceeds
    ``target_cells_per_axis`` — unless ``max_pitch_mm`` asks for finer.

    ``max_pitch_mm`` is the coarsest pitch the design rules can be drawn
    on (the caller derives it from track width and clearance). Span / 400
    alone is a 0.5 mm pitch on a 200 mm board: measured on a real 140-part
    4-layer board, fine-pitch pins plus clearance walled in at that pitch
    and 81 of 89 nets came back unrouted. A small board never meets the
    cap (50 mm / 400 = 0.125 mm), so it routes exactly as before.
    ``max_cells_per_axis`` bounds memory when the cap would ask for more.

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
    pitch = span / target_cells_per_axis
    if max_pitch_mm is not None:
        pitch = min(pitch, max_pitch_mm)
    pitch = max(min_pitch_mm, pitch, span / max_cells_per_axis)
    # **Round the node count DOWN, not up.** ``ceil`` puts the last node at
    # ``x0 + ceil(span/pitch)*pitch``, which is >= ``x1`` — up to a full
    # pitch OUTSIDE the very rectangle ``bounds`` was passed in to enforce.
    # Copper then lands legally-by-the-grid but illegally-by-the-board:
    # measured as a via 0.010mm past the usable edge on the 40mm reference
    # fixture (`board_edge_clearance` 0.390 vs a 0.400mm floor), which is
    # not a missing edge check but this rounding handing back the margin
    # `realize._outline_clip` had already subtracted. Flooring makes the
    # last node land ON or INSIDE the clip, so the one place that decides
    # "how far from the board edge may copper be" stays the only place.
    nx = _nodes_within(x0, x1, pitch, cover_to=max(xs))
    ny = _nodes_within(y0, y1, pitch, cover_to=max(ys))
    return GridSpec(x0, y0, pitch, nx, ny, max(1, n_layers))


def _nodes_within(lo: float, hi: float, pitch: float, *, cover_to: float) -> int:
    """Node count whose last node sits at or inside ``hi``.

    ``cover_to`` is the far edge of the PADS, and it wins if flooring would
    leave a pad off the grid: the clip above already guarantees
    ``hi >= cover_to``, so this only fires when a pad sits within one pitch
    of the usable boundary. Dropping that pad from the grid would turn a
    placement problem into a silent routing failure (``grid_for``'s own
    clip comment makes the same choice for the same reason), so the grid
    grows by the one node needed to hold it and the board-edge rule
    reports it honestly instead.
    """
    n = math.floor((hi - lo) / pitch + 1e-9) + 1
    while lo + (n - 1) * pitch < cover_to - 1e-9:
        n += 1
    return max(n, 1)


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


def _point_in_polygon(gx: np.ndarray, gy: np.ndarray, poly: np.ndarray) -> np.ndarray:
    """Vectorised ray-casting point-in-polygon test (even-odd rule).
    ``gx``/``gy`` are same-shape coordinate grids; ``poly`` is an
    ``(n, 2)`` vertex ring — the wrap edge (last vertex back to the
    first) is always included, so callers never repeat the first point.
    ``O(n_vertices)`` python-level iterations, each a vectorised op over
    the whole window — the window is bounded to one pad's own bounding
    box (:meth:`OccupancyGrid.stamp_shape`), so this stays linear in pad
    count even for an EWOD electrode's ~1000-vertex crenellated ring
    (gripe 346962)."""
    inside = np.zeros(gx.shape, dtype=bool)
    n = len(poly)
    x1, y1 = poly[-1]
    for i in range(n):
        x2, y2 = poly[i]
        if y1 != y2:
            cond = (y1 > gy) != (y2 > gy)
            x_at_y = (x2 - x1) * (gy - y1) / (y2 - y1) + x1
            inside ^= cond & (gx < x_at_y)
        x1, y1 = x2, y2
    return inside


def _dist_to_polygon_edges(
    gx: np.ndarray, gy: np.ndarray, poly: np.ndarray
) -> np.ndarray:
    """Per-point Euclidean distance to the nearest EDGE of ``poly`` (same
    vertex convention as :func:`_point_in_polygon`) — the margin test
    :meth:`OccupancyGrid.stamp_shape` folds in for its dilated (CONTESTED
    pre-pass) claim: "inside the polygon OR within ``margin_mm`` of its
    boundary" is the correct buffer, not an approximation of one."""
    best = np.full(gx.shape, np.inf)
    n = len(poly)
    x1, y1 = poly[-1]
    for i in range(n):
        x2, y2 = poly[i]
        ex, ey = x2 - x1, y2 - y1
        seg_len2 = ex * ex + ey * ey
        if seg_len2 < 1e-12:
            d = np.hypot(gx - x1, gy - y1)
        else:
            t = np.clip(((gx - x1) * ex + (gy - y1) * ey) / seg_len2, 0.0, 1.0)
            d = np.hypot(gx - (x1 + t * ex), gy - (y1 + t * ey))
        best = np.minimum(best, d)
        x1, y1 = x2, y2
    return best


@dataclass(frozen=True, slots=True)
class PadShape:
    """A pad's TRUE footprint, in board coordinates, for
    :meth:`OccupancyGrid.stamp_shape`/:meth:`OccupancyGrid.stamp_pad_shape`
    — the shape-aware claim gripe 346962 exists to add. An enclosing
    CIRCLE (``kind="circle"``) was, until this change, the only shape
    :mod:`precis.pcb.maze` could claim at all; at fine pitch (0.8mm QFP
    pins) that circle is wider than the pitch, so a neighbouring pad's
    claim stole a pad's own centre cell and :meth:`OccupancyGrid.route`
    refused before ever searching. Three kinds:

    - ``"circle"``: a disc of radius ``max(w_mm, h_mm) / 2`` — real round
      pads, AND the conservative fallback for anything this router
      cannot represent exactly (see ``kind="rect"``'s own note).
    - ``"rect"``: an axis-aligned ``w_mm`` x ``h_mm`` box centred on
      ``(x, y)`` — a real rect/obround pad whose board-space rotation is
      an exact multiple of 90 degrees (:mod:`precis.pcb.padplace`'s own
      "aperture-less writer" limit for anything else — a caller with an
      oblique rotation must build a ``"circle"`` instead, at
      ``max(w_mm, h_mm) = hypot(true_w, true_h)`` to keep the OLD
      conservative enclosing-circle radius).
    - ``"poly"``: a closed board-space vertex ring — an EWOD electrode's
      crenellated pad, or any other polygon pad, claimed at its true
      outline rather than an enclosing circle.
    """

    kind: str
    x: float
    y: float
    w_mm: float = 0.0
    h_mm: float = 0.0
    poly: tuple[tuple[float, float], ...] = ()

    @property
    def enclosing_radius_mm(self) -> float:
        """The conservative circle this shape still records into
        :attr:`OccupancyGrid.pads` via :meth:`OccupancyGrid.
        stamp_pad_shape` — that consumer (the via keep-out,
        :meth:`OccupancyGrid.via_clears_pads`/``_pad_keepout_mask``)
        stays layer-blind-conservative ON PURPOSE (see
        :meth:`OccupancyGrid.stamp_pad`'s own docstring); a shaped CLAIM
        does not change that, it only makes the claim itself tighter."""
        if self.kind == "poly" and self.poly:
            return max(math.hypot(px - self.x, py - self.y) for px, py in self.poly)
        return math.hypot(self.w_mm, self.h_mm) / 2.0


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
        #: Every claimed PAD's ``(x, y, radius_mm)``, in board coordinates —
        #: populated only by :meth:`stamp_pad`, never by the generic
        #: :meth:`stamp_disk` a routed trace or via itself uses to claim
        #: copper. Kept separate from ``_owner`` for the same reason the
        #: reason ``_owner`` cannot answer "may a via go here": ``_owner``
        #: is a per-NET claim (whose whole job is to be silent about
        #: same-net overlap — see :meth:`via_clears_pads`), and a pad
        #: keep-out is a per-FEATURE question that must NOT go silent for
        #: the pad's own net. A flat list, not a grid mask, because pads
        #: number in the tens to low hundreds on any board this router
        #: sees — cheap to scan exactly, no discretisation to get wrong.
        self._pads: list[tuple[float, float, float]] = []
        #: ``(x, y, copper_radius_mm)`` of every committed via
        #: (:meth:`register_via`): the via search keeps a new via off these
        #: as it keeps off pads, net-blind (two holes, same net or not).
        self._vias: list[tuple[float, float, float]] = []
        #: ``(via_radius_mm, len(_pads)) -> mask`` for
        #: :meth:`_pad_keepout_mask`. ``_pads`` is append-only and
        #: ``clearance_mm`` is fixed at construction, so the pad count is a
        #: complete key. Measured 2026-10-01 on a 140-part board: rebuilding
        #: the mask on every :meth:`route` call was 32 of route's 43 s.
        self._pad_keepout_cache: dict[tuple[float, int], np.ndarray] = {}
        #: Whether the most recent :meth:`route` returned ``None`` because
        #: it hit ``max_expansions`` rather than because the open set ran
        #: dry. Both return ``None``; only the second proves there is no
        #: path. Read by ``realize._diagnose_unrouted``.
        self.last_route_exhausted = False
        #: ``(ny, nx)`` boolean, or ``None`` — cells under a placed
        #: component's own body, set (once, optionally) via
        #: :meth:`set_body_mask`. ``None`` is the every-caller-today
        #: default and means the ``via_body_cost_mm`` surcharge in
        #: :meth:`route` never fires, so an unset mask is a pure no-op.
        self._body_mask: np.ndarray | None = None

    @property
    def owner(self) -> np.ndarray:
        return self._owner

    @property
    def pads(self) -> tuple[tuple[float, float, float], ...]:
        return tuple(self._pads)

    def set_body_mask(self, mask: np.ndarray | None) -> None:
        """Mark cells that lie under a placed component's own body, for
        :meth:`route`'s ``via_body_cost_mm`` surcharge.

        Optional and separate from the constructor because it is
        placement-derived (:func:`precis.pcb.realize._courtyard_body_mask`)
        and every caller predating this feature builds a grid with no
        notion of component bodies at all — ``None`` (never calling this)
        reproduces that exactly, rather than forcing every construction
        site to thread a mask through just to pass ``None``.
        """
        if mask is not None and mask.shape != (self.spec.ny, self.spec.nx):
            raise ValueError(
                f"body_mask shape {mask.shape} != {(self.spec.ny, self.spec.nx)}"
            )
        self._body_mask = None if mask is None else mask.astype(bool)

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

    def stamp_pad(
        self,
        layers: Iterable[int],
        x: float,
        y: float,
        radius_mm: float,
        net_id: int,
        *,
        contest: bool = False,
    ) -> None:
        """:meth:`stamp_disk`, plus remembering ``(x, y, radius_mm)`` as a
        PAD for :meth:`via_clears_pads`/:meth:`route`'s via search.

        A separate entry point rather than a flag on every ``stamp_disk``
        call: most copper this grid ever claims is a trace or a via, and a
        via keep-out has to be asked of the pads specifically, not of
        "everything ever claimed" — a via next to another via, or next to
        a trace's own corridor, is exactly what routing normally produces
        and is not this rule's business. Callers that claim a pad's core
        disk (:mod:`precis.pcb.realize`'s ``_stamp_pads``) call this
        instead of ``stamp_disk`` for that one claim; the CONTESTED
        pre-pass (two overlapping pads' outer keep-out radii) stays on
        plain ``stamp_disk`` — it is not itself a pad's true footprint,
        just a collision marker, and recording it here would double-count
        one pad as two.

        ``_pads`` deliberately does not record ``layers`` — a stated
        simplification, not a silent one, and still a SAFE one now that a
        pad's claim can span one layer (SMD) or every board layer (a
        drilled/THT pad, :mod:`precis.pcb.realize`'s ``_stamp_pads``): a
        layer-blind keep-out is strictly the CONSERVATIVE direction — it
        can only refuse a via candidate a layer-aware check would have
        allowed near an off-layer SMD pad, never permit one a real
        drilled hole's full-layer span would have refused — the same
        "conservative costs a little routability, never a clearance
        violation" trade this module's other pad-radius claims already
        make. Precise enough to fix would mean this recording ``layers``
        too and :meth:`via_clears_pads`/:meth:`_pad_keepout_mask` (below)
        checking a candidate via's own layer span against it — a real
        routability gain near a fine-pitch SMD field, just not a
        correctness one, so it stays future work rather than part of this
        change.
        """
        self.stamp_disk(layers, x, y, radius_mm, net_id, contest=contest)
        self._pads.append((x, y, radius_mm))

    def stamp_shape(
        self,
        layers: Iterable[int],
        shape: PadShape,
        margin_mm: float,
        net_id: int,
        *,
        contest: bool = False,
    ) -> None:
        """The shape-aware sibling of :meth:`stamp_disk` (gripe 346962):
        claim every cell whose centre lies within ``margin_mm`` of
        ``shape``'s TRUE footprint (disc/axis-aligned rect/polygon), not
        just its enclosing circle. Same :data:`CONTESTED`/
        nearest-cell-fallback semantics as :meth:`stamp_disk` — a shape
        too small to cover any cell centre still claims the one cell
        nearest its own centre, and with ``contest=True`` a cell already
        owned by a DIFFERENT net becomes CONTESTED rather than stolen.

        ``layers`` is drained into a list up front — a drilled pad spans
        every board layer, and the ``inside`` mask below is the same for
        each of them, computed ONCE and only the write repeated per
        layer; a bare one-shot ``Iterable`` would silently claim nothing
        past the first layer once consumed."""
        layer_list = list(layers)
        spec = self.spec
        if shape.kind == "poly" and shape.poly:
            xs_v = [p[0] for p in shape.poly]
            ys_v = [p[1] for p in shape.poly]
            bx0, bx1 = min(xs_v) - margin_mm, max(xs_v) + margin_mm
            by0, by1 = min(ys_v) - margin_mm, max(ys_v) + margin_mm
        elif shape.kind == "rect":
            hx = shape.w_mm / 2.0 + margin_mm
            hy = shape.h_mm / 2.0 + margin_mm
            bx0, bx1 = shape.x - hx, shape.x + hx
            by0, by1 = shape.y - hy, shape.y + hy
        else:  # "circle" (also the fallback for an empty/unknown shape)
            r = max(shape.w_mm, shape.h_mm) / 2.0 + margin_mm
            bx0, bx1 = shape.x - r, shape.x + r
            by0, by1 = shape.y - r, shape.y + r
        lo_x = max(0, math.floor((bx0 - spec.x0) / spec.pitch))
        hi_x = min(spec.nx - 1, math.ceil((bx1 - spec.x0) / spec.pitch))
        lo_y = max(0, math.floor((by0 - spec.y0) / spec.pitch))
        hi_y = min(spec.ny - 1, math.ceil((by1 - spec.y0) / spec.pitch))
        if lo_x > hi_x or lo_y > hi_y:
            return
        px = spec.x0 + np.arange(lo_x, hi_x + 1) * spec.pitch
        py = spec.y0 + np.arange(lo_y, hi_y + 1) * spec.pitch
        if shape.kind == "poly" and shape.poly:
            poly = np.asarray(shape.poly, dtype=np.float64)
            gx, gy = np.meshgrid(px, py)
            inside = _point_in_polygon(gx, gy, poly)
            if margin_mm > 0:
                inside |= _dist_to_polygon_edges(gx, gy, poly) <= margin_mm
        elif shape.kind == "rect":
            dx = px[None, :] - shape.x
            dy = py[:, None] - shape.y
            inside = (np.abs(dx) <= shape.w_mm / 2.0 + margin_mm) & (
                np.abs(dy) <= shape.h_mm / 2.0 + margin_mm
            )
        else:
            dx = px[None, :] - shape.x
            dy = py[:, None] - shape.y
            r = max(shape.w_mm, shape.h_mm) / 2.0 + margin_mm
            inside = (dx**2 + dy**2) <= r**2
        if not inside.any():
            # Same "a shape smaller than half a cell diagonal can miss
            # every cell centre" fallback :meth:`stamp_disk` uses — claim
            # the single cell nearest the shape's own centre so its net
            # is never left with nothing to start a route from.
            cx, cy = spec.to_cell(shape.x, shape.y)
            iy_hit, ix_hit = cy - lo_y, cx - lo_x
            if not (0 <= iy_hit < inside.shape[0] and 0 <= ix_hit < inside.shape[1]):
                return  # the shape's own centre cell isn't even in this window
            inside[iy_hit, ix_hit] = True
        for layer in layer_list:
            window = self._owner[layer, lo_y : hi_y + 1, lo_x : hi_x + 1]
            if contest:
                clash = inside & (window != FREE) & (window != net_id)
                window[inside] = net_id
                window[clash] = CONTESTED
            else:
                window[inside] = net_id

    def stamp_pad_shape(
        self,
        layers: Iterable[int],
        shape: PadShape,
        net_id: int,
        *,
        contest: bool = False,
    ) -> None:
        """:meth:`stamp_shape` at zero margin, plus remembering
        ``(x, y, enclosing_radius_mm)`` as a PAD — the shape-aware
        sibling of :meth:`stamp_pad`, for the same reason: a via keep-out
        query (:meth:`via_clears_pads`/``_pad_keepout_mask``) needs this
        pad in :attr:`pads` regardless of whether its CLAIM is a circle,
        a rect or a polygon (see :attr:`PadShape.enclosing_radius_mm`'s
        own docstring for why that record stays the conservative circle
        even here)."""
        self.stamp_shape(layers, shape, 0.0, net_id, contest=contest)
        self._pads.append((shape.x, shape.y, shape.enclosing_radius_mm))

    def claim_centre(
        self, layers: Iterable[int], x: float, y: float, net_id: int
    ) -> None:
        """Force this pad's own NEAREST cell to ``net_id``, unconditionally
        — even over a foreign claim or :data:`CONTESTED`. See
        :func:`precis.pcb.realize._stamp_pads`'s docstring (pass 3) for
        why every pad needs this: a shaped claim a tight neighbour can
        legitimately CONTEST away is still the right outcome for the
        copper in general, but the one cell a route must start or end on
        can never be allowed to lose that race — the exact defect gripe
        346962 opened against (a 0.8mm-pitch neighbour's disc claiming a
        pad's own centre cell before this pass existed)."""
        ix, iy = self.spec.to_cell(x, y)
        for layer in layers:
            self._owner[layer, iy, ix] = net_id

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

    def chord_is_free(
        self,
        layer: int,
        a: tuple[float, float],
        b: tuple[float, float],
        n: int,
        radius_mm: float,
        net_id: int,
    ) -> bool:
        """:meth:`disk_is_free` at the ``n + 1`` evenly spaced points from
        ``a`` to ``b`` on one layer, all of which must be free — the same
        answer, compiled (`_chord_free_kernel`): path straightening asked
        it ~10 M times on a 140-part board, 137 s as Python."""
        spec = self.spec
        return bool(
            _chord_free_kernel(
                self._owner,
                int(layer),
                float(a[0]),
                float(a[1]),
                float(b[0]),
                float(b[1]),
                int(n),
                float(radius_mm),
                int(net_id),
                float(spec.x0),
                float(spec.y0),
                float(spec.pitch),
            )
        )

    def via_clears_pads(self, x: float, y: float, via_radius_mm: float) -> bool:
        """May a via of this (undilated) copper radius be centred at
        ``(x, y)`` without landing on, or crowding, ANY claimed pad —
        including one on the via's own net?

        **A different question from :meth:`disk_is_free`, on purpose —
        the same relationship :func:`precis.pcb.drc.check_connectivity`
        has to :func:`precis.pcb.drc.check_clearance`.** ``disk_is_free``
        (and the clearance guarantee this whole module exists for) is
        deliberately SILENT about same-net copper: a trace legally ends
        ON its own pad, that is how a net joins one. A via is not a
        trace — it is a hole drilled through the board — and landing that
        hole on a pad it is nominally allowed to touch wicks solder down
        the barrel and starves the joint, or on a through-hole pad drills
        a second hole through the first. Tightening ``disk_is_free``'s
        clearance to cover this would make the wrong case illegal too
        (same-net trace-into-pad would break); this is a second, narrower
        question about the same geometry, asked only of pads, and it is
        net-blind on purpose.

        The margin is ``self.clearance_mm`` — not a new number. It is
        already this grid's own fab-derived copper-isolation figure
        (:mod:`precis.pcb.rules`'s ``resolve_net_rules`` resolves it off
        ``capabilities.py``'s ``trace_spacing_mm``, at house_default
        tier), the same figure every OTHER net already has to clear this
        pad by; a via's barrel is, physically, exactly the kind of
        independent copper feature that figure exists to keep apart.
        """
        for px, py, pr in (*self._pads, *self._vias):
            if math.hypot(x - px, y - py) - via_radius_mm - pr < self.clearance_mm:
                return False
        return True

    def register_via(self, x: float, y: float, radius_mm: float) -> None:
        """Record a committed via so later via candidates clear it, net-blind.

        Owner disks keep OTHER nets' vias away. Nothing kept a net's own
        next via off its last one, so two connections of one net could
        drop vias with overlapping drills, which ``drc.check_via_via_keepout``
        refuses whatever the net (measured on Reto's board 2026-10-02: AD2,
        VCC and BTN1 pairs 0.10-0.20 mm apart). Same margin and arithmetic
        as a pad (:meth:`via_clears_pads`). Cached keep-out masks take the
        new disk in place rather than being rebuilt."""
        self._vias.append((x, y, radius_mm))
        for (via_r, _n), mask in self._pad_keepout_cache.items():
            self._or_keepout_disk(mask, x, y, via_r + radius_mm + self.clearance_mm)

    def _pad_keepout_mask(self, via_radius_mm: float) -> np.ndarray:
        """``(ny, nx)`` boolean: True where a via of this radius would fail
        :meth:`via_clears_pads` against SOME claimed pad — the vectorised
        form :meth:`route` folds into its via-candidate mask so the search
        never proposes a layer change there in the first place, rather
        than proposing one and rejecting it after the fact. Same
        circle-membership arithmetic as :meth:`stamp_disk`/
        :meth:`disk_is_free`, unioned over every pad instead of queried
        for one point at a time.

        Cached per via radius; callers must not mutate the returned array
        (``route`` only ORs it into a fresh one)."""
        n = len(self._pads)
        key = (via_radius_mm, n)
        hit = self._pad_keepout_cache.get(key)
        if hit is not None:
            return hit
        # A pad was added since: every cached radius is stale.
        self._pad_keepout_cache = {
            k: v for k, v in self._pad_keepout_cache.items() if k[1] == n
        }
        mask = self._build_pad_keepout_mask(via_radius_mm)
        self._pad_keepout_cache[key] = mask
        return mask

    def _build_pad_keepout_mask(self, via_radius_mm: float) -> np.ndarray:
        mask = np.zeros((self.spec.ny, self.spec.nx), dtype=bool)
        for px, py, pr in (*self._pads, *self._vias):
            self._or_keepout_disk(mask, px, py, via_radius_mm + pr + self.clearance_mm)
        return mask

    def _or_keepout_disk(
        self, mask: np.ndarray, px: float, py: float, radius_mm: float
    ) -> None:
        spec = self.spec
        r_cells = math.ceil(radius_mm / spec.pitch)
        cx, cy = spec.to_cell(px, py)
        lo_x, hi_x = max(0, cx - r_cells), min(spec.nx - 1, cx + r_cells)
        lo_y, hi_y = max(0, cy - r_cells), min(spec.ny - 1, cy + r_cells)
        if lo_x > hi_x or lo_y > hi_y:
            return
        ix = np.arange(lo_x, hi_x + 1)
        iy = np.arange(lo_y, hi_y + 1)
        dx = spec.x0 + ix * spec.pitch - px
        dy = spec.y0 + iy * spec.pitch - py
        inside = (dy[:, None] ** 2 + dx[None, :] ** 2) <= radius_mm**2
        mask[lo_y : hi_y + 1, lo_x : hi_x + 1] |= inside

    def stamp_path(self, path: RoutePath, width_mm: float) -> None:
        """Claim a routed path's corridor. Sampling every point of the
        (already collinear-merged) polyline is not enough — merged runs
        skip intermediate cells — so each span is re-sampled at half-pitch
        steps before stamping (:func:`path_samples`)."""
        radius = self.core_radius_mm(width_mm)
        for px, py, lo, hi in path_samples(path, self.spec.pitch / 2.0):
            self.stamp_disk(range(lo, hi + 1), px, py, radius, path.net_id)
        self.register_attach(path)

    def register_attach(self, path: RoutePath) -> None:
        """Record ``path``'s centreline as attach points for its net's next
        connection, without claiming any copper. :meth:`stamp_path` does
        both; :class:`Negotiation` needs only this half, because a
        negotiated path's copper is counted in its usage map, not owned."""
        spec = self.spec
        plane = spec.nx * spec.ny
        attach = self._routed_cells.setdefault(path.net_id, {})
        for px, py, lo, hi in path_samples(path, spec.pitch / 2.0):
            ix, iy = spec.to_cell(px, py)
            for layer in range(lo, hi + 1):
                attach[layer * plane + iy * spec.nx + ix] = (px, py)

    def forget_attach(self, net_id: int) -> None:
        """Drop every attach point of ``net_id`` — its paths were ripped up."""
        self._routed_cells.pop(net_id, None)

    def is_attach_point(self, net_id: int, x: float, y: float, layer: int) -> bool:
        """Is ``(x, y)`` on ``layer`` exactly a centreline point this net's
        copper already occupies? A negotiated path that began on its own
        net's copper is only valid where that copper still lies."""
        spec = self.spec
        ix, iy = spec.to_cell(x, y)
        at = self._routed_cells.get(net_id, {}).get(
            layer * spec.nx * spec.ny + iy * spec.nx + ix
        )
        return at is not None and abs(at[0] - x) < 1e-9 and abs(at[1] - y) < 1e-9

    def path_is_legal(
        self,
        path: RoutePath,
        *,
        width_mm: float,
        via_dia_mm: float | None,
        start_exempt: bool,
        goal_exempt: bool,
    ) -> bool:
        """Would :meth:`route` have been allowed to walk ``path`` on this
        grid as it stands? The same keep-outs, cell for cell: a track cell
        must have no foreign copper within the routing net's half-width
        plus one cell; a via site none within the via's, on any layer, and
        no pad keep-out. ``start_exempt``/``goal_exempt`` mark an end that
        sits on the net's own pad, where :meth:`_route_in` ignores
        CONTESTED slivers (``endpoint_passable``).

        Used to commit a :class:`Negotiation` result onto a hard grid: a
        path that passes is exactly as legal as one the search found."""
        tl, tiy, tix, viy, vix = path_cells(path, self.spec)
        if tl.shape[0] == 0:
            return False
        r_track = math.ceil((width_mm / 2.0) / self.spec.pitch) + 1
        if via_dia_mm is None:
            if viy.shape[0]:
                return False
            r_via, half_via, pad_keep = 0, _disk_half_chords(0), np.zeros((1, 1), bool)
        else:
            r_via = math.ceil((via_dia_mm / 2.0) / self.spec.pitch) + 1
            half_via = _disk_half_chords(r_via)
            pad_keep = self._pad_keepout_mask(via_dia_mm / 2.0)
        return bool(
            _path_legal_kernel(
                self._owner,
                int(path.net_id),
                tl,
                tiy,
                tix,
                r_track,
                _disk_half_chords(r_track),
                viy,
                vix,
                r_via,
                half_via,
                pad_keep,
                bool(start_exempt),
                bool(goal_exempt),
            )
        )

    # -- the search ----------------------------------------------------
    def search_window(
        self, points: Sequence[tuple[float, float]]
    ) -> tuple[int, int, int, int]:
        """``(y0, y1, x0, x1)`` cell slice a search between ``points``
        starts in: their bounding box plus :data:`WINDOW_MARGIN_MM` or
        :data:`WINDOW_SPAN_FRACTION` of the span, whichever is larger,
        clipped to the grid."""
        spec = self.spec
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        span = max(max(xs) - min(xs), max(ys) - min(ys))
        m = max(WINDOW_MARGIN_MM, WINDOW_SPAN_FRACTION * span)
        x0 = max(0, math.floor((min(xs) - m - spec.x0) / spec.pitch))
        x1 = min(spec.nx, math.ceil((max(xs) + m - spec.x0) / spec.pitch) + 1)
        y0 = max(0, math.floor((min(ys) - m - spec.y0) / spec.pitch))
        y1 = min(spec.ny, math.ceil((max(ys) + m - spec.y0) / spec.pitch) + 1)
        return y0, y1, x0, x1

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
        start_layer: int | None = None,
        goal_layer: int | None = None,
        attach: bool = True,
        via_cost_mm: float = VIA_COST_MM,
        via_body_cost_mm: float = 0.0,
        max_expansions: int = MAX_EXPANSIONS,
        layer_prefs: dict[int, str] | None = None,
        extra_start_terminals: Sequence[tuple[tuple[float, float], int]] = (),
        extra_goal_terminals: Sequence[tuple[tuple[float, float], int]] = (),
        negotiation: Negotiation | None = None,
    ) -> RoutePath | None:
        """Search a window around the endpoints first, then the whole grid.

        Everything per-call in the search — the other-net dilation, the via
        mask, the open set — is proportional to the area searched, and on a
        fine grid over a large board the whole-board version dominated
        routing time (2026-10-01: 1850 of 2068 s at 0.1 mm on 200x75 mm).
        Cells outside the window are simply impassable, so a path found
        inside it is a legal path on the full grid.

        The full-grid retry runs only when the window's search ended with
        the open set EMPTY — a detour may exist outside the box. A search
        that ran out of ``max_expansions`` is not retried: the larger
        search would exhaust too, and a budget failure stays reported as
        one (:attr:`last_route_exhausted`). See :meth:`_route_in` for the
        search itself and every parameter.
        """
        spec = self.spec
        full = (0, spec.ny, 0, spec.nx)
        pts = [start, goal, *(p for p, _ in extra_start_terminals)]
        pts += [p for p, _ in extra_goal_terminals]
        window = self.search_window(pts)
        kwargs: dict[str, Any] = {
            "layers": layers,
            "width_mm": width_mm,
            "via_dia_mm": via_dia_mm,
            "pad_layer": pad_layer,
            "start_layer": start_layer,
            "goal_layer": goal_layer,
            "attach": attach,
            "via_cost_mm": via_cost_mm,
            "via_body_cost_mm": via_body_cost_mm,
            "max_expansions": max_expansions,
            "layer_prefs": layer_prefs,
            "extra_start_terminals": extra_start_terminals,
            "extra_goal_terminals": extra_goal_terminals,
            "negotiation": negotiation,
        }
        path = self._route_in(window, net_id, start, goal, **kwargs)
        if path is not None or window == full or self.last_route_exhausted:
            return path
        return self._route_in(full, net_id, start, goal, **kwargs)

    def _route_in(
        self,
        window: tuple[int, int, int, int],
        net_id: int,
        start: tuple[float, float],
        goal: tuple[float, float],
        *,
        layers: list[int],
        width_mm: float,
        via_dia_mm: float | None = None,
        pad_layer: int | None = None,
        start_layer: int | None = None,
        goal_layer: int | None = None,
        attach: bool = True,
        via_cost_mm: float = VIA_COST_MM,
        via_body_cost_mm: float = 0.0,
        max_expansions: int = MAX_EXPANSIONS,
        layer_prefs: dict[int, str] | None = None,
        extra_start_terminals: Sequence[tuple[tuple[float, float], int]] = (),
        extra_goal_terminals: Sequence[tuple[tuple[float, float], int]] = (),
        negotiation: Negotiation | None = None,
    ) -> RoutePath | None:
        """Weighted-A* from ``start`` to ``goal`` for a trace of
        ``width_mm``, through cells this net's centreline may legally
        occupy. ``None`` when no path exists within ``max_expansions`` —
        an honest "unrouted", never a path drawn through someone else's
        copper.

        The passable set is computed once per call: every other net's
        core, dilated by this net's own half-width (plus one cell of
        discretisation slack). See :class:`OccupancyGrid` for why the
        dilation belongs here and not in the claim.

        ``via_body_cost_mm`` (default ``0.0``, a no-op) surcharges a layer
        change whose transition cell falls in :meth:`set_body_mask`'s
        mask, on top of ``via_cost_mm`` — see
        :data:`VIA_UNDER_BODY_COST_MM`. Deliberately left OUT of
        ``heuristic`` below: folding it in there would make the heuristic
        overestimate a path that turns out to avoid every body cell,
        breaking A*'s admissibility guarantee for a search that is
        supposed to stay optimal-under-weighting, not just fast.

        ``start_layer``/``goal_layer`` let the two ends sit on DIFFERENT
        pad layers — a bottom-mounted pin's pad is not on the same copper
        as a top-mounted one's, so a segment between them has no single
        honest ``pad_layer``. Each defaults independently to ``pad_layer``
        (``allowed[0]`` if that too is ``None``) — the prior single-``entry``
        behaviour, unchanged for every caller that does not pass them.
        Either landing outside ``layers`` refuses the whole call the same
        way an out-of-range ``pad_layer`` always has.

        ``extra_start_terminals``/``extra_goal_terminals`` are additional
        ``((x, y), layer)`` candidates the search may begin/end on, ON TOP
        OF ``start``/``goal`` — never instead of them (precis.pcb.realize's
        island-terminal seam: a pin already bridged to fixed copper offers
        every one of that island's via centres and track endpoints as a
        further source/target, because the plain pad-to-pad corridor a
        dense field of foreign claims can wall off entirely). Each becomes
        its own zero-cost source (mirroring ``attach``'s own-routed-copper
        sourcing below) or an additional member of a MULTI-TARGET search —
        the search still stops at the FIRST one reached, never all of
        them, same as a single goal. A candidate whose layer is not in
        ``layers`` or whose cell is not currently passable for this net is
        silently dropped, the same "nothing to check against" convention
        every other claim in this module already follows for data it
        cannot resolve."""
        spec = self.spec
        self.last_route_exhausted = False
        if not layers:
            return None
        sx, sy = spec.to_cell(*start)
        gx, gy = spec.to_cell(*goal)
        plane = spec.nx * spec.ny
        allowed = sorted(layers)
        layer_set = set(allowed)
        # Both endpoints are pads, and a pad lives on exactly one layer —
        # the search enters and leaves there, and buys a via (twice) if it
        # wants an inner layer in between. `start_layer`/`goal_layer` let
        # the two ends disagree (a segment between a top- and a
        # bottom-mounted pin has no single honest `entry`); each falls
        # back to `pad_layer`'s resolution independently, so the OLD
        # single-`entry` behaviour is exactly what a caller supplying
        # neither still gets.
        entry = allowed[0] if pad_layer is None else pad_layer
        if start_layer is None:
            start_layer = entry
        if goal_layer is None:
            goal_layer = entry
        if start_layer not in layer_set or goal_layer not in layer_set:
            return None

        # The search is confined to `window` (see `route`); outside it every
        # cell is blocked. Keep-outs are checked ON DEMAND, cell by cell, as
        # the search reaches them (`_disk_hits_foreign`, memoised inside
        # `_astar_kernel`): a cell is blocked when another net's copper lies
        # within this net's half-width (plus one cell of discretisation
        # slack) of it. Dilating the whole window up front gave the same
        # answer but cost ~170 s of a 280 s real-board realize — the window
        # is most of the board, and the search touches a small part of it.
        wy0, wy1, wx0, wx1 = window
        r_cells = math.ceil((width_mm / 2.0) / spec.pitch) + 1
        half_track = _disk_half_chords(r_cells)
        owner = self._owner
        # A via is not a track. It is wider (an annulus, not a trace) and
        # it exists on every layer it spans, so a cell the TRACK may
        # legally occupy is routinely a cell the via may not — the search
        # planned corridors at track width, dropped via-sized copper into
        # them, and put back 21 clearance errors an otherwise sound
        # occupancy grid had just eliminated. Layer changes are therefore
        # gated on their own, wider disk, checked on every layer because a
        # through via has to clear copper on all of them.
        if via_dia_mm is None:
            via_r_cells = 0
            half_via = None
            pad_keep = None
        else:
            via_r_cells = math.ceil((via_dia_mm / 2.0) / spec.pitch) + 1
            half_via = _disk_half_chords(via_r_cells)
            # A pad keep-out on top of the other-net disk above, not
            # instead of it: those two answer different questions
            # (another net's copper vs. ANY net's pad — see
            # :meth:`via_clears_pads`'s own docstring for why the second
            # one cannot be folded into clearance). Folded in here, not
            # left to a post-hoc check, so a via candidate that would land
            # on a pad is simply never offered to the search — the same
            # "claim before draw" discipline this module's own module
            # docstring describes, applied to the one shape (pads) that
            # was exempt from it.
            pad_keep = self._pad_keepout_mask(via_dia_mm / 2.0)

        def passable(idx: int) -> bool:
            layer, rem = divmod(idx, plane)
            iy, ix = divmod(rem, spec.nx)
            if not (wy0 <= iy < wy1 and wx0 <= ix < wx1):
                return False
            return not _disk_hits_foreign(
                owner, layer, iy, ix, r_cells, half_track, net_id
            )

        def endpoint_passable(idx: int) -> bool:
            """An endpoint on this net's own pad: CONTESTED is not copper.

            At fine pitch two neighbouring pads' clearance keep-outs overlap
            in a sliver between them (``realize._stamp_pads``), and that
            sliver is stamped CONTESTED. It sits a cell or two from the pad
            centre, inside this search's keep-out disk, so the pad itself
            read as walled in: 11 nets on a 140-part board reported
            ``no_path`` (2026-10-01). The trace starts on its own pad's
            copper, so the sliver is no obstacle THERE; it stays one for
            every other cell, and another net's copper still blocks."""
            layer, rem = divmod(idx, plane)
            iy, ix = divmod(rem, spec.nx)
            if not (wy0 <= iy < wy1 and wx0 <= ix < wx1):
                return False
            return not _disk_hits_net(owner, layer, iy, ix, r_cells, half_track, net_id)

        start_idx = start_layer * plane + sy * spec.nx + sx
        goal_idx = goal_layer * plane + gy * spec.nx + gx
        if start_idx == goal_idx:
            x, y = spec.to_point(sx, sy)
            return RoutePath(net_id, ((x, y, start_layer),), 0.0)
        no_extra_terminals = not extra_start_terminals and not extra_goal_terminals
        start_ok = endpoint_passable(start_idx)
        goal_ok = endpoint_passable(goal_idx)
        if no_extra_terminals and (not start_ok or not goal_ok):
            return None

        pitch = spec.pitch

        # The MULTI-TARGET set: the primary goal cell (if it is even
        # passable — with extra terminals supplied it need not be, exactly
        # the island-terminal scenario this parameter exists for: the plain
        # pad-to-pad corridor is the one thing that's walled off) plus every
        # extra goal terminal whose own layer/cell resolves to something
        # this net could legally land on. `goal_anchor` carries the EXACT
        # (not cell-centre) coordinate for every terminal beyond the
        # primary — :meth:`_reconstruct` snaps the path's final point there,
        # the same discipline :meth:`stamp_path`'s `anchors` already gives
        # the SOURCE side, and for the same reason: a cell-centre landing on
        # an off-grid via centre can be up to half a cell diagonal short of
        # the copper it is meant to touch.
        targets: list[tuple[int, int, int, int]] = []
        goal_anchor: dict[int, tuple[float, float]] = {}
        if goal_ok:
            targets.append((gx, gy, goal_layer, goal_idx))
        for (tx, ty), layer in extra_goal_terminals:
            if layer not in layer_set:
                continue
            tcx, tcy = spec.to_cell(tx, ty)
            tidx = layer * plane + tcy * spec.nx + tcx
            if tidx == start_idx or any(tidx == t[3] for t in targets):
                continue
            if not passable(tidx):
                continue
            targets.append((tcx, tcy, layer, tidx))
            goal_anchor[tidx] = (tx, ty)
        if not targets:
            return None
        target_idx_set = {t[3] for t in targets}

        def heuristic(ix: int, iy: int, layer: int) -> float:
            best = math.inf
            for tgx, tgy, tlayer, _ in targets:
                dx, dy = abs(ix - tgx), abs(iy - tgy)
                octile = pitch * (max(dx, dy) + (_SQRT2 - 1.0) * min(dx, dy))
                if layer != tlayer:
                    octile += via_cost_mm
                if octile < best:
                    best = octile
            return best * HEURISTIC_WEIGHT

        # Multi-source: this net's own already-routed copper is a legal
        # place to start, because connecting to a net means reaching ANY
        # point of it, not one designated pad. Without this, every pad the
        # segment decomposition gives a high degree has to carry all of its
        # tree edges through its own escape corridor — measured at 26 GND
        # segments radiating from one pin, of which about two fit.
        # cell -> f at push time, in push order (the heap's tie-break).
        seeds: dict[int, float] = {}
        # cell -> the exact copper coordinate that source represents, so the
        # reconstructed path can BEGIN on the trunk instead of near it.
        anchors: dict[int, tuple[float, float]] = {}
        if start_ok:
            seeds[start_idx] = heuristic(sx, sy, start_layer)
        if attach:
            for src, at in self._routed_cells.get(net_id, {}).items():
                s_layer, s_rem = divmod(src, plane)
                # **Only on layers this caller allows.** `stamp_path`
                # registers a via's attach cells on every layer the barrel
                # PASSES THROUGH, which is right — that is where the copper
                # is, and connectivity depends on it. It is not a licence to
                # route there. Without this filter a net that already owned
                # a through via could start a later connection inside the
                # barrel on an inner layer and run a trace along it: on the
                # reference board, three traces and three vias landed on
                # In1.Cu, a PLANE layer, which shorts to the plane the
                # moment one is poured. Found by rendering the board, not by
                # reading the code — nothing else was looking at which
                # layers carried copper.
                if s_layer not in layer_set:
                    continue
                if src in target_idx_set or src in seeds or not passable(src):
                    continue
                anchors[src] = at
                s_iy, s_ix = divmod(s_rem, spec.nx)
                seeds[src] = heuristic(s_ix, s_iy, s_layer)
        # The island-terminal sources themselves — same zero-cost seeding
        # as `attach`'s own-routed-copper sources right above, for AUTHORED
        # (not router-drawn) same-net copper instead.
        for (tx, ty), layer in extra_start_terminals:
            if layer not in layer_set:
                continue
            tcx, tcy = spec.to_cell(tx, ty)
            tidx = layer * plane + tcy * spec.nx + tcx
            if tidx in target_idx_set or tidx in seeds or not passable(tidx):
                continue
            anchors[tidx] = (tx, ty)
            seeds[tidx] = heuristic(tcx, tcy, layer)
        if not seeds:
            return None

        # The search itself is `_astar_kernel`, compiled (it was ~450 s of
        # a 1096 s real-board realize as Python). Per-layer step costs are
        # `pitch * weight * _step_penalty(...)`, evaluated here once.
        nx = spec.nx
        n_steps = len(_STEPS)
        step_dx = np.zeros((spec.n_layers, n_steps), dtype=np.int64)
        step_dy = np.zeros((spec.n_layers, n_steps), dtype=np.int64)
        step_off = np.zeros((spec.n_layers, n_steps), dtype=np.int64)
        step_cost = np.zeros((spec.n_layers, n_steps), dtype=np.float64)
        for layer in allowed:
            pref = None if layer_prefs is None else layer_prefs.get(layer)
            for k, (dx, dy, weight) in enumerate(_STEPS):
                step_dx[layer, k] = dx
                step_dy[layer, k] = dy
                step_off[layer, k] = dy * nx + dx
                step_cost[layer, k] = pitch * weight * _step_penalty(pref, dx, dy)
        body = self._body_mask
        no_mask = np.zeros((1, 1), dtype=np.bool_)
        no_soft = np.zeros((1, 1, 1), dtype=np.int16)
        no_hist = np.zeros((1, 1, 1), dtype=np.float32)
        outcome, goal_cell, length, path = _astar_kernel(
            owner,
            int(net_id),
            r_cells,
            half_track,
            via_r_cells,
            half_track if half_via is None else half_via,
            half_via is not None,
            no_mask if pad_keep is None else pad_keep,
            no_mask if body is None else body,
            body is not None,
            nx,
            plane,
            wx0,
            wx1,
            wy0,
            wy1,
            np.asarray(allowed, dtype=np.int64),
            step_dx,
            step_dy,
            step_off,
            step_cost,
            np.asarray([t[0] for t in targets], dtype=np.int64),
            np.asarray([t[1] for t in targets], dtype=np.int64),
            np.asarray([t[2] for t in targets], dtype=np.int64),
            np.asarray([t[3] for t in targets], dtype=np.int64),
            np.asarray(list(seeds), dtype=np.int64),
            np.asarray(list(seeds.values()), dtype=np.float64),
            float(pitch),
            float(via_cost_mm),
            float(via_body_cost_mm),
            float(HEURISTIC_WEIGHT),
            int(max_expansions),
            negotiation is not None,
            no_soft if negotiation is None else negotiation.usage,
            no_hist if negotiation is None else negotiation.hist,
            0.0 if negotiation is None else float(negotiation.pres_fac),
            0.0 if negotiation is None else float(negotiation.hist_fac),
        )
        if outcome == _EXHAUSTED:
            self.last_route_exhausted = True
            return None
        if outcome != _FOUND:
            return None
        came = {int(path[i]): int(path[i + 1]) for i in range(len(path) - 1)}
        return self._reconstruct(
            came,
            int(goal_cell),
            net_id,
            float(length),
            anchors,
            goal_anchor.get(int(goal_cell)),
        )

    def _reconstruct(
        self,
        came: dict[int, int],
        goal: int,
        net_id: int,
        length: float,
        anchors: dict[int, tuple[float, float]] | None = None,
        goal_anchor: tuple[float, float] | None = None,
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
        # Symmetric snap for the GOAL end: ``goal_anchor`` is set only when
        # the search landed on an ``extra_goal_terminals`` candidate (an
        # island's via centre or track endpoint, off-grid like any real
        # copper), never on the primary ``goal`` — that one is a plain pad
        # centre and :mod:`precis.pcb.realize`'s own ``_snap_to_pads`` is
        # what already pulls it exactly onto the pad, same as it always
        # has. A path can carry BOTH an attached source and a snapped goal
        # at once (an island-to-island connection).
        if goal_anchor is not None and points:
            gxp, gyp = goal_anchor
            points = (*points[:-1], (gxp, gyp, points[-1][2]))
        return RoutePath(net_id, points, length, attached)


@numba.njit(cache=False, nogil=True)
def _chord_free_kernel(
    owner: np.ndarray,
    layer: int,
    ax: float,
    ay: float,
    bx: float,
    by: float,
    n: int,
    radius_mm: float,
    net_id: int,
    x0: float,
    y0: float,
    pitch: float,
) -> bool:
    """:meth:`OccupancyGrid.chord_is_free`, compiled. Each sample point is
    :meth:`OccupancyGrid.disk_is_free` term for term: the same nearest-cell
    rounding (half to even, as Python's ``round``), the same clamped box,
    the same ``<= r**2`` disc."""
    n_layers, ny, nx = owner.shape
    if layer < 0 or layer >= n_layers:
        return False
    r_cells = math.ceil(radius_mm / pitch)
    r2 = radius_mm * radius_mm
    for k in range(n + 1):
        x = ax + (bx - ax) * k / n
        y = ay + (by - ay) * k / n
        cx = min(max(int(np.rint((x - x0) / pitch)), 0), nx - 1)
        cy = min(max(int(np.rint((y - y0) / pitch)), 0), ny - 1)
        lo_x, hi_x = max(0, cx - r_cells), min(nx - 1, cx + r_cells)
        lo_y, hi_y = max(0, cy - r_cells), min(ny - 1, cy + r_cells)
        if lo_x > hi_x or lo_y > hi_y:
            return False
        for iy in range(lo_y, hi_y + 1):
            dy = y0 + iy * pitch - y
            for ix in range(lo_x, hi_x + 1):
                dx = x0 + ix * pitch - x
                if dy * dy + dx * dx <= r2:
                    o = owner[layer, iy, ix]
                    if o != FREE and o != net_id:
                        return False
    return True


def _disk_half_chords(r_cells: int) -> np.ndarray:
    """``half[dy + r]`` = how far the disk of radius ``r_cells`` reaches
    along x at row offset ``dy`` (``dx² + dy² <= r²``)."""
    return np.array(
        [
            math.isqrt(r_cells * r_cells - dy * dy)
            for dy in range(-r_cells, r_cells + 1)
        ],
        dtype=np.int64,
    )


@numba.njit(cache=False, nogil=True)
def _disk_hits_foreign(
    owner: np.ndarray,
    layer: int,
    iy: int,
    ix: int,
    r: int,
    half: np.ndarray,
    net_id: int,
) -> bool:
    """Is any cell within ``r`` of ``(ix, iy)`` on ``layer`` (Euclidean,
    ``dx² + dy² <= r²``) owned by a net other than ``net_id``? Contested
    cells count as foreign.

    A Euclidean disk, not the L1 diamond the earlier whole-window dilation
    built while documenting itself as Chebyshev: a diamond reaches only
    ``r / sqrt(2)`` along a diagonal, so a wide track's keep-out came up
    short exactly there (2026-10-01)."""
    n_l, ny, nx = owner.shape
    for dy in range(-r, r + 1):
        y = iy + dy
        if y < 0 or y >= ny:
            continue
        w = half[dy + r]
        x0 = max(0, ix - w)
        x1 = min(nx - 1, ix + w)
        for x in range(x0, x1 + 1):
            o = owner[layer, y, x]
            if o != FREE and o != net_id:
                return True
    return False


@numba.njit(cache=False, nogil=True)
def _disk_hits_net(
    owner: np.ndarray,
    layer: int,
    iy: int,
    ix: int,
    r: int,
    half: np.ndarray,
    net_id: int,
) -> bool:
    """:func:`_disk_hits_foreign` with CONTESTED cells ignored: is another
    NET's claim within ``r``? For a route's endpoint on its own pad only
    (``_route_in``'s ``endpoint_passable``)."""
    n_l, ny, nx = owner.shape
    for dy in range(-r, r + 1):
        y = iy + dy
        if y < 0 or y >= ny:
            continue
        w = half[dy + r]
        x0 = max(0, ix - w)
        x1 = min(nx - 1, ix + w)
        for x in range(x0, x1 + 1):
            o = owner[layer, y, x]
            if o != FREE and o != CONTESTED and o != net_id:
                return True
    return False


#: :func:`_astar_kernel` outcomes.
_FOUND, _EMPTY, _EXHAUSTED = 0, 1, 2


@numba.njit(cache=False, nogil=True)
def _astar_kernel(
    owner: np.ndarray,
    net_id: int,
    r_track: int,
    half_track: np.ndarray,
    r_via: int,
    half_via: np.ndarray,
    has_via: bool,
    pad_keep: np.ndarray,
    body: np.ndarray,
    has_body: bool,
    nx: int,
    plane: int,
    wx0: int,
    wx1: int,
    wy0: int,
    wy1: int,
    allowed: np.ndarray,
    step_dx: np.ndarray,
    step_dy: np.ndarray,
    step_off: np.ndarray,
    step_cost: np.ndarray,
    tx: np.ndarray,
    ty: np.ndarray,
    tl: np.ndarray,
    tidx: np.ndarray,
    seed_idx: np.ndarray,
    seed_f: np.ndarray,
    pitch: float,
    via_cost_mm: float,
    via_body_cost_mm: float,
    weight_h: float,
    max_expansions: int,
    negotiate: bool,
    usage: np.ndarray,
    hist: np.ndarray,
    pres_fac: float,
    hist_fac: float,
) -> tuple[int, int, float, np.ndarray]:
    """The A* loop of :meth:`OccupancyGrid._route_in`, compiled.

    Same search as the pure-Python loop it replaced: the same step order,
    the same costs (precomputed by the caller with the same expressions),
    and a heap ordered on ``(f, cell)`` tuples, so ties pop in the same
    order. Cells are full-grid flat indices (``layer * plane + iy * nx +
    ix``); the bookkeeping arrays (``g``, ``came``, ``state``) cover only
    the window.

    Keep-outs are decided when the search first reaches a cell and
    memoised in ``state``: a track cell is blocked when
    :func:`_disk_hits_foreign` finds another net's copper within
    ``r_track`` on its layer; a via site when it does so within ``r_via``
    on ANY layer, or ``pad_keep`` marks it.

    With ``negotiate`` (:class:`Negotiation`), entering a cell also pays
    for other nets' claims in the same keep-out disk: the step cost plus
    ``hist_fac * pitch * hist`` at the cell, times ``1 + pres_fac * k``
    where ``k`` is the most nets claiming one cell of the disk. A via pays
    ``1 + pres_fac * k`` over its own disk on every layer. Costs only grow,
    so the heuristic stays admissible.

    Returns ``(outcome, goal, length, path)`` — ``path`` lists the cells
    from the goal back to the source it was reached from."""
    ww = wx1 - wx0
    wplane = (wy1 - wy0) * ww
    n_layers = owner.shape[0]
    n_local = n_layers * wplane
    g = np.empty(n_local, dtype=np.float64)
    came = np.empty(n_local, dtype=np.int64)
    # Bits 0-1: 0 = unseen, 1 = open (g valid), 2 = closed. Bit 2: target.
    # Bit 3: keep-out checked; bit 4: blocked.
    state = np.zeros(n_local, dtype=np.uint8)
    # Per window plane cell: 0 = unchecked, 1 = a via may go here, 2 = not.
    via_state = np.zeros(wplane, dtype=np.uint8)
    # Negotiated surcharges, filled when bit 3 is set: the cell's present
    # multiplier and its additive history term; per plane cell, a via's.
    n_soft = n_local if negotiate else 1
    soft_m = np.ones(n_soft, dtype=np.float64)
    soft_h = np.zeros(n_soft, dtype=np.float64)
    via_m = np.ones(wplane if negotiate else 1, dtype=np.float64)
    empty_path = np.empty(0, dtype=np.int64)
    n_targets = tidx.shape[0]
    sqrt2m1 = math.sqrt(2.0) - 1.0

    for k in range(n_targets):
        t = tidx[k]
        layer = t // plane
        rem = t - layer * plane
        iy = rem // nx
        ix = rem - iy * nx
        # Bit 3 too: the caller already decided each target is passable
        # (`_route_in`'s `endpoint_passable` for the primary goal), and
        # re-checking it here with the plain keep-out would undo that.
        tli = layer * wplane + (iy - wy0) * ww + (ix - wx0)
        state[tli] |= 4 | 8
        if negotiate:
            ku = _disk_max_usage(usage, layer, iy, ix, r_track, half_track)
            soft_m[tli] = 1.0 + pres_fac * ku
            soft_h[tli] = hist_fac * pitch * hist[layer, iy, ix]

    heap = [(seed_f[0], seed_idx[0])]
    for k in range(1, seed_idx.shape[0]):
        heapq.heappush(heap, (seed_f[k], seed_idx[k]))
    for k in range(seed_idx.shape[0]):
        s = seed_idx[k]
        layer = s // plane
        rem = s - layer * plane
        iy = rem // nx
        ix = rem - iy * nx
        li = layer * wplane + (iy - wy0) * ww + (ix - wx0)
        g[li] = 0.0
        came[li] = -1
        state[li] = (state[li] & 0xFC) | 1

    expansions = 0
    while len(heap) > 0:
        item = heapq.heappop(heap)
        cur = item[1]
        layer = cur // plane
        rem = cur - layer * plane
        iy = rem // nx
        ix = rem - iy * nx
        lbase = layer * wplane
        li = lbase + (iy - wy0) * ww + (ix - wx0)
        if (state[li] & 3) == 2:
            continue
        state[li] = (state[li] & 0xFC) | 2
        if state[li] & 4:
            n = 1
            j = li
            while came[j] >= 0:
                n += 1
                c = came[j]
                cl = c // plane
                cr = c - cl * plane
                cy = cr // nx
                j = cl * wplane + (cy - wy0) * ww + (cr - cy * nx - wx0)
            path = np.empty(n, dtype=np.int64)
            path[0] = cur
            j = li
            k = 1
            while came[j] >= 0:
                c = came[j]
                path[k] = c
                k += 1
                cl = c // plane
                cr = c - cl * plane
                cy = cr // nx
                j = cl * wplane + (cy - wy0) * ww + (cr - cy * nx - wx0)
            return _FOUND, cur, g[li], path
        expansions += 1
        if expansions > max_expansions:
            return _EXHAUSTED, -1, 0.0, empty_path
        base = g[li]
        for k in range(step_off.shape[1]):
            nxi = ix + step_dx[layer, k]
            nyi = iy + step_dy[layer, k]
            if nxi < wx0 or nxi >= wx1 or nyi < wy0 or nyi >= wy1:
                continue
            nidx = cur + step_off[layer, k]
            nli = lbase + (nyi - wy0) * ww + (nxi - wx0)
            sv = state[nli]
            if not sv & 8:
                sv |= 8
                if _disk_hits_foreign(
                    owner, layer, nyi, nxi, r_track, half_track, net_id
                ):
                    sv |= 16
                elif negotiate:
                    ku = _disk_max_usage(usage, layer, nyi, nxi, r_track, half_track)
                    soft_m[nli] = 1.0 + pres_fac * ku
                    soft_h[nli] = hist_fac * pitch * hist[layer, nyi, nxi]
                state[nli] = sv
            if sv & 16:
                continue
            st = sv & 3
            if st == 2:
                continue
            if negotiate:
                tentative = base + (step_cost[layer, k] + soft_h[nli]) * soft_m[nli]
            else:
                tentative = base + step_cost[layer, k]
            if st == 0 or tentative < g[nli]:
                g[nli] = tentative
                came[nli] = cur
                state[nli] = (state[nli] & 0xFC) | 1
                best = math.inf
                for t in range(n_targets):
                    dx = abs(nxi - tx[t])
                    dy = abs(nyi - ty[t])
                    octile = pitch * (max(dx, dy) + sqrt2m1 * min(dx, dy))
                    if layer != tl[t]:
                        octile += via_cost_mm
                    if octile < best:
                        best = octile
                heapq.heappush(heap, (tentative + best * weight_h, nidx))
        # A via changes to ANY allowed layer — see `_route_in`.
        if not has_via:
            continue
        lrem = li - lbase
        vs = via_state[lrem]
        if vs == 0:
            vs = 1
            if pad_keep[iy, ix]:
                vs = 2
            else:
                for vl in range(n_layers):
                    if _disk_hits_foreign(owner, vl, iy, ix, r_via, half_via, net_id):
                        vs = 2
                        break
            if vs == 1 and negotiate:
                ku = 0
                for vl in range(n_layers):
                    kl = _disk_max_usage(usage, vl, iy, ix, r_via, half_via)
                    if kl > ku:
                        ku = kl
                via_m[lrem] = 1.0 + pres_fac * ku
            via_state[lrem] = vs
        if vs == 2:
            continue
        under_body = has_body and body[iy, ix]
        via_mult = via_m[lrem] if negotiate else 1.0  # read only when negotiating
        for a in range(allowed.shape[0]):
            other = allowed[a]
            if other == layer:
                continue
            nidx = other * plane + rem
            nli = other * wplane + lrem
            sv = state[nli]
            if not sv & 8:
                sv |= 8
                if _disk_hits_foreign(
                    owner, other, iy, ix, r_track, half_track, net_id
                ):
                    sv |= 16
                elif negotiate:
                    ku = _disk_max_usage(usage, other, iy, ix, r_track, half_track)
                    soft_m[nli] = 1.0 + pres_fac * ku
                    soft_h[nli] = hist_fac * pitch * hist[other, iy, ix]
                state[nli] = sv
            if sv & 16:
                continue
            st = sv & 3
            if st == 2:
                continue
            if negotiate:
                tentative = (
                    base
                    + (via_cost_mm + (via_body_cost_mm if under_body else 0.0))
                    * via_mult
                    + soft_h[nli] * soft_m[nli]
                )
            else:
                tentative = (
                    base + via_cost_mm + (via_body_cost_mm if under_body else 0.0)
                )
            if st == 0 or tentative < g[nli]:
                g[nli] = tentative
                came[nli] = cur
                state[nli] = (state[nli] & 0xFC) | 1
                best = math.inf
                for t in range(n_targets):
                    dx = abs(ix - tx[t])
                    dy = abs(iy - ty[t])
                    octile = pitch * (max(dx, dy) + sqrt2m1 * min(dx, dy))
                    if other != tl[t]:
                        octile += via_cost_mm
                    if octile < best:
                        best = octile
                heapq.heappush(heap, (tentative + best * weight_h, nidx))
    return _EMPTY, -1, 0.0, empty_path


def path_samples(path: RoutePath, step: float) -> list[tuple[float, float, int, int]]:
    """``(x, y, layer_lo, layer_hi)`` points covering ``path``'s copper:
    each span re-sampled at ``step``, each via once over every layer it
    spans. The one sampling :meth:`OccupancyGrid.stamp_path`,
    :meth:`OccupancyGrid.register_attach` and :meth:`Negotiation.
    claim_cells` share, so a negotiated claim covers the cells a hard stamp
    would."""
    out: list[tuple[float, float, int, int]] = []
    for a, b in zip(path.points, path.points[1:], strict=False):
        if a[2] != b[2]:
            out.append((a[0], a[1], min(a[2], b[2]), max(a[2], b[2])))
            continue
        seg_len = math.hypot(b[0] - a[0], b[1] - a[1])
        n = max(1, math.ceil(seg_len / step))
        for k in range(n + 1):
            t = k / n
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2], a[2]))
    return out


def path_cells(
    path: RoutePath, spec: GridSpec
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """The cells a :meth:`OccupancyGrid.route` result walks:
    ``(layer, iy, ix)`` of every centreline cell in path order, then
    ``(iy, ix)`` of every layer change. Exact for a search result, whose
    polyline is cell centres in unit-step runs (an attach or goal anchor
    moves an end point within its own cell only)."""
    cells = [(*spec.to_cell(x, y), layer) for x, y, layer in path.points]
    tl: list[int] = []
    tiy: list[int] = []
    tix: list[int] = []
    viy: list[int] = []
    vix: list[int] = []
    if cells:
        ix, iy, layer = cells[0]
        tl.append(layer)
        tiy.append(iy)
        tix.append(ix)
    for (ax, ay, al), (bx, by, bl) in itertools.pairwise(cells):
        if al != bl:
            viy.append(ay)
            vix.append(ax)
            tl.append(bl)
            tiy.append(by)
            tix.append(bx)
            continue
        n = max(abs(bx - ax), abs(by - ay))
        for k in range(1, n + 1):
            tl.append(al)
            tiy.append(ay + round((by - ay) * k / n))
            tix.append(ax + round((bx - ax) * k / n))
    as_i64 = lambda v: np.asarray(v, dtype=np.int64)  # noqa: E731
    return as_i64(tl), as_i64(tiy), as_i64(tix), as_i64(viy), as_i64(vix)


class Negotiation:
    """Negotiated congestion (PathFinder, McMurchie & Ebeling 1995) over
    an :class:`OccupancyGrid`'s cells.

    The hard grid lets the first net to claim a corridor keep it, so a
    connection that loses the race fails even when a different split of
    the board would fit both. Here routed copper is not OWNED: each cell
    counts how many nets' claims cover it (``usage``), and a search pays to
    come near another net's copper instead of being refused. The price
    rises every iteration (``pres_fac``), and cells that stayed contested
    accumulate a permanent surcharge (``hist``), so nets that have
    somewhere else to go move away and the one that needs the corridor
    keeps it.

    The grid's own owner array still holds the static copper (pads, fixed
    copper, holes, plane fan-out); that stays a hard wall. A negotiated
    result is only a proposal: :func:`precis.pcb.realize._realize_maze`
    commits it onto a fresh hard grid through
    :meth:`OccupancyGrid.path_is_legal`, so the no-overlap guarantee never
    rests on the negotiation having converged.

    A history cost on the HARD grid was tried first and lost (backlog
    ``pcb-router-fails-at-real-board-size.md`` step 12): without sharing,
    nets cannot negotiate, only detour."""

    def __init__(
        self, spec: GridSpec, *, pres_fac: float = 0.5, hist_fac: float = 1.0
    ) -> None:
        self.spec = spec
        shape = (spec.n_layers, spec.ny, spec.nx)
        self.usage = np.zeros(shape, dtype=np.int16)
        self.hist = np.zeros(shape, dtype=np.float32)
        self.pres_fac = pres_fac
        self.hist_fac = hist_fac
        self._own = np.zeros(shape, dtype=np.uint8)
        self._mark = np.zeros(spec.n_cells, dtype=np.uint8)
        self._net_cells: dict[int, np.ndarray] = {}

    def claim_cells(
        self, samples: Sequence[tuple[float, float, int, int, float]]
    ) -> np.ndarray:
        """Flat indices of the cells a hard grid's :meth:`OccupancyGrid.
        stamp_disk` would claim for each ``(x, y, layer_lo, layer_hi,
        radius_mm)``, deduplicated."""
        if not samples:
            return np.empty(0, dtype=np.int64)
        arr = np.asarray(samples, dtype=np.float64)
        spec = self.spec
        rc = np.ceil(arr[:, 4] / spec.pitch)
        bound = int((((2 * rc + 1) ** 2) * (arr[:, 3] - arr[:, 2] + 1)).sum())
        out = np.empty(bound, dtype=np.int64)
        n = _claim_cells_kernel(
            self._mark,
            arr[:, 0].copy(),
            arr[:, 1].copy(),
            arr[:, 2].astype(np.int64),
            arr[:, 3].astype(np.int64),
            arr[:, 4].copy(),
            float(spec.x0),
            float(spec.y0),
            float(spec.pitch),
            spec.ny,
            spec.nx,
            out,
        )
        return out[:n]

    def set_net(self, net_id: int, cells: Sequence[np.ndarray]) -> None:
        """Make ``cells`` (the union of the net's connection claims) the
        net's usage, replacing whatever it held."""
        self.rip_net(net_id)
        if not cells:
            return
        union = np.unique(np.concatenate(cells))
        self.usage.reshape(-1)[union] += 1
        self._net_cells[net_id] = union

    def rip_net(self, net_id: int) -> None:
        cells = self._net_cells.pop(net_id, None)
        if cells is not None:
            self.usage.reshape(-1)[cells] -= 1

    def conflicts(
        self,
        net_id: int,
        path: RoutePath,
        *,
        width_mm: float,
        via_dia_mm: float | None,
    ) -> np.ndarray:
        """Flat indices of ``path``'s centreline cells (and via sites, on
        every layer) that another net's claim reaches — the cells
        :meth:`OccupancyGrid.path_is_legal` would refuse were that net's
        copper owned. Empty means no conflict."""
        spec = self.spec
        tl, tiy, tix, viy, vix = path_cells(path, spec)
        own = self._own.reshape(-1)
        mine = self._net_cells.get(net_id)
        if mine is not None:
            own[mine] = 1
        r_track = math.ceil((width_mm / 2.0) / spec.pitch) + 1
        r_via = (
            0 if via_dia_mm is None else math.ceil((via_dia_mm / 2.0) / spec.pitch) + 1
        )
        hits = _conflict_kernel(
            self.usage,
            self._own,
            tl,
            tiy,
            tix,
            r_track,
            _disk_half_chords(r_track),
            viy,
            vix,
            r_via,
            _disk_half_chords(r_via),
        )
        if mine is not None:
            own[mine] = 0
        return hits

    def add_history(self, cells: np.ndarray) -> None:
        if cells.shape[0]:
            self.hist.reshape(-1)[np.unique(cells)] += 1.0


@numba.njit(cache=False, nogil=True)
def _claim_cells_kernel(
    mark: np.ndarray,
    xs: np.ndarray,
    ys: np.ndarray,
    lo: np.ndarray,
    hi: np.ndarray,
    rad: np.ndarray,
    x0: float,
    y0: float,
    pitch: float,
    ny: int,
    nx: int,
    out: np.ndarray,
) -> int:
    """:meth:`Negotiation.claim_cells`: :meth:`OccupancyGrid.stamp_disk`'s
    cell set per sample (same rounding, same nearest-cell fallback when the
    disk covers no cell centre), written once each into ``out``."""
    plane = ny * nx
    n = 0
    for s in range(xs.shape[0]):
        x = xs[s]
        y = ys[s]
        r = rad[s]
        rc = math.ceil(r / pitch)
        cx = min(max(int(np.rint((x - x0) / pitch)), 0), nx - 1)
        cy = min(max(int(np.rint((y - y0) / pitch)), 0), ny - 1)
        lo_x, hi_x = max(0, cx - rc), min(nx - 1, cx + rc)
        lo_y, hi_y = max(0, cy - rc), min(ny - 1, cy + rc)
        any_inside = False
        best = math.inf
        bx = lo_x
        by = lo_y
        for iy in range(lo_y, hi_y + 1):
            dy = y0 + iy * pitch - y
            for ix in range(lo_x, hi_x + 1):
                dx = x0 + ix * pitch - x
                d2 = dy * dy + dx * dx
                if d2 < best:
                    best = d2
                    bx = ix
                    by = iy
                if d2 <= r * r:
                    any_inside = True
                    for layer in range(lo[s], hi[s] + 1):
                        idx = layer * plane + iy * nx + ix
                        if mark[idx] == 0:
                            mark[idx] = 1
                            out[n] = idx
                            n += 1
        if not any_inside and lo_x <= hi_x and lo_y <= hi_y:
            for layer in range(lo[s], hi[s] + 1):
                idx = layer * plane + by * nx + bx
                if mark[idx] == 0:
                    mark[idx] = 1
                    out[n] = idx
                    n += 1
    for i in range(n):
        mark[out[i]] = 0
    return n


@numba.njit(cache=False, nogil=True)
def _disk_max_usage(
    usage: np.ndarray, layer: int, iy: int, ix: int, r: int, half: np.ndarray
) -> int:
    """Most nets' claims on any one cell within ``r`` of ``(ix, iy)``."""
    n_l, ny, nx = usage.shape
    best = 0
    for dy in range(-r, r + 1):
        y = iy + dy
        if y < 0 or y >= ny:
            continue
        w = half[dy + r]
        for x in range(max(0, ix - w), min(nx - 1, ix + w) + 1):
            u = usage[layer, y, x]
            if u > best:
                best = u
    return best


@numba.njit(cache=False, nogil=True)
def _disk_hits_other_usage(
    usage: np.ndarray,
    own: np.ndarray,
    layer: int,
    iy: int,
    ix: int,
    r: int,
    half: np.ndarray,
) -> bool:
    n_l, ny, nx = usage.shape
    for dy in range(-r, r + 1):
        y = iy + dy
        if y < 0 or y >= ny:
            continue
        w = half[dy + r]
        for x in range(max(0, ix - w), min(nx - 1, ix + w) + 1):
            if usage[layer, y, x] - own[layer, y, x] > 0:
                return True
    return False


@numba.njit(cache=False, nogil=True)
def _conflict_kernel(
    usage: np.ndarray,
    own: np.ndarray,
    tl: np.ndarray,
    tiy: np.ndarray,
    tix: np.ndarray,
    r_track: int,
    half_track: np.ndarray,
    viy: np.ndarray,
    vix: np.ndarray,
    r_via: int,
    half_via: np.ndarray,
) -> np.ndarray:
    """:meth:`Negotiation.conflicts`: flat indices of conflicting cells."""
    n_l, ny, nx = usage.shape
    plane = ny * nx
    out = np.empty(tl.shape[0] + viy.shape[0] * n_l, dtype=np.int64)
    n = 0
    for i in range(tl.shape[0]):
        if _disk_hits_other_usage(
            usage, own, tl[i], tiy[i], tix[i], r_track, half_track
        ):
            out[n] = tl[i] * plane + tiy[i] * nx + tix[i]
            n += 1
    for i in range(viy.shape[0]):
        hit = False
        for layer in range(n_l):
            if _disk_hits_other_usage(
                usage, own, layer, viy[i], vix[i], r_via, half_via
            ):
                hit = True
                break
        if hit:
            for layer in range(n_l):
                out[n] = layer * plane + viy[i] * nx + vix[i]
                n += 1
    return out[:n]


@numba.njit(cache=False, nogil=True)
def _path_legal_kernel(
    owner: np.ndarray,
    net_id: int,
    tl: np.ndarray,
    tiy: np.ndarray,
    tix: np.ndarray,
    r_track: int,
    half_track: np.ndarray,
    viy: np.ndarray,
    vix: np.ndarray,
    r_via: int,
    half_via: np.ndarray,
    pad_keep: np.ndarray,
    start_exempt: bool,
    goal_exempt: bool,
) -> bool:
    """:meth:`OccupancyGrid.path_is_legal`."""
    n_l = owner.shape[0]
    last = tl.shape[0] - 1
    for i in range(tl.shape[0]):
        if (i == 0 and start_exempt) or (i == last and goal_exempt):
            if _disk_hits_net(
                owner, tl[i], tiy[i], tix[i], r_track, half_track, net_id
            ):
                return False
        elif _disk_hits_foreign(
            owner, tl[i], tiy[i], tix[i], r_track, half_track, net_id
        ):
            return False
    for i in range(viy.shape[0]):
        if pad_keep[viy[i], vix[i]]:
            return False
        for layer in range(n_l):
            if _disk_hits_foreign(
                owner, layer, viy[i], vix[i], r_via, half_via, net_id
            ):
                return False
    return True


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
    "VIA_COST_MM",
    "VIA_UNDER_BODY_COST_MM",
    "GridSpec",
    "OccupancyGrid",
    "RoutePath",
    "grid_for",
]
