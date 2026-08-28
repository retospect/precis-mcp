"""Sketch → copper geometry — the realizer. See
docs/backlog/pcb-guided-place-route.md §"Sketch + realize".

**Runs at CHECKPOINTS, never in the inner loop.** Sketch (L0-L2, plus L3
placement) is canonical; copper (L5) is derived and regenerable, the same
discipline as chunks→embeddings — this module is the "regenerate" half. It
is a pure function of a settled :class:`~precis.pcb.ir.PcbIR` snapshot
(plus obstacle/config data); it never mutates the IR's L0-L3 state (moving
a component, reassigning a layer, or flipping a side happens through
``optimize.py``'s move classes, not here) — it only ever *reads* the
sketch and writes L5-shaped output.

**Geometry is arcs and tangent lines, NOT beziers** (backlog, verbatim):
the shortest path around a single circular clearance obstacle is exactly
straight tangents joined by a circular arc, computable in closed form —
see :func:`tangent_arc_path`. Gerber has arcs natively (G02/G03) and no
bezier primitive, so a bezier would just be flattened to polylines on
export anyway; the exact tangent/arc form is simultaneously cheaper and
more manufacturable.

**Output matches :mod:`precis.pcb.gerber`'s expected model shape exactly**
— a track's ``segments`` entries are ``{"shape": "line"|"arc", "start":
[x, y], "end": [x, y]}`` (arc adds ``"center": [x, y], "cw": bool}``), the
same dict shape :func:`precis.pcb.gerber.copper_gerber` reads via
``_emit_stroke``. This was verified by direct inspection of that
function, not assumed — see :func:`to_gerber_model` and
``tests/test_pcb_realize.py``'s round-trip test through
:func:`precis.pcb.gerber.export_gerbers`.

**Single-obstacle closed form only.** A segment blocked by more than one
obstacle at once is a genuine multi-obstacle rubber-band problem this
module does not attempt to solve in closed form (that is a harder,
iterative shortest-path-in-a-polygon-forest problem, out of this slice's
scope) — it realizes around the single nearest blocking obstacle and
falls back to a straight line with the remaining obstacles reported as
still-blocking, rather than silently emitting a geometrically wrong path.
"Fail legibly" (backlog), applied to the realizer's own limits.

**Per-gap capacity accounting moves INTO this module too** (as data, not
just the optimizer's cheap estimate): every realized segment's binding
gap — the same ``nearest_other_instance`` neighbourhood
:mod:`precis.pcb.optimize` uses for its L4 estimate — is tallied, and a
gap whose strand usage exceeds its capacity produces a legible
:class:`CongestionWarning` naming the blocking gap, the participants, and
the clearance arithmetic (backlog acceptance criterion, verbatim).

**Vias are emitted wherever a track's realized layer differs from its pad
layer** (closing the master backlog's "no via geometry is realized" gap,
2026-08-28) — see :func:`_vias_for_track` for the exact rule and
:class:`RealizedVia` for why it ALWAYS carries a layer SPAN (``layer_lo``/
``layer_hi``), never a scalar layer: a scalar via already made every via
DRC rule silently blind on every layer once (backlog "Bugs this build
produced" #5), and this module is precisely where that shape decision is
made. Via COUNT scales with the net's current annotation
(:func:`precis.pcb.rules.via_count_for_current`) rather than always
emitting exactly one — a via array that can't carry its rail's current is
the same silent-failure class as the missing geometry itself.

**Rip-up primitives**: :func:`rip_net` removes one net's tracks (and its
warnings) from a result, leaving every other net's geometry
byte-identical — the regenerate-in-place discipline copper needs.
:func:`pin_topology` is a thin, documented delegate to
:meth:`precis.pcb.ir.PcbIR.set_side` (pinning a topology choice IS a
sketch edit, not a realizer concern) — kept here only because a caller
mid-rip-up-loop reaches for it in the same breath.
:func:`re_realize_segments` recomputes exactly the named segments'
tracks against an already-realized result, replacing only those entries.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from precis.pcb.capabilities import CapabilityRow, capability_for
from precis.pcb.geom import Point, dist
from precis.pcb.ir import UNSET_LAYER, PcbIR, nearest_other_instance
from precis.pcb.rules import (
    PAD_LAYER,
    NetRules,
    implied_via_count,
    layer_is_outer,
    net_current_a_or_none,
    resolve_net_rules,
)

#: Re-exported from :mod:`precis.pcb.rules` (the constant's new home) so
#: every existing ``from precis.pcb.realize import PAD_LAYER`` call site
#: keeps working. Moved there (2026-08-28, alongside :func:`precis.pcb.
#: rules.implied_via_count`) because :mod:`precis.pcb.cost`'s ``via_count``
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


def _dist_point_to_segment(p: Point, a: Point, b: Point) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 < 1e-12:
        return dist(p, a)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return dist(p, (ax + t * dx, ay + t * dy))


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
    if _dist_point_to_segment(center, start, end) >= radius - 1e-9:
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
        if _dist_point_to_segment(o.center, start, end) < eff_r:
            candidates.append(o)
    if not candidates:
        return None
    return min(candidates, key=lambda o: _dist_point_to_segment(o.center, start, end))


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
    start, end = _instance_point(ir, ia), _instance_point(ir, ib)
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
    (2026-08-28) rather than re-derived here — that function is the exact
    same predicate this docstring used to describe standalone, hoisted out
    so :mod:`precis.pcb.cost`'s ``via_count`` MONEY term can share it and
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


# ── the checkpoint entry point ────────────────────────────────────────────


def realize(
    ir: PcbIR,
    *,
    obstacles: list[Obstacle] | None = None,
    config: RealizeConfig = RealizeConfig(),
    seg_ids: list[int] | None = None,
) -> RealizeResult:
    """Realize every segment in ``seg_ids`` (default: the whole board) —
    the checkpoint call. Never touches the IR (module docstring); pure
    function of a snapshot in, geometry + vias + a legible congestion
    digest out."""
    if obstacles is None:
        obstacles = _default_obstacles(ir, config)
    ids = list(range(ir.n_segments)) if seg_ids is None else seg_ids
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
    rather than the per-net resolution.

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
    return {"layers": layers, "outline": outline, "copper": copper}


__all__ = [
    "PAD_LAYER",
    "CongestionWarning",
    "Obstacle",
    "RealizeConfig",
    "RealizeResult",
    "RealizedTrack",
    "RealizedVia",
    "pin_topology",
    "re_realize_segments",
    "realize",
    "realize_segment",
    "rip_net",
    "tangent_arc_path",
    "to_gerber_model",
]
