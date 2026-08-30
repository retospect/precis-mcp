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

from precis.pcb import maze, padplace
from precis.pcb.capabilities import CapabilityRow, capability_for
from precis.pcb.geom import Point, dist, dist_point_to_segment
from precis.pcb.ir import NO_NET, UNSET_LAYER, PcbIR, nearest_other_instance, pin_point
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

    if int(ir.net_plane_layer[net_id]) != UNSET_LAYER:
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
            vias.append(
                RealizedVia(
                    seg_id=track.seg_id,
                    net_id=track.net_id,
                    x=point[0] + offset,
                    y=point[1],
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
    """Stackup indices that can carry a routed trace. Mirrors
    :func:`precis.pcb.session.signal_layers` (four lines, duplicated
    rather than imported: ``session.py`` is a store-facing module this
    pure one has no other reason to depend on). Falls back to the pad
    layer for a stackup that declares no roles at all, so a synthetic IR
    still routes somewhere rather than nowhere."""
    layers = [i for i, layer in enumerate(ir.stackup) if layer.get("role") == "signal"]
    return layers or [PAD_LAYER]


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
        return [], [], list(ids), [], []

    rules_by_net = {
        n: _resolve_track_rules(ir, n, PAD_LAYER, config) for n in range(ir.n_nets)
    }
    if not rules_by_net:
        return [], [], list(ids), [], []
    clearance = max(
        config.clearance_mm, max(r.clearance_mm for r in rules_by_net.values())
    )
    # The board-edge inset is a DIFFERENT figure from inter-net clearance
    # (drc.check_board_edge_clearance reads board_edge_clearance_*_mm), and
    # it applies to the copper's edge, so half the widest track has to come
    # off it too. Take the house tier where the fab publishes one, so the
    # result clears the advisory threshold and not merely the hard one.
    edge_min = config.fab_caps.house_default.get(
        "board_edge_clearance_vcut_mm"
    ) or config.fab_caps.jlc_min.get("board_edge_clearance_vcut_mm")
    widest = max(r.track_width_mm for r in rules_by_net.values())
    edge_inset = max(clearance, float(edge_min or 0.0)) + widest / 2.0
    spec = maze.grid_for(
        [p for p, _, _ in pads],
        n_layers=len(ir.stackup) or 1,
        bounds=_outline_clip(ir, edge_inset),
    )
    plane_ids = [
        s for s in ids if int(ir.net_plane_layer[int(ir.seg_net[s])]) != UNSET_LAYER
    ]
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
        ir, tracks, vias, unrouted, plane_ids, clearance, edge_inset
    )
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
    return tracks, vias, unrouted + extra_unrouted, pours, reasons


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
        grid.stamp_disk((PAD_LAYER,), point[0], point[1], radius, net)


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
        ir, plane_ids, grid, config, rules_by_net
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
                maze.preferred_directions(signal_layers)
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
        tracks.extend(_tracks_from_path(seg_id, net_id, path, rules.track_width_mm))
    return tracks, vias, unrouted


def _pour_planes(
    ir: PcbIR,
    tracks: list[RealizedTrack],
    vias: list[RealizedVia],
    unrouted: list[int],
    plane_ids: list[int],
    clearance: float,
    edge_inset: float,
) -> tuple[list[dict[str, Any]], list[int]]:
    """Pour every plane-assigned layer over the FINISHED copper.

    Last, because a pour is defined by what it has to avoid and cannot be
    computed before the thing it avoids exists. The copper list is built by
    :func:`to_gerber_model` rather than assembled here, so "what shape is a
    track" has one answer. Returns the pours and any additional unrouted
    segments the pouring revealed.
    """
    layer_names = [str(layer.get("name")) for layer in ir.stackup]
    # A plane layer carries one net (optimize._gen_plane_promote enforces
    # it). If an externally-authored IR ever contradicts that, the LOWEST
    # net id wins — deterministically, so two runs of the same board pour
    # the same copper. Every other net on that layer then shows up as
    # disconnected in check_connectivity rather than as a plausible board,
    # which is the reporting direction that gets the contradiction fixed.
    plane_nets: dict[int, str] = {}
    for n in range(ir.n_nets):
        layer_idx = int(ir.net_plane_layer[n])
        if layer_idx != UNSET_LAYER:
            plane_nets.setdefault(layer_idx, str(ir.net_name[n]))
    pours: list[dict[str, Any]] = []
    if plane_nets and ir.outline:
        interim = RealizeResult(tuple(tracks), tuple(vias), ())
        pours = plane_pours(
            outline=[[float(p[0]), float(p[1])] for p in ir.outline],
            layers=layer_names,
            plane_nets=plane_nets,
            copper=to_gerber_model(interim, ir, layers=layer_names, outline=[])[
                "copper"
            ],
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
        if int(ir.net_plane_layer[n]) != UNSET_LAYER
        and str(ir.net_name[n]) not in poured_nets
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
    for net_id, seg_id in sorted(seg_of_net.items()):
        plane_layer = int(ir.net_plane_layer[net_id])
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
            lo, hi = min(PAD_LAYER, plane_layer), max(PAD_LAYER, plane_layer)
            stub_end = _drop_via_site(
                grid, point, (ux, uy), net_id, rules, config, range(lo, hi + 1)
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


def _drop_via_site(
    grid: maze.OccupancyGrid,
    pad: Point,
    direction: tuple[float, float],
    net_id: int,
    rules: NetRules,
    config: RealizeConfig,
    layers: range,
) -> Point | None:
    """Where this pin's drop via can legally sit, or ``None``.

    Asks the occupancy grid before claiming, for both the via annulus and
    the stub that feeds it — the same claim-then-draw discipline every
    routed trace already follows, applied to the one piece of copper that
    was exempt from it.
    """
    if rules.via_dia_mm is None or rules.via_drill_mm is None:
        return None  # this fab publishes no via figures — don't invent any
    via_r = grid.core_radius_mm(rules.via_dia_mm)
    stub_r = grid.core_radius_mm(rules.track_width_mm)
    ux, uy = direction
    for step in range(1, _DROP_SEARCH_STEPS + 1):
        reach = config.dogbone_stub_mm * step
        for angle in _DROP_SEARCH_ANGLES:
            cos_a, sin_a = math.cos(angle), math.sin(angle)
            vx, vy = ux * cos_a - uy * sin_a, ux * sin_a + uy * cos_a
            site = (pad[0] + vx * reach, pad[1] + vy * reach)
            if not grid.disk_is_free(layers, site[0], site[1], via_r, net_id):
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
        via_ok = far_ok and grid.disk_is_free(
            range(0, grid.spec.n_layers),
            candidate[0],
            candidate[1],
            grid.core_radius_mm(via_group_extent_mm),
            net_id,
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
    seg_id: int, net_id: int, path: maze.RoutePath, width_mm: float
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
            out += _track_from_run(seg_id, net_id, layer, run, width_mm)
            run = [(x, y)]
            layer = this_layer
        else:
            run.append((x, y))
    out += _track_from_run(seg_id, net_id, layer, run, width_mm)
    return out


def _track_from_run(
    seg_id: int,
    net_id: int,
    layer: int,
    run: list[tuple[float, float]],
    width_mm: float,
) -> list[RealizedTrack]:
    if len(run) < 2:
        return []
    segments = [
        {"shape": "line", "start": list(a), "end": list(b)}
        for a, b in itertools.pairwise(run)
    ]
    length = sum(dist(a, b) for a, b in itertools.pairwise(run))
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
        tracks, vias, unrouted, pours, reasons = _realize_maze(
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
    return RealizeResult(tracks, vias, warnings)


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
    return RealizeResult(tracks, vias, warnings)


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
    return {
        "layers": layers,
        "outline": outline,
        "copper": copper,
        "pads": pads_for_ir(ir, layers, footprints),
    }


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
