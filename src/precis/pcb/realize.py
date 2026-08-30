"""Sketch → copper geometry — the realizer. See
docs/backlog/pcb-guided-place-route.md §"Sketch + realize".

**Runs at checkpoints, never in the inner loop.** Sketch (L0-L2, plus L3
placement) is canonical; copper (L5) is derived and regenerable, same
discipline as chunks→embeddings. A pure function of a settled
:class:`~precis.pcb.ir.PcbIR` snapshot (plus obstacle/config data) — never
mutates L0-L3 state (that's ``optimize.py``'s move classes) — only reads
the sketch, writes L5-shaped output.

**Two routers**; ``RealizeConfig.router`` selects, opposite guarantees:

- ``'maze'`` (default, :mod:`precis.pcb.maze`): claims each route's
  corridor on a shared occupancy grid before drawing it, so inter-net
  ``clearance`` violations are structurally impossible. Chooses its own
  layers/path (``seg_layer`` is a preference, not an instruction); reports
  what it couldn't route in :attr:`RealizeResult.unrouted`.
- ``'tangent'``: one straight-or-hugging track per segment on that
  segment's ``seg_layer``, drawn unconditionally, blind to every other
  track. Right for a single-segment question (:func:`realize_segment` IS
  this); wrong for a board — 234 same-layer clearance violations on the
  reference fixture, unrepairable by any later pass.

**Geometry is arcs and tangent lines, not beziers**: the shortest path
around a single circular clearance obstacle is exactly straight tangents
joined by a circular arc, closed-form (:func:`tangent_arc_path`). Gerber
has native arcs (G02/G03), no bezier primitive — a bezier would just be
flattened to polylines on export.

**Output matches :mod:`precis.pcb.gerber`'s model shape exactly**: a
track's ``segments`` entries are ``{"shape": "line"|"arc", "start": [x,
y], "end": [x, y]}`` (arc adds ``"center": [x, y], "cw": bool}``) — the
dict shape :func:`precis.pcb.gerber.copper_gerber` reads via
``_emit_stroke`` (see :func:`to_gerber_model`,
``tests/test_pcb_realize.py``'s round-trip through
:func:`precis.pcb.gerber.export_gerbers`).

**Single-obstacle closed form only.** A segment blocked by more than one
obstacle at once (a multi-obstacle rubber-band problem) is out of scope —
realizes around the single nearest blocking obstacle, falls back to a
straight line, reports the rest as still-blocking rather than emitting a
wrong path. "Fail legibly", applied to the realizer's own limits.

**Per-gap capacity accounting lives here too**, as data not just the
optimizer's estimate: every realized segment's binding gap (the same
``nearest_other_instance`` neighbourhood :mod:`precis.pcb.optimize` uses
for L4) is tallied; a gap whose strand usage exceeds capacity produces a
:class:`CongestionWarning` naming the gap, participants, and clearance
arithmetic.

**Vias.** Under ``'tangent'``, emitted wherever a track's realized layer
differs from its pad layer. Under ``'maze'``, emitted wherever the search
changed layer, gated on the STITCHED GROUP's full extent fitting in
cleared space (not just the trace's) — a rail crossing layers through one
via is a fuse; stitching four vias for a group planned around one puts
three in somebody else's copper. See :func:`_vias_for_track` (tangent
rule) and :class:`RealizedVia`, which always carries a layer SPAN
(``layer_lo``/``layer_hi``), never a scalar — a scalar via makes every via
DRC rule blind on every layer but one. Via COUNT scales with the net's
current annotation (:func:`precis.pcb.rules.via_count_for_current`)
rather than always emitting one — an array that can't carry its rail's
current is the same silent-failure class as missing geometry.

**Plane fragment stitching is a PRODUCER, deliberately independent of its
own checker** (:func:`_stitch_plane_fragments`, gripe 270637). A net poured
on several layers (``ir.net_plane_layers``, a bitmask —
:func:`~precis.pcb.ir.plane_layers_of`) used to come out as one electrical
net only where a per-pin drop via *happened* to span every poured sheet, or
where a foreign via's antipad *happened* to leave a net's own pin
population inside every fragment it cut a same-layer pour into — both
purely incidental, and measured (gripe 270637) to fragment on real boards
(GND, more pins, got lucky every seed; VCC3V3, fewer pins, did not).
:func:`_stitch_plane_fragments` places deliberate stitching vias instead,
in two stages: a generous regular-grid SPRINKLE across every pair of a
net's poured sheets that spatially overlap (stage 1 — what a real fab tool
does, reusing this module's own keep-out checks:
:meth:`~precis.pcb.maze.OccupancyGrid.via_clears_pads`,
:func:`_via_clears_vias`, :meth:`~precis.pcb.maze.OccupancyGrid.
disk_is_free` — no new geometry predicate duplicates any of those), then a
bounded TARGETED pass that finds whatever pieces remain disconnected and
tries the single nearest legal via to join them (stage 2 — see that
function's own docstring for exactly which gaps one via can and cannot
bridge), and finally a JUMPER for a pair stage 2 has just proved no single
via can close (stage 3, :func:`_try_plane_jumper`): a via out of each
fragment plus a trace between them on a spare layer. Stage 3 is the only
mechanism here that can join two fragments of one net on the SAME layer —
a via joins LAYERS, not lateral gaps, so no number of them ever could —
and it is the only reason this pass emits TRACKS as well as vias.

**This pass computes its own connectivity, with its own union-find
(:class:`_UnionFind`), and never calls
:func:`precis.pcb.connectivity.net_islands` or imports from that module.**
``net_islands`` is the INDEPENDENT CHECKER for exactly this property
(:mod:`precis.pcb.connectivity`'s own module docstring); if this pass
decided when to stop by asking that same checker whether it was happy, the
``connectivity`` DRC finding could never fire again for a REAL defect
either — not because boards became correct, but because the check became
tautological. So the two are deliberately duplicated, not shared, for the
same reason :mod:`precis.pcb.drc`'s own ``clearance_violations_naive``
reference oracle reimplements its own layer logic rather than importing
the accelerated engine's: a producer and its checker must stay
independent, even though a *shared definition* elsewhere in this codebase
(a clearance constant, a pad's real shape) still belongs in ONE place. The
one thing this pass DOES reuse is generic, checker-agnostic geometry —
shapely polygon construction/intersection/nearest-point queries, and this
module's own existing keep-out predicates — never ``connectivity.py``'s
reasoning about what "connected" means.

**Rip-up primitives**: :func:`rip_net` removes one net's tracks (and
warnings), leaving every other net's geometry byte-identical.
:func:`pin_topology` delegates to :meth:`precis.pcb.ir.PcbIR.set_side`
(pinning a topology choice is a sketch edit, not a realizer concern) —
kept here for rip-up-loop callers' convenience.
:func:`re_realize_segments` recomputes only the named segments' tracks
against an already-realized result.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Any

# shapely ships no py.typed marker here -- same suppression drc.py/planes.py
# carry, same reason: only the polygon booleans this module's stitching
# pass (below) needs, never a track/via/pad's own shape (that stays this
# module's existing closed-form Point/dist arithmetic).
from shapely.geometry import (  # type: ignore[import-untyped]
    LineString as _ShapelyLineString,
)
from shapely.geometry import Point as _ShapelyPoint
from shapely.geometry import Polygon as _ShapelyPolygon

from precis.pcb import geom, maze, padplace
from precis.pcb.capabilities import CapabilityRow, capability_for
from precis.pcb.geom import Point, dist, dist_point_to_segment
from precis.pcb.ir import (
    NO_NET,
    PcbIR,
    nearest_other_instance,
    pin_point,
    plane_layers_of,
    routable_layers,
)
from precis.pcb.planes import plane_pours
from precis.pcb.rules import (
    PAD_LAYER,
    NetRules,
    implied_via_count,
    layer_is_outer,
    net_current_a_or_none,
    resolve_net_rules,
    via_count_for_current,
)

#: Re-exported from :mod:`precis.pcb.rules` (the constant's new home) so
#: every existing ``from precis.pcb.realize import PAD_LAYER`` call site
#: keeps working. Moved there, alongside :func:`precis.pcb.
#: rules.implied_via_count`, because :mod:`precis.pcb.cost`'s ``via_count``
#: term needs the SAME "did this segment change layer" test this module's
#: :func:`_vias_for_track` applies, and ``cost.py`` has no existing import
#: relationship with ``realize.py`` to piggyback on — ``rules.py`` is the
#: shared, lower-level module both already depend on, so hoisting there
#: (rather than having ``cost.py`` newly import ``realize.py``) adds no
#: coupling that wasn't already there.
PAD_LAYER = PAD_LAYER

# ── config + obstacles ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RealizeConfig:
    clearance_mm: float = 0.15
    #: Fallback courtyard radius for an instance with no explicit obstacle
    #: supplied (see :func:`_default_obstacles`) — a generic component-body
    #: half-size, not a real footprint courtyard (that data lives in
    #: ``part_footprints``, a store concern this pure module doesn't read).
    default_obstacle_radius_mm: float = 1.0
    #: Class-generic trace pitch (width + clearance) for GAP-CAPACITY
    #: accounting (:func:`_gap_usage`) — the same fallback
    #: :mod:`precis.pcb.cost`'s ``default_pitch_mm`` uses. Deliberately
    #: NOT wired to ``class_rules`` below (a different question — "how
    #: many strands fit through this gap", not "how wide is one net's own
    #: trace" — out of this module's current scope).
    pitch_mm: float = 0.3
    #: Plane-served nets fan out via a short dog-bone stub instead of a
    #: routed trace (backlog cost policy, verbatim) — this is that stub's
    #: length. The via-to-plane connection itself is ``planes.py``'s job,
    #: a later module; this realizer only ever draws the stub.
    dogbone_stub_mm: float = 0.5
    #: Corner radius for filleting routed traces, as a MULTIPLE of the
    #: track's own width — so a wide power trace gets a proportionally
    #: wider corner rather than every trace on the board sharing one
    #: absolute radius (the flat-constant mistake this module has made in
    #: five other places: a courtyard radius, a seed pitch, a claim radius,
    #: ``PAD_RADIUS_MM``, and the drop-via reach).
    #:
    #: **This is an engineering/appearance choice, not a fab figure.** No
    #: row in ``capabilities.py``/``pcb_capabilities.json`` publishes a
    #: corner radius, and none is needed: the radius is additionally
    #: clamped per run so filleting can only remove copper (see
    #: :func:`_track_from_run`). ``0.0`` disables filleting and restores
    #: mitered corners exactly.
    fillet_radius_tracks: float = 1.5
    #: The fab this board realizes against — every emitted track's width
    #: is clamped to this table's minimum (:mod:`precis.pcb.rules`'s own
    #: resolver discipline). Defaults to the house 4-layer row so a bare
    #: ``RealizeConfig()`` (most tests, most synthetic boards) still
    #: resolves a sane width; a real board's caller should pass the row
    #: for its ACTUAL stackup (``drc.process_for_stackup``).
    fab_caps: CapabilityRow = field(default_factory=lambda: capability_for("4layer"))
    #: ``pcb_net_classes.rules`` overrides, keyed by net_class name —
    #: ``pcb_graph()``'s own ``net_classes`` dict, unmodified. A class with
    #: no row here (the common case) falls through to the current-derived
    #: or fab-floor tiers (:func:`precis.pcb.rules.resolve_net_rules`).
    class_rules: dict[str, dict[str, Any]] | None = None
    #: IPC-2221 target temperature rise / copper weight for the
    #: current-derived width tier — see :mod:`precis.pcb.rules`.
    temp_rise_c: float = 10.0
    copper_oz: float = 1.0
    #: Which router draws the copper.
    #:
    #: ``'maze'`` (the default) claims each route's corridor on a shared
    #: occupancy grid (:mod:`precis.pcb.maze`), so two nets' copper can
    #: never overlap — ``clearance`` DRC errors become structurally
    #: impossible and the residual failure mode is an UNROUTED net
    #: (``RealizeResult.unrouted``) instead of a shorted one.
    #:
    #: ``'tangent'`` is the original closed-form drawer: a straight line
    #: per segment, optionally hugging one component courtyard, blind to
    #: every other track. Kept because it is the right primitive for a
    #: single-segment question (``realize_segment`` is still exactly this)
    #: and because a caller that wants geometry without an occupancy grid
    #: — a sketch preview, a cost probe — should not pay for one. It is
    #: not a routing strategy: measured on the reference fixture it drew
    #: 234 same-layer clearance violations that no later pass can repair.
    router: str = "maze"
    #: Cap on A* expansions per connection before that connection is
    #: declared unrouted. See :data:`precis.pcb.maze.MAX_EXPANSIONS`.
    max_expansions: int = maze.MAX_EXPANSIONS
    #: How many times the whole maze pass may be re-run with the previous
    #: attempt's failures moved to the front (see :func:`_realize_maze`).
    #: 1 is a single shortest-first pass — the prior behaviour. Each extra
    #: pass costs one full route (~0.4s on the reference board) and can
    #: only reduce the unrouted count, never the correctness of a result.
    #:
    #: 12 because the loop STOPS at the first fully-routed pass, so the
    #: cost is only paid on a board that needs it. Measured on the
    #: reference fixture: four of five seeds finish on pass 1 (~0.3s) and
    #: the fifth needs 12 (~4.8s) to place its last connection. Raising
    #: ``max_expansions`` instead does not help — 400k expansions leaves
    #: the same connection unrouted, because it is losing a corridor race,
    #: not running out of search.
    route_passes: int = 12
    #: Give each signal layer a preferred routing axis (H, V, diagonal, in
    #: stackup order — :func:`precis.pcb.maze.preferred_directions`).
    #: Off-axis steps cost more; nothing is forbidden. Unstructured routing
    #: on every layer fragments free space into islands too small to route
    #: through and too awkward to pour, which is why every VLSI router does
    #: this.
    preferred_directions: bool = True
    #: Pull each routed path taut against the occupancy grid before
    #: claiming it (:func:`_straighten`), removing the 45-degree staircases
    #: an octile search necessarily produces. Can only remove copper, never
    #: add or move it into someone else's corridor.
    straighten: bool = True
    #: Let :func:`_straighten` nudge a via's (x, y) — never its layer SPAN —
    #: within :attr:`via_shove_radius_mm` when doing so lets a wire go
    #: straighter, re-validated on the SAME occupancy grid using the via's
    #: own wider (group-extent) mask, collapsed across every layer it
    #: spans (:mod:`precis.pcb.maze`'s own discipline for a layer change,
    #: reused rather than re-derived). Every layer the via anchors moves
    #: together because the shove happens on the shared (x, y) the route
    #: search itself already threads through both the pre- and
    #: post-transition points — see :func:`_shove_vias`.
    #:
    #: **On by default — measured to cost nothing.** ESP32-C3 reference
    #: fixture, seeds 1-5, real place+route through the job dispatch path
    #: (not a synthetic probe), straighten+preferred-directions baseline
    #: vs. the same run with ``shove_vias=True``:
    #:
    #: ============  =========  ==========  ========  ===========  ===========
    #: seed          segments   copper mm   vias      DRC errors   routed
    #: ============  =========  ==========  ========  ===========  ===========
    #: 1  off/on     126 / 125  544.3/543.8 26 / 26   0 / 0        11/11, 11/11
    #: 2  off/on     119 / 118  529.9/522.8 25 / 27   0 / 0        11/11, 11/11
    #: 3  off/on     136 / 113  491.4/476.9 25 / 24   0 / 0        11/11, 11/11
    #: 4  off/on     131 / 129  541.0/540.1 30 / 30   0 / 0        11/11, 11/11
    #: 5  off/on     127 / 127  537.1/529.4 29 / 30   0 / 0        11/11, 11/11
    #: ============  =========  ==========  ========  ===========  ===========
    #:
    #: Segment count and copper length never went UP on any seed (the
    #: monotonic-non-increase this flag's own correctness argument
    #: predicts); DRC stayed at zero and the routed count never moved on
    #: any seed either. Via count moved by ±1-2 on two seeds — expected
    #: and benign: shoving one connection's via changes what corridor a
    #: LATER connection in the same sequential pass finds, which can shift
    #: where ITS via lands, not a violation of "shoving moves a via, it
    #: doesn't add one" (that invariant holds per-connection, in isolation
    #: — see this module's test). Net effect: ~4% fewer segments, ~1%
    #: less copper, zero correctness cost, so — unlike
    #: ``preferred_directions``, which traded routability for structure —
    #: there is no case for leaving this off.
    shove_vias: bool = True
    #: How far a via may move from its searched position, in mm. Small on
    #: purpose ("slightly", per the backlog) — this is a cosmetic taut-up
    #: of the search's own result, not a second placement pass.
    via_shove_radius_mm: float = 0.5
    #: Regular-grid pitch for stage-1 "sprinkle" stitching-via placement
    #: (:func:`_stitch_plane_fragments`, gripe 270637): candidate via sites
    #: are tried every this many mm across the OVERLAP of two of one net's
    #: poured sheets. Not a fab figure — neither ``capabilities.py`` nor
    #: ``pcb_capabilities.json`` publish a hole-to-hole spacing row to pin
    #: this to (checked before adding this field). ``None`` (the default)
    #: derives it from geometry already in hand instead of a bare
    #: constant: twice the resolved via's own (diameter + clearance) — the
    #: SAME pitch :func:`_route_pass`/:func:`_vias_for_track` already use
    #: to space members of one via GROUP, doubled so consecutive grid
    #: candidates don't immediately crowd each other's own keep-out before
    #: the keep-out filter below even gets a chance to accept either one.
    #: An explicit override here is an ENGINEERING choice about stitching
    #: density (more vias = more thermal/EMI margin = more copper cost),
    #: never a fab minimum.
    stitch_pitch_mm: float | None = None
    #: Cap on ACCEPTED sprinkle vias per (sheet, sheet) overlap region — a
    #: runtime/copper-budget guard, not a connectivity requirement: the
    #: residue stage is what GUARANTEES the fragment count goes down, not
    #: this cap. An overlap the size of the whole board at a fine pitch
    #: would otherwise sprinkle thousands of vias for a property that
    #: needs at most one.
    max_sprinkle_vias_per_overlap: int = 24
    #: Bound on the residue stage's stitch-and-recheck loop, per net (see
    #: :func:`_stitch_plane_fragments`). Each iteration either merges two
    #: pieces or proves that pair has no legal single-via bridge and moves
    #: on — a rejected candidate leaves a net exactly as fragmented as it
    #: was, never more, so this bounds worst-case runtime, not correctness.
    max_stitch_iterations: int = 8
    #: Cap on JUMPERS (two vias plus a detour-layer trace,
    #: :func:`_try_plane_jumper`) placed for one net. A jumper is the only
    #: mechanism that can join two fragments of one net on the SAME layer —
    #: no via can, and that is a proof, not a search limit
    #: (:func:`_stitch_one_net`'s docstring) — but it is also the most
    #: expensive thing this pass emits: a full A* on the detour layer plus
    #: real copper on a layer this net had no business being on. A net in
    #: ``k`` same-layer pieces needs ``k-1`` jumpers, so the default carries
    #: a 5-piece plane; past that, an honest ``UnstitchedNet`` beats
    #: silently threading a board full of detours.
    max_plane_jumpers_per_net: int = 4


@dataclass(frozen=True, slots=True)
class Obstacle:
    """A layer-agnostic circular clearance obstacle — an instance's
    courtyard, expanded by the class clearance the caller wants respected.
    ``radius`` should already include clearance; :func:`tangent_arc_path`
    treats it as the hard keep-out radius."""

    instance: int
    center: Point
    radius: float


def _instance_point(ir: PcbIR, inst: int) -> Point | None:
    x, y = float(ir.inst_x[inst]), float(ir.inst_y[inst])
    if math.isnan(x) or math.isnan(y):
        return None
    return (x, y)


def _pin_point(ir: PcbIR, pin_id: int) -> Point | None:
    """A track endpoint: the PAD, not the part centre.

    Using the instance centroid here meant every net leaving a part
    started at the same coordinate, so tracks of different nets were
    coincident before any routing ran — ~600 `clearance` findings at an
    exact 0.000mm gap, which no routing algorithm can separate.
    """
    return pin_point(ir, pin_id)


def _default_obstacles(ir: PcbIR, config: RealizeConfig) -> list[Obstacle]:
    """One obstacle per placed instance, at the config's generic radius —
    the honest fallback when no real footprint courtyard data is
    supplied. A caller with real courtyards should build its own
    ``obstacles`` list and pass it to :func:`realize` instead."""
    out = []
    for inst in range(ir.n_instances):
        p = _instance_point(ir, inst)
        if p is not None:
            out.append(Obstacle(inst, p, config.default_obstacle_radius_mm))
    return out


# ── the closed-form geometric primitive ──────────────────────────────────


def _tangent_points(p: Point, c: Point, r: float) -> tuple[Point, Point]:
    """The two points on circle ``(c, r)`` touched by a tangent line from
    external point ``p``. Raises if ``p`` is inside/on the circle — no
    tangent exists there. See the module-level derivation: for the right
    triangle (center, tangent point, p) with the right angle at the
    tangent point, ``cos(angle at center) = r / |p - c|``."""
    dx, dy = c[0] - p[0], c[1] - p[1]
    d = math.hypot(dx, dy)
    if d <= r + 1e-9:
        raise ValueError("tangent point undefined: point is inside/on the circle")
    theta = math.acos(min(1.0, r / d))
    phi = math.atan2(p[1] - c[1], p[0] - c[0])
    t1 = (c[0] + r * math.cos(phi + theta), c[1] + r * math.sin(phi + theta))
    t2 = (c[0] + r * math.cos(phi - theta), c[1] + r * math.sin(phi - theta))
    return t1, t2


def _signed_shorter_sweep(c: Point, r: float, ta: Point, tb: Point) -> float:
    """The signed angle (radians, in ``(-pi, pi]``) swept from ``ta`` to
    ``tb`` around ``c`` going the SHORT way — positive = counterclockwise
    (increasing angle), negative = clockwise."""
    a1 = math.atan2(ta[1] - c[1], ta[0] - c[0])
    a2 = math.atan2(tb[1] - c[1], tb[0] - c[0])
    diff = (a2 - a1) % (2 * math.pi)
    if diff > math.pi:
        diff -= 2 * math.pi
    return diff


def tangent_arc_path(
    start: Point, end: Point, center: Point, radius: float
) -> tuple[list[dict[str, Any]], float]:
    """The shortest path from ``start`` to ``end`` that clears the circle
    ``(center, radius)`` — a straight line when the direct segment already
    clears it, else two tangent lines joined by a circular arc, all in
    closed form. Returns ``(segments, length_mm)`` where ``segments`` is
    ALREADY in :mod:`precis.pcb.gerber`'s exact track-segment shape.

    **Why brute-force over the 4 tangent-point pairings, not a single
    "same-side" formula.** For two external points on genuinely different
    sides of the circle, which tangent point (of each point's own two)
    forms a valid, non-self-intersecting hugging path is NOT a fixed
    labeling ("+theta with +theta") — it depends on the two points'
    relative angular position (verified empirically against 500
    randomized obstacle placements while building this function: the
    "same label" pairing is sometimes the long way around, ~40% farther,
    not merely suboptimal). Enumerating all 4 ``(tangent(start), tangent
    (end))`` pairs, each with its own shorter-direction arc sweep, and
    taking the global minimum total length reliably finds the correct
    valid path — the two "wrong" pairings are always longer (this is
    what the property test in ``tests/test_pcb_realize.py`` checks over
    many random obstacle placements: real clearance is honoured
    everywhere, not just at the one hand-computed fixture point)."""
    if dist_point_to_segment(center, start, end) >= radius - 1e-9:
        return [{"shape": "line", "start": list(start), "end": list(end)}], dist(
            start, end
        )

    s1, s2 = _tangent_points(start, center, radius)
    e1, e2 = _tangent_points(end, center, radius)
    best: tuple[float, Point, Point, float] | None = None
    for sp in (s1, s2):
        for ep in (e1, e2):
            sweep = _signed_shorter_sweep(center, radius, sp, ep)
            arc_len = abs(sweep) * radius
            total = dist(start, sp) + arc_len + dist(ep, end)
            if best is None or total < best[0]:
                best = (total, sp, ep, sweep)
    assert best is not None
    total, sp, ep, sweep = best

    segments: list[dict[str, Any]] = []
    if dist(start, sp) > 1e-9:
        segments.append({"shape": "line", "start": list(start), "end": list(sp)})
    segments.append(
        {
            "shape": "arc",
            "start": list(sp),
            "end": list(ep),
            "center": list(center),
            # atan2/math convention: increasing angle is counterclockwise,
            # which is G03 in gerber.py's `_emit_stroke` (cw=False -> G03).
            "cw": sweep < 0,
        }
    )
    if dist(ep, end) > 1e-9:
        segments.append({"shape": "line", "start": list(ep), "end": list(end)})
    return segments, total


def _blocking_obstacle(
    start: Point, end: Point, obstacles: list[Obstacle], clearance_mm: float
) -> Obstacle | None:
    """The single nearest obstacle whose (radius + clearance) disk both
    blocks the direct S->E line AND leaves both endpoints strictly
    outside it — the second condition matters for tightly-packed
    components: an obstacle already touching (or containing) one of this
    segment's own endpoints has no tangent line from that endpoint at
    all (:func:`_tangent_points` is undefined there), so it is not a
    candidate for the tangent-arc construction and is excluded rather
    than crashing. This is a real, honest limitation for very tight
    packing, not an oversight — see the module docstring's
    single-obstacle-closed-form-only note."""
    candidates = []
    for o in obstacles:
        eff_r = o.radius + clearance_mm
        if dist(start, o.center) <= eff_r or dist(end, o.center) <= eff_r:
            continue
        if dist_point_to_segment(o.center, start, end) < eff_r:
            candidates.append(o)
    if not candidates:
        return None
    return min(candidates, key=lambda o: dist_point_to_segment(o.center, start, end))


# ── per-segment realization ──────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RealizedTrack:
    seg_id: int
    net_id: int
    layer: int
    segments: tuple[dict[str, Any], ...]
    length_mm: float
    #: The instance id of the single obstacle routed around, or ``None``
    #: for a straight run (or a dog-boned plane connection).
    blocked_by: int | None
    #: This track's resolved copper width — :func:`precis.pcb.rules.
    #: resolve_net_rules`'s answer for this net/layer, ALREADY clamped to
    #: the fab minimum. The one place a track's width is decided; every
    #: consumer (gerber export, ``pcb_route``'s persisted copper) reads
    #: this field rather than re-deriving its own default.
    width_mm: float
    #: True for a plane-promoted net's short dog-bone stub, never a real
    #: routed trace — the "never route ground/power beyond the dog-bone
    #: fanout" cost policy (backlog, verbatim), enforced here rather than
    #: merely hoped for by a cost term.
    is_dogbone: bool = False


@dataclass(frozen=True, slots=True)
class RealizedVia:
    """One realized via — ALWAYS a layer SPAN (``layer_lo``/``layer_hi``,
    both stackup-index ints, inclusive, ``layer_lo <= layer_hi``), never a
    scalar layer (module docstring — this is the exact shape a prior
    scalar-layer regression made invisible to every layer's clearance
    candidate set; see ``docs/backlog/pcb-guided-place-route.md``'s "Bugs
    this build produced" #5). :func:`to_gerber_model` is the ONE place
    these ints become the ``span`` layer-NAME pair :mod:`precis.pcb.drc`/
    :mod:`precis.pcb.gerber` read (the IR's own "layers are integer
    indexes, names are an export concern only" discipline)."""

    seg_id: int
    net_id: int
    x: float
    y: float
    dia_mm: float
    drill_mm: float
    layer_lo: int
    layer_hi: int
    #: Which of the segment's two endpoints this via serves ('a' or 'b') —
    #: a stitched-group provenance/debugging aid only, never consumed
    #: downstream (not part of the gerber/DRC-facing shape).
    endpoint: str


def _resolve_track_rules(
    ir: PcbIR, net_id: int, layer: int, config: RealizeConfig
) -> NetRules:
    """This net's resolved geometry rules (module docstring's single
    resolver, shared with :mod:`precis.pcb.drc` and :mod:`precis.pcb.cost`)
    — the ``track_width_mm`` every :class:`RealizedTrack` this module emits
    carries."""
    net_class = str(ir.net_class[net_id])
    overrides = (config.class_rules or {}).get(net_class)
    current_a = net_current_a_or_none(float(ir.net_current_a[net_id]))
    return resolve_net_rules(
        net_class,
        layer_is_outer=layer_is_outer(ir, layer),
        fab_caps=config.fab_caps,
        overrides=overrides,
        current_a=current_a,
        temp_rise_c=config.temp_rise_c,
        copper_oz=config.copper_oz,
    )


def realize_segment(
    ir: PcbIR, seg_id: int, obstacles: list[Obstacle], config: RealizeConfig
) -> RealizedTrack | None:
    """One segment's realized track, or ``None`` when its endpoints have
    no L3 position yet (nothing to draw)."""
    a, b = int(ir.seg_pin_a[seg_id]), int(ir.seg_pin_b[seg_id])
    ia, ib = int(ir.pin_instance[a]), int(ir.pin_instance[b])
    start, end = _pin_point(ir, a), _pin_point(ir, b)
    if start is None or end is None:
        return None
    net_id = int(ir.seg_net[seg_id])
    layer = int(ir.seg_layer[seg_id])
    width_mm = _resolve_track_rules(ir, net_id, layer, config).track_width_mm

    if int(ir.net_plane_layers[net_id]) != 0:
        # Dog-bone fanout: a short stub off the near pad, not a full route
        # to the far end (which is served by the plane, not this trace).
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = math.hypot(dx, dy)
        if length < 1e-9:
            stub_end = start
        else:
            ratio = min(1.0, config.dogbone_stub_mm / length)
            stub_end = (start[0] + dx * ratio, start[1] + dy * ratio)
        segments: list[dict[str, Any]] = [
            {"shape": "line", "start": list(start), "end": list(stub_end)}
        ]
        return RealizedTrack(
            seg_id,
            net_id,
            layer,
            tuple(segments),
            dist(start, stub_end),
            None,
            width_mm,
            is_dogbone=True,
        )

    relevant = [o for o in obstacles if o.instance not in (ia, ib)]
    blocker = _blocking_obstacle(start, end, relevant, config.clearance_mm)
    if blocker is None:
        segs, length = (
            [{"shape": "line", "start": list(start), "end": list(end)}],
            dist(start, end),
        )
        blocked_by = None
    else:
        segs, length = tangent_arc_path(
            start, end, blocker.center, blocker.radius + config.clearance_mm
        )
        blocked_by = blocker.instance
    return RealizedTrack(
        seg_id, net_id, layer, tuple(segs), length, blocked_by, width_mm
    )


def _vias_for_track(
    ir: PcbIR, track: RealizedTrack, config: RealizeConfig
) -> tuple[RealizedVia, ...]:
    """Vias for ONE realized track, wherever its routed layer differs from
    :data:`PAD_LAYER` — "emit a via wherever a net's realized route changes
    layer" (the master backlog's via-geometry gap), restated at this
    module's actual per-segment granularity: each :class:`RealizedTrack`
    carries exactly one layer, so "a route changes layer" here means "this
    track's layer isn't the layer its own two pads sit on", checked once
    per track rather than diffed against a neighbouring segment.

    A dog-bone stub is explicitly OUT of scope (module docstring: the
    via-to-plane connection is ``planes.py``'s job, a later module) and an
    unassigned (``UNSET_LAYER``) or already-pad-layer track has nothing to
    transition — both return no vias. Both checks, PLUS the via COUNT
    itself, are delegated to :func:`precis.pcb.rules.implied_via_count`
    rather than re-derived here — that function is the same predicate,
    hoisted out so :mod:`precis.pcb.cost`'s ``via_count`` MONEY term can share it and
    the two can never drift apart again (see that function's own docstring
    for the defect this closes: an optimizer that paid nothing for a via
    while this function quietly emitted real ones).

    Sized via the SAME resolver every other consumer uses
    (:func:`precis.pcb.rules.resolve_net_rules` — never a second sizing
    path); a fab process publishing no via figures (``via_dia_mm``/
    ``via_drill_mm`` both ``None``) means genuinely nothing to size
    against, not an invented default. The stitched group is spread along a
    straight line through each endpoint at via-pitch spacing (no real
    footprint/keepout geometry exists yet to route an array around), one
    group at the start point and one at the end point — both this track's
    own two pads need the same layer transition; ``implied_via_count``'s
    total is always even (one stitched group of the SAME size at each of
    the two endpoints), so splitting it in half below exactly recovers the
    per-endpoint count :func:`precis.pcb.rules.via_count_for_current`
    itself produced.
    """
    total = implied_via_count(
        ir,
        track.seg_id,
        fab_caps=config.fab_caps,
        class_rules=config.class_rules,
        temp_rise_c=config.temp_rise_c,
        copper_oz=config.copper_oz,
    )
    if total == 0:
        return ()
    rules = _resolve_track_rules(ir, track.net_id, track.layer, config)
    assert rules.via_dia_mm is not None and rules.via_drill_mm is not None, (
        "implied_via_count already returns 0 when either is unresolved"
    )
    layer_lo, layer_hi = min(PAD_LAYER, track.layer), max(PAD_LAYER, track.layer)
    n = total // 2
    pitch = rules.via_dia_mm + config.clearance_mm
    start = (float(track.segments[0]["start"][0]), float(track.segments[0]["start"][1]))
    end = (float(track.segments[-1]["end"][0]), float(track.segments[-1]["end"][1]))
    vias: list[RealizedVia] = []
    for endpoint, point in (("a", start), ("b", end)):
        for k in range(n):
            offset = (k - (n - 1) / 2.0) * pitch
            # These vias never touch the routing grid, so nothing else
            # keeps them off the board edge — see `_clamp_via_into_board`.
            vx, vy = _clamp_via_into_board(
                ir, config, point[0] + offset, point[1], rules.via_dia_mm
            )
            vias.append(
                RealizedVia(
                    seg_id=track.seg_id,
                    net_id=track.net_id,
                    x=vx,
                    y=vy,
                    dia_mm=rules.via_dia_mm,
                    drill_mm=rules.via_drill_mm,
                    layer_lo=layer_lo,
                    layer_hi=layer_hi,
                    endpoint=endpoint,
                )
            )
    return tuple(vias)


# ── per-gap capacity accounting + the legible congestion digest ─────────


@dataclass(frozen=True, slots=True)
class CongestionWarning:
    """A legible over-capacity gap — "bundle cannot pass between X and Y,
    gap N mm, needs M mm" (backlog acceptance criterion, verbatim shape)."""

    participants_key: tuple[int, ...]  # the instance-id neighbourhood sharing this gap
    gap_mm: float
    pitch_mm: float
    capacity: int
    usage: int
    nets: tuple[str, ...]

    @property
    def needed_mm(self) -> float:
        return self.usage * self.pitch_mm

    def message(self) -> str:
        return (
            f"gap {self.gap_mm:.3f} mm between instances {self.participants_key} "
            f"fits {self.capacity} strand(s) at {self.pitch_mm:.3f} mm pitch, but "
            f"{self.usage} want through ({', '.join(self.nets)}) — needs "
            f"{self.needed_mm:.3f} mm"
        )


@dataclass(frozen=True, slots=True)
class RealizeResult:
    tracks: tuple[RealizedTrack, ...]
    vias: tuple[RealizedVia, ...]
    warnings: tuple[CongestionWarning, ...]
    #: Segment ids the maze router could not route without crossing
    #: another net's copper. **This is the honest residue of a guaranteed-
    #: clean router**: the tangent drawer never reports an unrouted
    #: segment because it always draws something, even straight through
    #: three other nets. Always empty for ``router='tangent'``.
    unrouted: tuple[int, ...] = ()
    #: Copper pours, one per connected fragment of each plane-assigned
    #: layer (:mod:`precis.pcb.planes`). Empty when no net is promoted to a
    #: plane — which is the common case, and was the reason a promoted net
    #: being entirely disconnected went unnoticed for so long.
    pours: tuple[dict[str, Any], ...] = ()
    #: WHY each entry of ``unrouted`` failed — index-aligned by ``seg_id``,
    #: never by position (:func:`_realize_maze` builds ``unrouted`` and
    #: this together but does not promise they land in the same order).
    #: Closes "total routing failure produces no diagnostic at all"
    #: (docs/backlog/pcb-engine-plan.md "BOARD TWO" finding 1): before
    #: this, a caller saw a net name and nothing else, whether the real
    #: cause was a corridor that never existed, a race this connection
    #: lost to one that routed first, or an endpoint walled in on every
    #: allowed layer — three very different fixes wearing one word. Always
    #: empty for ``router='tangent'`` (nothing there is ever unrouted).
    unrouted_reasons: tuple[UnroutedReason, ...] = ()
    #: Plane-promoted nets :func:`_stitch_plane_fragments` could NOT bring
    #: to a single connected piece within its own bounded attempt (module
    #: docstring's stitching note, gripe 270637). Always empty for
    #: ``router='tangent'`` (that drawer never pours a plane at all) and
    #: usually empty for ``'maze'`` too — non-empty is this pass's own
    #: honest "tried and could not", independent of whatever
    #: :func:`precis.pcb.connectivity.net_islands` separately finds.
    unstitched: tuple[UnstitchedNet, ...] = ()


@dataclass(frozen=True, slots=True)
class UnroutedReason:
    """Why one segment ended up in :attr:`RealizeResult.unrouted` — a
    closed, small vocabulary a caller can branch on, rather than parsing
    prose. **Deliberately does NOT attempt the underlying width fix**
    (docs/backlog/pcb-engine-plan.md decision 3b: per-net current applied
    uniformly to every segment of a star topology over-widens every leaf —
    a real, large, unbuilt slice). This is the diagnostic half only: when
    the router fails, it must say why, distinguishable by kind.

    - ``'width'`` — the required copper (this net's trace width, or — at a
      layer transition — the via GROUP's full ampacity-sized extent) does
      not fit any corridor between the two endpoints, even with every
      OTHER net's copper cleared away. The connection was never going to
      route on this board's geometry regardless of routing order.
    - ``'congestion'`` — a corridor of the required width DOES exist with
      other nets' copper cleared away (the same probe that rules out
      ``'width'``), so this connection lost a race for it to a net that
      routed first. Rip-up/retry (:attr:`RealizeConfig.route_passes`)
      already re-orders failures to the front of the next attempt; a
      connection reported here survived every attempt anyway.
    - ``'no_path'`` — no path connects the two endpoints on the allowed
      layers AT ANY WIDTH (probed at a near-zero width with no via
      requirement). Not "too tight" — genuinely walled in, e.g. by a ring
      of keep-outs with no gap on any allowed layer.
    - ``'plane_drop_blocked'`` — this segment belongs to a plane-promoted
      net; the failure is one of its PINS never finding a legal drop-via
      site (:func:`_drop_via_site`) within its search radius, which is a
      different search entirely from the point-to-point route above.
    - ``'unpourable_plane'`` — this segment's net is plane-promoted and
      the router found everything it needed to, but the board has no
      outline to pour a plane INTO (:func:`_pour_planes`), so the
      connection has nowhere to land. Was previously folded into the
      generic ``unrouted`` bucket with no distinguishing cause at all —
      see docs/backlog/pcb-engine-plan.md's "BOARD TWO" finding 2's
      sibling note.
    """

    seg_id: int
    net_id: int
    kind: str
    message: str


@dataclass(frozen=True, slots=True)
class UnstitchedNet:
    """One plane-promoted net :func:`_stitch_plane_fragments` could NOT
    bring to a single piece within its own bounded attempt — this pass's
    own honest failure report, independent of whatever
    :func:`precis.pcb.connectivity.net_islands` separately finds on the
    same board (module docstring's independence note: this is the
    PRODUCER's signal, not a restatement of the checker's verdict).
    Reported so a caller sees a concrete "tried and could not", never
    silence standing in for "checked, clean" — the same "fail legibly"
    discipline :class:`UnroutedReason` already applies to the router half
    of this module."""

    net_id: int
    net: str
    #: Distinct connected pieces remaining after the bounded attempt.
    #: Always >= 2 — a net with 0 or 1 pieces is never reported here.
    fragments: int
    #: How many of ``fragments`` are poured copper with NONE of this net's
    #: own vias or traces inside them.
    #:
    #: **Two different defects wear the same piece count, and only this
    #: field separates them.** An island holding a via or a trace is an
    #: ELECTRICAL split: part of the net cannot reach the rest, which is
    #: what :func:`precis.pcb.connectivity.net_islands` reports and what
    #: makes a board unfinished. An island holding nothing is FLOATING
    #: copper: undesirable (an unreferenced plate is an antenna, and it
    #: wastes area) but electrically inert, and invisible to that checker
    #: for the honest reason that a pour is a joiner in its model, never a
    #: node. Reporting the second as the first is how a real routing
    #: failure and a cosmetic pour artefact end up sharing one alarm.
    bare_fragments: int
    message: str


def _gap_usage(
    ir: PcbIR, seg_ids: list[int], config: RealizeConfig
) -> list[CongestionWarning]:
    buckets: dict[tuple[int, ...], list[int]] = {}
    gaps: dict[tuple[int, ...], float] = {}
    for seg_id in seg_ids:
        found = nearest_other_instance(ir, seg_id)
        if found is None:
            continue
        gap_mm, nearest_id = found
        a, b = int(ir.seg_pin_a[seg_id]), int(ir.seg_pin_b[seg_id])
        ia, ib = int(ir.pin_instance[a]), int(ir.pin_instance[b])
        key = tuple(sorted({ia, ib, nearest_id}))
        buckets.setdefault(key, []).append(seg_id)
        gaps[key] = min(gaps.get(key, math.inf), gap_mm)

    warnings: list[CongestionWarning] = []
    for key, members in buckets.items():
        gap_mm = gaps[key]
        capacity = max(0, math.floor(gap_mm / config.pitch_mm))
        usage = len(members)
        if usage > capacity:
            nets = tuple(str(ir.net_name[int(ir.seg_net[s])]) for s in members)
            warnings.append(
                CongestionWarning(key, gap_mm, config.pitch_mm, capacity, usage, nets)
            )
    return warnings


# ── the maze router ───────────────────────────────────────────────────────


def _signal_layers(ir: PcbIR) -> list[int]:
    """Stackup indices that can carry a routed trace —
    :func:`precis.pcb.ir.routable_layers` (the single answer, no longer
    duplicated: this used to keep its own four-line copy rather than
    import :mod:`precis.pcb.session`'s, on layering grounds; the query
    itself has since moved down to :mod:`precis.pcb.ir`, which both this
    module and ``session.py`` already depend on, so there is no longer a
    second copy to drift). Falls back to the pad layer for a stackup that
    declares no roles/routable flags at all, so a synthetic IR still
    routes somewhere rather than nowhere."""
    return routable_layers(ir) or [PAD_LAYER]


def _layer_preferences(ir: PcbIR, signal_layers: list[int]) -> dict[int, str]:
    """Per-group H/V/diagonal axis assignment
    (:func:`precis.pcb.maze.preferred_directions`), computed separately
    for the OUTER and INNER members of ``signal_layers`` rather than once
    over the whole routable set.

    ``preferred_directions`` cycles H, V, diagonal, H, ... in STACKUP
    INDEX order over whatever list it is given — feeding it the whole
    routable set in one call means an outer layer (always the lowest
    index once it is ALSO routable) claims 'H' first and pushes every
    inner layer's axis one further round the cycle for each outer layer
    that precedes it. That is invisible today (:data:`precis.pcb.
    DEFAULT_STACKUP`: only F.Cu/B.Cu are routable, and a single call over
    ``[0, 3]`` already gives F.Cu='H', B.Cu='V') but wrong the moment an
    inner layer becomes ALSO routable — this module's new "a layer can
    carry BOTH traces and copper fill" capability: a board with all four
    layers routable and no split would give F.Cu='H', In1.Cu='V',
    In2.Cu='diagonal', B.Cu='H', not the In1='H'/In2='V' a caller who
    explicitly marked exactly those two layers routable actually asked
    for.

    Splitting by :func:`precis.pcb.rules.layer_is_outer` (the SAME
    inner/outer question :mod:`precis.pcb.rules`'s IPC-2221 width
    resolver already asks, reused rather than re-derived) restarts the
    H/V/diagonal cycle at each group's own first (lowest-index) member,
    so In1.Cu — the first INNER routable layer — gets 'H' and In2.Cu gets
    'V' regardless of how many outer layers are also routable. Two calls
    into a pure, order-preserving function and a dict merge; no change to
    :mod:`precis.pcb.maze` itself."""
    outer = [layer for layer in signal_layers if layer_is_outer(ir, layer)]
    inner = [layer for layer in signal_layers if not layer_is_outer(ir, layer)]
    return {**maze.preferred_directions(outer), **maze.preferred_directions(inner)}


def _outline_clip(
    ir: PcbIR, clearance_mm: float
) -> tuple[float, float, float, float] | None:
    """The board rectangle copper may occupy: the outline's bounding box
    inset by the copper-to-edge clearance. ``None`` when the design has
    authored no outline — there is then no board edge to respect, and
    inventing one (a bounding box around the parts, say) would silently
    constrain a design that never asked to be constrained.

    A bounding box is an over-approximation for a non-rectangular
    outline: copper can still land inside the bbox but outside a
    concave/rounded board. That is the same approximation
    ``cost.outline_bbox`` and the placer's own bounds already make, so it
    is at least consistent; a true point-in-polygon clip belongs with
    whatever first needs a non-rectangular board.
    """
    if not ir.outline or len(ir.outline) < 3:
        return None
    xs = [float(p[0]) for p in ir.outline]
    ys = [float(p[1]) for p in ir.outline]
    x0, y0 = min(xs) + clearance_mm, min(ys) + clearance_mm
    x1, y1 = max(xs) - clearance_mm, max(ys) - clearance_mm
    if x1 <= x0 or y1 <= y0:
        return None  # an outline smaller than its own clearance — no clip
    return (x0, y0, x1, y1)


def _board_edge_min_mm(config: RealizeConfig) -> float:
    """The fab's copper-to-board-edge figure, house tier where published.

    Hoisted so the routing grid's inset, and every via placed OUTSIDE that
    grid, read the one value — ``drc.check_board_edge_clearance`` reads the
    same ``board_edge_clearance_*_mm`` field, and a second copy of this
    lookup is how a placement site ends up disagreeing with the checker
    that grades it.
    """
    return float(
        config.fab_caps.house_default.get("board_edge_clearance_vcut_mm")
        or config.fab_caps.jlc_min.get("board_edge_clearance_vcut_mm")
        or 0.0
    )


def _clamp_via_into_board(
    ir: PcbIR, config: RealizeConfig, x: float, y: float, via_dia_mm: float
) -> tuple[float, float]:
    """Pull a via centre inside the board-edge inset for its OWN diameter.

    **Why a clamp and not a check.** Ampacity vias
    (:func:`_current_vias`) are anchored to a track ENDPOINT — a pad
    coordinate — and never consult the routing grid, so
    :func:`_outline_clip`'s inset, which is what keeps every grid-placed
    via legal, simply does not apply to them. A pad placed legally can
    still sit nearer the edge than a via hung off it may go, and the
    result was measured on the 40mm reference fixture: a VCC3V3 via
    0.010mm inside the 0.400mm V-cut floor, reported by the checker and
    fixable by nothing upstream, because no code owned the question.

    The group is already spread off its anchor by design (see
    ``_current_vias``), so a small correction keeps it on its own pad's
    copper. A LARGE one would not — a via dragged far from its endpoint
    stops carrying current between layers, which is the whole reason it
    exists. That case is left to :func:`precis.pcb.drc.check_connectivity`
    to report rather than silently accepted here: a clamp that quietly
    detaches a via would trade a reported defect for an unreported one.
    """
    clip = _outline_clip(ir, _board_edge_min_mm(config) + via_dia_mm / 2.0)
    if clip is None:
        return x, y
    cx0, cy0, cx1, cy1 = clip
    return min(max(x, cx0), cx1), min(max(y, cy0), cy1)


def _seg_span_mm(ir: PcbIR, seg_id: int) -> float:
    a, b = int(ir.seg_pin_a[seg_id]), int(ir.seg_pin_b[seg_id])
    pa, pb = pin_point(ir, a), pin_point(ir, b)
    if pa is None or pb is None:
        return math.inf
    return dist(pa, pb)


def _realize_maze(
    ir: PcbIR,
    ids: list[int],
    config: RealizeConfig,
    footprints: dict[str, dict[str, Any]] | None = None,
) -> tuple[
    list[RealizedTrack],
    list[RealizedVia],
    list[int],
    list[dict[str, Any]],
    list[UnroutedReason],
    list[UnstitchedNet],
]:
    """Route ``ids`` on a shared occupancy grid — see :mod:`precis.pcb.
    maze` for why this cannot emit overlapping copper, and what it gives
    up in exchange (unrouted segments, reported not hidden).

    **Rip-up and retry, by re-ordering.** One shortest-first pass is
    order-dependent: a connection fails because earlier ones took the
    corridor it needed, not because no route exists. So the whole pass is
    re-run on a fresh grid with the failures moved to the front, up to
    ``config.route_passes`` times, and the best result wins. This is
    PathFinder's idea (Ebeling & McMurchie 1995) in its crudest form —
    history-based *ordering* rather than history-based *cost* — chosen
    because the occupancy grid has no incremental un-claim: a path's cells
    are stamped, not owned, so ripping one net out of a settled grid is
    not a cheap operation, while re-running a 0.5s pass is.

    Every pass is individually clean by construction, so this trades
    runtime for routability and never for correctness — a later pass
    cannot introduce an overlap a single pass would have refused.
    """
    signal_layers = _signal_layers(ir)
    # Each pad's own keep-out radius, not one flat constant for every pin
    # regardless of package — see PcbIR.pin_w's docstring for the defect
    # this closes. `pad_geometry` is the ONE place that decides real vs
    # synthesized per pin; the router only ever asks it for a size.
    pad_geoms = pad_geometry(ir, footprints)
    pads: list[tuple[Point, int, float]] = []
    for pid in range(ir.n_pins):
        point = pin_point(ir, pid)
        if point is not None:
            geom = pad_geoms[pid]
            # A conservative ENCLOSING circle, not the true (possibly
            # rectangular) footprint — the router's occupancy grid only
            # ever queries/claims disks (see maze.OccupancyGrid), and a
            # real rect-vs-rect keep-out would need a second geometry
            # engine inside the grid for a gain that doesn't matter here:
            # a claim slightly larger than the true copper costs a little
            # routability, never a clearance violation, which is the safe
            # direction to be conservative in.
            radius = math.hypot(geom.w_mm, geom.h_mm) / 2.0
            net = int(ir.pin_net[pid])
            if net == NO_NET:
                # LATENT DEFECT, found while testing this change (not part
                # of items 1-3, fixed in passing because it lives in this
                # exact loop): NO_NET is -1, the SAME value as
                # maze.FREE. Stamping an unconnected (test-point/NC/
                # mounting-hole) pin's pad under net=-1 makes it read as
                # genuinely EMPTY board to every other net's `owner !=
                # FREE` foreign-copper test — the pad is real copper that
                # the router could then route straight through with zero
                # clearance. Give every unconnected pin its own sentinel,
                # guaranteed distinct from FREE (-1), CONTESTED (-2), and
                # every real net id (0..n_nets-1) — and distinct PER PIN,
                # so two NC pads are never treated as one shared net
                # either (each is its own island of copper that nothing,
                # including another NC pad, may route through).
                net = ir.n_nets + pid
            pads.append((point, net, radius))
    if not pads:
        return [], [], list(ids), [], [], []

    rules_by_net = {
        n: _resolve_track_rules(ir, n, PAD_LAYER, config) for n in range(ir.n_nets)
    }
    if not rules_by_net:
        return [], [], list(ids), [], [], []
    clearance = max(
        config.clearance_mm, max(r.clearance_mm for r in rules_by_net.values())
    )
    # The board-edge inset is a DIFFERENT figure from inter-net clearance
    # (drc.check_board_edge_clearance reads board_edge_clearance_*_mm), and
    # it applies to the copper's edge, so half the widest COPPER FEATURE has
    # to come off it too. Take the house tier where the fab publishes one,
    # so the result clears the advisory threshold and not merely the hard
    # one.
    edge_min = _board_edge_min_mm(config)
    # A via is not a track (see maze.py's module docstring): its copper is
    # an annulus at its OWN diameter, not the routing net's track width, and
    # nothing downstream of this inset is via-aware — `grid.route`'s via
    # search and `_shove_vias` both place a via anywhere the shared
    # occupancy grid says is in-bounds, with no separate edge test of their
    # own. `_outline_clip`'s bounds (fed straight into `maze.grid_for`) are
    # therefore the ONE place "how far from the board edge may copper be"
    # gets decided, for a track's centreline AND a via's centre alike — a
    # via-blind inset here silently reused a track-sized margin for a
    # via-sized hole. Widening the shared clip to the widest of every net's
    # track width OR via diameter (rather than adding a second, via-only
    # edge check at each placement site) keeps that one definition true:
    # a per-site check would have to be threaded through both `maze.
    # OccupancyGrid.route`'s via mask and `_shove_vias`'s candidate test,
    # duplicating the same "distance to true edge" arithmetic this
    # subsystem has already drifted out of step on once (see maze.py's
    # via-vs-track mask history). The cost is symmetric: every track's
    # routable margin also grows to the widest via's, not just the widest
    # track's, giving up a little edge-adjacent routability on nets that
    # never place a via there — the same "conservative is the safe
    # direction" trade `stamp_disk`'s pad radius already makes.
    widest = max(
        max(r.track_width_mm for r in rules_by_net.values()),
        max(
            (r.via_dia_mm for r in rules_by_net.values() if r.via_dia_mm is not None),
            default=0.0,
        ),
    )
    edge_inset = max(clearance, edge_min) + widest / 2.0
    spec = maze.grid_for(
        [p for p, _, _ in pads],
        n_layers=len(ir.stackup) or 1,
        bounds=_outline_clip(ir, edge_inset),
    )
    plane_ids = [s for s in ids if int(ir.net_plane_layers[int(ir.seg_net[s])]) != 0]
    route_ids = [s for s in ids if s not in set(plane_ids)]
    # Shortest-first is the opening order. A short connection has the
    # fewest alternative corridors, so letting a long one claim the board
    # first strands it; a heuristic, not a guarantee, and the guarantee
    # (no overlapping copper) does not depend on it.
    order = sorted(route_ids, key=lambda s: _seg_span_mm(ir, s))
    best: tuple[list[RealizedTrack], list[RealizedVia], list[int]] | None = None
    for _attempt in range(max(1, config.route_passes)):
        outcome = _route_pass(
            ir,
            order,
            plane_ids,
            maze.OccupancyGrid(spec, clearance_mm=clearance),
            config,
            rules_by_net,
            pads,
            clearance,
            signal_layers,
            spec,
            pad_geoms,
        )
        if best is None or len(outcome[2]) < len(best[2]):
            best = outcome
        if not outcome[2]:
            break
        # Failures go to the front for the next attempt, keeping their
        # relative order. A connection that has already lost a race gets
        # first refusal on the next one.
        failed = [s for s in order if s in set(outcome[2])]
        order = failed + [s for s in order if s not in set(failed)]
    assert best is not None  # the loop runs at least once
    tracks, vias, unrouted = best
    pours, extra_unrouted = _pour_planes(
        ir, tracks, vias, unrouted, plane_ids, clearance, edge_inset, footprints
    )
    # Deliberate stitching vias, AFTER pouring -- this pass needs the
    # finished pour polygons to know what it is joining (module docstring's
    # "Plane fragment stitching" note, gripe 270637).
    stitch_vias, stitch_tracks, unstitched = _stitch_plane_fragments(
        ir,
        tracks,
        vias,
        pours,
        spec=spec,
        clearance=clearance,
        pads=pads,
        rules_by_net=rules_by_net,
        config=config,
    )
    vias = vias + stitch_vias
    # Jumper traces join the board AFTER pouring, deliberately: they are
    # this net's own copper on a spare layer and are cleared against every
    # foreign pour geometrically (`_clears_foreign_pours`), so they need no
    # antipad cut and must not re-trigger one.
    tracks = tracks + stitch_tracks
    reasons = _diagnose_all(
        ir,
        unrouted,
        extra_unrouted,
        set(plane_ids),
        spec,
        pads,
        clearance,
        signal_layers,
        rules_by_net,
        config.max_expansions,
    )
    return tracks, vias, unrouted + extra_unrouted, pours, reasons, unstitched


def _diagnose_all(
    ir: PcbIR,
    unrouted: list[int],
    extra_unrouted: list[int],
    plane_id_set: set[int],
    spec: maze.GridSpec,
    pads: list[tuple[Point, int, float]],
    clearance: float,
    signal_layers: list[int],
    rules_by_net: dict[int, NetRules],
    max_expansions: int,
) -> list[UnroutedReason]:
    """One :class:`UnroutedReason` per segment in ``unrouted +
    extra_unrouted`` — split out of :func:`_realize_maze` purely to keep
    that function's already-long body from growing further, not because
    anything here is reused elsewhere."""
    reasons: list[UnroutedReason] = []
    for seg_id in unrouted:
        net_id = int(ir.seg_net[seg_id])
        if seg_id in plane_id_set:
            # This segment never went through `grid.route` at all -- see
            # `_route_pass`'s `plane_ids`/`route_ids` split -- so its
            # failure is a PIN's drop-via search coming up empty
            # (`_plane_fanout`/`_drop_via_site`), a different search
            # entirely from the point-to-point one `_diagnose_unrouted`
            # re-runs. Diagnosing it as a route-search failure would ask
            # the wrong question and could report a misleading answer.
            reasons.append(
                UnroutedReason(
                    seg_id,
                    net_id,
                    "plane_drop_blocked",
                    "this plane-promoted net had at least one pin with no "
                    "legal drop-via site within its search radius (blocked by "
                    "other copper) -- not a point-to-point route failure",
                )
            )
            continue
        rules = rules_by_net[net_id]
        n_vias, group_extent = _via_group_extent(ir, net_id, rules, clearance)
        reasons.append(
            _diagnose_unrouted(
                ir,
                seg_id,
                spec,
                pads,
                clearance,
                signal_layers,
                rules,
                n_vias,
                group_extent,
                max_expansions,
            )
        )
    for seg_id in extra_unrouted:
        net_id = int(ir.seg_net[seg_id])
        reasons.append(
            UnroutedReason(
                seg_id,
                net_id,
                "unpourable_plane",
                f"net {ir.net_name[net_id]} is plane-promoted but the board has "
                "no outline to pour a plane into, so this connection has "
                "nowhere to land",
            )
        )
    return reasons


def _stamp_pads(grid: maze.OccupancyGrid, pads: list[tuple[Point, int, float]]) -> None:
    """Claim every pad on ``grid`` — the router's static baseline, factored
    out so a diagnostic probe (:func:`_diagnose_unrouted`) can build the
    SAME starting grid a real route pass would, minus every other net's
    ROUTED copper, rather than hand-rolling a second copy of this.

    Pads are claimed on the PAD LAYER only — an SMD pad is copper on one
    layer, and blocking all four would make every inner layer unusable
    underneath exactly the fine-pitch parts that need the escape. (A
    through-hole pad does block every layer; the IR carries no SMD/THT
    distinction yet, so this picks the assumption that keeps inner layers
    routable rather than the one that silently doesn't.) Two passes: the
    core first, where two nets' pads collide the cell goes to NEITHER
    (``maze.CONTESTED``), then each pad's inner disk is re-asserted so its
    owner always has a cell to start a route from. Each pad brings its OWN
    radius (``pads``' third element, from :func:`pad_geometry` — real
    footprint size where supplied, package-family synthesis otherwise)
    rather than every pad on the board claiming the same disc.

    **The second pass calls :meth:`~precis.pcb.maze.OccupancyGrid.
    stamp_pad`, never plain ``stamp_disk``** — found 2026-08-29 as a live,
    currently-reproducible defect (gripe 269811 comment 2): this loop used
    to call ``stamp_disk`` directly, which claims the SAME copper on
    ``grid._owner`` but never appends to ``grid._pads``. Every consumer of
    a pad's true footprint as a KEEP-OUT rather than an occupancy claim —
    :meth:`~precis.pcb.maze.OccupancyGrid.via_clears_pads` (called by
    :func:`_drop_via_site`, the plane fan-out's own drop-via search) and
    :meth:`~precis.pcb.maze.OccupancyGrid._pad_keepout_mask` (folded into
    :meth:`~precis.pcb.maze.OccupancyGrid.route`'s own via candidate mask)
    — reads ONLY ``grid._pads``. With that list permanently empty, both
    guards were vacuously true for every pad on every board this module
    has ever routed: ``via_clears_pads`` allowed a drop via to land
    directly on (or well inside) its own pin's pad, or a neighbour's,
    because it had nothing to check against. Measured on the ESP32-C3
    reference fixture with GND/VCC3V3 plane-promoted: 55 of 57
    ``via_pad_keepout`` DRC findings were exactly this — the fix is this
    one entry-point swap, not a new keep-out rule (the rule already
    existed and was already correct; it just never received any data).
    The first (CONTESTED) pass stays on plain ``stamp_disk`` unchanged —
    :meth:`stamp_pad`'s own docstring is explicit that the pre-pass is a
    collision marker, not a pad's true footprint, and recording it in
    ``_pads`` too would double-count one pad as two keep-out entries.
    """
    for point, net, radius in pads:
        grid.stamp_disk(
            (PAD_LAYER,),
            point[0],
            point[1],
            grid.core_radius_mm(2.0 * radius),
            net,
            contest=True,
        )
    for point, net, radius in pads:
        grid.stamp_pad((PAD_LAYER,), point[0], point[1], radius, net)


def _via_group_extent(
    ir: PcbIR, net_id: int, rules: NetRules, clearance: float
) -> tuple[int, float | None]:
    """``(n_vias, group_extent_mm)`` for a layer change on ``net_id`` — the
    exact figure :func:`_route_pass` feeds ``grid.route``'s ``via_dia_mm``
    with, hoisted out so :func:`_diagnose_unrouted` asks the search the
    same question it already asked rather than a smaller, unvalidated one."""
    n_vias = (
        via_count_for_current(
            net_current_a_or_none(float(ir.net_current_a[net_id])),
            rules.via_dia_mm,
        )
        if rules.via_dia_mm is not None
        else 1
    )
    group_extent = (
        None
        if rules.via_dia_mm is None
        else n_vias * rules.via_dia_mm + (n_vias - 1) * clearance
    )
    return n_vias, group_extent


#: The probe width used to test "does ANY path exist, ignoring how wide
#: the real trace needs to be" — see :func:`_diagnose_unrouted`. Not zero:
#: `grid.route` still queries a disk, and a true zero-radius query can slip
#: between cells the real (nonzero-width) search never could, which would
#: report a path where none usably exists. A quarter of a cell is
#: negligible copper and still exercises the same dilation machinery.
_PROBE_WIDTH_FRACTION_OF_PITCH = 0.25


def _diagnose_unrouted(
    ir: PcbIR,
    seg_id: int,
    spec: maze.GridSpec,
    pads: list[tuple[Point, int, float]],
    clearance: float,
    signal_layers: list[int],
    rules: NetRules,
    n_vias: int,
    group_extent: float | None,
    max_expansions: int,
) -> UnroutedReason:
    """WHY ``seg_id`` never routed, in one of :class:`UnroutedReason`'s
    three route-search categories — 'BOARD TWO' finding 1's diagnostic gap
    (docs/backlog/pcb-engine-plan.md), closed without touching the width
    MODEL that produces the failure (decision 3b, explicitly out of scope
    here).

    **The technique: a fresh grid with only pads claimed — no other net's
    routed copper — answers "does a corridor exist at all", independent of
    routing order.** Re-running the real search on that empty-of-congestion
    grid at the connection's REAL required width (trace width, or — at a
    layer transition — the via GROUP's full extent, the same figure
    :func:`_route_pass` already feeds ``grid.route``) either finds a path
    (a corridor DOES exist physically, so the failure on the busy grid was
    losing a race: ``'congestion'``) or does not. If it does not, a second
    probe at a near-zero width with no via requirement asks the weaker
    question — does ANY path exist, at ANY width: if that also fails, the
    endpoint is topologically unreachable on the allowed layers
    (``'no_path'``); if it succeeds, a path exists but not one wide enough
    for this net (``'width'``).
    """
    net_id = int(ir.seg_net[seg_id])
    a, b = int(ir.seg_pin_a[seg_id]), int(ir.seg_pin_b[seg_id])
    start, end = pin_point(ir, a), pin_point(ir, b)
    if start is None or end is None:
        return UnroutedReason(
            seg_id,
            net_id,
            "no_path",
            "this connection's endpoint has no placed (x, y) yet — nothing to route",
        )
    probe = maze.OccupancyGrid(spec, clearance_mm=clearance)
    _stamp_pads(probe, pads)
    clear_path = probe.route(
        net_id,
        start,
        end,
        layers=signal_layers,
        width_mm=rules.track_width_mm,
        via_dia_mm=group_extent,
        pad_layer=PAD_LAYER,
        attach=False,
        max_expansions=max_expansions,
    )
    if clear_path is not None:
        via_note = (
            f", stitching {n_vias} via(s) at a layer change" if group_extent else ""
        )
        return UnroutedReason(
            seg_id,
            net_id,
            "congestion",
            f"a corridor wide enough for this net ({rules.track_width_mm:.3f}mm"
            f"{via_note}) exists between its endpoints once every OTHER net's "
            "copper is cleared away, so this connection lost a race for it to "
            "a net that routed first (rip-up/retry already re-tries failures "
            "first on the next pass)",
        )
    thin_path = probe.route(
        net_id,
        start,
        end,
        layers=signal_layers,
        width_mm=spec.pitch * _PROBE_WIDTH_FRACTION_OF_PITCH,
        via_dia_mm=None,
        pad_layer=PAD_LAYER,
        attach=False,
        max_expansions=max_expansions,
    )
    if thin_path is not None:
        via_note = (
            f" (including a {group_extent:.3f}mm via GROUP at a layer change)"
            if group_extent
            else ""
        )
        return UnroutedReason(
            seg_id,
            net_id,
            "width",
            f"a path exists between this connection's endpoints, but this net's "
            f"required width ({rules.track_width_mm:.3f}mm{via_note}) does not "
            "fit any corridor along it — the width itself is the blocker, not "
            "routing order",
        )
    return UnroutedReason(
        seg_id,
        net_id,
        "no_path",
        "no path connects this connection's endpoints on the allowed layers at "
        "any width — the endpoint is walled in, not merely too tight",
    )


def _route_pass(
    ir: PcbIR,
    order: list[int],
    plane_ids: list[int],
    grid: maze.OccupancyGrid,
    config: RealizeConfig,
    rules_by_net: dict[int, NetRules],
    pads: list[tuple[Point, int, float]],
    clearance: float,
    signal_layers: list[int],
    spec: maze.GridSpec,
    pad_geoms: list[PadGeom],
) -> tuple[list[RealizedTrack], list[RealizedVia], list[int]]:
    """One complete routing attempt onto a fresh ``grid``, in ``order``."""
    _stamp_pads(grid, pads)

    tracks: list[RealizedTrack] = []
    vias: list[RealizedVia] = []
    unrouted: list[int] = []

    # Plane-served segments are dog-bone stubs, not searched routes — but
    # they ARE copper, so they get realized (and claimed) first, before
    # any route can be planned through where they sit.
    plane_tracks, plane_vias, plane_failed = _plane_fanout(
        ir, plane_ids, grid, config, rules_by_net, pad_geoms
    )
    tracks += plane_tracks
    vias += plane_vias
    # A pin with no legal drop leaves its net's segments unserved. Report
    # the segments, because that is the unit `unrouted` is counted in and
    # the unit a reader can act on.
    if plane_failed:
        stranded = {int(ir.pin_net[p]) for p in plane_failed}
        unrouted += [s for s in plane_ids if int(ir.seg_net[s]) in stranded]

    for seg_id in order:
        a, b = int(ir.seg_pin_a[seg_id]), int(ir.seg_pin_b[seg_id])
        start, end = pin_point(ir, a), pin_point(ir, b)
        if start is None or end is None:
            continue  # no L3 position yet — nothing to draw, not a failure
        net_id = int(ir.seg_net[seg_id])
        rules = rules_by_net[net_id]
        # How much room ONE layer change costs this net. A via group is
        # sized by ampacity, not by geometry: a 5A rail cannot cross
        # layers through a single via, and a router that plans for one
        # and then stitches four has just put three of them through
        # somebody else's copper. So the search is told the group's full
        # extent up front and only changes layer where the whole group
        # fits.
        n_vias, group_extent = _via_group_extent(ir, net_id, rules, clearance)
        path = grid.route(
            net_id,
            start,
            end,
            layers=signal_layers,
            width_mm=rules.track_width_mm,
            via_dia_mm=group_extent,
            pad_layer=PAD_LAYER,
            max_expansions=config.max_expansions,
            layer_prefs=(
                _layer_preferences(ir, signal_layers)
                if config.preferred_directions
                else None
            ),
        )
        if path is None or len(path.points) < 2:
            unrouted.append(seg_id)
            continue
        path = _snap_to_pads(path, start, end, spec.pitch)
        if config.straighten:
            path = _straighten(
                path,
                grid,
                net_id,
                rules.track_width_mm,
                shove_vias=config.shove_vias,
                via_group_extent_mm=group_extent,
                via_shove_radius_mm=config.via_shove_radius_mm,
            )
        grid.stamp_path(path, rules.track_width_mm)
        via_r = (
            grid.core_radius_mm(rules.via_dia_mm)
            if rules.via_dia_mm is not None
            else 0.0
        )
        for vx, vy, lo, hi in path.vias:
            if rules.via_dia_mm is None or rules.via_drill_mm is None:
                continue  # this fab publishes no via figures — don't invent any
            # The stitched group, spread along x at the transition. The
            # search already cleared a disk of the group's full extent
            # there (`group_extent` above), so every member lands in
            # cleared space rather than the first one landing legally and
            # the rest wherever they fall.
            pitch = rules.via_dia_mm + clearance
            for k in range(n_vias):
                gx = vx + (k - (n_vias - 1) / 2.0) * pitch
                grid.stamp_disk(range(0, spec.n_layers), gx, vy, via_r, net_id)
                vias.append(
                    RealizedVia(
                        seg_id=seg_id,
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
            # A group of n>1 is spread AROUND the transition point, so for
            # even n no via sits where the trace ends and for odd n only the
            # middle one does: the outer annuli are copper islands and the
            # ampacity the group was sized for does not exist. Join them
            # with a bar, on each layer the trace is actually on. It runs
            # inside the group extent the search already cleared, at the
            # annulus width, so it adds no new clearance obligation.
            if n_vias > 1 and rules.via_dia_mm is not None:
                half = (n_vias - 1) / 2.0 * (rules.via_dia_mm + clearance)
                for layer in (lo, hi):
                    tracks += _track_from_run(
                        seg_id,
                        net_id,
                        layer,
                        [(vx - half, vy), (vx + half, vy)],
                        rules.via_dia_mm,
                    )
        tracks.extend(
            _tracks_from_path(
                seg_id,
                net_id,
                path,
                rules.track_width_mm,
                fillet_radius_mm=config.fillet_radius_tracks * rules.track_width_mm,
            )
        )
    return tracks, vias, unrouted


def _pad_blockers(
    ir: PcbIR, layers: list[str], footprints: dict[str, dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """Every placed pad, reshaped into a ``model["copper"]`` item
    :func:`precis.pcb.drc._copper_item_polygon` already knows how to turn
    into a polygon — so :func:`_pour_planes` can hand it to
    :func:`~precis.pcb.planes.plane_pours` as just another blocker,
    without ``planes.py`` (or ``drc.py``, neither of which this module
    may edit) ever having to learn a fourth ``ctype``.

    **Why a pad has to be a blocker at all.** ``to_gerber_model``'s
    ``model["copper"]`` is tracks/vias/pours only — real pin copper is a
    SEPARATE ``model["pads"]`` key (:func:`pads_for_ir`'s own docstring).
    :func:`_pour_planes` used to pass only ``["copper"]`` to
    ``plane_pours``, so a fill flowed around a via's antipad but straight
    OVER every pad on its layer, merging every net's pads on a filled
    layer into the fill net — on a board with GND filled on F.Cu, that is
    every net on the board shorted to GND. Found from the render, not
    from a check: a pad's antipad was missing entirely, not merely the
    wrong shape.

    A circular pad fakes a single-layer ``via`` (its own ``dia_mm``,
    pinned to just this one layer through the ``layers`` override
    :func:`~precis.pcb.drc._via_layer_names` reads before ``span`` — no
    stackup-index lookup needed for a pad that never spans anything). A
    rectangular pad fakes a ``pour`` (its four corners, axis-aligned, as
    ``polygon`` — the same enclosing-rectangle-in-board-frame treatment
    :func:`pad_geometry` already gives every pad's KEEPOUT; a true
    per-footprint rotation is a finer shape than anything else in this
    router tracks). Neither fake item is buffered here:
    ``plane_pours`` buffers every blocker by its own ``clearance_mm``
    uniformly, so doing it again here would double it.

    ``plane_pours`` already skips a blocker whose ``net`` matches the
    pour's own net (own-net copper is the connection, not an obstacle) —
    the SAME check that keeps a GND pad merged into a GND pour applies to
    these fakes for free, because they carry ``net`` too. An unconnected
    pin's pad (``net=""``, :func:`pads_for_ir`'s own NO_NET handling)
    therefore correctly gets an antipad on every real net's plane, since
    ``""`` never equals a real net name — exactly right: an NC pad is not
    part of the fill.
    """
    out: list[dict[str, Any]] = []
    for pad in pads_for_ir(ir, layers, footprints):
        net = str(pad.get("net") or "")
        layer = str(pad["layer"])
        x, y = float(pad["x"]), float(pad["y"])
        if pad.get("shape") == "circle":
            out.append(
                {
                    "ctype": "via",
                    "net": net,
                    "x": x,
                    "y": y,
                    "dia_mm": float(pad["w"]),
                    "layers": [layer],
                }
            )
            continue
        half_w = float(pad["w"]) / 2.0
        half_h = float(pad.get("h", pad["w"])) / 2.0
        out.append(
            {
                "ctype": "pour",
                "net": net,
                "layer": layer,
                "polygon": [
                    [x - half_w, y - half_h],
                    [x + half_w, y - half_h],
                    [x + half_w, y + half_h],
                    [x - half_w, y + half_h],
                ],
            }
        )
    return out


def _pour_planes(
    ir: PcbIR,
    tracks: list[RealizedTrack],
    vias: list[RealizedVia],
    unrouted: list[int],
    plane_ids: list[int],
    clearance: float,
    edge_inset: float,
    footprints: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[int]]:
    """Pour every plane-assigned layer over the FINISHED copper.

    Last, because a pour is defined by what it has to avoid and cannot be
    computed before the thing it avoids exists. The copper list is built by
    :func:`to_gerber_model` rather than assembled here, so "what shape is a
    track" has one answer — plus every placed PAD (:func:`_pad_blockers`,
    its own docstring has the defect this closes), which ``to_gerber_model``
    does NOT fold into ``model["copper"]``. Returns the pours and any
    additional unrouted segments the pouring revealed.
    """
    layer_names = [str(layer.get("name")) for layer in ir.stackup]
    # A plane LAYER carries one net (optimize._gen_plane_promote enforces
    # it) — but one NET may now carry several layers (net_plane_layers is
    # a bitmask, module docstring), so this is keyed by layer, not net:
    # a net's name can legally appear as the value for several different
    # layer_idx keys at once, one dict entry per poured sheet of copper.
    # If an externally-authored IR ever contradicts the one-net-per-layer
    # half of that rule, the LOWEST net id wins for that layer —
    # deterministically, so two runs of the same board pour the same
    # copper. Every other net on that layer then shows up as disconnected
    # in check_connectivity rather than as a plausible board, which is the
    # reporting direction that gets the contradiction fixed.
    plane_nets: dict[int, str] = {}
    for n in range(ir.n_nets):
        for layer_idx in plane_layers_of(int(ir.net_plane_layers[n])):
            plane_nets.setdefault(layer_idx, str(ir.net_name[n]))
    pours: list[dict[str, Any]] = []
    if plane_nets and ir.outline:
        interim = RealizeResult(tuple(tracks), tuple(vias), ())
        copper = to_gerber_model(interim, ir, layers=layer_names, outline=[])[
            "copper"
        ] + _pad_blockers(ir, layer_names, footprints)
        pours = plane_pours(
            outline=[[float(p[0]), float(p[1])] for p in ir.outline],
            layers=layer_names,
            plane_nets=plane_nets,
            copper=copper,
            clearance_mm=clearance,
            edge_clearance_mm=edge_inset,
        )
    # A promoted net whose plane did not get poured is not connected to
    # anything — its pins reach a via and the via reaches an empty layer.
    # The usual cause is a design with no authored board outline: a pour
    # has no extent without one, and _outline_clip's docstring is right
    # that inventing a boundary would silently constrain a design that
    # never asked for one. So the net is reported UNROUTED, which is what
    # it is. Silence here would be the whole failure mode this session
    # exists to remove: a clean number over a board that does not work.
    poured_nets = {str(p["net"]) for p in pours}
    unpoured = {
        n
        for n in range(ir.n_nets)
        if int(ir.net_plane_layers[n]) != 0 and str(ir.net_name[n]) not in poured_nets
    }
    extra: list[int] = []
    if unpoured:
        already = set(unrouted)
        extra = [
            s for s in plane_ids if int(ir.seg_net[s]) in unpoured and s not in already
        ]
    return pours, extra


def _plane_fanout(
    ir: PcbIR,
    plane_ids: list[int],
    grid: maze.OccupancyGrid,
    config: RealizeConfig,
    rules_by_net: dict[int, NetRules],
    pad_geoms: list[PadGeom],
) -> tuple[list[RealizedTrack], list[RealizedVia], list[int]]:
    """Connect every pin of a plane-promoted net DOWN to its plane.

    One stub plus one drop via per PIN, not per segment. A net's segments
    are a star from ``member_pins[0]`` (``ir.from_graph``), so per-segment
    fanout emitted the hub pin's stub once per connection and gave the leaf
    pins nothing at all — and it drew each stub on ``ir.seg_layer``, the L1
    sketch layer, which for a pad on ``PAD_LAYER`` means a stub that starts
    on a layer its own pad is not on. Both bugs are the same mistake:
    treating a plane connection as a property of a *connection* when it is
    a property of a *pin*.

    The stub runs outward along the pin's own land-pattern offset — away
    from the part body, which is where the escape has to go anyway — and
    ends in a via spanning ``PAD_LAYER`` to the plane layer. That via is
    what :func:`precis.pcb.planes.plane_pours` deliberately does not carve
    an antipad around: it is the connection.

    Returns the pins it could not give a legal drop to. A plane connection
    that will not fit is an unrouted connection and is reported as one; the
    alternative — stamping the via anyway — is how this function first put
    26 clearance errors on a board the occupancy grid had guaranteed clean.
    """
    tracks: list[RealizedTrack] = []
    vias: list[RealizedVia] = []
    failed: list[int] = []
    seg_of_net: dict[int, int] = {}
    for seg_id in plane_ids:
        seg_of_net.setdefault(int(ir.seg_net[seg_id]), seg_id)
    # Every drop via this call places, (x, y, via_radius_mm), NET-BLIND —
    # shared across every net's pins in this one pass, not reset per net.
    # `disk_is_free`'s same-net exemption (right for a trace legally
    # ending on its own pad) is the wrong exemption for two via BARRELS:
    # a same-net cluster of GND drop vias is exactly where two are most
    # likely to land close together, and their drilled holes do not care
    # whose net they carry. See :func:`_via_clears_vias`'s own docstring
    # (gripe 269811 comment 2: two SCL drop vias measured 0.1598mm apart,
    # dia 0.75mm each — copper overlapping by 0.590mm and, worse, their
    # 0.25mm drills overlapping too).
    placed_via_sites: list[tuple[float, float, float]] = []
    for net_id, seg_id in sorted(seg_of_net.items()):
        # A net may now be poured on SEVERAL layers at once
        # (net_plane_layers is a bitmask). One drop via per pin still
        # suffices: a via is ALWAYS a contiguous span (RealizedVia's own
        # docstring — never a scalar layer) and a through barrel connects
        # every layer it passes, not just its two ends (confirmed by
        # connectivity.py's via-group union, which joins a via's
        # primitives on EVERY layer `_via_layer_names` reports for it, not
        # just `layer_lo`/`layer_hi`). So spanning from the pad's own
        # layer to the FARTHEST of this net's poured layers reaches every
        # nearer poured layer for free, and is simultaneously the
        # stitching connection between them — a GND fill on F.Cu and
        # In1.Cu is tied into ONE net the moment any GND pin's drop via
        # spans both, no separate stitching-via pass required.
        plane_layers = plane_layers_of(int(ir.net_plane_layers[net_id]))
        span_lo = min([PAD_LAYER, *plane_layers])
        span_hi = max([PAD_LAYER, *plane_layers])
        rules = rules_by_net[net_id]
        for pid in range(ir.n_pins):
            if int(ir.pin_net[pid]) != net_id:
                continue
            point = pin_point(ir, pid)
            if point is None:
                continue
            dx, dy = float(ir.pin_dx[pid]), float(ir.pin_dy[pid])
            norm = math.hypot(dx, dy)
            # A pin at its instance's exact centroid has no outward
            # direction to offer. +x is arbitrary but deterministic, and
            # the grid still refuses to let the stub overlap anything.
            ux, uy = (dx / norm, dy / norm) if norm > 1e-9 else (1.0, 0.0)
            lo, hi = span_lo, span_hi
            # Same conservative enclosing-circle radius `_realize_maze`'s own
            # pad-claim loop uses (see its docstring) — the drop via has to
            # clear THIS pin's own pad, whatever its real (possibly
            # rectangular) footprint.
            geom = pad_geoms[pid]
            pad_radius = math.hypot(geom.w_mm, geom.h_mm) / 2.0
            stub_end = _drop_via_site(
                grid,
                point,
                (ux, uy),
                net_id,
                rules,
                config,
                range(lo, hi + 1),
                pad_radius,
                placed_via_sites,
                _outline_clip(
                    ir,
                    _board_edge_min_mm(config) + (rules.via_dia_mm or 0.0) / 2.0,
                ),
            )
            if stub_end is None:
                failed.append(pid)
                continue
            tracks += [
                RealizedTrack(
                    seg_id,
                    net_id,
                    PAD_LAYER,
                    (
                        {
                            "shape": "line",
                            "start": list(point),
                            "end": list(stub_end),
                        },
                    ),
                    dist(point, stub_end),
                    None,
                    rules.track_width_mm,
                    is_dogbone=True,
                )
            ]
            grid.stamp_path(
                maze.RoutePath(
                    net_id,
                    (
                        (point[0], point[1], PAD_LAYER),
                        (stub_end[0], stub_end[1], PAD_LAYER),
                    ),
                    dist(point, stub_end),
                ),
                rules.track_width_mm,
            )
            assert rules.via_dia_mm is not None and rules.via_drill_mm is not None, (
                "_drop_via_site returns None when either is unresolved"
            )
            grid.stamp_disk(
                range(lo, hi + 1),
                stub_end[0],
                stub_end[1],
                grid.core_radius_mm(rules.via_dia_mm),
                net_id,
            )
            vias.append(
                RealizedVia(
                    seg_id=seg_id,
                    net_id=net_id,
                    x=stub_end[0],
                    y=stub_end[1],
                    dia_mm=rules.via_dia_mm,
                    drill_mm=rules.via_drill_mm,
                    layer_lo=lo,
                    layer_hi=hi,
                    endpoint="a",
                )
            )
            placed_via_sites.append((stub_end[0], stub_end[1], rules.via_dia_mm / 2.0))
    return tracks, vias, failed


#: How far out from a pad a drop via may be pushed to find clear copper,
#: as a multiple of the nominal stub length. Beyond this the escape is
#: genuinely blocked and saying so beats drawing a longer and longer stub
#: through a congested fanout.
_DROP_SEARCH_STEPS = 6
#: Angles (radians) tried at each distance, in order. Straight out along
#: the pin's own offset first — that is the direction the land pattern
#: already says is outward — then progressively off-axis.
_DROP_SEARCH_ANGLES = (0.0, 0.4, -0.4, 0.9, -0.9, 1.6, -1.6)


def _via_clears_vias(
    x: float,
    y: float,
    via_radius_mm: float,
    placed: list[tuple[float, float, float]],
    clearance_mm: float,
) -> bool:
    """May a via of this (undilated) copper radius be centred at
    ``(x, y)`` without landing on, or crowding, ANY already-placed via in
    ``placed`` — mirrors :meth:`~precis.pcb.maze.OccupancyGrid.
    via_clears_pads`'s own formula and reasoning exactly, asked of OTHER
    vias instead of pads, and just as deliberately NET-BLIND.

    ``disk_is_free``'s same-net exemption is right for a TRACE (it must
    legally end on its own pad) and wrong here for the identical reason
    ``via_clears_pads`` itself exists: two via BARRELS this close is a
    broken drill bit or an unintended slot, regardless of whose net(s)
    they carry — a same-net cluster of plane drop vias (several GND pins
    close together) is exactly where two are most likely to land close,
    so exempting same-net pairs would leave the most common case
    unguarded. Checking the COPPER (dia) radius with the same clearance
    margin ``via_clears_pads`` uses is sufficient to also protect the
    DRILL: a via's drill is always narrower than its own copper annulus,
    so a legal copper-to-copper gap is provably a wider drill-to-drill
    gap. Found live (gripe 269811 comment 2): two SCL drop vias 0.1598mm
    apart, dia 0.75mm each — copper overlapping by 0.590mm and, worse,
    their 0.25mm drills overlapping too (0.1598mm < 0.25mm)."""
    for px, py, pr in placed:
        if math.hypot(x - px, y - py) - via_radius_mm - pr < clearance_mm:
            return False
    return True


def _drop_via_site(
    grid: maze.OccupancyGrid,
    pad: Point,
    direction: tuple[float, float],
    net_id: int,
    rules: NetRules,
    config: RealizeConfig,
    layers: range,
    pad_radius_mm: float,
    placed_via_sites: list[tuple[float, float, float]],
    edge_clip: tuple[float, float, float, float] | None = None,
) -> Point | None:
    """Where this pin's drop via can legally sit, or ``None``.

    Asks the occupancy grid before claiming, for both the via annulus and
    the stub that feeds it — the same claim-then-draw discipline every
    routed trace already follows, applied to the one piece of copper that
    was exempt from it.

    **A drop via must also clear its OWN pad — same-net copper included.**
    :meth:`~precis.pcb.maze.OccupancyGrid.disk_is_free` is deliberately
    same-net-blind (a trace legally ends ON its own pad); a via is not a
    trace, it is a hole drilled through the board, and landing it on —
    or crowding — the very pad it drops from wicks solder down the barrel
    exactly as it would for any other pad (:meth:`~precis.pcb.maze.
    OccupancyGrid.via_clears_pads`'s own docstring). ``grid.route``'s own
    via search already folds this into its candidate mask
    (``_pad_keepout_mask``); this search used to try candidate sites
    without ever asking the one question that mask exists to answer —
    measured on the reference board as 55 of 57 DRC errors, one
    ``via_pad_keepout`` at essentially every plane-promoted pin, all of
    them this exact via landing on its own pad. Root cause was one level
    deeper than "the call was missing": :func:`_stamp_pads` was claiming
    every pad via plain ``stamp_disk`` rather than ``stamp_pad``, so
    ``grid._pads`` — what :meth:`via_clears_pads` and
    ``_pad_keepout_mask`` both actually read — was empty on every board
    this module has ever routed; see :func:`_stamp_pads`'s own docstring
    for the fix.

    **A drop via must also clear every OTHER drop via this same fan-out
    pass already placed** (:func:`_via_clears_vias`, ``placed_via_sites``)
    — a second, narrower guard than the pad one above, for the same
    same-net-blind-is-wrong-here reason.

    **And it must stay inside the board.** ``edge_clip`` is
    :func:`_outline_clip` inset for this via's own radius; a candidate
    outside it is rejected so the search simply tries the next angle or
    reach. Nothing else covers this site: the candidates below are
    CONTINUOUS positions swept around the pad, never grid nodes, so the
    inset baked into the routing grid's bounds — which is what keeps every
    grid-placed via legal — never applied to them, and the three checks
    above all ask about copper, not about the board's edge. Measured on
    the 40mm reference fixture as a VCC3V3 drop via 0.010mm inside the
    0.400mm V-cut floor. Rejecting beats clamping here precisely BECAUSE
    this is a search: a clamped site would have to be re-tested against
    all three copper checks anyway, and a clamp that fails them silently
    yields a worse via than the next candidate the sweep would have found.
    """
    if rules.via_dia_mm is None or rules.via_drill_mm is None:
        return None  # this fab publishes no via figures — don't invent any
    via_radius_mm = rules.via_dia_mm / 2.0
    via_r = grid.core_radius_mm(rules.via_dia_mm)
    stub_r = grid.core_radius_mm(rules.track_width_mm)
    # The nominal reach a via needs from its own pad is not a flat
    # constant (this subsystem's most-repeated defect — a courtyard
    # radius, a seed pitch, a claim radius, PAD_RADIUS_MM, and now this,
    # tuned to nothing rather than derived): it is exactly the distance
    # `via_clears_pads` itself requires — the pad's own (enclosing-circle)
    # radius, plus the via's, plus the net's clearance. `config.
    # dogbone_stub_mm` stays as a FLOOR, not the figure itself — a courtesy
    # minimum visible stub length for a vanishingly small pad, not a
    # substitute for the pad-derived distance that actually varies by
    # pad size and is the one that prevents the keep-out violation.
    base_reach = max(
        config.dogbone_stub_mm, pad_radius_mm + via_radius_mm + grid.clearance_mm
    )
    ux, uy = direction
    for step in range(1, _DROP_SEARCH_STEPS + 1):
        reach = base_reach * step
        for angle in _DROP_SEARCH_ANGLES:
            cos_a, sin_a = math.cos(angle), math.sin(angle)
            vx, vy = ux * cos_a - uy * sin_a, ux * sin_a + uy * cos_a
            site = (pad[0] + vx * reach, pad[1] + vy * reach)
            if edge_clip is not None and not (
                edge_clip[0] <= site[0] <= edge_clip[2]
                and edge_clip[1] <= site[1] <= edge_clip[3]
            ):
                continue
            if not grid.disk_is_free(layers, site[0], site[1], via_r, net_id):
                continue
            if not grid.via_clears_pads(site[0], site[1], via_radius_mm):
                continue
            if not _via_clears_vias(
                site[0], site[1], via_radius_mm, placed_via_sites, grid.clearance_mm
            ):
                continue
            # The stub between pad and via has to be clear too — a via in
            # free space fed by a trace through someone else's copper is
            # still a short.
            n = max(1, int(reach / (grid.spec.pitch / 2.0)))
            if all(
                grid.disk_is_free(
                    (PAD_LAYER,),
                    pad[0] + vx * reach * k / n,
                    pad[1] + vy * reach * k / n,
                    stub_r,
                    net_id,
                )
                for k in range(n + 1)
            ):
                return site
    return None


# ── plane fragment stitching (gripe 270637) ───────────────────────────────
#
# See the module docstring's "Plane fragment stitching" note for the two
# stages and the independence-from-connectivity.py discipline. Everything
# below is scoped to one call, from `_realize_maze`, after both routing AND
# pouring have finished (`_stitch_plane_fragments` needs the FINISHED pour
# polygons to know what it's joining).


class _UnionFind:
    """A small, PRIVATE union-find over one net's poured-fragment indices —
    written fresh here rather than imported from
    :mod:`precis.pcb.connectivity`'s own ``_DisjointSet``, on purpose: see
    this module's docstring on why the stitching PRODUCER must not share
    machinery with the connectivity CHECKER, even though the two data
    structures are one-for-one identical in shape. That is the deliberate
    duplication, not an oversight."""

    def __init__(self, n: int) -> None:
        self._parent = list(range(n))

    def find(self, a: int) -> int:
        root = a
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[a] != root:  # path compression
            self._parent[a], a = root, self._parent[a]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra


def _default_stitch_pitch(rules: NetRules, clearance_mm: float) -> float:
    """See :attr:`RealizeConfig.stitch_pitch_mm`'s own docstring for why
    this is derived from geometry already in hand rather than a bare
    constant."""
    assert rules.via_dia_mm is not None
    return 2.0 * (rules.via_dia_mm + clearance_mm)


def _pour_polygon(pour: dict[str, Any]) -> Any:
    """One pour item's shapely polygon, holes included — built directly
    from the SAME ``polygon``/``holes`` ring shape :mod:`precis.pcb.planes`
    (:func:`~precis.pcb.planes.plane_pours`) already emits and
    :mod:`precis.pcb.drc`'s ``_copper_item_polygon`` already turns into
    shapely for the clearance engine. Constructed independently here
    (never imported from ``drc.py``) for the same reason the rest of this
    pass is independent of its checkers: ``drc.py``'s reference oracle is
    the entity that PROVES this shape correct against the accelerated
    engine, and this producer must not depend on that proof holding to
    decide where it is allowed to place copper. Returns an empty polygon
    for a degenerate (<3-point) ring rather than raising — a malformed
    pour is this pass's cue to skip it, not to crash the whole board."""
    ext = pour.get("polygon") or []
    if len(ext) < 3:
        return _ShapelyPolygon()
    holes = [h for h in (pour.get("holes") or []) if len(h) >= 3]
    poly = _ShapelyPolygon(
        [(float(p[0]), float(p[1])) for p in ext],
        [[(float(p[0]), float(p[1])) for p in h] for h in holes],
    )
    if not poly.is_valid:
        poly = poly.buffer(0)
    return poly


def _touches(poly: Any, x: float, y: float) -> bool:
    """Does point ``(x, y)`` touch ``poly`` (interior OR boundary)? Used to
    ask "does this via's CENTRE already land on this net's own copper" —
    boundary counts, the same "boundary cases go to inside" choice
    :func:`precis.pcb.planes.point_in_pour` makes and for the same reason:
    under-reporting a touch here would invent a fragment that isn't real."""
    return not poly.is_empty and bool(poly.intersects(_ShapelyPoint(x, y)))


def _grid_candidates(poly: Any, pitch: float) -> list[tuple[float, float]]:
    """Every point of a ``pitch``-spaced grid that lands strictly inside
    ``poly`` — stage 1's "sprinkle" candidate set (module docstring)."""
    if poly.is_empty or pitch <= 0:
        return []
    minx, miny, maxx, maxy = poly.bounds
    out: list[tuple[float, float]] = []
    y = miny + pitch / 2.0
    while y <= maxy:
        x = minx + pitch / 2.0
        while x <= maxx:
            if poly.contains(_ShapelyPoint(x, y)):
                out.append((x, y))
            x += pitch
        y += pitch
    return out


def _candidate_points_in(region: Any, via_r: float) -> list[tuple[float, float]]:
    """A handful of candidate sites inside ``region`` for stage 2's
    targeted single-via bridge: the region's own guaranteed-interior
    representative point first (works for a concave/oddly-shaped region
    where a plain centroid can land outside it), then a small ring of
    points offset by the via's own radius around it — a fallback for the
    case where the exact representative point is legal geometry but
    already claimed by other copper the keep-out filter (below) will
    reject. Every candidate is filtered back through ``region.contains``,
    so an offset can only ever narrow the set, never propose a point
    outside the region it was computed from."""
    if region.is_empty:
        return []
    rp = region.representative_point()
    out = [(rp.x, rp.y)]
    for k in range(8):
        ang = k * math.pi / 4.0
        cand = (rp.x + math.cos(ang) * via_r, rp.y + math.sin(ang) * via_r)
        if region.contains(_ShapelyPoint(*cand)):
            out.append(cand)
    return out


def _stamp_realized_track(grid: maze.OccupancyGrid, track: RealizedTrack) -> None:
    """Claim ``track``'s corridor on a FRESH scratch grid — replaying
    :meth:`~precis.pcb.maze.OccupancyGrid.stamp_path`'s own discipline by
    hand, because :func:`_stitch_plane_fragments` builds a NEW grid from
    the finished :class:`RealizeResult` rather than reusing a route pass's
    own (``_realize_maze`` opens one ``OccupancyGrid`` per attempt and
    keeps none of them once routing is decided — see that function's own
    docstring). Only handles a ``"line"`` segment: this pass runs only
    under the maze router (:func:`realize`'s ``'maze'`` branch is the only
    caller of :func:`_stitch_plane_fragments`), which never emits an arc."""
    if track.layer < 0:
        return
    radius = grid.core_radius_mm(track.width_mm)
    step = grid.spec.pitch / 2.0
    for seg in track.segments:
        ax, ay = float(seg["start"][0]), float(seg["start"][1])
        bx, by = float(seg["end"][0]), float(seg["end"][1])
        span = math.hypot(bx - ax, by - ay)
        n = max(1, math.ceil(span / step))
        for k in range(n + 1):
            t = k / n
            grid.stamp_disk(
                (track.layer,),
                ax + (bx - ax) * t,
                ay + (by - ay) * t,
                radius,
                track.net_id,
            )


#: ``seg_id`` for copper this stitching pass invents. Every other track and
#: via in this module is the realization of ONE L1 segment and carries that
#: segment's id; a stitching via or jumper answers a property of the
#: FINISHED board (is this net one piece?) and belongs to no segment at all.
#: A sentinel says so; reusing some nearby segment's id would quietly claim
#: a provenance that is not true.
_STITCH_SEG_ID = -1

#: Circle-to-polygon refinement for the jumper's own foreign-pour guard
#: (:func:`_clears_foreign_pours`). ``buffer`` approximates a disk with an
#: INSCRIBED polygon, i.e. slightly SMALLER than the true circle — the
#: unsafe direction for a keep-out test, since a shape that just barely
#: touches would be missed. At 32 segments per quadrant the shortfall is
#: ~0.05% of the radius (sub-micron at these dimensions), well under the
#: nanometre-scale noise the rest of this module already tolerates.
_JUMPER_QUAD_SEGS = 32


#: How many anchor sites a jumper considers per fragment, and how coarsely
#: it sweeps to find them. A fragment is a whole poured sheet, often most of
#: a board face, and the site must be simultaneously inside the polygon,
#: clear of every other net's copper on every layer its barrel spans, and
#: reachable by a route on a spare layer — so the FIRST spot tried is
#: routinely occupied. The first version of this reused
#: :func:`_candidate_points_in`, whose nine points are a representative
#: point plus a ring one via-radius around it: correct for the small
#: polygon INTERSECTION it was written for, and useless here, because all
#: nine sit within half a millimetre of each other and a single pad or
#: trace at that spot rejects the whole fragment. Measured on the 40mm
#: fixture: one jumper placed, the rest refused.
_JUMPER_SITES_PER_FRAGMENT = 8
_JUMPER_SWEEP_DIVISIONS = 6
#: Ceiling on A* calls one jumper may spend. Sites are tried nearest-pair
#: first (shortest detour = least copper), so the bound truncates the tail
#: of a search that was already ordered best-first, rather than sampling it
#: arbitrarily.
_JUMPER_ROUTE_ATTEMPTS = 12


def _snapped_sites_in(region: Any, spec: maze.GridSpec, via_r: float) -> list[Point]:
    """Candidate via sites spread across ``region``, SNAPPED to
    routing-grid cell centres and re-filtered for containment.

    The snap is what makes a jumper's connectivity provable rather than
    lucky. :meth:`~precis.pcb.maze.OccupancyGrid.route` returns cell
    CENTRES (:func:`~precis.pcb.maze._merge_collinear`), so a track routed
    from an unsnapped point would start up to half a cell diagonal away
    from the via meant to anchor it — the exact defect
    :mod:`precis.pcb.connectivity`'s own module docstring opens with
    ("track ends short of a pad", found by measuring a rendered board).
    Snapping FIRST makes the two coincide exactly, because
    ``to_point(to_cell(p))`` is the identity on a point already produced by
    ``to_point``.

    Re-filtering after the snap is not belt-and-braces: a candidate near a
    fragment's edge can snap OUT of the polygon it came from, and a via
    there would sit on this net's name but not on this net's copper.

    The representative point is tried first — it is the one point shapely
    guarantees is inside the polygon — but it is snapped and re-filtered
    like every other candidate, and it can therefore be rejected. **A
    fragment thinner than one grid pitch in some direction can end up
    offering NO site at all, and that is the honest answer rather than a
    gap**: the anchor has to be a via that a grid route can actually reach,
    and a fragment containing no cell centre contains nowhere for one to
    land. The caller reports "no legal jumper" for it, which is true —
    though note it is true of THIS mechanism, not of the geometry in
    general, and a finer grid would change the answer."""
    minx, miny, maxx, maxy = region.bounds
    step = max(
        max(maxx - minx, maxy - miny) / _JUMPER_SWEEP_DIVISIONS,
        2.0 * via_r,
        spec.pitch,
    )
    rp = region.representative_point()
    raw: list[Point] = [(rp.x, rp.y)]
    y = miny + step / 2.0
    while y <= maxy:
        x = minx + step / 2.0
        while x <= maxx:
            raw.append((x, y))
            x += step
        y += step

    out: list[Point] = []
    seen: set[tuple[int, int]] = set()
    for cand in raw:
        cell = spec.to_cell(*cand)
        if cell in seen:
            continue
        seen.add(cell)
        site = spec.to_point(*cell)
        if region.contains(_ShapelyPoint(*site)):
            out.append(site)
        if len(out) >= _JUMPER_SITES_PER_FRAGMENT:
            break
    return out


def _clears_foreign_pours(
    shape: Any, layers: range, foreign_pours: dict[int, list[Any]]
) -> bool:
    """Does ``shape`` (already grown by clearance) miss every OTHER net's
    poured copper on every layer in ``layers``?

    **This is the one keep-out the occupancy grid cannot answer.** The
    scratch grid :func:`_stitch_plane_fragments` builds is stamped from
    tracks, vias and pads — never from pours, which do not exist yet when a
    route pass runs and are not stamped afterwards. That gap is stated as a
    known limitation for a stitching VIA, where both fixtures happen to
    avoid it. A jumper cannot inherit that reasoning: it puts a whole
    TRACE, of arbitrary length, on a layer chosen precisely because this
    net was not already using it — which on any real board is where another
    net's plane lives. So the guard is asked here, geometrically, against
    the finished pour polygons themselves."""
    for layer in layers:
        for poly in foreign_pours.get(layer, ()):
            if not poly.is_empty and poly.intersects(shape):
                return False
    return True


def _try_plane_jumper(
    net_id: int,
    frag_a: tuple[int, Any],
    frag_b: tuple[int, Any],
    *,
    grid: maze.OccupancyGrid,
    via_dia_mm: float,
    via_drill_mm: float,
    via_is_legal: Any,
    track_width_by_layer: dict[int, float],
    foreign_pours: dict[int, list[Any]],
    config: RealizeConfig,
) -> tuple[list[RealizedVia], RealizedTrack] | None:
    """A same-net JUMPER between two poured fragments: a via down out of
    fragment A, a trace across a spare layer, a via back up into fragment
    B. Returns the two vias and the trace, or ``None`` when no legal one
    exists (which is a real answer, not a failure to try hard enough).

    **Why this exists at all.** :func:`_stitch_one_net`'s stage 2 proves
    that a single via can only join two fragments that geometrically
    OVERLAP — so two fragments of one net on the SAME layer, which by
    definition never overlap, are unreachable by any number of vias.
    Measured on the 40mm fixture: ``GND`` poured on ``F.Cu`` alone, in four
    pieces, reported by :func:`precis.pcb.connectivity.net_islands` and
    unfixable by the pass that was supposed to fix it. A plane in pieces is
    a real manufacturing defect — the return path the plane exists to
    provide is not there — so the mechanism, not the report, was what
    needed to change.

    **The detour layer is never either fragment's own layer**, and that
    restriction is about the CHECKER, not about taste. A pour joins the
    connectivity union-find by containment of a primitive's FIRST point
    only (:func:`precis.pcb.connectivity.net_islands`), so a bare trace
    ending inside fragment B registers as connected to A and not to B — it
    would look like a fix and measure as one fewer piece only by accident.
    A via at each end has its own centre inside its own fragment, so both
    joins are containment-true by construction, and the barrel asserts the
    layer change independently of any geometry. Requiring ``D`` to differ
    from both layers is what guarantees both ends actually get one.

    Every candidate is checked in cost order — the cheap arithmetic guards
    (containment, via legality, mutual barrel spacing) before the A* — and
    nothing is placed or stamped until the whole jumper is known to be
    legal, so a half-built jumper can never be left behind by a late
    rejection."""
    layer_a, poly_a = frag_a
    layer_b, poly_b = frag_b
    via_r = via_dia_mm / 2.0
    clearance = grid.clearance_mm
    sites_a = _snapped_sites_in(poly_a, grid.spec, via_r)
    sites_b = _snapped_sites_in(poly_b, grid.spec, via_r)
    if not sites_a or not sites_b:
        return None

    detours = [
        d
        for d in range(grid.spec.n_layers)
        if d != layer_a and d != layer_b and d in track_width_by_layer
    ]
    # Nearest pair first: the shortest jumper is the least copper, the
    # least detour-layer congestion for whatever routes next, and the most
    # likely to find a clear corridor at all.
    pairs = sorted(
        (
            (math.hypot(qb[0] - qa[0], qb[1] - qa[1]), qa, qb)
            for qa in sites_a
            for qb in sites_b
        ),
        key=lambda t: t[0],
    )
    # ONE budget for the whole call, not one per detour layer: the ceiling
    # is on what a single jumper may spend, and a board with three spare
    # layers should not silently cost three times as much searching.
    attempts = 0
    for detour in detours:
        width_mm = track_width_by_layer[detour]
        lo_a, hi_a = min(layer_a, detour), max(layer_a, detour)
        lo_b, hi_b = min(layer_b, detour), max(layer_b, detour)
        for gap, qa, qb in pairs:
            if attempts >= _JUMPER_ROUTE_ATTEMPTS:
                break
            # The two barrels are checked against each OTHER by hand:
            # `via_is_legal` asks about vias already PLACED, and neither
            # of these is placed yet.
            if gap - 2.0 * via_r < clearance:
                continue
            if not via_is_legal(qa[0], qa[1], lo_a, hi_a):
                continue
            if not via_is_legal(qb[0], qb[1], lo_b, hi_b):
                continue
            disc_a = _ShapelyPoint(*qa).buffer(
                via_r + clearance, quad_segs=_JUMPER_QUAD_SEGS
            )
            disc_b = _ShapelyPoint(*qb).buffer(
                via_r + clearance, quad_segs=_JUMPER_QUAD_SEGS
            )
            if not _clears_foreign_pours(
                disc_a, range(lo_a, hi_a + 1), foreign_pours
            ) or not _clears_foreign_pours(
                disc_b, range(lo_b, hi_b + 1), foreign_pours
            ):
                continue
            attempts += 1
            path = grid.route(
                net_id,
                qa,
                qb,
                layers=[detour],
                width_mm=width_mm,
                # No via geometry offered, so the search may not change
                # layer: this trace stays on `detour`, which is what
                # makes the two barrels above the whole layer story.
                via_dia_mm=None,
                pad_layer=detour,
                # Never attach: a multi-source start would begin the
                # path on this net's copper somewhere else entirely,
                # and the trace would no longer meet the via at `qa`.
                attach=False,
                max_expansions=config.max_expansions,
            )
            if path is None or len(path.points) < 2:
                continue
            run = [(x, y) for x, y, _ in path.points]
            corridor = _ShapelyLineString(run).buffer(
                width_mm / 2.0 + clearance, quad_segs=_JUMPER_QUAD_SEGS
            )
            if not _clears_foreign_pours(
                corridor, range(detour, detour + 1), foreign_pours
            ):
                continue
            tracks = _track_from_run(
                _STITCH_SEG_ID,
                net_id,
                detour,
                run,
                width_mm,
                # Unfilleted on purpose: `_track_from_run`'s fillet is
                # a cosmetic taut-up of a ROUTED corner, and this
                # trace's two ends must stay exactly on their vias.
                fillet_radius_mm=None,
            )
            if not tracks:
                continue
            vias = [
                RealizedVia(
                    seg_id=_STITCH_SEG_ID,
                    net_id=net_id,
                    x=qa[0],
                    y=qa[1],
                    dia_mm=via_dia_mm,
                    drill_mm=via_drill_mm,
                    layer_lo=lo_a,
                    layer_hi=hi_a,
                    endpoint="a",
                ),
                RealizedVia(
                    seg_id=_STITCH_SEG_ID,
                    net_id=net_id,
                    x=qb[0],
                    y=qb[1],
                    dia_mm=via_dia_mm,
                    drill_mm=via_drill_mm,
                    layer_lo=lo_b,
                    layer_hi=hi_b,
                    endpoint="b",
                ),
            ]
            return vias, tracks[0]
    return None


def _stitch_one_net(
    net_id: int,
    net_name: str,
    frags: list[tuple[int, Any]],
    existing_vias: list[RealizedVia],
    grid: maze.OccupancyGrid,
    rules: NetRules,
    config: RealizeConfig,
    placed_via_sites: list[tuple[float, float, float]],
    track_width_by_layer: dict[int, float],
    foreign_pours: dict[int, list[Any]],
    net_tracks: list[RealizedTrack],
) -> tuple[list[RealizedVia], list[RealizedTrack], int, int, str]:
    """One net's stitching attempt, both stages of the module docstring.

    **The graph has TWO kinds of node, not one — ``frags`` (this net's own
    poured fragments, one entry per ``(layer_idx, polygon)``; a "sheet" may
    already be more than one fragment on the SAME layer, antipad-cut by
    foreign copper) AND ``existing_vias`` (this net's already-realized
    per-pin drop vias).** An earlier version of this function unioned only
    fragments and treated a via purely as a JOINER between them — which
    silently dropped a via that touches NO fragment at all off the graph
    entirely: :func:`precis.pcb.connectivity.net_islands` never turns a
    bare, untouched pour into a graph node either (a pour merges whatever
    already falls inside it; it is never a node on its own), so that via
    (with its own pad and dog-bone stub) is real, disconnected copper the
    independent checker WILL flag — measured live on the
    ``esp32c3_reference`` flood-all-four fixture (gr270637): VCC3V3's 23rd
    drop via landed nowhere near either poured sheet, this function's
    fragment-only graph had no way to even represent that as a problem, and
    it reported "fully stitched" while ``net_islands`` reported a real
    2-piece split. A via node is now first-class in the graph FOR COUNTING
    PURPOSES: it can join a fragment (the seed step below) and its
    isolation is visible in the returned piece count either way.

    **This net's own TRACKS are a third thing again: joiners, not nodes.**
    A routed trace is never something this pass would stitch TO, so it gets
    no node — but it is real copper, and copper landing in two fragments
    connects them. Omitting them made the piece count over-report; see the
    seed step below for the measurement that caught it.

    **A via node is never a BRIDGING TARGET for a new via, and this is a
    proof, not a missing feature.** Two DISTINCT, DRC-legal vias of the
    SAME net must still be at least ``clearance_mm`` apart
    (:func:`_via_clears_vias`'s own docstring: a barrel-to-barrel gap this
    close "is a broken drill bit... regardless of whose net(s) they
    carry") — but :mod:`precis.pcb.connectivity`'s own via-via rule only
    counts two vias as CONNECTED when their gap is <= ``TOUCH_EPS_MM``
    (effectively zero). Those two thresholds can never both hold at once
    (``clearance_mm > 0``), so a brand-new via can never be close enough to
    an EXISTING via to register as "touching" it without first being
    illegal. This is a hard, board-geometry-independent fact, not
    something a smarter search could someday satisfy — so stage 2 below
    never treats a via node as one half of a bridge.

    **The same proof condemns two fragments on the SAME layer, and stage 3
    is the answer to it.** A via joins copper at one ``(x, y)``, so it can
    only bridge fragments that geometrically OVERLAP; two fragments of one
    net on one layer never do. That is not a tuning failure either, and it
    is not rare — ``GND`` on the 40mm fixture is poured on ``F.Cu`` alone
    and comes out in four pieces. The mechanism that CAN close it is a
    jumper: a via out of each fragment and a trace between them on a spare
    layer (:func:`_try_plane_jumper`), which is why this function now
    returns tracks as well as vias. It remains true that a STRAY VIA — one
    of this net's own drop vias touching no fragment at all — has no fix
    here: a jumper anchors on poured copper at both ends, and a bare via
    offers nothing to anchor to. Moving that via (a "shove", a different
    mechanism this pass does not perform) is still its only remedy, and the
    returned message still says so.

    Returns the vias and the jumper tracks it placed, the number of
    DISTINCT connected pieces remaining afterward across ALL nodes (0 or 1
    means fully stitched, never more than
    ``len(frags) + len(existing_vias)``), and a message that is non-empty
    only when that count is > 1.

    Assumes ``rules.via_dia_mm``/``via_drill_mm`` are already known — the
    caller filters out nets whose fab publishes neither (nothing to stitch
    WITH, the same refusal every other via-placing function in this module
    already makes rather than inventing a figure)."""
    n_frags = len(frags)
    n = n_frags + len(existing_vias)
    dsu = _UnionFind(n)
    assert rules.via_dia_mm is not None and rules.via_drill_mm is not None
    via_dia_mm, via_drill_mm = rules.via_dia_mm, rules.via_drill_mm
    via_r = via_dia_mm / 2.0

    # Seed: any of this net's ALREADY-realized drop vias that touch one or
    # more fragments joins them (and joins the via itself into that group)
    # for free — the "lucky" case gripe 270637 itself measured (GND, more
    # pins, already comes out as one island on every seed without this
    # pass doing anything). Both stages below only ever need to close what
    # this seed did not. Point-exact on purpose (never via-radius-tolerant)
    # — this is :func:`precis.pcb.planes.point_in_pour`'s own convention,
    # the SAME test :mod:`precis.pcb.connectivity` uses for pour
    # membership (class docstring's independence note: this is physics
    # copied from the checker's own model, not the checker consulted at
    # runtime).
    #: Fragment indices with at least one of this net's own vias or traces
    #: inside them — the fragments the independent checker can even SEE, and
    #: the distinction the closing message turns into "floating copper" vs.
    #: "electrically split".
    populated: set[int] = set()
    for k, v in enumerate(existing_vias):
        node = n_frags + k
        for i, (layer, poly) in enumerate(frags):
            if v.layer_lo <= layer <= v.layer_hi and _touches(poly, v.x, v.y):
                dsu.union(node, i)
                populated.add(i)

    # Seed, part two: **this net's own TRACKS are joiners too.** They are
    # not nodes — a routed trace is never a thing this pass would stitch TO
    # — but they are real copper, and copper that lands in two fragments
    # connects them just as surely as a via does.
    #
    # Leaving them out made this function over-report, and the
    # over-reporting was invisible until `RealizeResult.unstitched` got its
    # first reader. Measured on the 40mm fixture: the pass claimed 2
    # remaining pieces with ZERO stray vias (so both were fragment groups),
    # while :func:`precis.pcb.connectivity.net_islands` — which models pads
    # and tracks as primitives too — said one component. The checker was
    # right; the producer's graph simply had no way to see the trace doing
    # the joining. A producer independent of its checker still has to model
    # the copper that exists.
    #
    # Union-only, never a split: adding a joiner can lower the piece count
    # and can never raise it, so this cannot manufacture a fragmentation
    # report. Its one behavioural consequence is that a pair already joined
    # by a trace no longer buys stitching vias — correct, since they are
    # connected, and consistent with stage 1's existing "extra density
    # beyond what connectivity needs is not this pass's job".
    for track in net_tracks:
        touched: list[int] = []
        endpoints = [
            (float(seg[end][0]), float(seg[end][1]))
            for seg in track.segments
            for end in ("start", "end")
        ]
        for i, (layer, poly) in enumerate(frags):
            if layer == track.layer and any(_touches(poly, x, y) for x, y in endpoints):
                touched.append(i)
                populated.add(i)
        for k, v in enumerate(existing_vias):
            if v.layer_lo <= track.layer <= v.layer_hi and any(
                math.hypot(x - v.x, y - v.y) <= v.dia_mm / 2.0 for x, y in endpoints
            ):
                touched.append(n_frags + k)
        for other in touched[1:]:
            dsu.union(touched[0], other)

    if n <= 1:
        return [], [], min(n, 1), 0, ""

    core_r = grid.core_radius_mm(via_dia_mm)
    pitch = config.stitch_pitch_mm or _default_stitch_pitch(rules, grid.clearance_mm)
    new_vias: list[RealizedVia] = []
    new_tracks: list[RealizedTrack] = []

    def via_is_legal(x: float, y: float, lo: int, hi: int) -> bool:
        """The three keep-outs a stitching via must satisfy, asked WITHOUT
        placing anything — stage 3 needs to know both of a jumper's barrels
        are legal before it commits either one."""
        return (
            grid.disk_is_free(range(lo, hi + 1), x, y, core_r, net_id)
            and grid.via_clears_pads(x, y, via_r)
            and _via_clears_vias(x, y, via_r, placed_via_sites, grid.clearance_mm)
        )

    def claim_via(via: RealizedVia) -> None:
        new_vias.append(via)
        grid.stamp_disk(
            range(via.layer_lo, via.layer_hi + 1), via.x, via.y, core_r, net_id
        )
        placed_via_sites.append((via.x, via.y, via_r))

    def try_via(x: float, y: float, lo: int, hi: int) -> bool:
        if not via_is_legal(x, y, lo, hi):
            return False
        claim_via(
            RealizedVia(
                seg_id=_STITCH_SEG_ID,  # no L1 segment -- see the constant
                net_id=net_id,
                x=x,
                y=y,
                dia_mm=via_dia_mm,
                drill_mm=via_drill_mm,
                layer_lo=lo,
                layer_hi=hi,
                endpoint="a",
            )
        )
        return True

    # Stage 1 -- sprinkle: every pair of DIFFERENT sheets whose footprints
    # overlap gets a generous grid of stitching vias across that overlap
    # (module docstring: "what real tools do"). A candidate is only ever
    # drawn from the literal polygon INTERSECTION (``poly.contains``), so
    # its centre is, by construction, exactly inside both fragments — the
    # point-exact touch rule the seed step above uses, satisfied for free
    # rather than checked after the fact. A pair the seed step already
    # joined is skipped -- extra density beyond what connectivity needs is
    # a real feature (thermal/EMI margin) but not this pass's job.
    # Same-layer pairs are left to stage 2 (they never spatially overlap
    # by definition — see stage 2's own note).
    for i, j in itertools.combinations(range(n_frags), 2):
        li, poly_i = frags[i]
        lj, poly_j = frags[j]
        if li == lj or dsu.find(i) == dsu.find(j):
            continue
        overlap = poly_i.intersection(poly_j)
        if overlap.is_empty or overlap.area <= 0.0:
            continue
        lo, hi = (li, lj) if li < lj else (lj, li)
        placed = 0
        for x, y in _grid_candidates(overlap, pitch):
            if placed >= config.max_sprinkle_vias_per_overlap:
                break
            if try_via(x, y, lo, hi):
                dsu.union(i, j)
                placed += 1

    # Stage 2 -- targeted residue over FRAGMENT PAIRS ONLY (class
    # docstring's proof: a via node can never be a legal bridging target).
    # Whatever stage 1 left disconnected — a narrow overlap sliver the
    # fixed pitch stepped over, or every candidate in a small overlap
    # already claimed — gets a bounded, closest-pair-first attempt with
    # the overlap's own interior point rather than a grid.
    #
    # **The provable limit.** A via joins copper at ONE (x, y) with a FIXED
    # radius (``via_r``): for it to touch both fragment A and fragment B
    # (point-exact, per the seed step's own rule), some point must lie
    # inside BOTH polygons at once — which is only possible if they
    # geometrically overlap. Two fragments with NO overlap at all cannot
    # be joined by ANY single via, full stop, independent of pitch or
    # search effort — this pass proves that rather than merely having
    # failed to find a site.
    #
    # **Stage 3 rides inside the same loop, as the fallback for a pair
    # stage 2 just proved un-bridgeable** (:func:`_try_plane_jumper`): two
    # vias and a spare-layer trace, which is the only mechanism that can
    # join two fragments that do not overlap. It is deliberately second —
    # a jumper is real copper on a layer this net was not using, so a pair
    # a single via CAN close should never buy one.
    #
    # **Closest-pair-first over a union-find IS a minimum spanning tree**
    # (that is Kruskal's algorithm), which settles the "one jumper per
    # pair, or a spanning tree?" question without any extra machinery: a
    # pair whose two sides are already in one component is skipped by the
    # `roots[i] != roots[j]` filter above, so a net in k pieces buys
    # exactly k-1 jumpers and never a redundant one.
    impossible: set[tuple[int, int]] = set()
    jumpers = 0
    for _ in range(config.max_stitch_iterations):
        roots = [dsu.find(i) for i in range(n)]
        if len(set(roots)) <= 1:
            break
        candidates = sorted(
            (
                (frags[i][1].distance(frags[j][1]), i, j)
                for i in range(n_frags)
                for j in range(i + 1, n_frags)
                if roots[i] != roots[j] and (i, j) not in impossible
            ),
            key=lambda t: t[0],
        )
        if not candidates:
            break  # every remaining fragment pair already tried and failed
        _gap, i, j = candidates[0]
        li, poly_i = frags[i]
        lj, poly_j = frags[j]
        lo, hi = (li, lj) if li <= lj else (lj, li)
        overlap = poly_i.intersection(poly_j)
        placed_here = False
        if not overlap.is_empty and overlap.area > 0.0:
            for x, y in _candidate_points_in(overlap, via_r):
                if try_via(x, y, lo, hi):
                    dsu.union(i, j)
                    placed_here = True
                    break
        if not placed_here and jumpers < config.max_plane_jumpers_per_net:
            jumper = _try_plane_jumper(
                net_id,
                frags[i],
                frags[j],
                grid=grid,
                via_dia_mm=via_dia_mm,
                via_drill_mm=via_drill_mm,
                via_is_legal=via_is_legal,
                track_width_by_layer=track_width_by_layer,
                foreign_pours=foreign_pours,
                config=config,
            )
            if jumper is not None:
                jumper_vias, jumper_track = jumper
                for v in jumper_vias:
                    claim_via(v)
                new_tracks.append(jumper_track)
                _stamp_realized_track(grid, jumper_track)
                dsu.union(i, j)
                jumpers += 1
                placed_here = True
        if not placed_here:
            impossible.add((i, j))

    remaining = len({dsu.find(i) for i in range(n)})
    message = ""
    bare_pieces = 0
    if remaining > 1:
        # **Which of the remaining pieces carry any of this net's actual
        # copper?** A fragment is a node here whether or not a single via,
        # trace or pad lands inside it, but
        # :func:`precis.pcb.connectivity.net_islands` counts PRIMITIVES and
        # treats a pour purely as a joiner — so a poured island with
        # nothing in it is a piece to this pass and invisible to the
        # checker. That is not the checker being wrong: an empty island is
        # floating copper, a different defect with a different remedy
        # (shrink or grow the pour) than a net split in two. Saying which
        # kind is left is the difference between a report someone can act
        # on and a number that sends them back to instrument a run.
        # **Refresh `populated` with the copper THIS pass just placed.** It
        # was seeded from the board as it arrived, and the stages above
        # then put stitching vias and jumper traces into fragments that had
        # none — so reading the stale set here would call a fragment
        # "empty" that now demonstrably holds this net's copper. The error
        # runs in the dangerous direction: an inflated `bare_fragments`
        # makes `pcb_route` classify a genuine remaining split as mere
        # floating copper (its `fragments - bare_fragments > 1` test), i.e.
        # it downgrades a real failure to a note. Same touch rule as the
        # seed steps, asked of the new copper.
        for v in new_vias:
            for i, (layer, poly) in enumerate(frags):
                if v.layer_lo <= layer <= v.layer_hi and _touches(poly, v.x, v.y):
                    populated.add(i)
        for t in new_tracks:
            ends = [
                (float(seg[end][0]), float(seg[end][1]))
                for seg in t.segments
                for end in ("start", "end")
            ]
            for i, (layer, poly) in enumerate(frags):
                if layer == t.layer and any(_touches(poly, x, y) for x, y in ends):
                    populated.add(i)

        roots_with_copper = {dsu.find(i) for i in populated} | {
            dsu.find(n_frags + k) for k in range(len(existing_vias))
        }
        bare_pieces = len({dsu.find(i) for i in range(n_frags)} - roots_with_copper)
        bare_note = (
            f" {bare_pieces} of those piece(s) hold NO via or trace of this "
            "net at all — floating poured copper, not an electrical split, "
            "and invisible to the connectivity checker for that reason."
            if bare_pieces
            else ""
        )
        stray_vias = sum(
            1
            for k in range(len(existing_vias))
            if dsu.find(n_frags + k) not in {dsu.find(i) for i in range(n_frags)}
        )
        via_note = (
            f" {stray_vias} of this net's own drop via(s) touch no poured "
            "fragment at all and cannot be rescued by ANY new via (see this "
            "function's own docstring's proof — a companion via close "
            "enough to touch one would violate that same via's own "
            "clearance); fixing them needs moving the via or a jumper, "
            "neither of which this pass attempts."
            if stray_vias
            else ""
        )
        message = (
            f"{net_name}: {remaining} disconnected piece(s) remain after the "
            f"stitching pass (sprinkle + up to {config.max_stitch_iterations} "
            f"targeted attempt(s), {jumpers} jumper(s) placed)."
            + bare_note
            + via_note
            + (
                " At least one remaining fragment pair has no spatial "
                "overlap for a single via to land in and no legal jumper "
                "either — no spare layer that is neither fragment's own, or "
                "no routable, pour-free corridor across one."
                if stray_vias < remaining - 1
                else ""
            )
        )
    return new_vias, new_tracks, remaining, bare_pieces, message


def _stitch_plane_fragments(
    ir: PcbIR,
    tracks: list[RealizedTrack],
    vias: list[RealizedVia],
    pours: list[dict[str, Any]],
    *,
    spec: maze.GridSpec,
    clearance: float,
    pads: list[tuple[Point, int, float]],
    rules_by_net: dict[int, NetRules],
    config: RealizeConfig,
) -> tuple[list[RealizedVia], list[RealizedTrack], list[UnstitchedNet]]:
    """Deliberate stitching vias for every plane-promoted net whose own
    poured copper is not already one piece — see the module docstring for
    the two-stage approach and why this function computes its own
    connectivity rather than asking
    :func:`precis.pcb.connectivity.net_islands`.

    Builds a FRESH scratch :class:`~precis.pcb.maze.OccupancyGrid` from the
    already-finished ``tracks``/``vias``/``pads`` (not the grid a route
    pass used — ``_realize_maze`` keeps none of those once routing is
    decided) so every keep-out check below (:meth:`~precis.pcb.maze.
    OccupancyGrid.disk_is_free`, :meth:`~precis.pcb.maze.OccupancyGrid.
    via_clears_pads`, :func:`_via_clears_vias`) sees the REAL, final board,
    not an intermediate one.

    **Board-edge clearance needs no separate check here.** Every candidate
    site this function ever proposes comes from INSIDE an already-computed
    pour polygon, and :func:`precis.pcb.planes.plane_pours` already insets
    its region by the edge clearance before it ever produces one — a site
    strictly inside a pour is, by construction, already inside the board's
    usable rectangle. Re-deriving that inset here would be the exact
    "second copy of a shared definition" this codebase's own discipline
    warns against.

    Nets whose fab publishes no via figures are skipped (nothing to stitch
    WITH); a net with no poured fragment at all is skipped (nothing to
    stitch TO — this includes every non-plane-promoted net, since
    ``pours`` never contains an entry for one); a net reduced to exactly
    one node total (one fragment and no drop vias, say) is skipped too —
    a single node cannot be disconnected from itself. Otherwise every
    plane-promoted net is checked, including one with only a SINGLE
    poured sheet: :func:`_stitch_one_net`'s graph includes this net's own
    drop vias as nodes too, so a lone stray via that missed its net's one
    and only sheet is exactly the case this pass exists to catch (see that
    function's own docstring for the gr270637 measurement that found it).

    **Known limitation, stated rather than hidden**: a stitching via's
    keep-out check (``disk_is_free``) is asked only about copper this
    module has already CLAIMED on the occupancy grid (routed tracks,
    vias, pads) — it has no notion of a THIRD net's finished pour on a
    layer the stitching via merely passes through. On both reference
    fixtures every plane-promoted net's own poured layers are adjacent
    (no foreign pour sits between them), so this never arises in the
    boards this module is tested against; a design that pours three
    different nets on three adjacent layers and stitches across all three
    could, in principle, place a via that shorts to the middle one. Fixing
    that would mean re-running :func:`_pour_planes` after stitching (to
    carve fresh antipads around the new vias) rather than stitching after
    pouring, a bigger reordering than this task's scope.

    **A JUMPER does not inherit that limitation**, and must not: it lays a
    whole trace on a layer picked precisely because this net was not on it,
    which is exactly where a foreign plane is likely to be. So the pour
    polygons are handed down per net (``foreign_pours``, keyed by layer
    index) and :func:`_clears_foreign_pours` checks the jumper's own copper
    against them geometrically. That guard is narrow on purpose — it
    answers only for the copper THIS function invents, and does not
    retroactively make the paragraph above true for anything else.
    """
    plane_net_ids = [n for n in range(ir.n_nets) if int(ir.net_plane_layers[n]) != 0]
    if not plane_net_ids or not pours:
        return [], [], []
    layer_names = [str(layer.get("name")) for layer in ir.stackup]
    layer_index = {name: i for i, name in enumerate(layer_names)}
    # Every pour's polygon, once — the per-net `foreign_pours` views below
    # are filtered from this list rather than rebuilt, so a board with many
    # planes does not pay to re-triangulate the same rings per net.
    pour_polys: list[tuple[str, int, Any]] = []
    for pour in pours:
        idx = layer_index.get(str(pour.get("layer", "")))
        if idx is None:
            continue
        poly = _pour_polygon(pour)
        if not poly.is_empty:
            pour_polys.append((str(pour.get("net", "")), idx, poly))

    grid = maze.OccupancyGrid(spec, clearance_mm=clearance)
    _stamp_pads(grid, pads)
    for t in tracks:
        _stamp_realized_track(grid, t)
    for v in vias:
        grid.stamp_disk(
            range(v.layer_lo, v.layer_hi + 1),
            v.x,
            v.y,
            grid.core_radius_mm(v.dia_mm),
            v.net_id,
        )
    placed_via_sites: list[tuple[float, float, float]] = [
        (v.x, v.y, v.dia_mm / 2.0) for v in vias
    ]

    extra_vias: list[RealizedVia] = []
    extra_tracks: list[RealizedTrack] = []
    unstitched: list[UnstitchedNet] = []
    for net_id in plane_net_ids:
        net_name = str(ir.net_name[net_id])
        rules = rules_by_net[net_id]
        if rules.via_dia_mm is None or rules.via_drill_mm is None:
            continue
        frags = [(idx, poly) for net, idx, poly in pour_polys if net == net_name]
        if not frags:
            continue  # nothing poured for this net at all -- nothing to stitch TO
        existing = [v for v in vias if v.net_id == net_id]
        if len(frags) + len(existing) <= 1:
            continue  # a single node, alone, cannot be disconnected from itself
        foreign_pours: dict[int, list[Any]] = {}
        for net, idx, poly in pour_polys:
            if net != net_name:
                foreign_pours.setdefault(idx, []).append(poly)
        # A jumper's trace is a real trace, so its width is this net's
        # resolved rule for the layer it lands on — the same single
        # resolver every other track in this module goes through, asked per
        # candidate detour layer rather than reusing the caller's `rules`
        # (which were resolved for one layer and would silently apply an
        # outer-layer width to an inner-layer run). Keying on
        # `routable_layers` is also what confines a jumper to a layer that
        # may legally carry a trace at all — a plane-only or non-copper
        # layer never appears here, so it is never offered as a detour.
        track_width_by_layer = {
            layer: _resolve_track_rules(ir, net_id, layer, config).track_width_mm
            for layer in routable_layers(ir)
        }
        net_vias, net_tracks, remaining, bare, message = _stitch_one_net(
            net_id,
            net_name,
            frags,
            existing,
            grid,
            rules,
            config,
            placed_via_sites,
            track_width_by_layer,
            foreign_pours,
            [t for t in tracks if t.net_id == net_id],
        )
        extra_vias += net_vias
        extra_tracks += net_tracks
        if remaining > 1:
            unstitched.append(UnstitchedNet(net_id, net_name, remaining, bare, message))
    return extra_vias, extra_tracks, unstitched


def _straighten(
    path: maze.RoutePath,
    grid: maze.OccupancyGrid,
    net_id: int,
    width_mm: float,
    *,
    shove_vias: bool = False,
    via_group_extent_mm: float | None = None,
    via_shove_radius_mm: float = 0.5,
) -> maze.RoutePath:
    """Pull a routed path taut: drop any interior vertex whose two
    neighbours can see each other through free copper.

    An octile grid search cannot produce a line that is not a multiple of
    45 degrees, so a run to a pad that sits at some other angle comes out
    as a staircase — measured on the reference board, 431 segments over 78
    tracks with a 0.41mm median segment. Every one of those corners is real
    copper with a real bend in it.

    The chord is tested against the SAME occupancy grid that proved the
    original path clear, at the same radius the search itself queries with
    (own half-width plus a cell of discretisation slack — not
    ``core_radius_mm``, which already contains the other net's clearance
    and would double-count it). So a straightened path is clear of
    everything routed before it, and it is stamped before anything routed
    after it. The clearance guarantee is not weakened; this pass can only
    ever remove copper — EXCEPT for the one bounded exception below.

    **Vias are fixed points by default** — a vertex where the layer changes
    is never dropped, because moving it would move a hole. With
    ``shove_vias=True`` (:attr:`RealizeConfig.shove_vias`) a via MAY move,
    within ``via_shove_radius_mm``, toward the straight line between its
    two outer (non-via) neighbours — see :func:`_shove_vias` for the
    validation a candidate move must pass before it is accepted. A second
    collapse pass runs afterward so any run that only became collinear
    because of the shove gets the same treatment as everything else.
    """
    pts = list(path.points)
    if len(pts) < 3:
        return path
    radius = width_mm / 2.0 + grid.spec.pitch
    step = grid.spec.pitch / 2.0
    pts = _collapse_straight(pts, grid, net_id, radius, step)
    if shove_vias and via_group_extent_mm is not None and len(pts) >= 4:
        pts, moved = _shove_vias(
            pts, grid, net_id, via_group_extent_mm, via_shove_radius_mm, radius, step
        )
        if moved:
            pts = _collapse_straight(pts, grid, net_id, radius, step)
    length = sum(
        dist((a[0], a[1]), (b[0], b[1]))
        for a, b in itertools.pairwise(pts)
        if a[2] == b[2]
    )
    return maze.RoutePath(path.net_id, tuple(pts), length, path.attached)


def _collapse_straight(
    pts: list[tuple[float, float, int]],
    grid: maze.OccupancyGrid,
    net_id: int,
    radius: float,
    step: float,
) -> list[tuple[float, float, int]]:
    """One taut-pull pass: drop any interior, same-layer-neighboured vertex
    whose surrounding chord tests clear. The vertex-collapse half of
    :func:`_straighten`, factored out so a via shove (which can create NEW
    collinear runs on either side of the moved via) gets a second pass
    through the exact same logic rather than a hand-rolled local cleanup."""
    pts = list(pts)
    changed = True
    while changed and len(pts) > 2:
        changed = False
        i = 1
        while i < len(pts) - 1:
            a, b, c = pts[i - 1], pts[i], pts[i + 1]
            if a[2] == b[2] == c[2] and _chord_is_free(
                grid, a, c, net_id, radius, step
            ):
                del pts[i]
                changed = True
            else:
                i += 1
    return pts


def _project_onto_segment(p: Point, a: Point, b: Point) -> Point:
    """The closest point to ``p`` on segment ``a``-``b`` (clamped, never
    the infinite line — a via should not be pulled past either of its own
    neighbours)."""
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 < 1e-12:
        return a
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return (ax + t * dx, ay + t * dy)


def _clamp_to_radius(candidate: Point, origin: Point, radius_mm: float) -> Point:
    """``candidate``, pulled back toward ``origin`` if it is further than
    ``radius_mm`` away — the "slightly" in "vias allowed to move
    slightly" (backlog, verbatim)."""
    d = dist(candidate, origin)
    if d <= radius_mm or d < 1e-12:
        return candidate
    t = radius_mm / d
    return (
        origin[0] + (candidate[0] - origin[0]) * t,
        origin[1] + (candidate[1] - origin[1]) * t,
    )


def _shove_vias(
    pts: list[tuple[float, float, int]],
    grid: maze.OccupancyGrid,
    net_id: int,
    via_group_extent_mm: float,
    shove_radius_mm: float,
    chord_radius_mm: float,
    step: float,
) -> tuple[list[tuple[float, float, int]], bool]:
    """One pass over every via TRANSITION in ``pts`` — a same-coordinate,
    different-layer consecutive pair with a fixed neighbour on each side
    (an edge-of-path via, with no outer neighbour to straighten toward, is
    left alone: moving it would need a new rule for what it is straightening
    against, not a smaller version of this one).

    **A via move must clear the via's OWN, wider mask — never the track's.**
    A via is not a track (:mod:`precis.pcb.maze`'s own module docstring):
    it is an annulus, wider than the trace, and it exists on every layer it
    spans. So the candidate disk is tested with :meth:`~precis.pcb.maze.
    OccupancyGrid.disk_is_free` at ``via_group_extent_mm`` (the SAME
    conservative group footprint :func:`_route_pass` clears for the search
    to place a via there in the first place — never the single via's own
    smaller diameter, which would under-claim relative to what the
    stitched group actually needs), collapsed across EVERY layer
    (``range(0, grid.spec.n_layers)``) — the identical discipline
    :meth:`~precis.pcb.maze.OccupancyGrid.route`'s own via mask uses,
    reused rather than re-derived. That collapse is also what keeps the
    move drill-to-drill AND drill-to-copper safe: every other net's copper
    on every layer, and every other via's own claimed disk, is already
    baked into ``grid``'s occupancy, on whichever layer it actually sits.

    **Every layer the via anchors moves together, for free.** Both the
    pre-transition point (this net's copper on the near layer) and the
    post-transition point (the far layer) share ONE coordinate in ``pts``
    — the same coordinate :func:`_route_pass` later reads to place the
    via disk, the stitched group, and the stitch bar. Moving that one
    coordinate here moves all of it coherently; there is no second place
    a via's position is decided from.

    A rejected candidate leaves the via exactly where the search put it —
    this can only change WHERE a via sits, never how many exist or what it
    connects, so it cannot sever a net the way a naive "just move it"
    edit could.
    """
    pts = list(pts)
    moved = False
    i = 1
    while i + 2 <= len(pts) - 1:
        v_pre, v_post = pts[i], pts[i + 1]
        if v_pre[2] == v_post[2]:
            i += 1
            continue
        prev_pt, next_pt = pts[i - 1], pts[i + 2]
        origin = (v_pre[0], v_pre[1])
        target = _project_onto_segment(
            origin, (prev_pt[0], prev_pt[1]), (next_pt[0], next_pt[1])
        )
        candidate = _clamp_to_radius(target, origin, shove_radius_mm)
        if dist(candidate, origin) < 1e-9:
            i += 1
            continue
        near_ok = _chord_is_free(
            grid,
            (prev_pt[0], prev_pt[1], v_pre[2]),
            (candidate[0], candidate[1], v_pre[2]),
            net_id,
            chord_radius_mm,
            step,
        )
        far_ok = near_ok and _chord_is_free(
            grid,
            (candidate[0], candidate[1], v_post[2]),
            (next_pt[0], next_pt[1], v_post[2]),
            net_id,
            chord_radius_mm,
            step,
        )
        via_ok = (
            far_ok
            and grid.disk_is_free(
                range(0, grid.spec.n_layers),
                candidate[0],
                candidate[1],
                grid.core_radius_mm(via_group_extent_mm),
                net_id,
            )
            # `disk_is_free` is deliberately SAME-NET-blind (right for a
            # trace legally ending on its own pad, module docstring above)
            # — a shoved via is not a trace, so it must also clear
            # `via_clears_pads` (net-blind, checks a pad's own net too)
            # or a shove can walk a via straight onto its own pad. Found
            # 2026-08-29 (gripe 269811): this loop moved a via by testing
            # only `disk_is_free`, so a candidate site that was merely
            # "not foreign copper" was accepted even when it sat on the
            # via's own net's pad, at the SAME group-extent radius the
            # occupancy check above already uses (the conservative
            # stitched-group footprint, never the single via's own
            # smaller diameter).
            and grid.via_clears_pads(
                candidate[0], candidate[1], via_group_extent_mm / 2.0
            )
        )
        if via_ok:
            pts[i] = (candidate[0], candidate[1], v_pre[2])
            pts[i + 1] = (candidate[0], candidate[1], v_post[2])
            moved = True
        i += 1
    return pts, moved


def _chord_is_free(
    grid: maze.OccupancyGrid,
    a: tuple[float, float, int],
    b: tuple[float, float, int],
    net_id: int,
    radius: float,
    step: float,
) -> bool:
    span = dist((a[0], a[1]), (b[0], b[1]))
    n = max(1, int(span / step))
    layers = (a[2],)
    return all(
        grid.disk_is_free(
            layers,
            a[0] + (b[0] - a[0]) * k / n,
            a[1] + (b[1] - a[1]) * k / n,
            radius,
            net_id,
        )
        for k in range(n + 1)
    )


def _snap_to_pads(
    path: maze.RoutePath, start: Point, end: Point, pitch: float
) -> maze.RoutePath:
    """Pull a routed path's ends onto the exact pad centres.

    The search works in cells, so its endpoints are cell CENTRES — up to
    half a cell diagonal from the pad they are supposed to land on.
    Measured on the reference board: 120 of 162 track ends stopped
    0.04-0.06mm short of their pad. That is inside a 0.2mm pad, so the
    connection happens to be made — by luck, not by construction, and the
    luck runs out as soon as pad geometry gets smaller than the routing
    grid. A track that ends *near* its pad is not connected to it.

    The far end is always the target pad, so it always snaps. **The near
    end snaps only when the path did not attach to its own net's copper**,
    which :attr:`maze.RoutePath.attached` reports and no distance test can:
    ``ir.from_graph`` decomposes a net into a STAR from ``member_pins[0]``,
    so every connection shares one hub pin and the trunk runs right past
    that pad. The earlier proximity proxy therefore fired on branches that
    had attached to the trunk — dragging the head off the trunk and onto
    the hub pad, on whichever layer the branch happened to be. Measured on
    seed 2: SDA in five pieces, three B.Cu branches sitting on the
    coordinates of an F.Cu pad they were never connected to, with DRC
    clean and nothing reported unrouted. Snapping an attached head is not
    a smaller version of connecting it; it is a disconnection.

    The move is bounded by half a cell diagonal, which is inside the one
    cell of slack :meth:`maze.OccupancyGrid.route` already adds to its
    query dilation, so this cannot walk copper out of its cleared
    corridor.
    """
    points = list(path.points)
    limit = pitch * math.sqrt(2.0) / 2.0 + 1e-9
    if not path.attached and dist((points[0][0], points[0][1]), start) <= limit:
        points[0] = (start[0], start[1], points[0][2])
    if dist((points[-1][0], points[-1][1]), end) <= limit:
        points[-1] = (end[0], end[1], points[-1][2])
    length = sum(
        dist((a[0], a[1]), (b[0], b[1]))
        for a, b in itertools.pairwise(points)
        if a[2] == b[2]
    )
    return maze.RoutePath(path.net_id, tuple(points), length, path.attached)


def _tracks_from_path(
    seg_id: int,
    net_id: int,
    path: maze.RoutePath,
    width_mm: float,
    *,
    fillet_radius_mm: float | None = None,
) -> list[RealizedTrack]:
    """Split one routed path into per-layer :class:`RealizedTrack`\\ s —
    that class carries a single ``layer``, so a route that changes layer
    becomes several tracks sharing one ``seg_id`` (which is what a real
    multi-layer net is)."""
    out: list[RealizedTrack] = []
    run: list[tuple[float, float]] = []
    layer = path.points[0][2]
    for x, y, this_layer in path.points:
        if this_layer != layer:
            out += _track_from_run(
                seg_id,
                net_id,
                layer,
                run,
                width_mm,
                fillet_radius_mm=fillet_radius_mm,
            )
            run = [(x, y)]
            layer = this_layer
        else:
            run.append((x, y))
    out += _track_from_run(
        seg_id, net_id, layer, run, width_mm, fillet_radius_mm=fillet_radius_mm
    )
    return out


def _track_from_run(
    seg_id: int,
    net_id: int,
    layer: int,
    run: list[tuple[float, float]],
    width_mm: float,
    *,
    fillet_radius_mm: float | None = None,
) -> list[RealizedTrack]:
    """One per-layer run of routed points, as a track.

    **Corners are rounded, and the radius is clamped so that rounding can
    only REMOVE copper.** A fillet cuts the outside of a corner but bulges
    inward, by ``r·(1 − sin(θ/2))`` — ``0.29·r`` at a right angle
    (:func:`precis.pcb.geom.max_inward_deviation`). That inward bulge is
    new copper on the concave side, exactly where a pad or via may be
    sitting, so an unclamped fillet can introduce a clearance violation on
    a path the router had already proved clear.

    Rather than fillet freely and re-run DRC to find out, the radius is
    capped so the worst inward deviation on this run fits inside the
    track's own half-width. Copper within half a width of the mitered
    centreline is copper the unfilleted track already occupied, so the
    filleted track is a strict subset of the straight one and the router's
    existing clearance guarantee carries over untouched.

    :func:`precis.pcb.geom.max_radius_for_deviation` computes that cap in
    closed form. **It replaced a proportional ``radius *= budget / worst``
    step that did not work**: deviation is linear in the radius only at an
    UNCLAMPED corner, and ``fillet_polyline`` clamps the setback to half
    the shorter adjoining leg, back-solving an ``r_eff`` that ignores the
    requested radius entirely. On a 60-degree corner with legs 4x the
    track width, the proportional step moved the deviation by exactly zero
    and left it 0.1443mm against a 0.125mm budget — a fillet bulging new
    copper past the envelope the router proved clear, which is the one
    thing this budget exists to prevent.

    ``length`` stays the PRE-fillet centreline length: it is the routing
    cost figure the maze search itself optimised and is reported against,
    not a fabrication dimension. Filleting shortens the real copper
    slightly; quoting the router's own number here keeps the reported
    length comparable across a change to the corner treatment.
    """
    if len(run) < 2:
        return []
    length = sum(dist(a, b) for a, b in itertools.pairwise(run))
    segments: list[dict[str, Any]]
    if fillet_radius_mm and fillet_radius_mm > 0.0 and len(run) > 2:
        budget = width_mm / 2.0
        radius = min(fillet_radius_mm, geom.max_radius_for_deviation(list(run), budget))
        segments = geom.fillet_polyline(list(run), radius)
    else:
        segments = [
            {"shape": "line", "start": list(a), "end": list(b)}
            for a, b in itertools.pairwise(run)
        ]
    return [
        RealizedTrack(seg_id, net_id, layer, tuple(segments), length, None, width_mm)
    ]


# ── the checkpoint entry point ────────────────────────────────────────────


def realize(
    ir: PcbIR,
    *,
    obstacles: list[Obstacle] | None = None,
    config: RealizeConfig = RealizeConfig(),
    seg_ids: list[int] | None = None,
    footprints: dict[str, dict[str, Any]] | None = None,
) -> RealizeResult:
    """Realize every segment in ``seg_ids`` (default: the whole board) —
    the checkpoint call. Never touches the IR (module docstring); pure
    function of a snapshot in, geometry + vias + a legible congestion
    digest out.

    ``config.router`` picks the drawer: ``'maze'`` (default) routes on a
    shared occupancy grid and can leave segments ``unrouted``;
    ``'tangent'`` draws every segment unconditionally and can leave them
    overlapping. See :attr:`RealizeConfig.router`. ``footprints`` (maze
    router only — the tangent drawer's obstacles are component courtyards,
    not pad geometry) is forwarded to :func:`pad_geometry` for the
    occupancy grid's own pad claims; ``None`` (the default) claims every
    pad at :mod:`precis.pcb.landpattern`'s synthesized size."""
    ids = list(range(ir.n_segments)) if seg_ids is None else seg_ids
    if config.router == "maze":
        tracks, vias, unrouted, pours, reasons, unstitched = _realize_maze(
            ir, ids, config, footprints
        )
        warnings = _gap_usage(ir, ids, config)
        return RealizeResult(
            tuple(tracks),
            tuple(vias),
            tuple(warnings),
            tuple(unrouted),
            tuple(pours),
            tuple(reasons),
            tuple(unstitched),
        )
    if config.router != "tangent":
        raise ValueError(
            f"unknown router {config.router!r} — expected 'maze' or 'tangent'"
        )
    if obstacles is None:
        obstacles = _default_obstacles(ir, config)
    tracks = [
        t
        for t in (realize_segment(ir, s, obstacles, config) for s in ids)
        if t is not None
    ]
    vias = [v for t in tracks for v in _vias_for_track(ir, t, config)]
    warnings = _gap_usage(ir, ids, config)
    return RealizeResult(tuple(tracks), tuple(vias), tuple(warnings))


# ── rip-up primitives ─────────────────────────────────────────────────────


def rip_net(
    ir: PcbIR,
    result: RealizeResult,
    net_id: int,
    config: RealizeConfig = RealizeConfig(),
) -> RealizeResult:
    """Remove ``net_id``'s tracks (and vias) from ``result`` — every OTHER
    net's :class:`RealizedTrack` is the SAME object afterward (untouched,
    not recomputed): rip-up = edit a sketch row, never a wholesale
    re-realize. Congestion warnings are recomputed from the remaining
    segments only (a gap's usage can only go DOWN when a net is ripped,
    never up, so this never invents a new violation — it can only clear
    one that the ripped net was itself party to)."""
    tracks = tuple(t for t in result.tracks if t.net_id != net_id)
    vias = tuple(v for v in result.vias if v.net_id != net_id)
    warnings = tuple(_gap_usage(ir, [t.seg_id for t in tracks], config))
    # Stitching status is a whole-board diagnostic, not per-track state, and
    # this function never touches plane copper (rip-up is per-net, and a
    # ripped net's own removal is not this pass's concern) -- carried over
    # unchanged rather than silently dropped.
    return RealizeResult(tracks, vias, warnings, unstitched=result.unstitched)


def pin_topology(ir: PcbIR, seg_id: int, side: int) -> None:
    """Pin a topology choice — a thin, documented delegate to
    :meth:`precis.pcb.ir.PcbIR.set_side` (module docstring: pinning a
    side IS a sketch edit, not a realizer concern; this exists only so a
    rip-up caller doesn't need a second import mid-loop)."""
    ir.set_side(seg_id, side)


def re_realize_segments(
    ir: PcbIR,
    result: RealizeResult,
    seg_ids: list[int],
    *,
    obstacles: list[Obstacle] | None = None,
    config: RealizeConfig = RealizeConfig(),
) -> RealizeResult:
    """Recompute exactly ``seg_ids``' tracks (and vias) and merge them into
    a COPY of ``result`` — every other segment's :class:`RealizedTrack` is
    the same object, untouched, never re-derived. Congestion warnings are
    recomputed from the merged track set's segment ids (cheap — a
    dict-bucket pass, not a geometry re-derivation) since a re-realized
    segment can change which gap it binds."""
    if obstacles is None:
        obstacles = _default_obstacles(ir, config)
    replaced = {
        t.seg_id: t
        for t in (realize_segment(ir, s, obstacles, config) for s in seg_ids)
        if t is not None
    }
    existing_ids = {t.seg_id for t in result.tracks}
    # Preserve the original ordering/objects for every untouched track;
    # a re-realized id gets its fresh RealizedTrack in the same slot; a
    # seg_id that's genuinely new (not in `result` yet) is appended.
    tracks = tuple(replaced.get(t.seg_id, t) for t in result.tracks)
    tracks += tuple(
        replaced[s] for s in seg_ids if s in replaced and s not in existing_ids
    )
    # Vias are keyed off their originating seg_id — drop every via that
    # belonged to a segment this call re-realized, then recompute fresh
    # vias for exactly the replaced tracks (a re-realized track's layer,
    # and therefore its via need, can change). Untouched segments' vias
    # are left as the same objects, same discipline as their tracks.
    touched = set(seg_ids)
    vias = tuple(v for v in result.vias if v.seg_id not in touched)
    vias += tuple(v for t in replaced.values() for v in _vias_for_track(ir, t, config))
    warnings = tuple(_gap_usage(ir, [t.seg_id for t in tracks], config))
    # See rip_net's own comment: stitching status is a whole-board
    # diagnostic this per-segment recompute has no new information about.
    return RealizeResult(tracks, vias, warnings, unstitched=result.unstitched)


# ── gerber.py hand-off ────────────────────────────────────────────────────


def to_gerber_model(
    result: RealizeResult,
    ir: PcbIR,
    *,
    layers: list[str],
    outline: list[list[float]],
    track_width_mm: float | None = None,
    footprints: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble a :mod:`precis.pcb.gerber`-shaped model dict from a
    :class:`RealizeResult` — ``layers`` is the board's layer-name list
    (export label only, per the IR's own "names are an export concern
    only" discipline: this is the ONE place an integer layer index gets
    turned into a name, at hand-off to gerber). Each track's width is its
    OWN :attr:`RealizedTrack.width_mm` — already resolved per net/layer by
    :mod:`precis.pcb.rules` at realize time (module docstring). Passing an
    explicit ``track_width_mm`` overrides every track uniformly, for a
    caller that genuinely wants one flat width (e.g. a quick sanity export)
    rather than the per-net resolution. ``footprints`` is forwarded to
    :func:`pads_for_ir` unchanged (real per-pin pad size where supplied,
    :mod:`precis.pcb.landpattern` synthesis otherwise).

    Each via becomes a ``"span"`` layer-NAME pair — the SAME two-name shape
    :mod:`precis.pcb.gerber`'s ``_via_layers``/:mod:`precis.pcb.drc`'s
    ``_via_layer_names`` both already read — never a ``"layer"`` key
    (:class:`RealizedVia`'s own docstring: that scalar shape is the exact
    prior regression this function must not reintroduce)."""
    copper: list[dict[str, Any]] = []
    for t in result.tracks:
        if t.layer < 0 or t.layer >= len(layers):
            continue  # UNSET_LAYER or out-of-range -- nothing to emit yet
        copper.append(
            {
                "ctype": "track",
                "layer": layers[t.layer],
                "net": str(ir.net_name[t.net_id]),
                "width_mm": t.width_mm if track_width_mm is None else track_width_mm,
                "segments": list(t.segments),
            }
        )
    for v in result.vias:
        if v.layer_lo < 0 or v.layer_hi >= len(layers):
            continue  # a layer index out of range for this export -- skip, don't guess
        copper.append(
            {
                "ctype": "via",
                "net": str(ir.net_name[v.net_id]),
                "x": v.x,
                "y": v.y,
                "dia_mm": v.dia_mm,
                "drill_mm": v.drill_mm,
                "span": [layers[v.layer_lo], layers[v.layer_hi]],
            }
        )
    copper += [dict(p) for p in result.pours]
    return _quantized(
        {
            "layers": layers,
            "outline": outline,
            "copper": copper,
            "pads": pads_for_ir(ir, layers, footprints),
        }
    )


def _quantized(value: Any) -> Any:
    """Snap every millimetre in a gerber model to the gerber unit.

    ``gerber.py`` writes fixed-point integers — ``_u(mm) = round(mm *
    10**6)`` — so it rounds every coordinate at EMISSION anyway. Doing it
    HERE instead, once, at the hand-off, means the artefact on disk is
    exactly the geometry this module computed rather than a rounded copy
    of it: what a human inspects and what DRC checked are then the same
    numbers, not two roundings of a third.

    It also makes exact-equality geometry decidable. Two coordinates
    produced by different float paths that ought to coincide — a fillet's
    tangent point and the leg it sits on, two arcs that should share a
    centre — differ in the last bits and compare unequal forever, which is
    why an alignment reward over raw floats is measure-zero and can never
    fire. After quantisation they are simply equal.

    **Applied to every float in the structure, deliberately.** The gerber
    model is a pure geometry carrier: every float in it is a millimetre
    (coordinates, widths, diameters, drills, pad sizes). An allow-list of
    keys would be one more place to forget a key when a new primitive is
    added — the exact failure this subsystem keeps shipping. ``bool`` is
    checked before ``float`` because ``bool`` is a subclass of ``int`` and
    an arc's ``cw`` flag must stay a bool; ``int`` layer indices are left
    alone so their type does not silently become float.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return geom.quantize(value)
    if isinstance(value, dict):
        return {k: _quantized(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_quantized(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_quantized(v) for v in value)
    return value


@dataclass(frozen=True, slots=True)
class PadGeom:
    """One pin's resolved pad SIZE — ``(w, h, shape, synthesized)``. The
    return element of :func:`pad_geometry`, this module's single answer to
    "how big is this pad" (the size counterpart to :func:`pads_for_ir`'s
    own "where is this pad" claim)."""

    w_mm: float
    h_mm: float
    shape: str
    synthesized: bool


def _real_pad_sizes(
    ir: PcbIR, inst_id: int, fp: dict[str, Any]
) -> dict[str, tuple[float, float, str]]:
    """This one instance's REAL per-pin pad size, keyed by NETLIST pin
    name, sourced from a cached ``part_footprints`` row (``fp`` in that
    row's own shape: ``{"pads": [...], "pin_map": {...}}``, the exact
    dict :func:`precis.pcb.padplace.place_footprint_pads` already
    consumes for the fab-export path).

    Delegates the mirror/rotate/90-degree-swap-for-rect-pads resolution to
    :func:`~precis.pcb.padplace.place_footprint_pads` itself rather than
    re-deriving a second copy of that logic here (the exact "two call
    sites, drifted" defect :func:`pads_for_ir`'s own docstring names) —
    the trick is asking it to resolve each pad's ``net`` field as the
    pin's own NAME (``pin_to_net={name: name for ...}``), which smuggles
    pin identity through the one field of its output that survives the
    transform unmolested, letting real w/h/shape be read back out keyed by
    the same pin name this module already indexes everything else by.
    Position is irrelevant here (only size is read back), so the probe
    instance is placed at the origin regardless of where the real
    instance actually sits.
    """
    pin_names = {
        str(ir.pin_label[p])
        for p in range(ir.n_pins)
        if int(ir.pin_instance[p]) == inst_id
    }
    if not pin_names:
        return {}
    rot = float(ir.inst_rot[inst_id])
    probe_inst = {"x": 0.0, "y": 0.0, "rot": 0.0 if math.isnan(rot) else rot}
    pads, _drills = padplace.place_footprint_pads(
        fp.get("pads") or [],
        probe_inst,
        layers=["_pad_geometry_probe"],
        pin_map=fp.get("pin_map"),
        pin_to_net={name: name for name in pin_names},
    )
    out: dict[str, tuple[float, float, str]] = {}
    for pad in pads:
        name = str(pad.get("net") or "")
        if not name or name in out:
            continue  # a THT pad emits one entry per target layer -- first wins
        w = float(pad["w"])
        out[name] = (w, float(pad.get("h", w)), str(pad["shape"]))
    return out


def pad_geometry(
    ir: PcbIR, footprints: dict[str, dict[str, Any]] | None = None
) -> list[PadGeom]:
    """Every pin's resolved pad SIZE, index-aligned with ``ir.pin_*`` —
    the one place :func:`pads_for_ir` and the maze router's pad-claim
    stamping (:func:`_route_pass`) both read pad size from, so they cannot
    read two different numbers for the same pad again.

    ``footprints`` (optional, keyed by ``refdes``, each value a cached
    ``part_footprints`` row) is preferred per pin when supplied — real
    measured geometry, ``synthesized=False``. **Not yet wired to a live
    caller**: :meth:`~precis.store.Store.pcb_footprints_for` (the existing
    production source of this same data, already used by
    :mod:`precis.pcb.padplace`'s fab-export path) returns its dict keyed
    by LCSC part number, not refdes — :attr:`~precis.pcb.ir.PcbIR.
    instance_refdes` has no matching ``part_lcsc`` field on the IR today,
    so a caller wiring this up must remap ``{refdes: by_lcsc[part_lcsc]}``
    itself first (from whatever instance list it already has the LCSC
    numbers on) before passing it here. Any pin with no match (no
    ``footprints`` at all, this instance missing from it, or a pin name
    the real footprint's own ``pin_map`` doesn't cover) falls back to
    :mod:`precis.pcb.ir`'s own ``pin_w``/``pin_h``/``pin_shape`` —
    :mod:`precis.pcb.landpattern`'s package-family synthesis, computed once
    at :func:`~precis.pcb.ir.from_graph` time — with ``synthesized`` taken
    from ``ir.pin_pad_synthesized`` (always ``True`` for that path).
    """
    real_by_inst: dict[int, dict[str, tuple[float, float, str]]] = {}
    if footprints:
        for inst_id in range(ir.n_instances):
            fp = footprints.get(str(ir.instance_refdes[inst_id]))
            if fp and fp.get("pads"):
                real_by_inst[inst_id] = _real_pad_sizes(ir, inst_id, fp)
    out: list[PadGeom] = []
    for pid in range(ir.n_pins):
        inst_id = int(ir.pin_instance[pid])
        real = real_by_inst.get(inst_id, {}).get(str(ir.pin_label[pid]))
        if real is not None:
            w, h, shape = real
            out.append(PadGeom(w, h, shape, synthesized=False))
        else:
            out.append(
                PadGeom(
                    float(ir.pin_w[pid]),
                    float(ir.pin_h[pid]),
                    str(ir.pin_shape[pid]),
                    synthesized=bool(ir.pin_pad_synthesized[pid]),
                )
            )
    return out


def pads_for_ir(
    ir: PcbIR,
    layers: list[str],
    footprints: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Every placed pin as a :mod:`precis.pcb.gerber`-shaped pad.

    **The single answer to "where are this part's pads".** There were two,
    and they disagreed: the gerber path built pads from the cached
    footprint table while the IR, the router and DRC used
    :func:`precis.pcb.landpattern.offsets_for`. On a design with no LCSC
    parts the cache is empty, so the fab output flashed no pads at all —
    and had the cache been populated it would have flashed them at
    coordinates the router never routed to, which is the worse of the two.
    One rule, two call sites, drifted: this build's recurring defect. Every
    consumer that needs pad geometry calls this.

    **Size is now real where a real footprint is supplied** (``footprints``,
    threaded to :func:`pad_geometry`) rather than every pad reserving the
    same hardcoded disc regardless of package (see
    :attr:`precis.pcb.ir.PcbIR.pin_w`'s own docstring for the defect this
    closes). ``synthesized`` rides on each pad because
    :mod:`precis.pcb.landpattern`'s own docstring is explicit that a
    synthesized pattern is a dimensionally-plausible BOUND and "must never
    be exported to fabrication". They are in the model because
    connectivity cannot be evaluated without them — a track that ends
    *near* its pad is not connected to it — and the flag is what stops
    synthesized geometry from quietly becoming a gerber. See
    :func:`precis.pcb.gerber.export_fab`'s refusal.
    """
    if not layers:
        return []
    geoms = pad_geometry(ir, footprints)
    out: list[dict[str, Any]] = []
    for pid in range(ir.n_pins):
        point = pin_point(ir, pid)
        if point is None:
            continue
        geom = geoms[pid]
        # NO_NET must never index `net_name`: NO_NET is -1,
        # and `ir.net_name[-1]` is not an error in Python/numpy -- it
        # WRAPS to the LAST real net, silently mislabelling every
        # unconnected (test-point/NC/mounting-hole) pin's exported pad as
        # belonging to whatever net happens to sit last. On a single-net
        # board that net is the ONLY net, so every NC pad reads as a
        # second, physically disconnected piece of it --
        # `connectivity.net_islands` then reports a real net as broken
        # for a reason that has nothing to do with its own copper. Empty
        # string matches `connectivity._pad_primitives`'s own "a pad with
        # no net is skipped" convention, so an unconnected pad is still
        # IN the model (still checkable for clearance) without being
        # attributed to a net it was never wired to.
        net_id = int(ir.pin_net[pid])
        pad: dict[str, Any] = {
            "layer": layers[PAD_LAYER],
            "net": "" if net_id == NO_NET else str(ir.net_name[net_id]),
            "shape": geom.shape,
            "x": point[0],
            "y": point[1],
            "w": geom.w_mm,
            "synthesized": geom.synthesized,
            # X2 object identity for the gerber viewer's hover tooltip
            # (gerber.py's own module docstring / `%TO.P,<refdes>,<pin>*%`)
            # — the IR already carries both, so there is no reason a pad
            # can say its net and its coordinates but not which pin of
            # which part it is. Emitted for a SYNTHESIZED pad too: a
            # synthesized pad is still a real pin of a real part, only its
            # GEOMETRY is a bound — withholding identity there would make
            # the tooltip least informative exactly where the shape is
            # least trustworthy, backwards from the point of having one.
            "refdes": str(ir.instance_refdes[int(ir.pin_instance[pid])]),
            "pin": str(ir.pin_label[pid]),
        }
        if geom.shape != "circle":
            pad["h"] = geom.h_mm
        out.append(pad)
    return out


__all__ = [
    "PAD_LAYER",
    "CongestionWarning",
    "Obstacle",
    "PadGeom",
    "RealizeConfig",
    "RealizeResult",
    "RealizedTrack",
    "RealizedVia",
    "UnroutedReason",
    "UnstitchedNet",
    "pad_geometry",
    "pads_for_ir",
    "pin_topology",
    "re_realize_segments",
    "realize",
    "realize_segment",
    "rip_net",
    "tangent_arc_path",
    "to_gerber_model",
]
