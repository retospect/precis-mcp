"""Sketch → copper geometry — the realizer. See
docs/backlog/pcb-guided-place-route.md §"Sketch + realize".

**Runs at checkpoints, never in the inner loop.** Sketch (L0-L2, plus L3
placement) is canonical; copper (L5) is derived and regenerable, same
discipline as chunks→embeddings. A pure function of a settled
:class:`~precis.pcb.ir.PcbIR` snapshot (plus config data) — never
mutates L0-L3 state (that's ``optimize.py``'s move classes) — only reads
the sketch, writes L5-shaped output.

**One router**: ``vector`` (:mod:`precis.pcb.vroute`) — gridless,
octilinear, kernel-exact. The grid maze router, the tangent drawer, the
negotiation loop and the route clean-up pass were removed (Reto
2026-10-20): a track is a centreline polyline and legality is
:mod:`precis.pcb.clearance`'s distance test, so a corridor is open
exactly when the copper fits. What it cannot route is reported in
:attr:`RealizeResult.unrouted` with a per-segment
:class:`UnroutedReason`, never drawn through other copper.

**Vias.** Emitted wherever a route changes layer, gated on the STITCHED
GROUP's full ampacity extent fitting in cleared space (not just the
trace's) — a rail crossing layers through one via is a fuse; stitching
four vias for a group planned around one puts three in somebody else's
copper. See :class:`RealizedVia`, which always carries a layer SPAN
(``layer_lo``/``layer_hi``), never a scalar. Via COUNT scales with the
net's current annotation (:func:`precis.pcb.rules.via_count_for_current`).

**Plane fragment stitching is a PRODUCER, deliberately independent of its
own checker** (:func:`_stitch_plane_fragments`, gripe 270637). A net poured
on several layers used to come out as one electrical net only where a
per-pin drop via *happened* to span every poured sheet. This pass places
deliberate stitching vias instead — a regular-grid sprinkle across each
pair of a net's overlapping poured sheets, a bounded targeted pass for
whatever remains, then a JUMPER (a via out of each fragment plus a trace
on a spare layer) for the same-layer pairs a via provably cannot close.

**This pass computes its own connectivity, with its own union-find
(:class:`_UnionFind`), and never calls
:func:`precis.pcb.connectivity.net_islands`.** ``net_islands`` is the
INDEPENDENT CHECKER for exactly this property; a producer and its checker
must stay independent.

**Rip-up primitives**: :func:`rip_net` removes one net's tracks (and
warnings), leaving every other net's geometry byte-identical.
:func:`pin_topology` delegates to :meth:`precis.pcb.ir.PcbIR.set_side`;
:func:`re_realize_segments` recomputes only the named segments' tracks
against an already-realized result.
"""

from __future__ import annotations

import itertools
import math
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np

# shapely ships no py.typed marker here -- same suppression drc.py/planes.py
# carry, same reason: only the polygon booleans this module's stitching
# pass (below) needs, never a track/via/pad's own shape (that stays this
# module's existing closed-form Point/dist arithmetic).
from shapely.geometry import (
    LineString as _ShapelyLineString,
)
from shapely.geometry import Point as _ShapelyPoint
from shapely.geometry import Polygon as _ShapelyPolygon

from precis.pcb import connectivity as pcb_connectivity
from precis.pcb import geom, landpattern, padplace, pose, vroute
from precis.pcb import silk as pcb_silk
from precis.pcb.capabilities import CapabilityRow, capability_for
from precis.pcb.clearance import Body
from precis.pcb.geom import Point, dist, dist_point_to_segment
from precis.pcb.ir import (
    NO_NET,
    NO_PIN,
    FootprintPad,
    PcbIR,
    footprint_pad_set,
    instance_courtyard_polygon,
    layer_is_routable,
    nearest_other_instance,
    pin_point,
    plane_layers_of,
    routable_layers,
)
from precis.pcb.planes import (
    POWER_NET_CLASSES,
    DerivedRegion,
    PourQuality,
    PourSpec,
    derive_regions,
    plane_pours,
    pour_quality,
    region_polygon,
)
from precis.pcb.rules import (
    PAD_LAYER,
    NetRules,
    layer_is_outer,
    net_current_a_or_none,
    pin_current_a_or_none,
    resolve_net_rules,
    seg_current_a_or_none,
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

# ── config ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RealizeConfig:
    clearance_mm: float = 0.15
    #: Class-generic trace pitch (width + clearance) for GAP-CAPACITY
    #: accounting (:func:`_gap_usage`) — the same fallback
    #: :mod:`precis.pcb.cost`'s ``default_pitch_mm`` uses.
    pitch_mm: float = 0.3
    #: Plane-served nets fan out via a short dog-bone stub instead of a
    #: routed trace — this is that stub's length.
    dogbone_stub_mm: float = 0.5
    #: Corner radius for filleting routed traces, as a MULTIPLE of the
    #: track's own width. ``0.0`` disables filleting. See
    #: :func:`_track_from_run` — the radius is clamped per run so
    #: filleting can only remove copper.
    fillet_radius_tracks: float = 1.5
    #: The fab this board realizes against — every emitted track's width
    #: is clamped to this table's minimum (:mod:`precis.pcb.rules`'s own
    #: resolver discipline).
    fab_caps: CapabilityRow = field(default_factory=lambda: capability_for("4layer"))
    #: ``pcb_net_classes.rules`` overrides, keyed by net_class name.
    class_rules: dict[str, dict[str, Any]] | None = None
    #: IPC-2221 target temperature rise / copper weight for the
    #: current-derived width tier — see :mod:`precis.pcb.rules`.
    temp_rise_c: float = 10.0
    copper_oz: float = 1.0
    #: Which router draws the copper. Only ``'vector'`` exists — the grid
    #: maze router and the tangent drawer were removed (Reto 2026-10-20);
    #: any other value raises in :func:`realize`.
    router: str = "vector"
    #: Whole-pass search-work cap for the vector router, in A* node
    #: expansions shared across all connections (each ``vroute.Request``
    #: gets the remaining budget, capped by its own per-connection
    #: default). Work, not seconds, so a board routes the same on every
    #: machine.
    max_expansions: int = 60000
    #: Keep vias off part-outline silk (gr495393 comment 1): every via
    #: site the router or a fan-out picks stays the via-to-silk clearance
    #: off the outline :func:`precis.pcb.silk.build_silk` will draw.
    #: **Off by default, pending Reto's call** — measured 2026-10-17 it
    #: costs the router floors; turning it on is a floor change.
    silk_via_keepout: bool = False
    #: Keep other nets' TRACKS off a derived power region on a routable
    #: layer (gr495395). Vias still pass.
    reserve_power_region_tracks: bool = False
    #: Regular pitch for stage-1 "sprinkle" stitching-via placement
    #: (:func:`_stitch_plane_fragments`): ``None`` derives it as twice the
    #: resolved via's (diameter + clearance). An explicit override is an
    #: engineering choice about stitching density, never a fab minimum.
    stitch_pitch_mm: float | None = None
    #: Cap on ACCEPTED sprinkle vias per (sheet, sheet) overlap region —
    #: a runtime/copper-budget guard, not a connectivity requirement.
    max_sprinkle_vias_per_overlap: int = 24
    #: Bound on the residue stage's stitch-and-recheck loop, per net.
    max_stitch_iterations: int = 8
    #: Cap on JUMPERS (two vias plus a detour-layer trace,
    #: :func:`_try_plane_jumper`) placed for one net.
    max_plane_jumpers_per_net: int = 4


# ── the closed-form geometric primitive ──────────────────────────────────


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


def through_span(n_layers: int) -> tuple[int, int]:
    """``(layer_lo, layer_hi)`` of a through via: every copper layer. The
    router emits no blind or buried via (Reto 2026-10-18, gr495289: partial
    vias are a costly fab upgrade we do not buy). A via that does not
    connect on a layer still occupies it: the grid reserves its disk there
    and the pour pass cuts an antipad around it."""
    return 0, max(n_layers, 1) - 1


def _resolve_track_rules(
    ir: PcbIR,
    net_id: int,
    layer: int,
    config: RealizeConfig,
    *,
    current_a: float | None = None,
) -> NetRules:
    """This net's resolved geometry rules (module docstring's single
    resolver, shared with :mod:`precis.pcb.drc` and :mod:`precis.pcb.cost`)
    — the ``track_width_mm`` every :class:`RealizedTrack` this module emits
    carries.

    ``current_a`` overrides the net's ``est_current_a`` — the per-segment
    and per-pin callers pass the SEGMENT or PIN's resolved current
    (gr497904: a trunk carries the sum of its downstream loads, a branch
    only its own pin's). ``None`` keeps the net's own annotation."""
    net_class = str(ir.net_class[net_id])
    overrides = (config.class_rules or {}).get(net_class)
    if current_a is None:
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


def _seg_rules(
    ir: PcbIR,
    seg_id: int,
    rules_by_net: dict[int, NetRules],
    config: RealizeConfig,
) -> NetRules:
    """``rules_by_net`` for ``seg_id``'s net, re-resolved against the
    SEGMENT's own current (gr497904) when it differs from the net's —
    a trunk segment sizes wider than a branch of the same net. Via
    diameter and clearance are current-independent, so a segment
    carrying the net's whole annotation reuses the net row. Resolved at
    :data:`PAD_LAYER`, the same layer basis ``rules_by_net`` itself uses."""
    net_id = int(ir.seg_net[seg_id])
    rules = rules_by_net[net_id]
    seg_a = seg_current_a_or_none(ir, seg_id)
    if seg_a is None or seg_a == net_current_a_or_none(float(ir.net_current_a[net_id])):
        return rules
    return _resolve_track_rules(ir, net_id, PAD_LAYER, config, current_a=seg_a)


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
    #: Segment ids the vector router could not route without crossing
    #: another net's copper — the honest residue of a guaranteed-clean
    #: router.
    unrouted: tuple[int, ...] = ()
    #: Copper pours, one per connected fragment of each plane-assigned
    #: layer (:mod:`precis.pcb.planes`). Empty when no net is promoted to a
    #: plane — which is the common case, and was the reason a promoted net
    #: being entirely disconnected went unnoticed for so long.
    pours: tuple[dict[str, Any], ...] = ()
    #: WHY each entry of ``unrouted`` failed — index-aligned by ``seg_id``,
    #: never by position (:func:`_realize_vector` builds ``unrouted`` and
    #: this together but does not promise they land in the same order).
    #: Closes "total routing failure produces no diagnostic at all"
    #: (docs/backlog/pcb-engine-plan.md "BOARD TWO" finding 1): before
    #: this, a caller saw a net name and nothing else, whether the real
    #: cause was a corridor that never existed, a race this connection
    #: lost to one that routed first, or an endpoint walled in on every
    #: allowed layer — three very different fixes wearing one word.
    unrouted_reasons: tuple[UnroutedReason, ...] = ()
    #: Plane-promoted nets :func:`_stitch_plane_fragments` could NOT bring
    #: to a single connected piece within its own bounded attempt (module
    #: docstring's stitching note, gripe 270637). Always empty for
    #: ``router='tangent'`` (that drawer never pours a plane at all) and
    #: usually empty — non-empty is this pass's own
    #: honest "tried and could not", independent of whatever
    #: :func:`precis.pcb.connectivity.net_islands` separately finds.
    unstitched: tuple[UnstitchedNet, ...] = ()
    #: Segment ids :func:`realize` never handed to the router at all —
    #: pcb-pre-place-route-blocks Slice 1's "Realize seam": each of these
    #: segments' own two pins already sit in one connectivity component of
    #: the ``fixed_copper`` :func:`realize` was called with (
    #: :func:`precis.pcb.connectivity.connected_pin_pairs`), so the
    #: authored fabric has already done this connection's job. Disjoint
    #: from ``unrouted`` (a segment is in exactly one of the two — this
    #: one never entered the search at all) and from ``tracks`` (no
    #: derived copper is emitted for a segment realized this way — the
    #: whole point is that none is needed). Always empty when ``realize``
    #: was called with no ``fixed_copper``.
    #:
    #: **Known residue, stated rather than hidden**: only a segment whose
    #: BOTH pins land in one fixed-copper island qualifies for THIS
    #: short-circuit. A net with a free (unconnected) end — fixed copper
    #: reaches one pin but not the other — still gets searched, but now
    #: the search's own start/target set for the fixed pin is that
    #: island's every via centre and track endpoint, not only its pad
    #: (see :attr:`island_terminals`) — a route that starts or ends
    #: mid-span on authored copper, not only pin-to-pin.
    fixed_realized: tuple[int, ...] = ()
    #: Which segments' derived route actually started or ended on a
    #: fixed-copper ISLAND terminal — a via centre or track endpoint other
    #: than the pin's own pad — rather than the plain pad-to-pad corridor
    #: (pcb-pre-place-route-blocks, "island terminals"). Keyed by
    #: ``seg_id``, one short human-readable note per entry (e.g. "started
    #: from fixed via at (12.500, 4.200) on B.Cu"), or on one of the pin's
    #: own non-primary pads ("same-pin pad", gr339236,
    #: :func:`_sibling_pad_terminals_by_pin`). A segment absent here
    #: either routed pad-to-pad as always, or never routed at all — the
    #: honesty this module already applies to ``unrouted``/``unrouted_
    #: reasons`` extended to the OTHER surprising case: a route that
    #: succeeded but not from where a reader would assume.
    island_terminals: dict[int, str] = field(default_factory=dict)
    #: What the negotiated-congestion loop did, when it ran
    #: (``config.negotiate_iterations > 0`` AND the re-ordering passes
    #: left a net unrouted); ``None`` otherwise. The route job prints
    #: :meth:`EscapeReport.line` so a ``negotiate=N`` run that gains
    #: nothing says whether the loop converged, ran out of time, or never
    #: settled — the three very different next steps.
    #: One line per net class whose ``"layers"`` rule names layers this
    #: stackup does not route on while at least one named layer does
    #: (:func:`class_layer_drops`, gr472108): :func:`_net_class_layers`
    #: silently narrows such a class, and only a class left with NOTHING
    #: fails as ``'layer_lock'``.
    class_layer_drops: tuple[str, ...] = ()
    #: PathFinder's final present-congestion per cell (:class:`CongestionWarning`),
    #: from the LAST negotiation iteration's usage map — the
    #: over-subscribed cells the loop ended on, win or lose, grid-to-mm
    #: provenance included. Empty whenever ``negotiate_iterations`` was 0
    #: or no net was left unrouted for the loop to contest.
    #: How whole each plane net's fill is, per (net, layer): pieces,
    #: largest piece's area share, narrowest neck
    #: (:func:`precis.pcb.planes.pour_quality`, pcb-stackup-regions slice
    #: 1). Measured on the final pours, after stitching. Empty for
    #: ``router='tangent'`` and for a board with no planes.
    pour_quality: tuple[PourQuality, ...] = ()
    #: Pour pieces removed after stitching because none of their net's
    #: copper reached them and no via or jumper could
    #: (:func:`_drop_bare_islands`, pcb-floating-pour-island). Reported per
    #: net in the route note; never silent.
    dropped_islands: tuple[DroppedIsland, ...] = ()
    #: What the vector route pass did (:mod:`precis.pcb.vroute`).
    vector: vroute.EscapeReport | None = None


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
      connection reported here survived every attempt anyway. Only
      reported when another net's routed copper actually lies in that
      corridor; a connection that fails with none there is ``'no_path'``
      (gr472026).
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
    - ``'layer_lock'`` — this net's class names a ``"layers"`` restriction
      (Rulings 2026-09-19 item 7, e.g. the EWOD escape nets' ``["B.Cu"]``)
      that resolves to NO usable layer on this board — a name absent from
      the stackup, or one that intersects this board's routable signal
      layers to nothing. Never silently widened back to the full signal-
      layer set; the segment never reaches ``vroute.route`` at all
      (:func:`_net_class_layers`, checked in :func:`_realize_vector`).
    - ``'search_budget'`` — a search ran out of ``max_expansions`` before
      finding a path or proving there is none. Two origins: the segment's
      REAL search on the busy grid exhausted (:func:`_realize_vector` records
      it — reported directly, never re-probed, since a pads-only probe
      that finds a corridor would mislabel the budget failure
      ``'congestion'``; gr489909, 2026-10-20), or the diagnosis's own
      probe exhausted, leaving "walled in" and "too far to search"
      untold apart. The probe case was reported as ``'no_path'`` until
      2026-10-01; the real-search case as ``'congestion'`` until
      2026-10-20 — on a fine grid over a large board each conflation is
      the common case, not a corner.
    """

    seg_id: int
    net_id: int
    kind: str
    message: str
    #: The actual terminal pin ids the router implicated — populated only
    #: where the router KNOWS which terminal failed (today:
    #: ``'plane_drop_blocked'``, the pins :func:`_plane_fanout` could not
    #: give a legal drop-via site). Empty for every span-level kind
    #: (``'congestion'``/``'width'``/``'no_path'``/``'search_budget'``/
    #: ``'layer_lock'``/``'unpourable_plane'``): the diagnosis does not
    #: name an end, and a caller must not invent one — the route-
    #: feedback path treats non-empty ``pins`` as the ONLY escape-class
    #: evidence (feedback.py).
    pins: tuple[int, ...] = ()


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


# ── the vector router ───────────────────────────────────────────────────────


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


def _side_layer[T](ir: PcbIR, inst_id: int, layers: Sequence[T]) -> T:
    """Which end of ``layers`` one instance's pads sit on, driven by
    :attr:`PcbIR.inst_bottom` — bottom-mounted lands on the LAST entry,
    top-mounted on the FIRST (gr341516's rule). **The one implementation**,
    generic over ``layers``' element type so both existing hand-copies of
    this ternary collapse onto it: :func:`pads_for_ir` walks a stackup
    NAME list (``layers[-1] if bottom else layers[0]``, for the gerber/DRC
    pad dict's own ``"layer"`` key) and :func:`_realize_vector` below walks
    :func:`_signal_layers`' routable INDEX list (for the router's
    occupancy grid, which only ever speaks in indices) — two different
    element types answering the identical "which side does this instance
    mount on" question, which is exactly the kind of drift this module's
    own docstring warns "One rule, two call sites" about (see
    :func:`pads_for_ir`'s docstring, gr341516)."""
    return layers[-1] if bool(ir.inst_bottom[inst_id]) else layers[0]


def class_layer_drops(
    stackup: list[dict[str, Any]], class_rules: dict[str, Any] | None
) -> list[str]:
    """One line per net class that names a layer this stackup does not
    route on (absent, or not routable) while another named layer does —
    the partial narrowing :func:`_net_class_layers` applies silently. A
    class with NO surviving layer is not listed: that one fails its nets
    loudly as ``'layer_lock'``."""
    routable = {str(entry.get("name")) for entry in stackup if layer_is_routable(entry)}
    out: list[str] = []
    for cls, rules in sorted((class_rules or {}).items()):
        names = [str(n) for n in ((rules or {}).get("layers") or [])]
        dropped = [n for n in names if n not in routable]
        if dropped and len(dropped) < len(names):
            out.append(
                f"net class {cls!r} asked for {dropped}, not routable on this "
                f"stackup; routing on {sorted(set(names) - set(dropped))}"
            )
    return out


def _net_class_layers(
    ir: PcbIR, net_id: int, config: RealizeConfig, signal_layers: list[int]
) -> tuple[list[int], str | None]:
    """Rulings 2026-09-19 item 7's per-net layer lock: a
    ``pcb_net_classes.rules`` entry may name ``"layers"`` (stackup layer
    NAMES, e.g. ``["B.Cu"]`` for the EWOD escape nets) — resolved here to
    stackup INDICES and intersected with ``signal_layers``, the routable
    set every OTHER net still gets whole. No ``"layers"`` key (the
    overwhelming common case) returns ``signal_layers`` itself, unchanged —
    this function is a no-op for every net that doesn't opt in.

    The second return value is ``None`` for a clean resolution, or a
    human-legible reason naming the offending class/layer — a name this
    stackup doesn't carry, or one that intersects ``signal_layers`` to
    nothing (e.g. a plane-only or otherwise non-routable layer) — for a
    caller (:func:`_realize_vector`) to fail the net LEGIBLY as
    ``UnroutedReason(kind="layer_lock")`` rather than silently routing on
    the full set anyway (module-wide "fail legibly" discipline, same as
    every other :class:`UnroutedReason` kind)."""
    net_class = str(ir.net_class[net_id])
    names = ((config.class_rules or {}).get(net_class) or {}).get("layers")
    if not names:
        return signal_layers, None
    layer_name_to_idx = {
        str(layer.get("name")): i for i, layer in enumerate(ir.stackup)
    }
    missing = [n for n in names if n not in layer_name_to_idx]
    if missing:
        return [], (
            f"net class {net_class!r} names layer(s) {missing} that are not "
            "in this board's stackup"
        )
    allowed = [
        layer_name_to_idx[n] for n in names if layer_name_to_idx[n] in signal_layers
    ]
    if not allowed:
        return [], (
            f"net class {net_class!r}'s layers {list(names)} have no "
            "routable member among this board's signal layers"
        )
    return allowed, None


def _resolve_route_end(
    native_layer: int,
    native_point: Point,
    candidates: _EndTerminals,
    allowed_set: set[int],
) -> tuple[Point | None, int | None, _EndTerminals]:
    """One segment END's actual search terminal, honouring a net-class
    layer lock (Rulings 2026-09-19 item 7): the pin's own native pad wins
    when its layer is already ``allowed_set`` — the overwhelming common
    case, and the ONLY case before a lock could ever exclude a pin's own
    mount side. Otherwise the first real fixed-copper island terminal
    (``candidates``, :func:`_island_terminals_by_pin`'s per-pin answer)
    that IS on an allowed layer substitutes as the PRIMARY point instead
    — the plaza via's own B.Cu landing, for the EWOD escape case — never
    a fabricated point on bare board where this pin has no real copper.
    ``(None, None, candidates)`` when neither the native pad nor any
    candidate lands on an allowed layer: this end genuinely has no legal
    way to route on the locked layer set, for :func:`_realize_vector`'s own
    caller to fail the whole segment on rather than force an illegal
    ``vroute.route`` call (``start_layer``/``goal_layer`` outside its own
    ``layers=`` set refuses unconditionally)."""
    if native_layer in allowed_set:
        # A drilled primary pad's OTHER-layer landings (kind
        # "pad_through", `_sibling_pad_terminals_by_pin`) stay as extra
        # seeds: the pad is copper on every layer, so a route may leave
        # it from the far side with no via beside it (gr489175).
        return native_point, native_layer, candidates
    for i, (term, layer) in enumerate(candidates):
        if layer in allowed_set:
            return term.point, layer, candidates[:i] + candidates[i + 1 :]
    return None, None, candidates


def _outline_bbox(ir: PcbIR) -> tuple[float, float, float, float] | None:
    """The outline's bounding box, or ``None`` when the design authored
    no outline — the one place both ``_outline_clip`` and the routing
    grid's edge keep-out (the edge keep-out) read the
    boundary from."""
    if not ir.outline or len(ir.outline) < 3:
        return None
    xs = [float(p[0]) for p in ir.outline]
    ys = [float(p[1]) for p in ir.outline]
    return (min(xs), min(ys), max(xs), max(ys))


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
    bbox = _outline_bbox(ir)
    if bbox is None:
        return None
    x0, y0 = min(bbox[0], bbox[2]) + clearance_mm, min(bbox[1], bbox[3]) + clearance_mm
    x1, y1 = max(bbox[0], bbox[2]) - clearance_mm, max(bbox[1], bbox[3]) - clearance_mm
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


def _seg_span_mm(ir: PcbIR, seg_id: int) -> float:
    a, b = int(ir.seg_pin_a[seg_id]), int(ir.seg_pin_b[seg_id])
    pa, pb = pin_point(ir, a), pin_point(ir, b)
    if pa is None or pb is None:
        return math.inf
    return dist(pa, pb)


def pad_board_wh(geom: PadGeom, inst_rot: float) -> tuple[float, float]:
    """One pad's width/height in BOARD space — the single answer to "how
    wide is this pad, along the board's own x", for every consumer that
    reads ``PadGeom.w_mm``/``h_mm``.

    A REAL pad arrives already board-oriented: :func:`~precis.pcb.
    padplace.place_footprint_pads` swaps w/h itself when the pad's total
    rotation (instance + the pad's own ``rot``) is 90 degrees or 270, so
    the flashed aperture matches the rotated land, and
    :attr:`PadGeom.axis_aligned` records that it did. A SYNTHESIZED pad
    has no footprint to place, so its w/h are FOOTPRINT-LOCAL
    (:class:`PadGeom`'s own docstring) and the swap has to happen here.

    **Two functions used to answer this question independently and
    disagree** (the defect :func:`precis.pcb.drc._copper_item_polygon`'s
    docstring names as this subsystem's most-repeated one):
    :func:`pads_for_ir` swapped, :func:`pads_for_ir` did not, so on the
    esp32c3 reference board 45 synthesized pads on 90/270-rotated
    instances were claimed by the occupancy grid transposed 90 degrees
    from the pad the DRC, the gerber fallback and the fab preview
    measured. A GND track legally routed 0.1703mm clear of J2.RXD's
    CLAIM then read as 0.040mm from the model's transposed pad and fired
    a clearance error against copper that, at the claim's orientation,
    clears it — which looked exactly like a hole in the occupancy
    guarantee, and was investigated as one before the two pad definitions
    were measured against each other.

    Only rect/obround have an orientation to get wrong: a circle's w/h
    are its diameter, and a polygon pad carries a true rotated ring
    instead. An oblique (non-90-degree-multiple) rotation is NOT handled
    here — :func:`pads_for_ir` claims a conservative enclosing circle for
    that case, and ``pads_for_ir`` emits the true rotated outline as a
    polygon pad (:func:`oblique_pad_ring`), so w/h are not read."""
    if geom.shape not in ("rect", "obround"):
        return geom.w_mm, geom.h_mm
    rot = 0.0 if math.isnan(inst_rot) else inst_rot
    if geom.synthesized and padplace.rect_swaps_wh(rot):
        return geom.h_mm, geom.w_mm
    return geom.w_mm, geom.h_mm


#: Vertices per semicircular cap of an oblique obround's polygon outline.
_OBLIQUE_CAP_SEGMENTS = 16


def pad_is_oblique(geom: PadGeom, inst_rot: float) -> bool:
    """True for a rect/obround pad whose board-space extent is NOT an
    axis-aligned box — the case :func:`pad_board_wh`'s w/h cannot describe
    and :func:`oblique_pad_ring` exists for. Same axis-alignment decision
    :func:`pads_for_ir` makes (a synthesized pad's off the instance
    rotation, a real pad's off :attr:`PadGeom.axis_aligned`)."""
    if geom.shape not in ("rect", "obround"):
        return False
    rot = 0.0 if math.isnan(inst_rot) else inst_rot
    if geom.synthesized:
        return not padplace.pad_axis_aligned(rot)
    return not geom.axis_aligned


def oblique_pad_ring(
    geom: PadGeom, inst_rot: float, *, mirrored: bool = False
) -> list[tuple[float, float]]:
    """The true board-space outline of an oblique rect/obround pad, as
    vertices relative to the pad centre.

    The authored ``w`` x ``h`` outline is rotated by the pad's own ``rot``,
    then placed like any footprint-local offset
    (:func:`~precis.pcb.landpattern.rotate_offset`: mirror, then instance
    rotation). A rect is its four corners; an obround is a stadium — two
    semicircular caps of ``min(w, h) / 2`` joined along the long axis,
    tessellated at :data:`_OBLIQUE_CAP_SEGMENTS` per cap with every vertex
    ON the true arc (so the ring sits at most ~0.5% of the radius inside
    the real outline, always within the pad claim's circumscribed
    circle)."""
    w, h = geom.w_mm, geom.h_mm
    local: list[tuple[float, float]]
    if geom.shape == "obround" and w != h:
        r = min(w, h) / 2.0
        n = _OBLIQUE_CAP_SEGMENTS
        if w > h:
            half = w / 2.0 - r
            cap_a, cap_b, start_a, start_b = (half, 0.0), (-half, 0.0), -90.0, 90.0
        else:
            half = h / 2.0 - r
            cap_a, cap_b, start_a, start_b = (0.0, half), (0.0, -half), 0.0, 180.0
        local = []
        for centre, start in ((cap_a, start_a), (cap_b, start_b)):
            for i in range(n + 1):
                a = math.radians(start + 180.0 * i / n)
                local.append((centre[0] + r * math.cos(a), centre[1] + r * math.sin(a)))
    else:
        hw, hh = w / 2.0, h / 2.0
        local = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
    rot = 0.0 if math.isnan(inst_rot) else inst_rot
    out: list[tuple[float, float]] = []
    for lx, ly in local:
        px, py = landpattern.rotate_offset(lx, ly, geom.pad_rot_deg)
        out.append(landpattern.rotate_offset(px, py, rot, mirrored=mirrored))
    return out


def _net_clearances(
    rules_by_net: dict[int, NetRules], config: RealizeConfig
) -> dict[int, float]:
    """Each net's routing clearance: its class figure, floored at
    ``config.clearance_mm``."""
    return {
        n: max(config.clearance_mm, r.clearance_mm) for n, r in rules_by_net.items()
    }


def _net_layer_clearances(
    ir: PcbIR, rules_by_net: dict[int, NetRules], config: RealizeConfig
) -> dict[tuple[int, int], float]:
    """``(net, layer index)`` -> routing clearance where a net's figure
    depends on the layer: a layer its class names in ``clearance_by_layer``
    (vector-router slice 1), or any layer of a class with
    ``working_voltage_v``, whose floor reads B1 inside, B4 outside on a
    coated board and B2 outside on an uncoated one (slice 2; Reto
    2026-10-18, "Yes, IPC column per layer"). Floored at
    ``config.clearance_mm`` like :func:`_net_clearances`. Empty for a board
    whose classes name neither — the grid is then exactly the per-net
    one."""
    coated = _board_coated(ir)
    out: dict[tuple[int, int], float] = {}
    for n, r in rules_by_net.items():
        if not r.by_layer and not r.columns:
            continue
        for i, layer in enumerate(ir.stackup):
            tiers = r.tiers_on(
                str(layer.get("name")), outer=layer_is_outer(ir, i), coated=coated
            )
            out[(n, i)] = max(config.clearance_mm, tiers.clearance_mm)
    return out


def _board_coated(ir: PcbIR) -> bool:
    """The board takes the parylene coat: it carries an electrode pad (the
    pad half of :func:`precis.pcb.gerber.declares_parylene_coat`; a
    mask-open-only array reads uncoated here, the larger figure)."""
    return any(fp.raw.get("role") == "electrode" for fp in ir.footprint_pads)


def _plane_served_pins(
    ir: PcbIR,
    plane_ids: list[int],
    fixed_terminals_by_pin: dict[tuple[str, str], _EndTerminals],
) -> frozenset[int]:
    """Pins of plane-promoted nets that AUTHORED fixed copper already
    carries onto EVERY one of the net's poured layers: the pin's
    fixed-copper island (:func:`_island_terminals_by_pin` — a fixed via's
    centre on each layer it spans, a fixed track's endpoints on its own
    layer) has a terminal on each layer in the net's ``net_plane_layers``
    mask. Such a pin is already connected to the pour(s) and stitches them;
    :func:`_plane_fanout` must not drill a second drop via beside the
    source's own. A pin whose island reaches only SOME of several pours
    keeps its drop via, which is what ties the rest in. taz15 (2026-10-15):
    every GND/12V_POWER/PV+ pin of the EasyEDA-routed source reaches its
    plane through a source via, and the fan-out either stacked a new via
    on it (via_via_keepout, net stripped) or, with the source via counted
    as an obstacle, found no legal site and failed the whole net as
    ``plane_drop_blocked``."""
    if not fixed_terminals_by_pin or not plane_ids:
        return frozenset()
    plane_nets = {int(ir.seg_net[seg_id]) for seg_id in plane_ids}
    served: set[int] = set()
    for pid in range(ir.n_pins):
        net_id = int(ir.pin_net[pid])
        if net_id not in plane_nets:
            continue
        key = (
            str(ir.instance_refdes[int(ir.pin_instance[pid])]),
            str(ir.pin_label[pid]),
        )
        terminals = fixed_terminals_by_pin.get(key)
        if not terminals:
            continue
        poured = set(plane_layers_of(int(ir.net_plane_layers[net_id])))
        if poured and poured <= {layer for _terminal, layer in terminals}:
            served.add(pid)
    return frozenset(served)


@dataclass(frozen=True, slots=True)
class _RouteContext:
    """Everything one vector route pass is built from, before the first
    search — factored out of :func:`_realize_vector`'s preamble so the
    capacity map (``view='congestion'``) builds the same static context a
    real pass would."""

    signal_layers: list[int]
    pad_geoms: list[PadGeom]
    #: ``(point, net_id, enclosing_radius_mm, layers)`` per pin and per
    #: non-primary footprint land; ``net_id`` uses the ``n_nets + pid``
    #: sentinel for an unconnected pin.
    pads: list[tuple[Point, int, float, tuple[int, ...]]]
    rules_by_net: dict[int, NetRules]
    net_layers: dict[int, list[int]]
    net_layer_lock_fail: dict[int, str]
    #: The smallest net clearance on the board; ``net_clearance`` carries
    #: every net's own, ``clearance_max`` the widest (edge inset, pours).
    clearance: float
    net_clearance: dict[int, float]
    clearance_max: float
    plane_ids: list[int]
    route_ids: list[int]
    order: list[int]
    ink_field: _InkField | None
    island_terminals: dict[int, tuple[_EndTerminals, _EndTerminals]]
    fixed_copper: list[dict[str, Any]] | None
    edge_min: float
    plane_served_pins: frozenset[int] = frozenset()
    net_layer_clearance: dict[tuple[int, int], float] = field(default_factory=dict)
    #: ``(layer, polygon, owner net name)`` areas no other net's track may
    #: cross (:func:`_track_reserved_regions`).
    reserved_regions: tuple[tuple[int, Any, str], ...] = ()


def _route_context(
    ir: PcbIR,
    ids: list[int],
    config: RealizeConfig,
    footprints: dict[str, dict[str, Any]] | None,
    fixed_copper: list[dict[str, Any]] | None,
) -> _RouteContext | None:
    """The :func:`_realize_vector` preamble, standalone — ``None`` when the
    board has no pads or no routable nets."""
    signal_layers = _signal_layers(ir)
    # Every board layer, not just the routable ones — what a DRILLED pad's
    # claim spans (a hole is a physical fact on every layer).
    n_board_layers = len(ir.stackup) or 1
    pad_geoms = pad_geometry(ir, footprints)
    # 4th element: the LAYERS this pad's claim spans. A trace may run on a
    # DIFFERENT layer under an SMD pad but never through a drilled hole.
    pads: list[tuple[Point, int, float, tuple[int, ...]]] = []
    for pid in range(ir.n_pins):
        point = pin_point(ir, pid)
        if point is not None:
            geom = pad_geoms[pid]
            net = int(ir.pin_net[pid])
            if net == NO_NET:
                # An unconnected pin gets its own sentinel, distinct from
                # every real net and from every other NC pin — the pad is
                # real copper nothing may route through.
                net = ir.n_nets + pid
            layers = (
                tuple(range(n_board_layers))
                if geom.drill_mm
                else (_side_layer(ir, int(ir.pin_instance[pid]), signal_layers),)
            )
            pads.append((point, net, math.hypot(geom.w_mm, geom.h_mm) / 2.0, layers))
    if not pads:
        return None
    rules_by_net = {
        n: _resolve_track_rules(ir, n, PAD_LAYER, config) for n in range(ir.n_nets)
    }
    if not rules_by_net:
        return None
    net_layers: dict[int, list[int]] = {}
    net_layer_lock_fail: dict[int, str] = {}
    for n in rules_by_net:
        layers_n, problem = _net_class_layers(ir, n, config, signal_layers)
        net_layers[n] = layers_n
        if problem is not None:
            net_layer_lock_fail[n] = problem
    net_clearance = _net_clearances(rules_by_net, config)
    net_layer_clearance = _net_layer_clearances(ir, rules_by_net, config)
    clearance = min(net_clearance.values())
    clearance_max = max([*net_clearance.values(), *net_layer_clearance.values()])
    # The board-edge inset is a DIFFERENT figure from inter-net clearance
    # (drc.check_board_edge_clearance reads board_edge_clearance_*_mm).
    edge_min = _board_edge_min_mm(config)
    pads.extend(
        _footprint_pad_claims(
            ir,
            [str(entry.get("name") or "") for entry in ir.stackup],
            signal_layers,
            footprints,
            n_board_layers,
        )
    )
    plane_ids = [s for s in ids if int(ir.net_plane_layers[int(ir.seg_net[s])]) != 0]
    route_ids = [s for s in ids if s not in set(plane_ids)]
    # Shortest-first is the opening order. A short connection has the
    # fewest alternative corridors, so letting a long one claim the board
    # first strands it; a heuristic, not a guarantee.
    order = sorted(route_ids, key=lambda s: _seg_span_mm(ir, s))
    ink_field = _courtyard_ink_field(ir, config.fab_caps)
    fixed_terminals_by_pin = _island_terminals_by_pin(ir, footprints, fixed_copper)
    island_terminals = _seg_island_terminals(
        ir,
        route_ids,
        _merge_terminals(
            fixed_terminals_by_pin,
            _sibling_pad_terminals_by_pin(ir, footprints, signal_layers),
        ),
    )
    plane_served_pins = _plane_served_pins(ir, plane_ids, fixed_terminals_by_pin)
    return _RouteContext(
        signal_layers,
        pad_geoms,
        pads,
        rules_by_net,
        net_layers,
        net_layer_lock_fail,
        clearance,
        net_clearance,
        clearance_max,
        plane_ids,
        route_ids,
        order,
        ink_field,
        island_terminals,
        fixed_copper,
        edge_min,
        plane_served_pins,
        net_layer_clearance,
        _track_reserved_regions(ir, config),
    )


def _layer_index(ir: PcbIR) -> tuple[list[str], dict[str, int], int]:
    """``(layer names, name→index, layer count)`` for the stackup."""
    names = [str(e.get("name")) for e in ir.stackup]
    return names, {name: i for i, name in enumerate(names)}, len(names) or 1


def _space_clearance(ir: PcbIR, ctx: _RouteContext) -> Callable[[str, int], float]:
    """The per-(net-name, layer) routing clearance a :class:`vroute.Space`
    indexes by — ``ctx.net_layer_clearance`` then ``ctx.net_clearance``,
    floored at the board minimum for a name the IR does not carry."""
    net_id = {str(ir.net_name[i]): i for i in range(len(ir.net_name))}

    def clearance(net: str, layer: int) -> float:
        nid = net_id.get(net)
        if nid is None:
            return float(ctx.clearance)
        return float(
            ctx.net_layer_clearance.get(
                (nid, layer), ctx.net_clearance.get(nid, ctx.clearance)
            )
        )

    return clearance


def _keepout_obstacles(
    ir: PcbIR, config: RealizeConfig, n_layers: int
) -> list[vroute.Obstacle]:
    """Fiducial candidate sites, mounting-hole/NPTH rings and (opt-in) the
    silk-outline via keep-outs — one ``"__keepout__"``-netted obstacle
    each, so every real net treats them as foreign on every layer.
    ``blocks_vias=True`` makes them bar via sites too; the silk lines are
    ``via_only`` (tented copper under ink is fine, an annulus is not)."""
    out: list[vroute.Obstacle] = []
    all_layers = frozenset(range(n_layers))
    # Fiducials are minted by the handler at render/DRC time; claim the
    # obstacle-independent candidate SUPERSET so the router never draws
    # through the spot the mint later picks.
    if ir.outline and len(ir.outline) >= 3:
        for _corner, (fx, fy) in pcb_silk.fiducial_candidate_sites(ir.outline, ir=ir):
            out.append(
                vroute.Obstacle(
                    Body(
                        _ShapelyPoint(fx, fy),
                        (),
                        pcb_silk.FIDUCIAL_MASK_DIA_MM / 2.0,
                    ),
                    "__keepout__",
                    all_layers,
                    blocks_vias=True,
                )
            )
    npth = _npth_clearance_mm(config)
    for hole in ir.mounting_holes:
        r = max(float(hole.drill_mm), float(hole.ring_dia_mm)) / 2.0
        if r <= 0.0:
            continue
        if not hole.plated and float(hole.ring_dia_mm) <= 0.0:
            # A bare NPTH hole owes copper npth_clearance_mm, not the
            # board's net clearance — grow the keep-out by the difference.
            r += max(0.0, npth - config.clearance_mm)
        out.append(
            vroute.Obstacle(
                Body(_ShapelyPoint(float(hole.x), float(hole.y)), (), r),
                "__keepout__",
                all_layers,
                blocks_vias=True,
            )
        )
    if config.silk_via_keepout:
        for line, margin in pcb_silk.outline_via_keepouts(
            ir, capability=config.fab_caps
        ):
            out.append(
                vroute.Obstacle(
                    Body(_ShapelyLineString(line), (), margin),
                    "__silk__",
                    all_layers,
                    via_only=True,
                )
            )
    return out


def _static_obstacles(
    ir: PcbIR,
    ctx: _RouteContext,
    config: RealizeConfig,
    footprints: dict[str, dict[str, Any]] | None,
    fixed_copper: list[dict[str, Any]] | None,
) -> list[vroute.Obstacle]:
    """Pads, authored fixed copper, keep-outs and reserved track regions —
    everything a route pass is checked against before the first search."""
    names, index, n = _layer_index(ir)
    obstacles: list[vroute.Obstacle] = []
    for pad in pads_for_ir(ir, names, footprints):
        obstacles += vroute.obstacles_of({**pad, "ctype": "pad"}, index, n)
    for row in fixed_copper or []:
        if row.get("ctype") in ("track", "via", "pour"):
            obstacles += vroute.obstacles_of(row, index, n)
    obstacles += _keepout_obstacles(ir, config, n)
    for layer, poly, owner in ctx.reserved_regions:
        obstacles.append(
            vroute.Obstacle(
                Body(poly, (), 0.0), owner, frozenset((layer,)), track_only=True
            )
        )
    return obstacles


def _static_space(
    ir: PcbIR,
    ctx: _RouteContext,
    config: RealizeConfig,
    footprints: dict[str, dict[str, Any]] | None,
) -> tuple[vroute.Space, list[RealizedTrack], list[RealizedVia], list[int], list[int]]:
    """The Space one route pass starts from — static obstacles plus the
    plane fan-out's own stub+drop-via copper, reserved into the space as
    it is placed. Returns the space, the fan-out's copper, the plane
    segments it could not serve and the PIN ids it could not give a legal
    drop."""
    space = vroute.Space(
        _static_obstacles(ir, ctx, config, footprints, ctx.fixed_copper),
        _space_clearance(ir, ctx),
    )
    tracks, vias, failed = _plane_fanout(
        ir,
        ctx.plane_ids,
        space,
        config,
        ctx.rules_by_net,
        ctx.pad_geoms,
        ink_field=ctx.ink_field,
        served_pins=ctx.plane_served_pins,
    )
    unrouted: list[int] = []
    # A pin with no legal drop leaves its net's segments unserved; report
    # the segments (the unit ``unrouted`` is counted in) and keep the pin
    # ids themselves for ``UnroutedReason.pins``.
    if failed:
        stranded = {int(ir.pin_net[p]) for p in failed}
        unrouted += [s for s in ctx.plane_ids if int(ir.seg_net[s]) in stranded]
    return space, tracks, vias, unrouted, list(failed)


def _pads_only_space(
    ir: PcbIR,
    ctx: _RouteContext,
    config: RealizeConfig,
    footprints: dict[str, dict[str, Any]] | None,
    fixed_copper: list[dict[str, Any]] | None,
) -> vroute.Space:
    """The Space :func:`_diagnose_unrouted` probes: every static obstacle
    and pad, no routed copper — answers "does a corridor exist at all"."""
    return vroute.Space(
        _static_obstacles(ir, ctx, config, footprints, fixed_copper),
        _space_clearance(ir, ctx),
    )


def _final_board_space(
    ir: PcbIR,
    tracks: list[RealizedTrack],
    vias: list[RealizedVia],
    ctx: _RouteContext,
    config: RealizeConfig,
    footprints: dict[str, dict[str, Any]] | None,
    fixed_copper: list[dict[str, Any]] | None,
) -> vroute.Space:
    """A FRESH Space stamped from the finished tracks, vias and pads: the
    real final board (pours are never stamped)."""
    names, index, n = _layer_index(ir)
    obstacles = _static_obstacles(ir, ctx, config, footprints, fixed_copper)
    obstacles += _copper_obstacles(tracks, vias, ir, index, n, names)
    return vroute.Space(obstacles, _space_clearance(ir, ctx))


def _via_join_layers(path: vroute.VPath) -> list[tuple[int, int]]:
    """Per via site, the ``(pre, post)`` run layers it joins — a via sits
    exactly on the boundary between consecutive runs (the run ends and
    the next starts at the via's coordinate)."""
    out: list[tuple[int, int]] = []
    for x, y in path.vias:
        lo = hi = 0
        for layer, pts in path.runs:
            if pts[-1] == (x, y):
                lo = layer
            if pts[0] == (x, y):
                hi = layer
        out.append((lo, hi))
    return out


def _vpath_points(path: vroute.VPath) -> list[tuple[float, float, int]]:
    """A ``VPath`` as ``(x, y, layer)`` triples — every run's points on
    its layer, plus each via site on the two run layers it joins."""
    pts = [(x, y, layer) for layer, run in path.runs for x, y in run]
    for (x, y), (lo, hi) in zip(path.vias, _via_join_layers(path), strict=True):
        pts.append((x, y, lo))
        pts.append((x, y, hi))
    return pts


def _describe_used_terminal(
    used_start: vroute.Terminal,
    used_goal: vroute.Terminal,
    terms: tuple[_EndTerminals, _EndTerminals] | None,
) -> str | None:
    """The honesty half of the island-terminal seam: did this successful
    route actually start or end on one of the extra candidates offered,
    rather than the plain pad? Matched by exact coordinate — the vector
    search only ever starts/ends on a terminal's exact point."""
    if not terms:
        return None
    parts: list[str] = []
    for which, used, ends in (
        ("started from", used_start, terms[0]),
        ("ended at", used_goal, terms[1]),
    ):
        for t, layer in ends:
            if layer in used.layers and dist(t.point, (used.x, used.y)) < 1e-6:
                parts.append(
                    f"{which} {_terminal_noun(t)} at "
                    f"({t.point[0]:.3f}, {t.point[1]:.3f}) on {t.layer}"
                )
                break
    return "; ".join(parts) if parts else None


def _copper_obstacles(
    tracks: list[RealizedTrack],
    vias: list[RealizedVia],
    ir: PcbIR,
    index: dict[str, int],
    n: int,
    names: list[str],
) -> list[vroute.Obstacle]:
    """Newly made copper as Space obstacles (gerber-model rows →
    :func:`vroute.obstacles_of`), for stamping into a space."""
    out: list[vroute.Obstacle] = []
    interim = RealizeResult(tuple(tracks), tuple(vias), ())
    for row in result_copper_rows(interim, ir, names):
        out += vroute.obstacles_of(row, index, n)
    return out


def _copper_legal(
    space: vroute.Space,
    tracks: list[RealizedTrack],
    vias: list[RealizedVia],
    net_name: str,
    ir: PcbIR,
    index: dict[str, int],
    n: int,
    names: list[str],
) -> bool:
    """The final copper (filleted corners included) against the space —
    a fillet bulges a corner's copper inward past the straight legs the
    search proved clear, so emit only what the kernel still clears."""
    for ob in _copper_obstacles(tracks, vias, ir, index, n, names):
        for layer in ob.layers:
            if not space.clears(ob.body, net_name, layer, via_site=ob.blocks_vias):
                return False
    return True


def _route_seg_vector(
    space: vroute.Space,
    ir: PcbIR,
    seg_id: int,
    ctx: _RouteContext,
    config: RealizeConfig,
    index: dict[str, int],
    n: int,
    names: list[str],
    spent: list[int],
    island_notes: dict[int, str],
    exhausted: set[int],
) -> tuple[list[RealizedTrack], list[RealizedVia]] | None:
    """One segment's vector route — ``None`` when the search found no
    legal path within its share of the pass budget. Stamps the new copper
    into ``space`` on success; a layer-locked-to-nothing net returns
    ``None`` without searching (:class:`UnroutedReason` ``'layer_lock'``).

    Island terminals (:attr:`RealizeResult.island_terminals`): each end's
    candidate terminals are the pin's own pad first, then its
    fixed-copper island / sibling-pad terminals on an allowed layer; pairs
    are tried in order (cap 4) until ``vroute.route`` succeeds. A pad
    whose own layer the net's class lock excludes simply yields an empty
    ``start.layers ∩ layers`` — the search refuses it fast and the next
    candidate pair is tried, which is what :func:`_resolve_route_end`'s
    substitution did by hand for the grid."""
    a, b = int(ir.seg_pin_a[seg_id]), int(ir.seg_pin_b[seg_id])
    pa, pb = pin_point(ir, a), pin_point(ir, b)
    assert pa is not None and pb is not None  # the caller skips these
    net_id = int(ir.seg_net[seg_id])
    name = str(ir.net_name[net_id])
    rules = _seg_rules(ir, seg_id, ctx.rules_by_net, config)
    allowed = tuple(ctx.net_layers.get(net_id, ctx.signal_layers))
    if not allowed:
        return None
    # `ir.seg_layer` (the LAYER_ASSIGN move) is deliberately NOT consulted:
    # it was a soft preference under the maze too — the router picks the
    # layer that fits.
    width = float(rules.track_width_mm)
    allowed_set = set(allowed)
    terms = ctx.island_terminals.get(seg_id)

    def end_terms(pid: int, point: Point, cand: _EndTerminals) -> list[vroute.Terminal]:
        geom = ctx.pad_geoms[pid]
        pad_layers = (
            frozenset(range(n))
            if geom.drill_mm
            else frozenset(
                {_side_layer(ir, int(ir.pin_instance[pid]), ctx.signal_layers)}
            )
        )
        # Pad first when the search may enter it directly; islands first
        # when it cannot — a class lock whose pin already reaches the
        # locked layer through fixed copper gets its free entry before the
        # pad-only stub+via the term-only layer would have to buy.
        direct = bool(pad_layers & allowed_set)
        pad = vroute.Terminal(point[0], point[1], pad_layers)
        islands = [
            vroute.Terminal(t.point[0], t.point[1], frozenset({layer}))
            for t, layer in cand
            if layer in allowed_set
        ]
        return ([pad] + islands) if direct else (islands + [pad])

    cands_a = end_terms(a, pa, terms[0] if terms else ())
    cands_b = end_terms(b, pb, terms[1] if terms else ())
    n_vias, group_extent = _via_group_extent(ir, seg_id, rules, ctx.clearance)
    via = None
    if rules.via_dia_mm is not None and rules.via_drill_mm is not None:
        # Always offer the via geometry: an ampacity group's full extent
        # when one applies, else the rule's own barrel — a layer change
        # needs the spec whether the ampacity bookkeeping asked for one
        # or not.
        via = vroute.ViaSpec(
            dia_mm=group_extent if group_extent is not None else rules.via_dia_mm,
            drill_mm=rules.via_drill_mm,
        )
    inset = float(ctx.edge_min) + width / 2.0 + vroute.DELTA_MM
    bbox = _outline_bbox(ir)
    if bbox is not None:
        x0, y0, x1, y1 = bbox
        bounds = (x0 + inset, y0 + inset, x1 - inset, y1 - inset)
    else:
        bounds = (-1e6, -1e6, 1e6, 1e6)
    remaining = int(config.max_expansions) - spent[0]
    if remaining <= 0:
        return None
    path: vroute.VPath | None = None
    used_pair: tuple[vroute.Terminal, vroute.Terminal] | None = None
    attempts = 0
    stats: dict[str, Any] = {}
    for ts in cands_a:
        for tg in cands_b:
            if attempts >= 4:
                break
            # No terminal-layer ∩ route-layer check here: a pad sits on its
            # own mount layer and the VIA is exactly what bridges from it
            # into `allowed` — filtering on `allowed_set` would refuse the
            # only legal terminal a layer-assigned segment has.
            attempts += 1
            req = vroute.Request(
                net=name,
                width_mm=width,
                layers=allowed,
                start=ts,
                goal=tg,
                bounds=bounds,
                via=via,
                via_layers=tuple(range(n)),
                max_expansions=max(1, int(config.max_expansions) - spent[0]),
            )
            stats = {}
            path = vroute.route(space, req, stats=stats)
            spent[0] += int(stats.get("expansions", 0))
            if path is not None:
                used_pair = (ts, tg)
                break
        if path is not None or attempts >= 4:
            break
    if stats.get("stop") in ("budget", "exhausted"):
        exhausted.add(seg_id)
    if path is None:
        return None
    if used_pair is not None and terms:
        note = _describe_used_terminal(used_pair[0], used_pair[1], terms)
        if note:
            island_notes[seg_id] = note
    fillet = float(config.fillet_radius_tracks) * width
    made: list[RealizedTrack] = []
    for layer, pts in path.runs:
        run = _track_from_run(
            seg_id, net_id, layer, pts, width, fillet_radius_mm=fillet
        )
        if run and not _copper_legal(space, run, [], name, ir, index, n, names):
            # The fillet bulged copper inward past a leg the search proved
            # clear — emit the unfilleted run instead.
            run = _track_from_run(seg_id, net_id, layer, pts, width)
        made += run
    made_vias: list[RealizedVia] = []
    via_dia = rules.via_dia_mm
    join_layers = _via_join_layers(path)
    if via_dia is not None and rules.via_drill_mm is not None:
        for (vx, vy), (lo_, hi_) in zip(path.vias, join_layers, strict=True):
            pitch = via_dia + ctx.clearance
            off0 = (n_vias - 1) / 2.0 * pitch
            for k in range(n_vias):
                made_vias.append(
                    RealizedVia(
                        seg_id,
                        net_id,
                        vx + off0 - k * pitch,
                        vy,
                        via_dia,
                        rules.via_drill_mm,
                        0,
                        n - 1,
                        "a",
                    )
                )
            if n_vias > 1:
                # The bar joining the group's barrels on the two layers
                # the via joins — the group is ampacity-sized, the bar is
                # what makes it one electrical thing.
                for layer in dict.fromkeys((lo_, hi_)):
                    made += _track_from_run(
                        seg_id,
                        net_id,
                        layer,
                        [(vx - off0, vy), (vx + off0, vy)],
                        via_dia,
                    )
    if not _copper_legal(space, made, made_vias, name, ir, index, n, names):
        return None
    for ob in _copper_obstacles(made, made_vias, ir, index, n, names):
        space.add(ob)
    return made, made_vias


def _realize_vector(
    ir: PcbIR,
    ids: list[int],
    config: RealizeConfig,
    footprints: dict[str, dict[str, Any]] | None = None,
    fixed_copper: list[dict[str, Any]] | None = None,
) -> tuple[
    list[RealizedTrack],
    list[RealizedVia],
    list[int],
    list[dict[str, Any]],
    list[UnroutedReason],
    list[UnstitchedNet],
    dict[int, str],
    vroute.EscapeReport | None,
]:
    """The whole vector route pass — the board's one router
    (:mod:`precis.pcb.vroute`): static obstacles and the plane fan-out,
    then every routed segment net-by-net into one shared
    :class:`vroute.Space`, then the pour/rescue/stitch tail, then the
    unrouted diagnosis. Returns ``(tracks, vias, unrouted, pours,
    reasons, unstitched, island_notes, report)``."""
    ctx = _route_context(ir, ids, config, footprints, fixed_copper)
    if ctx is None:
        return [], [], list(ids), [], [], [], {}, None
    t0 = time.monotonic()
    space, tracks, vias, unrouted, plane_failed_pins = _static_space(
        ir, ctx, config, footprints
    )
    names, index, n = _layer_index(ir)
    island_notes: dict[int, str] = {}
    exhausted: set[int] = set()
    spent = [0]
    budget = int(config.max_expansions)
    edge_inset = max(ctx.clearance_max, ctx.edge_min) + 0.01

    def pour() -> tuple[list[dict[str, Any]], list[int]]:
        return _pour_planes(
            ir,
            tracks,
            vias,
            unrouted,
            ctx.plane_ids,
            ctx.clearance_max,
            edge_inset,
            footprints,
            fixed_copper,
            npth_clearance_mm=_npth_clearance_mm(config),
        )

    # A route that shatters a plane is a net loss even when legal: a net's
    # copper can cut another's pour in two. ``plane_misses`` counts the
    # (un-served plane segment, pour-piece) pair before and after a net's
    # copper; a rise rolls the net back (the structure lifted from
    # ``vroute.escape_pass``'s same guard).
    poured_layers = {
        layer
        for nid in range(len(ir.net_plane_layers))
        for layer in range(n)
        if int(ir.net_plane_layers[nid]) >> layer & 1
    }

    def plane_misses(
        extra_tracks: list[RealizedTrack], extra_vias: list[RealizedVia]
    ) -> tuple[int, int]:
        pours_, missed = _pour_planes(
            ir,
            tracks + extra_tracks,
            vias + extra_vias,
            unrouted,
            ctx.plane_ids,
            ctx.clearance_max,
            edge_inset,
            footprints,
            fixed_copper,
            npth_clearance_mm=_npth_clearance_mm(config),
        )
        return len(missed), len(pours_)

    misses = plane_misses([], []) if poured_layers else (0, 0)
    by_net: dict[int, list[int]] = {}
    for s in ctx.route_ids:
        by_net.setdefault(int(ir.seg_net[s]), []).append(s)

    def seg_span(s: int) -> float:
        pa_, pb_ = (
            pin_point(ir, int(ir.seg_pin_a[s])),
            pin_point(ir, int(ir.seg_pin_b[s])),
        )
        if pa_ is None or pb_ is None:
            return math.inf
        return vroute.octile(pa_, pb_)

    def net_span(segs: list[int]) -> float:
        return sum(seg_span(s) for s in segs)

    routed: set[int] = set()
    no_position: set[int] = set()
    tried = 0
    out_of_budget = False
    # Shortest net first — the same ordering the escape pass used.
    for net in sorted(by_net, key=lambda k: net_span(by_net[k])):
        if spent[0] >= budget:
            out_of_budget = True
            break
        mark = space.mark()
        net_tracks: list[RealizedTrack] = []
        net_vias: list[RealizedVia] = []
        ok = True
        for s in sorted(by_net[net], key=seg_span):
            pa_, pb_ = (
                pin_point(ir, int(ir.seg_pin_a[s])),
                pin_point(ir, int(ir.seg_pin_b[s])),
            )
            if pa_ is None or pb_ is None:
                no_position.add(s)
                continue
            tried += 1
            got = _route_seg_vector(
                space,
                ir,
                s,
                ctx,
                config,
                index,
                n,
                names,
                spent,
                island_notes,
                exhausted,
            )
            if got is None:
                ok = False
                break
            net_tracks += got[0]
            net_vias += got[1]
        if ok and poured_layers & {t.layer for t in net_tracks}:
            now = plane_misses(net_tracks, net_vias)
            if now[0] > misses[0] or now[1] > misses[1]:
                ok = False
            else:
                misses = now
        if not ok:
            space.rollback(mark)
            unrouted += [s for s in by_net[net] if s not in no_position]
            continue
        tracks += net_tracks
        vias += net_vias
        routed.update(by_net[net])
    # Every route segment never attempted (budget ran out mid-board) is
    # failed copper for this run — report it, never pretend it routed.
    for s in ctx.route_ids:
        if s not in routed and s not in no_position and s not in unrouted:
            unrouted.append(s)
    pours, extra_unrouted = pour()
    tracks, vias = _prune_redundant_drop_vias(ir, tracks, vias, pours)
    rescues = _rescue_plane_pins(
        ir,
        tracks,
        vias,
        pours,
        plane_failed_pins,
        ctx=ctx,
        config=config,
        footprints=footprints,
        fixed_copper=fixed_copper,
    )

    def repour(
        kept: list[_PlaneRescue],
    ) -> tuple[
        tuple[list[RealizedTrack], list[RealizedVia], list[dict[str, Any]]],
        dict[tuple[str, int], int],
    ]:
        r_tracks = tracks + [t for r in kept for t in r.tracks]
        r_vias = vias + [v for r in kept for v in r.vias]
        r_pours, _x = _pour_planes(
            ir,
            r_tracks,
            r_vias,
            unrouted,
            ctx.plane_ids,
            ctx.clearance_max,
            edge_inset,
            footprints,
            fixed_copper,
            npth_clearance_mm=_npth_clearance_mm(config),
        )
        return (r_tracks, r_vias, r_pours), _pour_pieces(
            ir, r_tracks, r_vias, r_pours, ctx.pads
        )

    if rescues:
        before_pieces = _pour_pieces(ir, tracks, vias, pours, ctx.pads)
        kept, (tracks, vias, pours) = _keep_splitless_rescues(
            rescues, repour, before_pieces
        )
    unrouted += extra_unrouted
    # Two stitch rounds, each on a fresh final-board space: sprinkle +
    # residue, re-pour, repeat — the same shape the grid pass kept.
    unstitched: list[UnstitchedNet] = []
    for _round in range(2):
        new_vias, new_tracks, unstitched = _stitch_plane_fragments(
            ir,
            tracks,
            vias,
            pours,
            ctx=ctx,
            config=config,
            footprints=footprints,
            fixed_copper=fixed_copper,
        )
        if not new_tracks and not new_vias:
            break
        tracks += new_tracks
        vias += new_vias
        pours, extra2 = pour()
        unrouted += extra2
    reasons = _diagnose_all(
        ir,
        unrouted,
        extra_unrouted,
        set(ctx.plane_ids),
        ctx,
        config,
        fixed_copper=fixed_copper,
        footprints=footprints,
        net_layer_lock_fail=ctx.net_layer_lock_fail,
        routed_tracks=tracks,
        routed_vias=vias,
        plane_failed_pins=plane_failed_pins,
        exhausted=exhausted,
    )
    report = vroute.EscapeReport(
        tried=tried,
        routed=len(routed),
        expansions=spent[0],
        elapsed_s=time.monotonic() - t0,
        out_of_budget=out_of_budget,
    )
    return tracks, vias, unrouted, pours, reasons, unstitched, island_notes, report


def _npth_clearance_mm(config: RealizeConfig) -> float:
    """Copper-to-NPTH clearance the fab wants: ``npth_annular_ring_mm``,
    house default first, then the JLC floor (the ``board_edge`` pattern
    above). ``drc.check_npth_clearance`` measures the same field, so a
    router that clears less lays copper the post-route gate strips."""
    caps = config.fab_caps
    return float(
        caps.house_default.get("npth_annular_ring_mm")
        or caps.jlc_min.get("npth_annular_ring_mm")
        or 0.0
    )


def _via_group_extent(
    ir: PcbIR, seg_id: int, rules: NetRules, clearance: float
) -> tuple[int, float | None]:
    """``(n_vias, group_extent_mm)`` for a layer change on ``seg_id`` — the
    exact figure :func:`_realize_vector` feeds ``vroute.route``'s ``via_dia_mm``
    with, hoisted out so :func:`_diagnose_unrouted` asks the search the
    same question it already asked rather than a smaller, unvalidated one.
    Sized by the SEGMENT's current (:func:`seg_current_a_or_none`,
    gr497904): a branch's layer change needs only its own pin's vias."""
    n_vias = (
        via_count_for_current(
            seg_current_a_or_none(ir, seg_id),
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


def _seg_point_dist(
    p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    n2 = dx * dx + dy * dy
    t = (
        0.0
        if n2 < 1e-18
        else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / n2))
    )
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def _seg_seg_dist(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    d: tuple[float, float],
) -> float:
    """Distance between segments ``ab`` and ``cd`` (0 when they cross)."""

    def orient(
        p: tuple[float, float], q: tuple[float, float], r: tuple[float, float]
    ) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    if orient(a, b, c) * orient(a, b, d) < 0 and orient(c, d, a) * orient(c, d, b) < 0:
        return 0.0
    return min(
        _seg_point_dist(a, c, d),
        _seg_point_dist(b, c, d),
        _seg_point_dist(c, a, b),
        _seg_point_dist(d, a, b),
    )


def _foreign_copper_near_path(
    points: list[tuple[float, float, int]],
    net_id: int,
    tracks: list[RealizedTrack],
    vias: list[RealizedVia],
    reach_mm: float,
) -> dict[int, set[int]]:
    """``{blocking net_id: {layers it contests}}`` — every OTHER net's
    routed track or via lying within ``reach_mm`` (+ its own half width /
    radius) of ``points`` (a ``_vpath_points`` triple list) on the path's
    layers. Empty dict means no foreign copper near the corridor.
    Conservative on arcs: a track's segment endpoints stand in for its
    curve. The blockers are returned, not just detected, so a
    ``'congestion'`` verdict can NAME who won the race (gr495293)."""
    runs = [
        ((a[0], a[1]), (b[0], b[1]), a[2])
        for a, b in itertools.pairwise(points)
        if a[2] == b[2]
    ]
    found: dict[int, set[int]] = {}
    for tr in tracks:
        if tr.net_id == net_id:
            continue
        for seg in tr.segments:
            if "start" not in seg or "end" not in seg:
                continue
            c, d = tuple(seg["start"]), tuple(seg["end"])
            reach = reach_mm + tr.width_mm / 2.0
            for a, b, layer in runs:
                if layer == tr.layer and _seg_seg_dist(a, b, c, d) <= reach:
                    found.setdefault(int(tr.net_id), set()).add(int(tr.layer))
                    break
            else:
                continue
            break
    for v in vias:
        if v.net_id == net_id:
            continue
        reach = reach_mm + v.dia_mm / 2.0
        for a, b, layer in runs:
            if (
                v.layer_lo <= layer <= v.layer_hi
                and _seg_point_dist((v.x, v.y), a, b) <= reach
            ):
                found.setdefault(int(v.net_id), set()).update(
                    range(v.layer_lo, v.layer_hi + 1)
                )
                break
    return found


def layer_names(ir: PcbIR, indices: Sequence[int]) -> list[str]:
    """Stackup layer names for routable indices — ``F.Cu``-style names a
    failure reason can cite instead of a bare stackup index (gr495293:
    the reason must say WHICH layer)."""
    return [
        str(ir.stackup[i].get("name", f"layer{i}"))
        if 0 <= i < len(ir.stackup)
        else f"layer{i}"
        for i in indices
    ]


def _constricting_pad_names(
    ir: PcbIR,
    thin_points: list[tuple[float, float, int]],
    net_id: int,
    pads: list[tuple[Point, int, float, tuple[int, ...]]],
    width_mm: float,
    clearance: float,
    *,
    max_names: int = 6,
) -> list[str]:
    """Which pads pinch the corridor the thin probe found — the blocking
    participants a ``'width'`` verdict names (gr495293). A pad of a
    DIFFERENT net whose enclosing circle comes within
    ``pad_radius + width_mm/2 + clearance`` of the probe path on a shared
    layer is what the required-width search cannot squeeze past. Pads are
    labelled ``REFDES.PIN (net)`` by matching their centre back to the
    pin that minted them; an unpinned footprint pad reads as
    ``pad@(x,y) (net)``. ``width_mm`` is the REQUIRED width, not the
    probe's."""
    runs = [
        ((a[0], a[1]), (b[0], b[1]), a[2])
        for a, b in itertools.pairwise(thin_points)
        if a[2] == b[2]
    ]
    if not runs:
        return []
    pin_label_at: dict[tuple[float, float], str] = {}
    for pid in range(ir.n_pins):
        pt = pin_point(ir, pid)
        if pt is None:
            continue
        inst = int(ir.pin_instance[pid])
        pin_label_at[(round(pt[0], 4), round(pt[1], 4))] = (
            f"{ir.instance_refdes[inst]}.{ir.pin_label[pid]}"
        )
    names: list[str] = []
    for point, pad_net, pad_r, pad_layers in pads:
        if int(pad_net) == net_id:
            continue  # our own pads are endpoints, not obstacles
        shared = [r for r in runs if r[2] in pad_layers]
        if not shared:
            continue
        reach = pad_r + width_mm / 2.0 + clearance
        if not any(_seg_point_dist(point, a, b) <= reach for a, b, _layer in shared):
            continue
        centre = (round(float(point[0]), 4), round(float(point[1]), 4))
        label = pin_label_at.get(centre, f"pad@({point[0]:.2f},{point[1]:.2f})")
        owner = (
            str(ir.net_name[pad_net])
            if 0 <= int(pad_net) < len(ir.net_name)
            else "unconnected"
        )
        entry = f"{label} ({owner})"
        if entry not in names:
            names.append(entry)
        if len(names) >= max_names:
            break
    return names


def _track_reserved_regions(
    ir: PcbIR, config: RealizeConfig
) -> tuple[tuple[int, Any, str], ...]:
    """``(layer, polygon, owner net name)`` for each derived power region
    (:attr:`PcbIR.plane_regions`) on a ROUTABLE layer, when
    :attr:`RealizeConfig.reserve_power_region_tracks` is on; empty
    otherwise. On a plane-only layer no track can cross a region anyway."""
    if not config.reserve_power_region_tracks:
        return ()
    routable = set(routable_layers(ir))
    out: list[tuple[int, Any, str]] = []
    for (net_id, layer), (region, _priority) in sorted(ir.plane_regions.items()):
        if layer not in routable:
            continue
        poly = region_polygon(region)
        if poly is None or poly.is_empty:
            continue
        out.append((layer, poly, str(ir.net_name[net_id])))
    return tuple(out)


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
        x, y = float(pad["x"]), float(pad["y"])
        # A DRILLED pad's land is real copper on every layer, not only the
        # outer one `pads_for_ir` reports in `pad["layer"]` — that
        # function's own docstring says so and names `pad["drill"]` as the
        # "spans every layer" signal DRC reads
        # (`drc.py::clearance_pairs_indexed`). This pass did not, so an
        # INNER plane poured solid through every through-hole barrel on
        # the board: measured 2026-09-27 on the EWOD dogfood the first
        # time a GND plane was authored on In1.Cu, `pour[GND] <->
        # pad[HV_RAIL]` / `pad[I2C_SDA]` / `pad[I2C_SCL]` at -0.09mm, the
        # pogo terminal's four through-hole rings. The fill has to yield
        # to a hole the fab will physically drill.
        pad_layers = list(layers) if pad.get("drill") else [str(pad["layer"])]
        if pad.get("shape") == "circle":
            out.append(
                {
                    "ctype": "via",
                    "net": net,
                    "x": x,
                    "y": y,
                    "dia_mm": float(pad["w"]),
                    "layers": pad_layers,
                }
            )
            continue
        half_w = float(pad["w"]) / 2.0
        half_h = float(pad.get("h", pad["w"])) / 2.0
        # The rect fake is a `pour`, which carries ONE `layer` key, so a
        # drilled rectangular pad needs one item per layer rather than a
        # `layers` list. (`_via_layer_names`' override is read for
        # `ctype='via'` only.)
        out.extend(
            {
                "ctype": "pour",
                "net": net,
                "layer": pad_layer,
                "polygon": [
                    [x - half_w, y - half_h],
                    [x + half_w, y - half_h],
                    [x + half_w, y + half_h],
                    [x - half_w, y + half_h],
                ],
            }
            for pad_layer in pad_layers
        )
    return out


def _mounting_hole_blockers(
    ir: PcbIR,
    layers: list[str],
    *,
    npth_clearance_mm: float = 0.0,
    clearance: float = 0.0,
) -> list[dict[str, Any]]:
    """Every :attr:`~precis.pcb.ir.PcbIR.mounting_holes` entry as a fake
    every-layer ``via`` blocker for :func:`~precis.pcb.planes.plane_pours`
    — same reshaping trick (and same reason) as :func:`_pad_blockers`
    above: the pour must antipad around a drill and its solder-nut ring,
    or a filled layer renders solid copper through a hole the fab will
    cut (and a floating nut ring merged into a plane is a picture of a
    short). ``net=""`` never equals a pour's net, so every plane yields
    an antipad; the diameter is the larger of drill and ring, unbuffered
    (``plane_pours`` applies its own uniform ``clearance_mm``). A bare
    NPTH hole grows by ``npth_clearance_mm - clearance`` per side, so the
    pour clears it by the fab's NPTH figure, as :func:`_keepout_obstacles`
    does for routed copper."""
    out: list[dict[str, Any]] = []
    for hole in ir.mounting_holes:
        dia = max(float(hole.drill_mm), float(hole.ring_dia_mm))
        if dia <= 0.0:
            continue
        if not hole.plated and float(hole.ring_dia_mm) <= 0.0:
            dia += 2.0 * max(0.0, npth_clearance_mm - clearance)
        out.append(
            {
                "ctype": "via",
                "net": "",
                "x": float(hole.x),
                "y": float(hole.y),
                "dia_mm": dia,
                "layers": list(layers),
            }
        )
    return out


def _prune_redundant_drop_vias(
    ir: PcbIR,
    tracks: list[RealizedTrack],
    vias: list[RealizedVia],
    pours: list[dict[str, Any]],
) -> tuple[list[RealizedTrack], list[RealizedVia]]:
    """Drop a plane fan-out's stub+via when its own pad already sits IN
    its net's pour on the pad layer — the pour IS the connection there
    (:func:`precis.pcb.planes.plane_pours` deliberately does not carve
    around own-net copper), so the dogbone adds a drilled hole and a
    stub for nothing. User review 2026-09-01: "R2 has two superfluous
    vias" on the motor board — every top-poured net's SMD pin was
    getting a drop via it didn't need, because :func:`_plane_fanout`
    runs BEFORE pouring and cannot know the pour will reach the pad.

    Runs after :func:`_pour_planes` and before
    :func:`_stitch_plane_fragments`, which rebuilds its grid and its
    node set from the PRUNED lists — so if a removed via happened to be
    the only bridge between this net's pours on two layers, the stitcher
    sees the disconnection and places a via where the join actually
    needs one, instead of at whichever pin the fan-out reached first.
    Containment is asked of the pad CENTRE against the pour polygon,
    holes included: the pour floods over own-net pads, so a covered
    pad's centre is interior, while a pad the pour only grazes keeps its
    dogbone (removal must never turn "connected by construction" into
    "connected if the DRC's tangency test agrees")."""
    layer_names = [str(layer.get("name")) for layer in ir.stackup]
    pad_layer_name = layer_names[PAD_LAYER] if layer_names else None
    pour_by_net: dict[str, Any] = {}
    for pour in pours:
        if str(pour.get("layer")) != pad_layer_name:
            continue
        poly = _pour_polygon(pour)
        if not poly.is_empty:
            net = str(pour.get("net", ""))
            prev = pour_by_net.get(net)
            pour_by_net[net] = poly if prev is None else prev.union(poly)
    if not pour_by_net:
        return tracks, vias
    dead_tracks: set[int] = set()
    dead_vias: set[tuple[float, float, int]] = set()
    for t_idx, track in enumerate(tracks):
        if not track.is_dogbone or not track.segments:
            continue
        seg = track.segments[0]
        if seg.get("shape") != "line":
            continue
        poly = pour_by_net.get(str(ir.net_name[track.net_id]))
        if poly is None:
            continue
        pad_pt = seg["start"]
        if not poly.contains(_ShapelyPoint(float(pad_pt[0]), float(pad_pt[1]))):
            continue
        end = seg["end"]
        dead_tracks.add(t_idx)
        dead_vias.add((float(end[0]), float(end[1]), track.net_id))
    if not dead_tracks:
        return tracks, vias
    kept_tracks = [t for i, t in enumerate(tracks) if i not in dead_tracks]
    kept_vias = [v for v in vias if (v.x, v.y, v.net_id) not in dead_vias]
    return kept_tracks, kept_vias


#: How many of :func:`_drop_via_site`'s nominal steps a derived power
#: region must cover around each of its net's pads
#: (:func:`derive_power_regions`). Two: the first step is where nearly every
#: drop via lands, the second absorbs a neighbour having taken it.
REGION_REACH_STEPS = 2


def derive_power_regions(
    ir: PcbIR, config: RealizeConfig
) -> tuple[list[tuple[int, int, DerivedRegion]], list[str]]:
    """Derived power regions for this placement (pcb-stackup-regions slice
    4): ``[(net_id, layer, region)]`` plus a note per power net left
    without one. The IR side of :func:`precis.pcb.planes.derive_regions`.

    The region layer is the first stackup layer marked ``power_regions``
    (the hybrid's In2.Cu). Candidates are nets whose class is a power class
    (:data:`precis.pcb.planes.POWER_NET_CLASSES`) and that pour nowhere yet
    — a net with a plane (authored, stackup or the anneal's) keeps it.

    The reach is :data:`REGION_REACH_STEPS` of :func:`_drop_via_site`'s
    nominal step (pad radius + via radius + clearance, at least the stub)
    for the net's largest pad. The fan-out searches only inside the region
    for such a net (:func:`_plane_fanout`), so a pin whose near sites are
    all taken looks further out within it, and failing that is a stranded
    pin the rescue pass routes — never a via dropped outside its region.
    The edge inset is the pour rim's (:func:`_realize_vector`)."""
    layers = [i for i, e in enumerate(ir.stackup) if e.get("power_regions")]
    if not layers or not ir.outline:
        return [], []
    layer = layers[0]
    # A whole-board plane on the region layer (an authored op='plane_net',
    # or the anneal's) is an instruction that the layer is that net's:
    # carving regions out of it would contradict it. A fill is not one.
    for n in range(ir.n_nets):
        if int(ir.net_plane_layers[n]) & (1 << layer):
            region, priority = ir.plane_regions.get((n, layer), (None, 0))
            if region is None and priority >= 0:
                return [], [
                    f"{ir.stackup[layer].get('name')} pours {ir.net_name[n]} "
                    "over the whole board, so no power regions were derived there"
                ]
    rules_by_net = {
        n: _resolve_track_rules(ir, n, PAD_LAYER, config) for n in range(ir.n_nets)
    }
    if not rules_by_net:
        return [], []
    clearance = max(_net_clearances(rules_by_net, config).values())
    net_pins: dict[str, list[tuple[float, float]]] = {}
    reach: dict[str, float] = {}
    for n in range(ir.n_nets):
        if str(ir.net_class[n]).lower() not in POWER_NET_CLASSES:
            continue
        if int(ir.net_plane_layers[n]) != 0:
            continue
        pids = [int(p) for p in np.flatnonzero(ir.pin_net == n)]
        pts = [p for pid in pids if (p := pin_point(ir, pid)) is not None]
        if len(pts) < 2:
            continue
        name = str(ir.net_name[n])
        net_pins[name] = [(float(x), float(y)) for x, y in pts]
        via_r = float(rules_by_net[n].via_dia_mm or 0.0) / 2.0
        pad_r = max(
            math.hypot(float(ir.pin_w[pid]), float(ir.pin_h[pid])) / 2.0 for pid in pids
        )
        reach[name] = REGION_REACH_STEPS * max(
            config.dogbone_stub_mm, pad_r + via_r + clearance
        )
    if not net_pins:
        return [], []
    regions, notes = derive_regions(
        net_pins,
        outline=[[float(p[0]), float(p[1])] for p in ir.outline],
        edge_clearance_mm=max(clearance, _board_edge_min_mm(config)) + 0.01,
        clearance_mm=clearance,
        reach_mm=reach,
        current_a={
            str(ir.net_name[n]): c
            for n in range(ir.n_nets)
            if (c := net_current_a_or_none(float(ir.net_current_a[n]))) is not None
        },
    )
    by_name = {str(ir.net_name[n]): n for n in range(ir.n_nets)}
    return [(by_name[r.net], layer, r) for r in regions], notes


def _pour_planes(
    ir: PcbIR,
    tracks: list[RealizedTrack],
    vias: list[RealizedVia],
    unrouted: list[int],
    plane_ids: list[int],
    clearance: float,
    edge_inset: float,
    footprints: dict[str, dict[str, Any]] | None = None,
    fixed_copper: list[dict[str, Any]] | None = None,
    *,
    npth_clearance_mm: float = 0.0,
) -> tuple[list[dict[str, Any]], list[int]]:
    """Pour every plane-assigned layer over the FINISHED copper.

    Last, because a pour is defined by what it has to avoid and cannot be
    computed before the thing it avoids exists. The copper list is built by
    :func:`to_gerber_model` rather than assembled here, so "what shape is a
    track" has one answer — plus every placed PAD (:func:`_pad_blockers`,
    its own docstring has the defect this closes), which ``to_gerber_model``
    does NOT fold into ``model["copper"]``. Returns the pours and any
    additional unrouted segments the pouring revealed.

    **AUTHORED fixed copper blocks too** (``fixed_copper`` — the same rows
    :func:`_static_obstacles` claims on the router's grid). Those rows
    are already in :mod:`precis.pcb.gerber` model shape (``pcb_copper_
    list`` unions them into exactly this dict shape for DRC), so they go
    in verbatim: a fixed track carries ``layer``/``width_mm``/``segments``
    and a fixed via carries ``x``/``y``/``dia_mm``/``span``, and ``span``
    is layer NAMES, which is what :func:`precis.pcb.drc._via_layer_names`
    reads. Without them a pour floods straight over an authored via
    barrel: measured 2026-09-27 on the EWOD dogfood the first time a GND
    plane was authored on In1.Cu — 61 ``clearance`` ERRORS, every one
    ``pour[GND] <-> via[ARR1_RxCy]`` at -0.09mm, one per plaza via the
    fill swallowed. Same shape of miss as the router's own
    (docs/backlog/pcb-escape-and-driver-chain.md item 2): a fixture the
    pass cannot see is one it draws straight through.
    """
    layer_names = [str(layer.get("name")) for layer in ir.stackup]
    # One spec per (net, poured layer) — net_plane_layers is a bitmask, so
    # a net may pour several layers, and since pcb-stackup-regions slice 4
    # a layer may carry several nets: each bit's region and priority ride
    # :attr:`PcbIR.plane_regions` (absent = whole board, priority 0).
    # Specs go in net-id order, so on a layer two WHOLE-BOARD nets at one
    # priority still resolve as they always did — the LOWEST net id pours,
    # deterministically, and the other shows up as disconnected in
    # check_connectivity rather than as a plausible board (the reporting
    # direction that gets the contradiction fixed).
    specs: list[PourSpec] = []
    for n in range(ir.n_nets):
        for layer_idx in plane_layers_of(int(ir.net_plane_layers[n])):
            region, priority = ir.plane_regions.get((n, layer_idx), (None, 0))
            specs.append(PourSpec(layer_idx, str(ir.net_name[n]), region, priority))
    pours: list[dict[str, Any]] = []
    if specs and ir.outline:
        interim = RealizeResult(tuple(tracks), tuple(vias), ())
        copper = (
            to_gerber_model(interim, ir, layers=layer_names, outline=[])["copper"]
            + _pad_blockers(ir, layer_names, footprints)
            + _mounting_hole_blockers(
                ir,
                layer_names,
                npth_clearance_mm=npth_clearance_mm,
                clearance=clearance,
            )
            + list(fixed_copper or [])
        )
        pours = plane_pours(
            outline=[[float(p[0]), float(p[1])] for p in ir.outline],
            layers=layer_names,
            specs=specs,
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


def fixed_copper_pours(
    ir: PcbIR,
    config: RealizeConfig,
    footprints: dict[str, dict[str, Any]] | None,
    fixed_copper: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The pours a realize pass would lay over ``fixed_copper`` alone: the
    same :func:`_pour_planes` call, clearances and bare-island drop as
    :func:`realize`, with no routed tracks or vias. For a board whose only
    copper is authored (an import with ``--copper fixed``, gr492989): its
    plane and pour-carried nets reach their pads through the fill, and a
    DRC model without the fill reads every such net as split. ``[]`` when
    the stackup pours nothing or the board has no outline."""
    rules_by_net = {
        n: _resolve_track_rules(ir, n, PAD_LAYER, config) for n in range(ir.n_nets)
    }
    if not rules_by_net:
        return []
    clearance_max = max(_net_clearances(rules_by_net, config).values())
    pours, _ = _pour_planes(
        ir,
        [],
        [],
        [],
        [],
        clearance_max,
        max(clearance_max, _board_edge_min_mm(config)) + 0.01,
        footprints,
        fixed_copper,
        npth_clearance_mm=_npth_clearance_mm(config),
    )
    if not pours:
        return []
    pours, _dropped = _drop_bare_islands(
        ir,
        [],
        [],
        pours,
        pads_for_ir(ir, [str(e.get("name")) for e in ir.stackup], footprints),
        fixed_copper,
    )
    return pours


# ── courtyard ink avoidance (round-4 silk review, item 8 residue) ─────────
#
# A part's courtyard OUTLINE is placement-derived and known in full before
# a single track is planned: nothing about where it is drawn depends on
# which corridor a router later picks (contrast a track/pad, which is
# copper the router itself decides). So a via that lands under it is not
# an unavoidable collision the way two nets contesting the same corridor
# is -- it is this pass never having asked a question whose answer was
# available for free. The two via-placement sites below (a plane drop via,
# :func:`_drop_via_site`; a straightened via transition, :func:`_shove_vias`)
# each defer to courtyard ink SOFTLY: prefer an ink-free legal site, but
# place the via under ink rather than fail the net outright -- a routed
# connection beats an unrouted one every time, silk legibility is a review
# concern, not a DRC one. The route search's own via mask/cost is
# deliberately untouched (out of scope): these two sites are where the
# round-4 review's remaining clips (U1/C5 motor board, D4 nano board) were
# traced to -- THT/plane-heavy spots, i.e. plane drop vias, with a route
# layer-change via as the secondary case.


#: Half the drawn courtyard stroke's own width. `instance_courtyard_polygon`
#: called at `clearance_mm=`:func:`~precis.pcb.silk.silk_clearance_mm`'s
#: result (as :func:`_courtyard_ink_field` below does, the same ring
#: :func:`precis.pcb.silk.build_silk` actually strokes) already returns the
#: silk CENTRELINE -- that function's own docstring's chain ends "+ drawn
#: stroke width / 2 -> silk CENTRELINE". So the ring polyline is not the ink
#: itself, it is the line a `stroke_width_mm`-wide pen travels along; a via
#: clips real ink once it comes within this half-width of that centreline,
#: not only when it crosses the mathematical polyline.
_INK_HALF_STROKE_MM = pcb_silk.DEFAULT_SILK_WIDTH_MM / 2.0


@dataclass(frozen=True, slots=True)
class _InkField:
    """Every drawn-courtyard-ring edge on the board, in world coordinates,
    each carrying its own precomputed bbox -- built ONCE per
    :func:`_realize_vector` call (route-independent, module note above) and
    threaded read-only through every route attempt and via-placement site
    that wants to avoid it. :meth:`clips` rejects a candidate with four
    float compares before it ever runs the real point-to-segment distance,
    so a per-via-candidate query costs O(nearby segments), not O(every
    part on the board) -- consulted, per the module note above, once for
    EVERY candidate site a via search tries, not once per via placed.
    """

    segments: tuple[tuple[float, float, float, float, float, float, float, float], ...]

    def clips(self, x: float, y: float, radius_mm: float) -> bool:
        """Does a disk of copper radius ``radius_mm`` centred at
        ``(x, y)`` touch drawn courtyard ink -- the via's own copper plus
        the ink's half-stroke width (:data:`_INK_HALF_STROKE_MM`), never
        the bare polyline distance alone."""
        test = radius_mm + _INK_HALF_STROKE_MM
        for ax, ay, bx, by, minx, miny, maxx, maxy in self.segments:
            if x < minx - test or x > maxx + test or y < miny - test or y > maxy + test:
                continue
            if dist_point_to_segment((x, y), (ax, ay), (bx, by)) <= test:
                return True
        return False


def _ink_field_from_rings(rings: list[list[Point]]) -> _InkField:
    """Every ring's edges, bbox-precomputed, flattened into one
    :class:`_InkField` -- the shared segment-building step
    :func:`_courtyard_ink_field` uses for a real board and a test uses to
    build a small, controlled one."""
    segments: list[tuple[float, float, float, float, float, float, float, float]] = []
    for ring in rings:
        for a, b in itertools.pairwise(ring):
            minx, maxx = (a[0], b[0]) if a[0] <= b[0] else (b[0], a[0])
            miny, maxy = (a[1], b[1]) if a[1] <= b[1] else (b[1], a[1])
            segments.append((a[0], a[1], b[0], b[1], minx, miny, maxx, maxy))
    return _InkField(tuple(segments))


def _courtyard_ink_field(ir: PcbIR, capability: CapabilityRow) -> _InkField:
    """Every PLACED instance's DRAWN courtyard ring (the silk CENTRELINE
    :func:`precis.pcb.silk.build_silk` actually strokes, not the bare
    clearance-0 hull :func:`precis.pcb.silk.world_courtyard_rings` returns
    for furniture avoidance -- see this section's module note above for
    why the offset matters here), in world coordinates.

    ``capability`` is ``config.fab_caps`` (:class:`RealizeConfig`'s own
    field, never ``None`` -- it defaults to a real capability row), the
    SAME row :func:`_board_edge_min_mm`/:func:`_outline_clip` already read,
    so this reuses the board's actual published silk-to-mask figures
    rather than guessing a fallback the way a module with no capability
    visibility at all would have to.

    A pinless placed instance (mounting hole, fiducial) contributes
    nothing -- no land pattern, no courtyard to protect. Bottom-side
    mirroring is ignored on purpose, the same call
    :func:`~precis.pcb.silk.world_courtyard_rings` makes and for the same
    reason (that function's own docstring): a mirror flips a ring about
    its own centre, and this is a SOFT via-placement preference with
    millimetres of its own slack, not a clearance guarantee."""
    margin_mm = pcb_silk.silk_clearance_mm(
        capability, stroke_width_mm=pcb_silk.DEFAULT_SILK_WIDTH_MM
    )
    pins_of_inst: dict[int, list[int]] = {}
    for pid in range(ir.n_pins):
        pins_of_inst.setdefault(int(ir.pin_instance[pid]), []).append(pid)
    rings: list[list[Point]] = []
    for inst in range(ir.n_instances):
        cx, cy = float(ir.inst_x[inst]), float(ir.inst_y[inst])
        if math.isnan(cx) or math.isnan(cy):
            continue
        ring_local = instance_courtyard_polygon(
            ir, inst, clearance_mm=margin_mm, pins=pins_of_inst.get(inst, [])
        )
        if not ring_local:
            continue
        rot = float(ir.inst_rot[inst])
        rings.append(
            landpattern.place_points(
                ring_local,
                cx=cx,
                cy=cy,
                rot_deg=0.0 if math.isnan(rot) else rot,
                mirrored=False,
            )
        )
    return _ink_field_from_rings(rings)


@dataclass(frozen=True, slots=True)
class _FanoutPin:
    """One plane pin's drop-via request, resolved by :func:`_plane_fanout`
    before any pin places copper."""

    pid: int
    net_id: int
    seg_id: int
    point: Point
    direction: tuple[float, float]
    pad_layer: int
    lo: int
    hi: int
    pad_radius: float
    rules: NetRules
    pour_reaches_pad: bool
    edge_clip: tuple[float, float, float, float] | None
    #: The net's derived region shrunk by the via radius, or ``None``
    #: (:func:`_drop_via_site`'s ``inside``).
    region_area: Any = None
    #: ``rules`` with the stub at the fab floor, tried when the net's own
    #: width finds no site; ``None`` when the net is already at the floor.
    narrow_rules: NetRules | None = None
    #: Which of the pin's parallel drops this is (0 = the primary); the
    #: extra drops carry the pad's current beyond one via's ampacity.
    copy: int = 0
    #: Unique reservation key: ``pid`` for the primary drop.
    key: int = -1


def _region_areas(ir: PcbIR, rules_by_net: dict[int, NetRules]) -> dict[int, Any]:
    """Net id -> its derived region (:attr:`PcbIR.plane_regions`) shrunk by
    its via radius, for every net poured inside a region."""
    out: dict[int, Any] = {}
    for (net_id, _layer), (region, _priority) in ir.plane_regions.items():
        poly = region_polygon(region)
        if poly is None:
            continue
        via_r = float(rules_by_net[net_id].via_dia_mm or 0.0) / 2.0
        out[net_id] = poly.buffer(-via_r)
    return out


def _plane_fanout(
    ir: PcbIR,
    plane_ids: list[int],
    space: vroute.Space,
    config: RealizeConfig,
    rules_by_net: dict[int, NetRules],
    pad_geoms: list[PadGeom],
    ink_field: _InkField | None = None,
    served_pins: frozenset[int] = frozenset(),
) -> tuple[list[RealizedTrack], list[RealizedVia], list[int]]:
    """Connect every pin of a plane-promoted net DOWN to its plane.

    ``served_pins`` (:func:`_plane_served_pins`) are the pins authored
    fixed copper already carries onto a poured layer; they get no stub
    and no drop via here.

    Two passes (pcb-stackup-regions slice 1): every pin's site is found
    and reserved first (``held`` — the reservation is pure geometry, the
    Space's incremental claim index being unchanged between a site check
    and its placement), then each pin releases its own reservation and
    places its stub and via, so pin order no longer decides which
    neighbour loses its site.

    ``ink_field`` (:func:`_courtyard_ink_field`), forwarded to
    :func:`_drop_via_site` unchanged, is the plane drop via's own defer-
    to-silk-softly preference.

    One stub plus one drop via per PIN, not per segment. A net's segments
    are a spanning TREE over its pins (``ir.from_graph``), so per-segment
    fanout emitted a stub once per tree EDGE — several for a high-degree
    pin, none for a pin the tree happened to reach once. Both a plane
    connection is a property of a *pin*, not of a connection.

    The stub runs outward along the pin's own land-pattern offset — away
    from the part body, which is where the escape has to go anyway — on
    the pin's OWN copper layer (:func:`_side_layer`; a bottom-mounted
    part gets a B.Cu stub, not an F.Cu one), and ends in a via spanning
    that layer to the plane layer. That via is
    what :func:`precis.pcb.planes.plane_pours` deliberately does not carve
    an antipad around: it is the connection.

    Returns the pins it could not give a legal drop to, except a pin whose
    net's only pour is on its own layer: that one trusts the pour, and the
    post-pour connectivity check reports it if the fill misses its land
    (the comment at ``pour_reaches_pad``). A plane connection
    that will not fit is an unrouted connection and is reported as one."""
    tracks: list[RealizedTrack] = []
    vias: list[RealizedVia] = []
    failed: list[int] = []
    # The OUTER copper pair by index, which is where a pad physically is —
    # deliberately the whole stackup rather than :func:`_signal_layers`,
    # because mount side decides a pad's layer and routability has no say
    # in it. `_side_layer` reads the first/last entry, so this is the same
    # (top, bottom) pair `pads_for_ir` picks from a stackup NAME list.
    outer_layers = range(len(ir.stackup))
    seg_of_net: dict[int, int] = {}
    for seg_id in plane_ids:
        seg_of_net.setdefault(int(ir.seg_net[seg_id]), seg_id)
    # Every drop via this call places, (x, y, via_radius_mm), NET-BLIND —
    # shared across every net's pins in this one pass, not reset per net.
    # ``Space.clears``' same-net exemption (right for a trace legally
    # ending on its own pad) is the wrong exemption for two via BARRELS:
    # a same-net cluster of GND drop vias is exactly where two are most
    # likely to land close together, and their drilled holes do not care
    # whose net they carry. See :func:`_via_clears_vias`'s own docstring
    # (gripe 269811 comment 2: two SCL drop vias measured 0.1598mm apart,
    # dia 0.75mm each — copper overlapping by 0.590mm and, worse, their
    # 0.25mm drills overlapping too).
    # Seeded with the board's AUTHORED vias (``ir.fixed_vias``, every
    # ``pcb_fixed_copper`` via row), for the same net-blind reason.
    placed_via_sites: list[tuple[float, float, float]] = [
        (float(v.x), float(v.y), float(v.dia_mm) / 2.0) for v in ir.fixed_vias
    ]
    pending: list[_FanoutPin] = []
    extra_drops: list[_FanoutPin] = []
    region_areas = _region_areas(ir, rules_by_net)
    # A power class's current-sized width is a TRUNK width; the stub is one
    # pin's tie to its plane. Drawn at 2.77 mm (nano GND) it cannot leave a
    # 0.25 mm pad between neighbours 1 mm away, so every fine-pitch pin of
    # the net was stranded before routing began and walled in by signal
    # copper before the rescue ran (gr492108: nano GND's and V5's last
    # pieces were exactly these pins). The fab floor is the fallback, as
    # the rescue and the jumper already use it.
    floor_width = resolve_net_rules(
        "", layer_is_outer=True, fab_caps=config.fab_caps
    ).track_width_mm
    for net_id, seg_id in sorted(seg_of_net.items()):
        # A net may now be poured on SEVERAL layers at once
        # (net_plane_layers is a bitmask). One drop via per pin still
        # suffices: a via is ALWAYS a contiguous span (RealizedVia's own
        # docstring — never a scalar layer) and a through barrel connects
        # every layer it passes, not just its two ends. So spanning from
        # the pad's own layer to the FARTHEST of this net's poured layers
        # reaches every nearer poured layer for free, and is
        # simultaneously the stitching connection between them.
        plane_layers = plane_layers_of(int(ir.net_plane_layers[net_id]))
        rules = rules_by_net[net_id]
        for pid in range(ir.n_pins):
            if int(ir.pin_net[pid]) != net_id:
                continue
            if pid in served_pins:
                continue  # the source's own via already reaches the pour
            point = pin_point(ir, pid)
            if point is None:
                continue
            # THIS pin's own copper layer (:func:`_side_layer`, the same
            # bottom/top rule `pads_for_ir` builds the DRC/gerber pad set
            # with), never the shared `PAD_LAYER` — a bottom-mounted pin
            # gets a stub on B.Cu, starting on the copper its own pad sits
            # on (measured 2026-09-28 on the EWOD dogfood).
            pad_layer = _side_layer(ir, int(ir.pin_instance[pid]), outer_layers)
            # The net's ONLY pour is on this pin's own copper layer. The
            # land USUALLY sits inside the fill, but not always — so the
            # pin still gets its stub and zero-span drop via whenever one
            # has a legal site. Only a pin with NO legal site trusts the
            # pour instead of failing the net as `plane_drop_blocked`.
            pour_reaches_pad = set(plane_layers) == {pad_layer}
            lo, hi = through_span(len(ir.stackup))
            dx, dy = float(ir.pin_dx[pid]), float(ir.pin_dy[pid])
            norm = math.hypot(dx, dy)
            # A pin at its instance's exact centroid has no outward
            # direction to offer. +x is arbitrary but deterministic.
            ux, uy = (dx / norm, dy / norm) if norm > 1e-9 else (1.0, 0.0)
            geom = pad_geoms[pid]
            pad_radius = math.hypot(geom.w_mm, geom.h_mm) / 2.0
            # The stub carries the pad's own current — the pin's resolved
            # exchange (gr497904), not the net's peak — necked to the pad
            # where the pad is narrower (never under the fab floor).
            pin_a = pin_current_a_or_none(ir, pid)
            pin_rules = rules
            if pin_a is not None and pin_a != net_current_a_or_none(
                float(ir.net_current_a[net_id])
            ):
                pin_rules = _resolve_track_rules(
                    ir, net_id, PAD_LAYER, config, current_a=pin_a
                )
            stub_rules = pin_rules
            neck = max(floor_width, min(geom.w_mm, geom.h_mm))
            if neck < pin_rules.track_width_mm:
                stub_rules = replace(pin_rules, track_width_mm=neck)
            primary = _FanoutPin(
                pid,
                net_id,
                seg_id,
                point,
                (ux, uy),
                pad_layer,
                lo,
                hi,
                pad_radius,
                stub_rules,
                pour_reaches_pad,
                _outline_clip(
                    ir,
                    _board_edge_min_mm(config) + (rules.via_dia_mm or 0.0) / 2.0,
                ),
                region_areas.get(net_id),
                replace(pin_rules, track_width_mm=floor_width)
                if floor_width < stub_rules.track_width_mm
                else None,
                key=pid,
            )
            pending.append(primary)
            # One via carries only `via_capacity_a` of the pad's current
            # (a pad on a different layer than its pour reaches the plane
            # through its drop vias alone): add parallel drops until their
            # summed ampacity covers it. Placed after every primary so an
            # extra drop never takes a neighbour's primary site.
            if not pour_reaches_pad and rules.via_dia_mm is not None:
                need = via_count_for_current(
                    pin_a,
                    rules.via_dia_mm,
                )
                for c in range(1, need):
                    extra_drops.append(
                        replace(primary, copy=c, key=pid + c * (int(ir.n_pins) + 1))
                    )
    pending.extend(extra_drops)

    # A held reservation: ``key -> (via site, via radius, stub leg ends,
    # stub half-width, stub layer)``. The Space itself is never touched by
    # a reservation — every site check against it is the kernel-exact
    # query (:meth:`vroute.Space.clears` with ``via_site=True``,
    # :meth:`vroute.Space.clears_legs`), and the held set supplies exactly
    # the mutual-collision checks two un-placed reservations still owe
    # each other: via-to-via (``_via_clears_vias``), stub-leg-to-stub-leg
    # and stub-leg-to-held-via on the pad's own layer.
    held: dict[int, tuple[Point, float, Point, Point, float, int, NetRules]] = {}

    def held_sites() -> list[tuple[float, float, float]]:
        return [(h[0][0], h[0][1], h[1]) for h in held.values()]

    def held_legs() -> list[tuple[Point, Point, float, int]]:
        return [(h[2], h[3], h[4], h[5]) for h in held.values()]

    def site_for(pin: _FanoutPin) -> tuple[Point, NetRules] | None:
        """The pin's drop site and the rules its stub is drawn with: the
        net's own width first, then the fab floor."""
        name = str(ir.net_name[pin.net_id])
        for rules in (pin.rules, pin.narrow_rules):
            if rules is None:
                continue
            site = _drop_via_site(
                space,
                pin.point,
                pin.direction,
                name,
                rules,
                config,
                range(pin.lo, pin.hi + 1),
                pin.pad_layer,
                pin.pad_radius,
                placed_via_sites + held_sites(),
                pin.edge_clip,
                ink_field=ink_field,
                inside=pin.region_area,
                held_legs=held_legs(),
                held_sites=held_sites(),
            )
            if site is not None:
                return site, rules
        return None

    # Pass 1 — reserve. Every pin's preferred site (via disk and stub
    # corridor) is held before any pin draws copper: a pin placed early
    # can no longer run its stub or drop its via across the site a later
    # neighbour needed (pcb-stackup-regions slice 1).
    for pin in pending:
        found = site_for(pin)
        if found is not None:
            site, rules = found
            assert rules.via_dia_mm is not None
            held[pin.key] = (
                site,
                rules.via_dia_mm / 2.0,
                pin.point,
                site,
                rules.track_width_mm / 2.0,
                pin.pad_layer,
                rules,
            )

    # Pass 2 — place, releasing each pin's reservation just before its own
    # search. A pin the reservations starve gets one retry with every
    # remaining reservation lifted; those pins are re-reserved at their
    # sites afterwards, so the hold can cost a pin its preferred site but
    # never its drop.
    for i, pin in enumerate(pending):
        held.pop(pin.key, None)
        stub_end = site_for(pin)
        if stub_end is None and held:
            lifted = {q: held.pop(q) for q in list(held)}
            stub_end = site_for(pin)
            if stub_end is not None:
                _place_drop(pin, *stub_end, space, ir, tracks, vias, placed_via_sites)
            for later in pending[i + 1 :]:
                old = lifted.get(later.key)
                if old is not None:
                    held[later.key] = old
            # An extra (parallel) drop that finds no site is not a failed
            # pin: the primary drop connects it; the shortfall is reported
            # by the power-drop ampacity check on the finished copper.
            if stub_end is None and not pin.pour_reaches_pad and pin.copy == 0:
                failed.append(pin.pid)
            continue
        if stub_end is None:
            if not pin.pour_reaches_pad and pin.copy == 0:
                failed.append(pin.pid)
            continue
        _place_drop(pin, *stub_end, space, ir, tracks, vias, placed_via_sites)
    return tracks, vias, failed


def _place_drop(
    pin: _FanoutPin,
    stub_end: Point,
    rules: NetRules,
    space: vroute.Space,
    ir: PcbIR,
    tracks: list[RealizedTrack],
    vias: list[RealizedVia],
    placed_via_sites: list[tuple[float, float, float]],
) -> None:
    """Draw and claim one pin's stub and drop via at ``stub_end``, the stub
    at ``rules``' width (:func:`_plane_fanout`'s ``site_for``) — claimed
    into the Space so a later pin's site check already sees it."""
    point, pad_layer, lo, hi = pin.point, pin.pad_layer, pin.lo, pin.hi
    name = str(ir.net_name[pin.net_id])
    tracks.append(
        RealizedTrack(
            pin.seg_id,
            pin.net_id,
            pad_layer,
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
    )
    space.add(
        vroute.Obstacle(
            Body(
                _ShapelyLineString([point, stub_end]),
                (),
                rules.track_width_mm / 2.0,
            ),
            name,
            frozenset((pad_layer,)),
        )
    )
    assert rules.via_dia_mm is not None and rules.via_drill_mm is not None, (
        "_drop_via_site returns None when either is unresolved"
    )
    space.add(
        vroute.Obstacle(
            Body(_ShapelyPoint(*stub_end), (), rules.via_dia_mm / 2.0),
            name,
            frozenset(range(lo, hi + 1)),
            blocks_vias=True,
        )
    )
    vias.append(
        RealizedVia(
            seg_id=pin.seg_id,
            net_id=pin.net_id,
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


#: How far out from a pad a drop via may be pushed to find clear copper,
#: as a multiple of the nominal stub length. Beyond this the escape is
#: genuinely blocked and saying so beats drawing a longer and longer stub
#: through a congested fanout.
_DROP_SEARCH_STEPS = 6
#: The eight octilinear unit directions, counter-clockwise from +x. The
#: diagonal component is one shared ``sqrt(0.5)`` float so a diagonal
#: stub's |dx| equals |dy| EXACTLY, which is what keeps the emitted
#: segment recognisably 45-degree under :func:`vroute.is_octilinear`'s epsilon
#: — ``cos(pi/4)`` and ``sin(pi/4)`` can differ in the last ulp and
#: would not.
_OCTO = math.sqrt(0.5)
_OCTO_DIRS: tuple[tuple[float, float], ...] = (
    (1.0, 0.0),
    (_OCTO, _OCTO),
    (0.0, 1.0),
    (-_OCTO, _OCTO),
    (-1.0, 0.0),
    (-_OCTO, -_OCTO),
    (0.0, -1.0),
    (_OCTO, -_OCTO),
)
#: Direction-table offsets (multiples of 45 degrees) tried at each reach,
#: in order: straight out along the pin's own offset — quantised to the
#: nearest octilinear direction, because the stub it aims is DRAWN copper
#: and every drawn segment must be a multiple of 45 degrees — then
#: progressively off-axis, ending with the full reversal. This replaces
#: the old free-radian sweep ``(0.0, 0.4, -0.4, 0.9, -0.9, 1.6, -1.6)``,
#: whose stubs were the largest single source of arbitrary-angle copper
#: on a rendered board.
_DROP_SEARCH_OFFSETS = (0, 1, -1, 2, -2, 3, -3, 4)


def _via_clears_vias(
    x: float,
    y: float,
    via_radius_mm: float,
    placed: list[tuple[float, float, float]],
    clearance_mm: float,
) -> bool:
    """May a via of this (undilated) copper radius be centred at
    ``(x, y)`` without landing on, or crowding, ANY already-placed via in
    ``placed`` — mirrors :meth:`vroute.Space.
    the via-site pad rule`'s own formula and reasoning exactly, asked of OTHER
    vias instead of pads, and just as deliberately NET-BLIND.

    ``clears``'s same-net exemption is right for a TRACE (it must
    legally end on its own pad) and wrong here for the identical reason
    ``the via-site pad rule`` itself exists: two via BARRELS this close is a
    broken drill bit or an unintended slot, regardless of whose net(s)
    they carry — a same-net cluster of plane drop vias (several GND pins
    close together) is exactly where two are most likely to land close,
    so exempting same-net pairs would leave the most common case
    unguarded. Checking the COPPER (dia) radius with the same clearance
    margin ``the via-site pad rule`` uses is sufficient to also protect the
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
    space: vroute.Space,
    pad: Point,
    direction: tuple[float, float],
    net_name: str,
    rules: NetRules,
    config: RealizeConfig,
    layers: range,
    pad_layer: int,
    pad_radius_mm: float,
    placed_via_sites: list[tuple[float, float, float]],
    edge_clip: tuple[float, float, float, float] | None = None,
    ink_field: _InkField | None = None,
    inside: Any = None,
    banned_sites: tuple[Point, ...] = (),
    held_sites: list[tuple[float, float, float]] | None = None,
    held_legs: list[tuple[Point, Point, float, int]] | None = None,
) -> Point | None:
    """Where this pin's drop via can legally sit, or ``None``.

    ``banned_sites`` are skipped (within 1 µm); ``held_sites`` /
    ``held_legs`` are :func:`_plane_fanout`'s un-placed reservations —
    via disks and stub corridors another pin has already claimed for its
    own drop, honoured exactly as placed copper (via-to-via spacing,
    stub-leg-to-stub-leg spacing and stub-leg-to-held-via spacing).

    ``inside`` (a shapely polygon, pcb-stackup-regions slice 4) confines
    the via centre to the net's derived region, already shrunk by the via
    radius: a via outside it would land in another net's pour.

    ``pad_layer`` is the copper the STUB is drawn on — the pin's own side
    (:func:`_side_layer`), which is what :func:`_plane_fanout` draws on.
    Checking the corridor on a hardcoded ``PAD_LAYER`` instead asks a
    bottom-mounted pin's question of the top layer: the answer is about
    copper that is not there (measured 2026-09-28 on the EWOD dogfood).

    **Defers to courtyard ink, softly.** ``ink_field``
    (:func:`_courtyard_ink_field`) — among every LEGAL candidate this
    sweep tries, the first one whose via annulus does not touch drawn
    courtyard ink is returned; if none is ink-free, the first legal
    candidate found is returned instead of failing the net.

    The via asks :meth:`vroute.Space.clears` with ``via_site=True`` on
    every layer its barrel spans — which is what makes it clear its OWN
    pad too (pads are ``blocks_vias`` obstacles, counted foreign to a via
    site whatever their net — the kernel-exact answer to what the grid's
    ``the via-site pad rule`` mask used to do separately; a via landing on the
    very pad it drops from is the defect that check exists to bar). The
    stub asks :meth:`vroute.Space.clears_legs` — an exact segment-distance
    query, not a sampled disk sweep.

    **And it must stay inside the board.** ``edge_clip`` is
    :func:`_outline_clip` inset for this via's own radius; a candidate
    outside it is rejected so the search simply tries the next angle or
    reach.
    """
    if rules.via_dia_mm is None or rules.via_drill_mm is None:
        return None  # this fab publishes no via figures — don't invent any
    via_radius_mm = rules.via_dia_mm / 2.0
    clearance = space.cl(net_name, pad_layer)
    stub_half = rules.track_width_mm / 2.0
    # The nominal reach a via needs from its own pad is not a flat
    # constant: it is exactly the distance via-site clearance itself
    # requires — the pad's own (enclosing-circle) radius, plus the via's,
    # plus the net's clearance. `config.dogbone_stub_mm` stays as a FLOOR,
    # not the figure itself.
    base_reach = max(config.dogbone_stub_mm, pad_radius_mm + via_radius_mm + clearance)
    ux, uy = direction
    # Quantise the pin's outward direction to the nearest octilinear table
    # entry: the stub this search aims is drawn copper, and every drawn
    # segment is required to be a multiple of 45 degrees.
    base = round(math.atan2(uy, ux) / (math.pi / 4.0)) % 8
    extra_sites = placed_via_sites + list(held_sites or ())
    extra_legs = list(held_legs or ())
    # The first LEGAL site found, remembered as the fallback the ink
    # preference below yields to when no legal candidate is ink-free.
    fallback_site: Point | None = None
    for step in range(1, _DROP_SEARCH_STEPS + 1):
        reach = base_reach * step
        for offset in _DROP_SEARCH_OFFSETS:
            vx, vy = _OCTO_DIRS[(base + offset) % 8]
            site = (pad[0] + vx * reach, pad[1] + vy * reach)
            if any(
                abs(site[0] - bx) < 1e-3 and abs(site[1] - by) < 1e-3
                for bx, by in banned_sites
            ):
                continue
            if edge_clip is not None and not (
                edge_clip[0] <= site[0] <= edge_clip[2]
                and edge_clip[1] <= site[1] <= edge_clip[3]
            ):
                continue
            if inside is not None and not inside.contains(_ShapelyPoint(*site)):
                continue
            disk = Body(_ShapelyPoint(*site), (), via_radius_mm)
            if not all(
                space.clears(disk, net_name, layer, via_site=True) for layer in layers
            ):
                continue
            if not _via_clears_vias(
                site[0], site[1], via_radius_mm, extra_sites, clearance
            ):
                continue
            # The stub between pad and via has to be clear too — a via in
            # free space fed by a trace through someone else's copper is
            # still a short. Exact leg query in the Space, plus the two
            # reservation-collision checks no Space can answer.
            if not space.clears_legs([(pad, site)], stub_half, net_name, pad_layer)[0]:
                continue
            if any(
                layer == pad_layer
                and _seg_seg_dist(pad, site, ha, hb) < stub_half + hh + clearance
                for ha, hb, hh, layer in extra_legs
            ):
                continue
            if any(
                _seg_point_dist((hx, hy), pad, site) < stub_half + hr + clearance
                for hx, hy, hr in extra_sites
            ):
                continue
            if fallback_site is None:
                fallback_site = site
            if ink_field is None or not ink_field.clips(
                site[0], site[1], via_radius_mm
            ):
                return site
    return fallback_site


# ── plane fragment stitching (gripe 270637) ───────────────────────────────
#
# See the module docstring's "Plane fragment stitching" note for the two
# stages and the independence-from-connectivity.py discipline. Everything
# below is scoped to one call, from `_realize_vector`, after both routing AND
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


def _spread_order(
    cands: list[tuple[float, float]], cap: int
) -> Iterator[tuple[float, float]]:
    """Iterate ``cands`` so the first ``cap`` accepted sites SPREAD across
    the whole list instead of clustering at its head. `_grid_candidates`
    enumerates its grid bottom-row-first; consuming that order directly
    under a per-overlap cap put every accepted sprinkle via into the
    lowest rows of a large overlap — measured on the nano fixture as two
    ~20-via rows marching along the board's bottom edge (round-3 review
    item 1). A strided interleave (every k-th candidate first, then the
    off-by-one pass, …) makes the first full pass an even sample of the
    region, while still eventually visiting every candidate so a blocked
    site falls through to its neighbours."""
    n = len(cands)
    if n <= cap or cap <= 0:
        yield from cands
        return
    k = math.ceil(n / cap)
    for off in range(k):
        yield from (cands[idx] for idx in range(off, n, k))


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


def _sites_in(region: Any, via_r: float, pitch: float) -> list[Point]:
    """Candidate via sites spread across ``region`` — the port of the old
    ``_snapped_sites_in``, minus the snap: the vector router reaches any
    coordinate exactly, so no cell-centre constraint exists. The
    representative point is tried first (it is the one point shapely
    guarantees is inside the polygon), then a sparse sweep — same spread
    discipline as before, continuous sites instead of grid nodes. A
    fragment that fits no via radius at all offers no site, which is the
    honest answer, not a gap."""
    if region.is_empty:
        return []
    minx, miny, maxx, maxy = region.bounds
    step = max(
        max(maxx - minx, maxy - miny) / _JUMPER_SWEEP_DIVISIONS,
        2.0 * via_r,
        pitch,
    )
    rp = region.representative_point()
    out: list[Point] = [(rp.x, rp.y)]
    seen: set[tuple[int, int]] = set()
    y = miny + step / 2.0
    while y <= maxy:
        x = minx + step / 2.0
        while x <= maxx:
            key = (round(x / via_r), round(y / via_r))
            if key not in seen and region.contains(_ShapelyPoint(x, y)):
                seen.add(key)
                out.append((x, y))
            if len(out) >= _JUMPER_SITES_PER_FRAGMENT:
                return out
            x += step
        y += step
    return out


def _clears_foreign_pours(
    shape: Any, layers: range, foreign_pours: dict[int, list[Any]]
) -> bool:
    """Does ``shape`` (already grown by clearance) miss every OTHER net's
    poured copper on every layer in ``layers``?

    **This is the one keep-out the Space cannot answer.** The Space
    :func:`_stitch_plane_fragments` builds is stamped from tracks, vias
    and pads — never from pours, which do not exist yet when a route pass
    runs and are not stamped afterwards. That gap is stated as a known
    limitation for a stitching VIA, where both fixtures happen to avoid
    it. A jumper cannot inherit that reasoning: it puts a whole TRACE, of
    arbitrary length, on a layer chosen precisely because this net was
    not already using it — which on any real board is where another net's
    plane lives. So the guard is asked here, geometrically, against the
    finished pour polygons themselves."""
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
    space: vroute.Space,
    net_name: str,
    n_layers: int,
    via_dia_mm: float,
    via_drill_mm: float,
    via_is_legal: Any,
    track_width_by_layer: dict[int, float],
    foreign_pours: dict[int, list[Any]],
    config: RealizeConfig,
    bounds: tuple[float, float, float, float],
) -> tuple[list[RealizedVia], RealizedTrack] | None:
    """A same-net JUMPER between two poured fragments: a via down out of
    fragment A, a trace across a spare layer, a via back up into fragment
    B. Returns the two vias and the trace, or ``None`` when no legal one
    exists (which is a real answer, not a failure to try hard enough).

    **Why this exists at all.** :func:`_stitch_one_net`'s stage 2 proves
    that a single via can only join two fragments that geometrically
    OVERLAP — so two fragments of one net on the SAME layer, which by
    definition never overlap, are unreachable by any number of vias.

    **The detour layer is never either fragment's own layer**, and that
    restriction is about the CHECKER, not about taste. A pour joins the
    connectivity union-find by containment of a primitive's FIRST point
    only (:func:`precis.pcb.connectivity.net_islands`), so a bare trace
    ending inside fragment B registers as connected to A and not to B.
    A via at each end has its own centre inside its own fragment, so both
    joins are containment-true by construction, and the barrel asserts the
    layer change independently of any geometry.

    Every candidate is checked in cost order — the cheap arithmetic guards
    (containment, via legality, mutual barrel spacing) before the A* — and
    nothing is placed or stamped until the whole jumper is known to be
    legal, so a half-built jumper can never be left behind by a late
    rejection."""
    layer_a, poly_a = frag_a
    layer_b, poly_b = frag_b
    via_r = via_dia_mm / 2.0
    clearance = space.cl(net_name, layer_a)
    pitch = max(2.0 * via_r, clearance)
    sites_a = _sites_in(poly_a, via_r, pitch)
    sites_b = _sites_in(poly_b, via_r, pitch)
    if not sites_a or not sites_b:
        return None

    detours = [
        d
        for d in range(n_layers)
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
    lo, hi = through_span(n_layers)
    for detour in detours:
        width_mm = track_width_by_layer[detour]
        for gap, qa, qb in pairs:
            if attempts >= _JUMPER_ROUTE_ATTEMPTS:
                break
            # The two barrels are checked against each OTHER by hand:
            # ``via_is_legal`` asks about vias already PLACED, and neither
            # of these is placed yet.
            if gap - 2.0 * via_r < clearance:
                continue
            if not via_is_legal(qa[0], qa[1], lo, hi):
                continue
            if not via_is_legal(qb[0], qb[1], lo, hi):
                continue
            # A through barrel may cross a foreign plane: the re-pour that
            # follows cuts its antipad, as it does for any other via.
            attempts += 1
            path = vroute.route(
                space,
                vroute.Request(
                    net=net_name,
                    width_mm=width_mm,
                    layers=(detour,),
                    start=vroute.Terminal(qa[0], qa[1], frozenset({detour})),
                    goal=vroute.Terminal(qb[0], qb[1], frozenset({detour})),
                    bounds=bounds,
                    # No via geometry offered, so the search may not
                    # change layer: this trace stays on `detour`.
                    via=None,
                    max_expansions=config.max_expansions,
                ),
            )
            if path is None:
                continue
            run = next((pts for layer, pts in path.runs if layer == detour), [])
            if len(run) < 2:
                continue
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
                    layer_lo=lo,
                    layer_hi=hi,
                    endpoint="a",
                ),
                RealizedVia(
                    seg_id=_STITCH_SEG_ID,
                    net_id=net_id,
                    x=qb[0],
                    y=qb[1],
                    dia_mm=via_dia_mm,
                    drill_mm=via_drill_mm,
                    layer_lo=lo,
                    layer_hi=hi,
                    endpoint="b",
                ),
            ]
            return vias, tracks[0]
    return None


def _stamp_track(space: vroute.Space, track: RealizedTrack, net_name: str) -> None:
    """One jumper trace's copper stamped into the Space — one leg-shaped
    obstacle per straight segment, at the track's own half-width (arc
    segments keep their polyline legs: a jumper is drawn unfilleted, so
    they never appear here, but the fallback keeps the function honest
    for any track shape)."""
    half = track.width_mm / 2.0
    for seg in track.segments:
        if seg["shape"] == "line":
            space.add(
                vroute.Obstacle(
                    Body(
                        _ShapelyLineString([seg["start"], seg["end"]]),
                        (),
                        half,
                    ),
                    net_name,
                    frozenset((track.layer,)),
                )
            )
        elif seg["shape"] == "arc":
            pts = [seg["start"], *seg.get("pts", []), seg["end"]]
            space.add(
                vroute.Obstacle(
                    Body(_ShapelyLineString(pts), (), half),
                    net_name,
                    frozenset((track.layer,)),
                )
            )


def _stitch_one_net(
    ir: PcbIR,
    net_id: int,
    net_name: str,
    frags: list[tuple[int, Any]],
    existing_vias: list[RealizedVia],
    space: vroute.Space,
    rules: NetRules,
    config: RealizeConfig,
    placed_via_sites: list[tuple[float, float, float]],
    track_width_by_layer: dict[int, float],
    foreign_pours: dict[int, list[Any]],
    net_tracks: list[RealizedTrack],
    n_layers: int,
    bounds: tuple[float, float, float, float],
    floor_width_by_layer: dict[int, float] | None = None,
) -> tuple[list[RealizedVia], list[RealizedTrack], int, int, str]:
    """One net's stitching attempt, both stages of the module docstring.

    **The graph has TWO kinds of node, not one — ``frags`` (this net's own
    poured fragments, one entry per ``(layer_idx, polygon)``) AND
    ``existing_vias`` (this net's already-realized per-pin drop vias).**
    A via node is first-class in the graph FOR COUNTING PURPOSES (its
    isolation shows in the returned piece count) but never a BRIDGING
    TARGET for a new via: two DRC-legal vias of one net must still sit at
    least ``clearance_mm`` apart, while a touch counts only at ~zero gap
    — a hard fact, not a tuning failure.

    **The same proof condemns two fragments on the SAME layer, and stage 3
    is the answer to it** (:func:`_try_plane_jumper` — a via out of each
    fragment plus a spare-layer trace). A STRAY VIA — one of this net's
    drop vias touching no fragment — has no fix here: a jumper anchors on
    poured copper at both ends.

    Returns the vias and the jumper tracks it placed, the number of
    DISTINCT connected pieces remaining afterward across ALL nodes, and a
    message that is non-empty only when that count is > 1.

    Via legality asks the Space's kernel-exact predicates: a candidate
    disk must clear foreign copper AND every pad (``via_site=True`` —
    :meth:`vroute.Space.clears`) on every layer the barrel spans, keep
    ``_via_clears_vias``' net-blind spacing from placed barrels, and sit
    inside the board's usable edge band (:func:`_outline_clip`, the
    edge-clearance inset the old ``edge_clear`` grid check carried)."""
    n_frags = len(frags)
    n = n_frags + len(existing_vias)
    dsu = _UnionFind(n)
    assert rules.via_dia_mm is not None and rules.via_drill_mm is not None
    via_dia_mm, via_drill_mm = rules.via_dia_mm, rules.via_drill_mm
    via_r = via_dia_mm / 2.0
    clearance = space.cl(net_name, 0)
    # A via centre must sit inside the board's usable edge band —
    # :func:`_outline_clip` inset by edge clearance plus the barrel radius,
    # the contains-check port of the edge keep-out's.
    edge_clip = _outline_clip(ir, _board_edge_min_mm(config) + via_r)

    # Seed: any of this net's ALREADY-realized drop vias that touch one or
    # more fragments joins them (and joins the via itself into that group)
    # for free. Point-exact on purpose — the same test
    # :mod:`precis.pcb.connectivity` uses for pour membership.
    populated: set[int] = set()
    for i, (layer, poly) in enumerate(frags):
        for k, v in enumerate(existing_vias):
            if v.layer_lo <= layer <= v.layer_hi and _touches(poly, v.x, v.y):
                dsu.union(i, n_frags + k)
                populated.add(i)
    # This net's own tracks are joiners, not nodes: a trace's endpoints
    # landing in two fragments connect them.
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

    pitch = config.stitch_pitch_mm or _default_stitch_pitch(rules, clearance)
    new_vias: list[RealizedVia] = []
    new_tracks: list[RealizedTrack] = []

    def via_is_legal(x: float, y: float, lo: int, hi: int) -> bool:
        """The three keep-outs a stitching via must satisfy, asked WITHOUT
        placing anything — stage 3 needs to know both of a jumper's
        barrels are legal before it commits either one."""
        return (
            all(
                space.clears(
                    Body(_ShapelyPoint(x, y), (), via_r),
                    net_name,
                    layer,
                    via_site=True,
                )
                for layer in range(lo, hi + 1)
            )
            and _via_clears_vias(x, y, via_r, placed_via_sites, clearance)
            and (
                edge_clip is None
                or (
                    edge_clip[0] <= x <= edge_clip[2]
                    and edge_clip[1] <= y <= edge_clip[3]
                )
            )
        )

    def claim_via(via: RealizedVia) -> None:
        new_vias.append(via)
        space.add(
            vroute.Obstacle(
                Body(_ShapelyPoint(via.x, via.y), (), via_r),
                net_name,
                frozenset(range(via.layer_lo, via.layer_hi + 1)),
                blocks_vias=True,
            )
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
    # overlap gets a generous grid of stitching vias across that overlap.
    # The via BUDGET is allocated per disjoint PART of the overlap (min 1
    # each) and each part's share is consumed in `_spread_order`.
    for i, j in itertools.combinations(range(n_frags), 2):
        li, poly_i = frags[i]
        lj, poly_j = frags[j]
        if li == lj or dsu.find(i) == dsu.find(j):
            continue
        overlap = poly_i.intersection(poly_j)
        if overlap.is_empty or overlap.area <= 0.0:
            continue
        lo, hi = through_span(n_layers)
        parts = [g for g in getattr(overlap, "geoms", [overlap]) if g.area > 0.0]
        # Smallest part first: an island's single guaranteed slot must
        # not depend on how much budget the big field consumed.
        parts.sort(key=lambda g: g.area)
        share = max(1, config.max_sprinkle_vias_per_overlap // max(1, len(parts)))
        for part in parts:
            placed = 0
            for x, y in _spread_order(_grid_candidates(part, pitch), share):
                if placed >= share:
                    break
                if try_via(x, y, lo, hi):
                    dsu.union(i, j)
                    placed += 1

    # Stage 2 -- targeted residue over FRAGMENT PAIRS ONLY (the via-node
    # proof above). Closest-pair-first over a union-find IS a minimum
    # spanning tree (Kruskal). Stage 3 rides inside the same loop as the
    # fallback for a pair no single via can bridge.
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
        lo, hi = through_span(n_layers)
        overlap = poly_i.intersection(poly_j)
        placed_here = False
        if not overlap.is_empty and overlap.area > 0.0:
            for x, y in _candidate_points_in(overlap, via_r):
                if try_via(x, y, lo, hi):
                    dsu.union(i, j)
                    placed_here = True
                    break
        if not placed_here and jumpers < config.max_plane_jumpers_per_net:
            jumper = None
            for widths in (track_width_by_layer, floor_width_by_layer):
                if widths is None or (
                    widths is floor_width_by_layer and widths == track_width_by_layer
                ):
                    continue
                jumper = _try_plane_jumper(
                    net_id,
                    frags[i],
                    frags[j],
                    space=space,
                    net_name=net_name,
                    n_layers=n_layers,
                    via_dia_mm=via_dia_mm,
                    via_drill_mm=via_drill_mm,
                    via_is_legal=via_is_legal,
                    track_width_by_layer=widths,
                    foreign_pours=foreign_pours,
                    config=config,
                    bounds=bounds,
                )
                if jumper is not None:
                    break
            if jumper is not None:
                jumper_vias, jumper_track = jumper
                for v in jumper_vias:
                    claim_via(v)
                new_tracks.append(jumper_track)
                _stamp_track(space, jumper_track, net_name)
                dsu.union(i, j)
                jumpers += 1
                placed_here = True
        if not placed_here:
            impossible.add((i, j))

    remaining = len({dsu.find(i) for i in range(n)})
    message = ""
    bare_pieces = 0
    if remaining > 1:
        # Refresh `populated` with the copper THIS pass just placed.
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
        # Name the reason each bare piece stayed unjoined: no via fits in
        # it, or a site exists but no legal jumper reaches the rest.
        bare_why: list[str] = []
        for i, (layer, poly) in enumerate(frags):
            if dsu.find(i) in roots_with_copper:
                continue
            sites = _sites_in(poly, via_r, pitch)
            bare_why.append(
                f"{poly.area:.2f}mm2 on layer {layer} at "
                f"({poly.centroid.x:.1f}, {poly.centroid.y:.1f}): "
                + (
                    "no via site fits inside it"
                    if not sites
                    else f"{len(sites)} via site(s) fit but no legal via or "
                    "jumper to the rest of the net"
                )
            )
        bare_note = (
            f" {bare_pieces} of those piece(s) hold NO via or trace of this "
            "net at all — floating poured copper, not an electrical split, "
            "and invisible to the connectivity checker for that reason"
            f" ({'; '.join(bare_why)})."
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
    ctx: _RouteContext,
    config: RealizeConfig,
    footprints: dict[str, dict[str, Any]] | None = None,
    fixed_copper: list[dict[str, Any]] | None = None,
) -> tuple[list[RealizedVia], list[RealizedTrack], list[UnstitchedNet]]:
    """Deliberate stitching vias for every plane-promoted net whose own
    poured copper is not already one piece — see the module docstring for
    the two-stage approach and why this function computes its own
    connectivity rather than asking
    :func:`precis.pcb.connectivity.net_islands`.

    Builds a FRESH :class:`vroute.Space` from the already-finished
    ``tracks``/``vias``/``pads`` (:func:`_final_board_space`), so every
    keep-out check below sees the REAL, final board.

    **Board-edge clearance needs no separate check for a SPRINKLE via.**
    Every sprinkle candidate comes from INSIDE an already-computed pour
    polygon, and :func:`precis.pcb.planes.plane_pours` already insets its
    region by the edge clearance before it ever produces one. The
    targeted-residue and jumper sites get the explicit
    :func:`_outline_clip` contains-check inside ``via_is_legal``.

    **Known limitation, stated rather than hidden**: a stitching via's
    keep-out check is asked only about copper this module has already
    claimed (routed tracks, vias, pads) — it has no notion of a THIRD
    net's finished pour on a layer the stitching via merely passes
    through. **A JUMPER does not inherit that limitation**: the pour
    polygons are handed down per net (``foreign_pours``) and
    :func:`_clears_foreign_pours` checks the jumper's own copper against
    them geometrically.
    """
    plane_net_ids = [n for n in range(ir.n_nets) if int(ir.net_plane_layers[n]) != 0]
    if not plane_net_ids or not pours:
        return [], [], []
    pour_polys = _pour_polygons(ir, pours)
    space = _final_board_space(ir, tracks, vias, ctx, config, footprints, fixed_copper)
    n_layers = len(ir.stackup) or 1
    placed_via_sites: list[tuple[float, float, float]] = [
        (v.x, v.y, v.dia_mm / 2.0) for v in vias
    ]
    # Route bounds for a jumper's detour trace — the same inset the route
    # pass uses.
    bounds = (0.0, 0.0, 0.0, 0.0)
    bbox = _outline_bbox(ir)
    if bbox is not None:
        inset = ctx.edge_min + ctx.clearance + vroute.DELTA_MM
        bounds = (bbox[0] + inset, bbox[1] + inset, bbox[2] - inset, bbox[3] - inset)

    extra_vias: list[RealizedVia] = []
    extra_tracks: list[RealizedTrack] = []
    unstitched: list[UnstitchedNet] = []
    for net_id in plane_net_ids:
        net_name = str(ir.net_name[net_id])
        rules = ctx.rules_by_net[net_id]
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
        # resolved rule for the layer it lands on — asked per candidate
        # detour layer. Keying on ``routable_layers`` is what confines a
        # jumper to a layer that may legally carry a trace at all.
        track_width_by_layer = {
            layer: _resolve_track_rules(ir, net_id, layer, config).track_width_mm
            for layer in routable_layers(ir)
        }
        floor_width = resolve_net_rules(
            "", layer_is_outer=True, fab_caps=config.fab_caps
        ).track_width_mm
        floor_width_by_layer = {
            layer: min(width, floor_width)
            for layer, width in track_width_by_layer.items()
        }
        net_vias, net_tracks, remaining, bare, message = _stitch_one_net(
            ir,
            net_id,
            net_name,
            frags,
            existing,
            space,
            rules,
            config,
            placed_via_sites,
            track_width_by_layer,
            foreign_pours,
            [t for t in tracks if t.net_id == net_id],
            n_layers,
            bounds,
            floor_width_by_layer,
        )
        extra_vias += net_vias
        extra_tracks += net_tracks
        if remaining > 1:
            unstitched.append(UnstitchedNet(net_id, net_name, remaining, bare, message))
    return extra_vias, extra_tracks, unstitched


@dataclass(frozen=True, slots=True)
class DroppedIsland:
    """A poured piece removed after stitching because nothing of its net
    reaches it and no via or jumper could (pcb-floating-pour-island: stitch,
    drop only what cannot be reached, and say so)."""

    net: str
    layer: str
    area_mm2: float
    centroid: tuple[float, float]

    def phrase(self) -> str:
        return (
            f"{self.layer} {self.area_mm2:.2f}mm2 at "
            f"({self.centroid[0]:.1f}, {self.centroid[1]:.1f})"
        )


def _drop_bare_islands(
    ir: PcbIR,
    tracks: list[RealizedTrack],
    vias: list[RealizedVia],
    pours: list[dict[str, Any]],
    pads: list[dict[str, Any]],
    fixed_copper: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[DroppedIsland]]:
    """Remove every pour piece the stitching passes left with none of its
    net's copper in reach: no via, track or pad in the piece, nor in any
    piece a via or track joins it to. Run after the last stitch round, so
    whatever is still bare had every via site and jumper tried.

    Anchors are counted generously, never short: every pad of the net
    (``pads``, :func:`pads_for_ir`'s whole footprint pad set, so a second
    land of one pin counts), and every point of the net's authored fixed
    copper (via centres, track vertices), counts
    on EVERY layer (a through-hole barrel joins them all; an SMD pad over
    an inner piece only keeps a piece that is then reported, not dropped).
    Dropping copper that carries a real connection would break a net;
    keeping a floating piece only leaves the report standing."""
    layer_index = {str(layer.get("name")): i for i, layer in enumerate(ir.stackup)}
    by_net: dict[str, list[int]] = {}
    for k, pour in enumerate(pours):
        by_net.setdefault(str(pour.get("net", "")), []).append(k)
    net_ids = {str(ir.net_name[n]): n for n in range(ir.n_nets)}
    drop: set[int] = set()
    dropped: list[DroppedIsland] = []
    for net, idxs in by_net.items():
        net_id = net_ids.get(net)
        if net_id is None:
            continue
        frags = [
            (
                k,
                layer_index.get(str(pours[k].get("layer", "")), -1),
                _pour_polygon(pours[k]),
            )
            for k in idxs
        ]
        net_vias = [v for v in vias if v.net_id == net_id]
        n_frag = len(frags)
        dsu = _UnionFind(n_frag + len(net_vias))
        anchored: set[int] = set()
        for j, v in enumerate(net_vias):
            for i, (_k, layer, poly) in enumerate(frags):
                if v.layer_lo <= layer <= v.layer_hi and _touches(poly, v.x, v.y):
                    dsu.union(n_frag + j, i)
        for t in tracks:
            if t.net_id != net_id:
                continue
            ends = [
                (float(seg[e][0]), float(seg[e][1]))
                for seg in t.segments
                for e in ("start", "end")
            ]
            hit = [
                i
                for i, (_k, layer, poly) in enumerate(frags)
                if layer == t.layer and any(_touches(poly, x, y) for x, y in ends)
            ]
            anchored.update(hit)
            for other in hit[1:]:
                dsu.union(hit[0], other)
        # (x, y, reach): a pad anchors a piece its land overlaps, not only
        # one holding its centre.
        anchors: list[tuple[float, float, float]] = [
            (
                float(pad["x"]),
                float(pad["y"]),
                math.hypot(float(pad.get("w") or 0.0), float(pad.get("h") or 0.0))
                / 2.0,
            )
            for pad in pads
            if str(pad.get("net") or "") == net
        ]
        fixed_polys: list[Any] = []
        for item in fixed_copper or []:
            if str(item.get("net") or "") != net:
                continue
            if item.get("x") is not None and item.get("y") is not None:
                r = float(item.get("dia_mm") or 0.0) / 2.0
                anchors.append((float(item["x"]), float(item["y"]), r))
            for seg in item.get("segments") or []:
                for e in ("start", "end", "mid"):
                    if seg.get(e) is not None:
                        anchors.append((float(seg[e][0]), float(seg[e][1]), 0.0))
            if item.get("polygon"):
                fixed_polys.append(_pour_polygon(item))
        for i, (_k, _layer, poly) in enumerate(frags):
            if poly.is_empty:
                continue
            if any(
                poly.distance(_ShapelyPoint(x, y)) <= r for x, y, r in anchors
            ) or any(poly.intersects(fp) for fp in fixed_polys if not fp.is_empty):
                anchored.add(i)
        roots = {dsu.find(i) for i in anchored} | {
            dsu.find(n_frag + j) for j in range(len(net_vias))
        }
        for i, (k, _layer, poly) in enumerate(frags):
            if dsu.find(i) in roots or poly.is_empty:
                continue
            drop.add(k)
            c = poly.centroid
            dropped.append(
                DroppedIsland(
                    net,
                    str(pours[k].get("layer", "")),
                    float(poly.area),
                    (float(c.x), float(c.y)),
                )
            )
    if not drop:
        return pours, []
    return [p for k, p in enumerate(pours) if k not in drop], dropped


def _without_dropped(
    unstitched: list[UnstitchedNet], dropped: list[DroppedIsland]
) -> list[UnstitchedNet]:
    """``unstitched`` with each net's dropped bare pieces taken off its
    piece and bare counts (a dropped piece is no longer copper), and a net
    left in one piece no longer reported. The stitcher's own message stays,
    with the drop appended."""
    if not dropped:
        return unstitched
    per_net: dict[str, int] = {}
    for d in dropped:
        per_net[d.net] = per_net.get(d.net, 0) + 1
    out: list[UnstitchedNet] = []
    for u in unstitched:
        n = per_net.get(u.net, 0)
        # A bare piece holds no via or trace, so it is one component of its
        # own; never take off more than the stitcher counted bare.
        gone = min(n, u.bare_fragments)
        if not n:
            out.append(u)
            continue
        fragments = u.fragments - gone
        if fragments <= 1:
            continue
        out.append(
            UnstitchedNet(
                u.net_id,
                u.net,
                fragments,
                u.bare_fragments - gone,
                f"{u.message} {n} unreachable bare piece(s) were then dropped.",
            )
        )
    return out


#: How many of a stranded pin's nearest same-net drop vias the rescue tries.
_RESCUE_TARGETS = 4


def _pour_polygons(
    ir: PcbIR, pours: list[dict[str, Any]]
) -> list[tuple[str, int, Any]]:
    """Every non-empty pour as ``(net name, layer index, polygon)``."""
    layer_index = {str(layer.get("name")): i for i, layer in enumerate(ir.stackup)}
    out: list[tuple[str, int, Any]] = []
    for pour in pours:
        idx = layer_index.get(str(pour.get("layer", "")))
        if idx is None:
            continue
        poly = _pour_polygon(pour)
        if not poly.is_empty:
            out.append((str(pour.get("net", "")), idx, poly))
    return out


@dataclass(frozen=True)
class _PlaneRescue:
    """One plane pin's rescue (:func:`_rescue_plane_pins`): the copper it
    adds, and the earlier rescue whose via it ends on (``None`` when it
    ends on a fan-out drop), which it cannot outlive."""

    pid: int
    tracks: list[RealizedTrack]
    vias: list[RealizedVia]
    after: int | None


def _pour_pieces(
    ir: PcbIR,
    tracks: list[RealizedTrack],
    vias: list[RealizedVia],
    pours: list[dict[str, Any]],
    pads: list[tuple[Point, int, float, tuple[int, ...]]],
) -> dict[tuple[str, int], int]:
    """``(net, layer) -> how many electrical pieces that net's pour on that
    layer is in``: poured pieces joined through the net's own vias, pads and
    track ends, the joins :func:`_stitch_one_net` counts (physics copied
    from the checker's model, not the checker consulted; this module's
    independence note). :func:`_keep_splitless_rescues` compares it before
    and after a re-pour, so only its rises matter."""
    net_ids = {str(ir.net_name[n]): n for n in range(ir.n_nets)}
    frags_by_net: dict[str, list[tuple[int, Any]]] = {}
    for net, layer, poly in _pour_polygons(ir, pours):
        for piece in getattr(poly, "geoms", [poly]):
            frags_by_net.setdefault(net, []).append((layer, piece))
    out: dict[tuple[str, int], int] = {}
    for net, frags in frags_by_net.items():
        net_id = net_ids.get(net)
        own_vias = [v for v in vias if v.net_id == net_id]
        own_pads = [(pt, lyrs) for pt, n, _shape, lyrs in pads if n == net_id]
        nf, nv = len(frags), len(own_vias)
        dsu = _UnionFind(nf + nv + len(own_pads))
        for i, (layer, poly) in enumerate(frags):
            for k, v in enumerate(own_vias):
                if v.layer_lo <= layer <= v.layer_hi and _touches(poly, v.x, v.y):
                    dsu.union(i, nf + k)
            for k, (pt, lyrs) in enumerate(own_pads):
                if layer in lyrs and _touches(poly, pt[0], pt[1]):
                    dsu.union(i, nf + nv + k)
        for track in tracks:
            if track.net_id != net_id:
                continue
            ends = [
                (float(seg[end][0]), float(seg[end][1]))
                for seg in track.segments
                for end in ("start", "end")
            ]
            touched = [
                i
                for i, (layer, poly) in enumerate(frags)
                if layer == track.layer and any(_touches(poly, x, y) for x, y in ends)
            ]
            touched += [
                nf + k
                for k, v in enumerate(own_vias)
                if v.layer_lo <= track.layer <= v.layer_hi
                and any(math.hypot(x - v.x, y - v.y) <= v.dia_mm / 2.0 for x, y in ends)
            ]
            touched += [
                nf + nv + k
                for k, (pt, lyrs) in enumerate(own_pads)
                if track.layer in lyrs
                and any(math.hypot(x - pt[0], y - pt[1]) <= 1e-6 for x, y in ends)
            ]
            for other in touched[1:]:
                dsu.union(touched[0], other)
        roots: dict[int, set[int]] = {}
        for i, (layer, _poly) in enumerate(frags):
            roots.setdefault(layer, set()).add(dsu.find(i))
        for layer, r in roots.items():
            out[(net, layer)] = len(r)
    return out


def _keep_splitless_rescues(
    rescues: list[_PlaneRescue],
    repour: Callable[[list[_PlaneRescue]], tuple[Any, dict[tuple[str, int], int]]],
    before: dict[tuple[str, int], int],
) -> tuple[list[_PlaneRescue], Any]:
    """The rescues whose copper, once the planes are poured again around
    it, leaves no plane in more electrical pieces than ``before``
    (:func:`_pour_pieces`), and ``repour``'s result for them. All are kept
    when together they split nothing (one re-pour); otherwise each is tried
    in order on top of those kept, and one that raises a count is dropped,
    with every later rescue that ends on its via (gr492108: a rescue's
    through-via antipad can cut a foreign plane's neck, which the
    geometric pre-check in :func:`_rescue_plane_pins` cannot see past the
    re-pour)."""

    def splits(pieces: dict[tuple[str, int], int]) -> bool:
        return any(n > before.get(key, 1) for key, n in pieces.items())

    result, pieces = repour(rescues)
    if not splits(pieces):
        return rescues, result
    kept: list[_PlaneRescue] = []
    result, _ = repour(kept)
    for rescue in rescues:
        if rescue.after is not None and rescue.after not in {r.pid for r in kept}:
            continue
        trial, pieces = repour([*kept, rescue])
        if not splits(pieces):
            kept.append(rescue)
            result = trial
    return kept, result


def _pour_yields(poly: Any, cut: Any, pads_covered: list[Point]) -> Any | None:
    """``poly`` less ``cut`` when the plane survives it -- no more pieces
    than before and every pad in ``pads_covered`` still touched -- else
    ``None`` (:func:`_rescue_plane_pins`'s guarded rescue)."""
    after = poly.difference(cut)
    if len(getattr(after, "geoms", [after])) > len(getattr(poly, "geoms", [poly])):
        return None
    if not all(_touches(after, x, y) for x, y in pads_covered):
        return None
    return after


def _rescue_plane_pins(
    ir: PcbIR,
    tracks: list[RealizedTrack],
    vias: list[RealizedVia],
    pours: list[dict[str, Any]],
    failed_pins: list[int],
    *,
    ctx: _RouteContext,
    config: RealizeConfig,
    footprints: dict[str, dict[str, Any]] | None = None,
    fixed_copper: list[dict[str, Any]] | None = None,
    pour_clearance: float | None = None,
) -> list[_PlaneRescue]:
    """Give a plane pin that found no drop-via site a ROUTED connection to
    one of its own net's drop vias that already reaches the pour (gr492108:
    nano V5's 15 pieces were exactly these pins).

    The fan-out only looks ``_DROP_SEARCH_STEPS`` stubs out from the pad. A
    pin walled in beyond that can still have a legal trace to a drop via
    further away, which the vector router finds on the final board. The
    trace may change layer; the re-pour that follows carves the planes
    around whatever it adds. Returns one :class:`_PlaneRescue` per pin
    rescued, in order; a pin with no reachable same-net drop stays failed
    and keeps its ``plane_drop_blocked`` reason.

    A trace on a layer another net pours, or a via through it, is accepted
    only when that plane survives it (:func:`_pour_yields`): cut by the
    trace, or by the via's antipad on every layer its barrel spans, at
    ``pour_clearance`` (the clearance the re-pour carves at), each yielding
    pour gains no piece and still covers every one of its net's pads it
    covered before. Otherwise it is refused, as the jumper refuses
    (gr492108: taz15's F.Cu pins under the 3V3 pour had no other way out).
    This is the geometric pre-check; the caller re-pours and judges the
    electrical pieces (:func:`_keep_splitless_rescues`)."""
    if not failed_pins or not pours:
        return []
    pour_polys = _pour_polygons(ir, pours)
    clearance = ctx.clearance
    cut_clearance = clearance if pour_clearance is None else pour_clearance
    # Each net's pour per layer, cut as rescues are accepted, and the pads
    # each one covers: what a yielding plane must still be and still reach.
    pour_state: dict[tuple[str, int], Any] = {}
    for n_, i, poly in pour_polys:
        prev = pour_state.get((n_, i))
        pour_state[(n_, i)] = poly if prev is None else prev.union(poly)
    covered: dict[tuple[str, int], list[Point]] = {}
    for pt, pad_net, _radius, pad_layers in ctx.pads:
        if pad_net < 0 or pad_net >= ir.n_nets:
            continue
        name = str(ir.net_name[pad_net])
        for layer in pad_layers:
            poly = pour_state.get((name, layer))
            if poly is not None and _touches(poly, pt[0], pt[1]):
                covered.setdefault((name, layer), []).append(pt)
    space = _final_board_space(ir, tracks, vias, ctx, config, footprints, fixed_copper)
    n_layers = len(ir.stackup) or 1
    names, _index, _n = _layer_index(ir)
    all_layers = range(n_layers)
    signal = list(routable_layers(ir))
    floor_width = resolve_net_rules(
        "", layer_is_outer=True, fab_caps=config.fab_caps
    ).track_width_mm
    bbox = _outline_bbox(ir)
    new_vias: list[RealizedVia] = []
    #: Which rescue placed each of ``new_vias`` (a later rescue may end on
    #: one, and depends on it).
    via_owner: list[int] = []
    rescues: list[_PlaneRescue] = []
    rescued: set[int] = set()
    lo, hi = through_span(len(all_layers))
    for pid in failed_pins:
        net_id = int(ir.pin_net[pid])
        rules = ctx.rules_by_net[net_id]
        point = pin_point(ir, pid)
        if point is None or rules.via_dia_mm is None or rules.via_drill_mm is None:
            continue
        pad_layer = _side_layer(ir, int(ir.pin_instance[pid]), all_layers)
        net_name = str(ir.net_name[net_id])
        widths = sorted({rules.track_width_mm, floor_width}, reverse=True)
        polys = [(i, poly) for n_, i, poly in pour_polys if n_ == net_name]
        targets = sorted(
            (
                v
                for v in [*vias, *new_vias]
                if v.net_id == net_id
                and any(
                    v.layer_lo <= i <= v.layer_hi and _touches(poly, v.x, v.y)
                    for i, poly in polys
                )
            ),
            key=lambda v: dist(point, (v.x, v.y)),
        )[:_RESCUE_TARGETS]
        # The net's own width first; a power class's current-sized width
        # often fits no corridor out of a crowded pad, so the fab floor is
        # the fallback (a single-pin tie to the plane, not a power trunk).
        for width in widths:
            if pid in rescued:
                break
            inset = ctx.edge_min + width / 2.0 + vroute.DELTA_MM
            bounds = (
                (bbox[0] + inset, bbox[1] + inset, bbox[2] - inset, bbox[3] - inset)
                if bbox is not None
                else (-1e6, -1e6, 1e6, 1e6)
            )
            for v in targets:
                goal_layer = next(
                    (layer for layer in (v.layer_lo, v.layer_hi) if layer in signal),
                    None,
                )
                if goal_layer is None:
                    continue
                path = vroute.route(
                    space,
                    vroute.Request(
                        net=net_name,
                        width_mm=width,
                        layers=tuple(signal),
                        start=vroute.Terminal(
                            point[0], point[1], frozenset({pad_layer})
                        ),
                        goal=vroute.Terminal(v.x, v.y, frozenset({goal_layer})),
                        bounds=bounds,
                        via=vroute.ViaSpec(rules.via_dia_mm, rules.via_drill_mm),
                        via_layers=tuple(range(n_layers)),
                        max_expansions=config.max_expansions,
                    ),
                )
                if path is None:
                    continue
                # A TRACK on a layer another net pours cuts that plane, and
                # so does a VIA's antipad on every layer its barrel spans
                # (the re-pour carves both); each is kept only if the
                # plane survives the cut.
                cuts: list[tuple[int, Any]] = []
                for layer, pts in path.runs:
                    if len(pts) >= 2:
                        cuts.append(
                            (
                                layer,
                                _ShapelyLineString(pts).buffer(
                                    width / 2.0 + cut_clearance,
                                    quad_segs=_JUMPER_QUAD_SEGS,
                                ),
                            )
                        )
                for vx, vy in path.vias:
                    disk = _ShapelyPoint(vx, vy).buffer(
                        rules.via_dia_mm / 2.0 + cut_clearance,
                        quad_segs=_JUMPER_QUAD_SEGS,
                    )
                    cuts += [(layer, disk) for layer in range(lo, hi + 1)]
                yielded: dict[tuple[str, int], Any] = {}
                refused = False
                for layer, cut in cuts:
                    for (n_, i), poly in pour_state.items():
                        if i != layer or n_ == net_name or not poly.intersects(cut):
                            continue
                        after = _pour_yields(
                            yielded.get((n_, i), poly), cut, covered.get((n_, i), [])
                        )
                        if after is None:
                            refused = True
                            break
                        yielded[(n_, i)] = after
                    if refused:
                        break
                if refused:
                    continue
                pour_state.update(yielded)
                made_tracks = [
                    t
                    for layer, pts in path.runs
                    for t in _track_from_run(_STITCH_SEG_ID, net_id, layer, pts, width)
                ]
                for ob in _copper_obstacles(made_tracks, [], ir, _index, _n, names):
                    space.add(ob)
                path_vias: list[RealizedVia] = []
                made_via_obs: list[vroute.Obstacle] = []
                for vx, vy in path.vias:
                    made_via_obs.append(
                        vroute.Obstacle(
                            Body(_ShapelyPoint(vx, vy), (), rules.via_dia_mm / 2.0),
                            net_name,
                            frozenset(range(lo, hi + 1)),
                            blocks_vias=True,
                        )
                    )
                    path_vias.append(
                        RealizedVia(
                            seg_id=_STITCH_SEG_ID,
                            net_id=net_id,
                            x=vx,
                            y=vy,
                            dia_mm=rules.via_dia_mm,
                            drill_mm=rules.via_drill_mm,
                            layer_lo=lo,
                            layer_hi=hi,
                            endpoint="a",
                        )
                    )
                for ob in made_via_obs:
                    space.add(ob)
                after = next(
                    (via_owner[k] for k, nv in enumerate(new_vias) if nv is v), None
                )
                new_vias += path_vias
                via_owner += [pid] * len(path_vias)
                rescues.append(_PlaneRescue(pid, made_tracks, path_vias, after))
                rescued.add(pid)
                break
    return rescues


def _diagnose_all(
    ir: PcbIR,
    unrouted: list[int],
    extra_unrouted: list[int],
    plane_id_set: set[int],
    ctx: _RouteContext,
    config: RealizeConfig,
    *,
    fixed_copper: list[dict[str, Any]] | None = None,
    footprints: dict[str, dict[str, Any]] | None = None,
    net_layer_lock_fail: dict[int, str] | None = None,
    routed_tracks: list[RealizedTrack] | None = None,
    routed_vias: list[RealizedVia] | None = None,
    plane_failed_pins: list[int] | None = None,
    exhausted: set[int] | None = None,
) -> list[UnroutedReason]:
    """One :class:`UnroutedReason` per segment in ``unrouted +
    extra_unrouted``.

    ``plane_failed_pins`` is the actual per-PIN list of terminals
    :func:`_plane_fanout` could not give a legal drop — grouped per net
    and stamped on the matching ``'plane_drop_blocked'`` reasons as
    ``UnroutedReason.pins``, so the actual terminal that could not
    escape survives into evidence.

    ``net_layer_lock_fail`` (Rulings 2026-09-19 item 7) is checked FIRST:
    a net whose class named a layer this board can't route it on never
    reached ``vroute.route`` at all, so re-running the point-to-point
    probes below would ask the wrong question.

    ``exhausted`` (:func:`_realize_vector`'s record of which segments'
    REAL search ended on the expansion cap, from the route's own
    ``stats['stop']``) is checked before the probes for the same reason:
    the busy-space search already answered why this segment failed — the
    budget ran out — so a pads-only probe that then finds a corridor
    would misreport the failure as a lost race (gr489909)."""
    reasons: list[UnroutedReason] = []
    pins_by_net: dict[int, list[int]] = {}
    for pid in plane_failed_pins or ():
        pins_by_net.setdefault(int(ir.pin_net[pid]), []).append(pid)
    probe: vroute.Space | None = None
    routed_copper = (
        (routed_tracks, routed_vias)
        if routed_tracks is not None and routed_vias is not None
        else None
    )
    for seg_id in unrouted:
        net_id = int(ir.seg_net[seg_id])
        if net_layer_lock_fail and net_id in net_layer_lock_fail:
            reasons.append(
                UnroutedReason(
                    seg_id, net_id, "layer_lock", net_layer_lock_fail[net_id]
                )
            )
            continue
        if seg_id in plane_id_set:
            # This segment never went through the point-to-point route at
            # all — its failure is a PIN's drop-via search coming up empty
            # (`_plane_fanout`/`_drop_via_site`).
            reasons.append(
                UnroutedReason(
                    seg_id,
                    net_id,
                    "plane_drop_blocked",
                    "this plane-promoted net had at least one pin with no "
                    "legal drop-via site within its search radius (blocked by "
                    "other copper) -- not a point-to-point route failure",
                    pins=tuple(pins_by_net.get(net_id, ())),
                )
            )
            continue
        if exhausted and seg_id in exhausted:
            reasons.append(
                UnroutedReason(
                    seg_id,
                    net_id,
                    "search_budget",
                    "this connection's route search ran out of expansions "
                    "(max_expansions) on the busy space before finding a "
                    "path or proving none — a budget failure, not a lost "
                    "corridor race; the diagnosis's pads-only probe is not "
                    "re-run, since its verdict could only mislabel this "
                    "congestion",
                )
            )
            continue
        rules = _seg_rules(ir, seg_id, ctx.rules_by_net, config)
        n_vias, group_extent = _via_group_extent(ir, seg_id, rules, ctx.clearance)
        if probe is None:
            probe = _pads_only_space(ir, ctx, config, footprints, fixed_copper)
        reasons.append(
            _diagnose_unrouted(
                ir,
                seg_id,
                ctx,
                rules,
                n_vias,
                group_extent,
                config.max_expansions,
                probe=probe,
                routed_copper=routed_copper,
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


def _diagnose_unrouted(
    ir: PcbIR,
    seg_id: int,
    ctx: _RouteContext,
    rules: NetRules,
    n_vias: int,
    group_extent: float | None,
    max_expansions: int,
    *,
    probe: vroute.Space | None = None,
    routed_copper: tuple[list[RealizedTrack], list[RealizedVia]] | None = None,
) -> UnroutedReason:
    """WHY ``seg_id`` never routed, in one of :class:`UnroutedReason`'s
    three route-search categories.

    **The technique: a fresh Space with only the static obstacles — no
    other net's routed copper — answers "does a corridor exist at all",
    independent of routing order.** Re-running the real search there at
    the connection's REAL required width (trace width, or — at a layer
    transition — the via GROUP's full extent) either finds a path (a
    corridor DOES exist physically, so the failure on the busy space was
    losing a race: ``'congestion'``) or does not. If it does not, a
    second probe at a near-zero width with no via requirement asks the
    weaker question — does ANY path exist, at ANY width: if that also
    fails, the endpoint is topologically unreachable (``'no_path'``); if
    it succeeds, a path exists but not one wide enough (``'width'``).

    ``probe`` is that pads-only space, prebuilt: it is the same for every
    segment and ``vroute.route`` never stamps the space it searches, so
    :func:`_diagnose_all` builds it once.

    ``routed_copper`` (the finished tracks and vias, gr472026) lets a
    ``'congestion'`` verdict be checked: when the pads-only probe finds a
    corridor but NO other net's routed copper lies in it, the connection
    did not lose a race — it failed alone, so it is reported as
    ``'no_path'`` with a message saying so.
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
    # This segment's OWN two pad layers (`_side_layer`), never a shared
    # `PAD_LAYER`.
    start_layer = _side_layer(ir, int(ir.pin_instance[a]), ctx.signal_layers)
    goal_layer = _side_layer(ir, int(ir.pin_instance[b]), ctx.signal_layers)
    name = str(ir.net_name[net_id])
    width = float(rules.track_width_mm)
    inset = ctx.edge_min + width / 2.0 + vroute.DELTA_MM
    bbox = _outline_bbox(ir)
    bounds = (
        (bbox[0] + inset, bbox[1] + inset, bbox[2] - inset, bbox[3] - inset)
        if bbox is not None
        else (-1e6, -1e6, 1e6, 1e6)
    )
    n_layers = len(ir.stackup) or 1
    if probe is None:
        raise AssertionError("probe space must be supplied by _diagnose_all")
    stats: dict[str, Any] = {}
    clear_path = vroute.route(
        probe,
        vroute.Request(
            net=name,
            width_mm=width,
            layers=tuple(ctx.signal_layers),
            start=vroute.Terminal(start[0], start[1], frozenset({start_layer})),
            goal=vroute.Terminal(end[0], end[1], frozenset({goal_layer})),
            bounds=bounds,
            via=(
                vroute.ViaSpec(group_extent, rules.via_drill_mm)
                if group_extent is not None and rules.via_drill_mm is not None
                else None
            ),
            via_layers=tuple(range(n_layers)),
            max_expansions=max_expansions,
        ),
        stats=stats,
    )
    clear_stop = stats.get("stop")
    if clear_path is not None:
        blockers: dict[int, set[int]] = {}
        if routed_copper is not None:
            blockers = _foreign_copper_near_path(
                _vpath_points(clear_path),
                net_id,
                routed_copper[0],
                routed_copper[1],
                width / 2.0 + ctx.clearance,
            )
        if routed_copper is not None and not blockers:
            return UnroutedReason(
                seg_id,
                net_id,
                "no_path",
                "this connection failed with no other net's routed copper in "
                "its corridor: a pads-only probe finds a path, so the blocker "
                "is static (fixed copper, pads, a layer restriction on its "
                "class) or the search itself, not contention with another "
                "net's routing",
            )
        via_note = (
            f", stitching {n_vias} via(s) at a layer change" if group_extent else ""
        )
        who = ""
        if blockers:
            who = " — contested by " + ", ".join(
                f"{ir.net_name[n] if 0 <= n < len(ir.net_name) else f'net#{n}'} "
                f"on {'/'.join(layer_names(ir, sorted(ls)))}"
                for n, ls in sorted(blockers.items())[:4]
            )
        return UnroutedReason(
            seg_id,
            net_id,
            "congestion",
            f"a corridor wide enough for this net ({width:.3f}mm"
            f"{via_note}) exists between its endpoints once every OTHER net's "
            f"copper is cleared away, so this connection lost a race for it to "
            f"a net that routed first{who}",
        )
    thin_stats: dict[str, Any] = {}
    thin_path = vroute.route(
        probe,
        vroute.Request(
            net=name,
            width_mm=0.05,
            layers=tuple(ctx.signal_layers),
            start=vroute.Terminal(start[0], start[1], frozenset({start_layer})),
            goal=vroute.Terminal(end[0], end[1], frozenset({goal_layer})),
            bounds=bounds,
            via=None,
            max_expansions=max_expansions,
        ),
        stats=thin_stats,
    )
    thin_stop = thin_stats.get("stop")
    # A verdict resting on a search that gave up is not a verdict.
    if (thin_path is not None and clear_stop in ("budget", "exhausted")) or (
        thin_path is None and thin_stop in ("budget", "exhausted")
    ):
        return UnroutedReason(
            seg_id,
            net_id,
            "search_budget",
            f"the search gave up after {max_expansions} expansions before "
            "finding a path or proving there is none — the endpoints may be "
            "walled in, or simply too far apart for the budget",
        )
    if thin_path is not None:
        via_note = (
            f" (including a {group_extent:.3f}mm via GROUP at a layer change)"
            if group_extent
            else ""
        )
        pinchers = _constricting_pad_names(
            ir, _vpath_points(thin_path), net_id, ctx.pads, width, ctx.clearance
        )
        pinched = f" — constricted by {', '.join(pinchers)}" if pinchers else ""
        return UnroutedReason(
            seg_id,
            net_id,
            "width",
            f"a path exists between this connection's endpoints "
            f"(on {'/'.join(layer_names(ir, ctx.signal_layers))}, "
            "corridor ≥0.05mm wide), but this net's required width "
            f"({width:.3f}mm{via_note}) does not fit any "
            f"corridor along it — the width itself is the blocker, not "
            f"routing order{pinched}",
        )
    return UnroutedReason(
        seg_id,
        net_id,
        "no_path",
        "no path connects this connection's endpoints on the allowed layers "
        f"({'/'.join(layer_names(ir, ctx.signal_layers))}) at "
        "any width — the endpoint is walled in, not merely too tight",
    )


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
    cost figure the route search itself optimised and is reported against,
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
    config: RealizeConfig = RealizeConfig(),
    seg_ids: list[int] | None = None,
    footprints: dict[str, dict[str, Any]] | None = None,
    fixed_copper: list[dict[str, Any]] | None = None,
) -> RealizeResult:
    """Realize every segment in ``seg_ids`` (default: the whole board) —
    the checkpoint call. Never touches the IR (module docstring); pure
    function of a snapshot in, geometry + vias + a legible congestion
    digest out.

    ``config.router`` exists only to reject the removed routers' names:
    the vector router (:mod:`precis.pcb.vroute`) is the sole drawer.
    ``footprints`` is forwarded to :func:`pad_geometry` so pads claim and
    are probed at their real footprint geometry; ``None`` (the default)
    claims every pad at :mod:`precis.pcb.landpattern`'s synthesized size.

    ``fixed_copper`` (pcb-pre-place-route-blocks Slice 1 —
    ``store.pcb_fixed_copper_list``'s own row shape) is AUTHORED copper
    this call must treat as already real: every row is a Space obstacle as
    its own net's copper before any segment is searched (so same-net
    routing may still touch it and every foreign net treats it as
    blocked, same as a pad), and any segment whose two pins ALREADY sit
    in one connectivity component of ``fixed_copper`` alone is never
    searched at all — see :func:`_split_fixed_realized` and
    :attr:`RealizeResult.fixed_realized`. ``None`` (the default)
    reproduces the prior behaviour exactly: nothing is claimed, nothing
    is short-circuited."""
    ids = list(range(ir.n_segments)) if seg_ids is None else seg_ids
    if config.router != "vector":
        raise ValueError(
            f"router={config.router!r} — the grid maze router and the "
            "tangent drawer were removed (Reto 2026-10-20); only 'vector' "
            "exists — see docs/backlog/pcb-place-route-datamodel.md"
        )
    route_ids, fixed_realized = _split_fixed_realized(ir, ids, footprints, fixed_copper)
    (
        tracks,
        vias,
        unrouted,
        pours,
        reasons,
        unstitched,
        island_notes,
        vector,
    ) = _realize_vector(ir, route_ids, config, footprints, fixed_copper)
    pours, dropped = _drop_bare_islands(
        ir,
        tracks,
        vias,
        pours,
        pads_for_ir(ir, [str(e.get("name")) for e in ir.stackup], footprints),
        fixed_copper,
    )
    unstitched = _without_dropped(unstitched, dropped)
    warnings = _gap_usage(ir, route_ids, config)
    return RealizeResult(
        tuple(tracks),
        tuple(vias),
        tuple(warnings),
        tuple(unrouted),
        tuple(pours),
        tuple(reasons),
        tuple(unstitched),
        tuple(fixed_realized),
        island_notes,
        class_layer_drops=tuple(class_layer_drops(ir.stackup, config.class_rules)),
        pour_quality=tuple(pour_quality(pours)),
        dropped_islands=tuple(dropped),
        vector=vector,
    )


def _fixed_copper_connectivity_model(
    ir: PcbIR,
    footprints: dict[str, dict[str, Any]] | None,
    fixed_copper: list[dict[str, Any]],
) -> dict[str, Any]:
    """The :mod:`precis.pcb.connectivity`-shaped model of "every placed
    pad, plus ONLY the authored ``fixed_copper`` — never derived copper,
    which does not exist yet at this point in a checkpoint run" — the
    input :func:`_split_fixed_realized` asks
    :func:`~precis.pcb.connectivity.connected_pin_pairs` about.

    ``pads`` is :func:`pads_for_ir`'s own output, unmodified — the single
    "where is this pad, and which pin" answer this module already gives
    every other consumer (module docstring). Each fixed-copper row's
    ``net`` is normalized to ``""`` when absent (``None``) rather than
    left as a bare dict key: :mod:`precis.pcb.drc`'s ``_Prim`` builders
    this module's connectivity siblings share do ``item.get("net", "")``,
    which reads back ``None`` (not the empty-string default) for a key
    that is PRESENT with a ``None`` value — exactly ``pcb_fixed_copper_
    list``'s own shape for a row with no net."""
    layers = [str(layer.get("name")) for layer in ir.stackup]
    return {
        "layers": layers,
        "pads": pads_for_ir(ir, layers, footprints),
        "copper": [{**row, "net": row.get("net") or ""} for row in fixed_copper],
    }


#: Terminal candidates for ONE end of a segment: every one of that pin's
#: fixed-copper island terminals, paired with the resolved layer INDEX the
#: vector router already speaks (:func:`_island_terminals_by_pin`'s own
#: output is keyed by layer NAME, connectivity's currency; this module's
#: grid is keyed by stackup index).
_EndTerminals = tuple[tuple[pcb_connectivity.FixedTerminal, int], ...]


def _island_terminals_by_pin(
    ir: PcbIR,
    footprints: dict[str, dict[str, Any]] | None,
    fixed_copper: list[dict[str, Any]] | None,
) -> dict[tuple[str, str], _EndTerminals]:
    """Every pin's fixed-copper island terminals
    (:func:`~precis.pcb.connectivity.fixed_copper_pin_terminals`), with
    each terminal's LAYER NAME resolved to this stackup's own layer
    INDEX — the same ``layer_name_to_idx`` join :func:`_static_obstacles`
    already does, repeated here (not threaded through as shared state)
    because the two call sites — claiming geometry vs. offering route
    terminals — share no other state. A terminal naming a layer this
    stackup does not carry is dropped, same "nothing to check against"
    convention :func:`_static_obstacles` already applies to a row it
    cannot resolve.

    Built ONCE per :func:`_realize_vector` call, outside the rip-up/retry
    loop — same reasoning as :func:`_courtyard_ink_field`/
    ``_keepout_obstacles`` right above it: this is fixed-copper- and
    placement-derived, identical on every attempt."""
    if not fixed_copper:
        return {}
    model = _fixed_copper_connectivity_model(ir, footprints, fixed_copper)
    by_pin = pcb_connectivity.fixed_copper_pin_terminals(model)
    if not by_pin:
        return {}
    layer_name_to_idx = {
        str(layer.get("name")): i for i, layer in enumerate(ir.stackup)
    }
    out: dict[tuple[str, str], _EndTerminals] = {}
    for key, terminals in by_pin.items():
        resolved = tuple(
            (t, layer_name_to_idx[t.layer])
            for t in terminals
            if t.layer in layer_name_to_idx
        )
        if resolved:
            out[key] = resolved
    return out


def _sibling_pad_terminals_by_pin(
    ir: PcbIR,
    footprints: dict[str, dict[str, Any]] | None,
    signal_layers: list[int],
) -> dict[tuple[str, str], _EndTerminals]:
    """Every pin's NON-PRIMARY pads as extra route terminals, in
    :func:`_island_terminals_by_pin`'s shape (gr339236).

    The IR carries one position per pin (``pin_dx``/``pin_dy``, the
    primary pad), so a segment's search started and ended there alone.
    A pin's other pads (a connector's second tab numbered like the first,
    a drilled land beside an SMD pad on the same pin) are already its
    net's copper to every other consumer — emitted by :func:`pads_for_ir`,
    claimed for the pin's net by :func:`_footprint_pad_claims`, unioned
    with the primary by :mod:`precis.pcb.connectivity` (gr451276) — so
    landing on any of them joins the pin. Offering them here lets a route
    reach the pin where its primary pad is walled in, and lets a net-class
    layer lock (:func:`_resolve_route_end`) start from a sibling drilled
    pad's landing on an allowed layer instead of failing ``layer_lock``.

    A drilled pad is a terminal on every signal layer, any other pad on
    its instance's mount side only — the layers
    :func:`_footprint_pad_claims` stamps it on. An unpinned pad is no
    pin's terminal. Empty for a board with no multi-pad pin, so the
    search is unchanged there."""
    out: dict[tuple[str, str], list[tuple[pcb_connectivity.FixedTerminal, int]]] = {}
    for pad, placed in _footprint_pad_rows(
        ir, [str(entry.get("name") or "") for entry in ir.stackup], footprints
    ):
        if pad.pin == NO_PIN or not signal_layers:
            continue
        point = (float(placed["x"]), float(placed["y"]))
        layer_idxs = (
            signal_layers
            if placed.get("drill")
            else [_side_layer(ir, pad.instance, signal_layers)]
        )
        key = (str(ir.instance_refdes[pad.instance]), str(ir.pin_label[pad.pin]))
        out.setdefault(key, []).extend(
            (
                pcb_connectivity.FixedTerminal(
                    point=point, layer=str(ir.stackup[i].get("name")), kind="pad"
                ),
                i,
            )
            for i in layer_idxs
        )
    # A pin whose PRIMARY pad is drilled — a through-hole connector pin,
    # a test point, a plated land standing in for a via — is copper on
    # every signal layer too, and `_footprint_pad_rows` above yields the
    # non-primary pads only. Offer the primary's own point on every OTHER
    # signal layer, so a net-class lock that excludes the mount side
    # (`_resolve_route_end`) starts on an allowed layer AT the pad instead
    # of failing `layer_lock`. Found by the synthetic QFP fanout family
    # (tests/fixtures/pcb/qfp_fanout.py, 2026-10-13): through-hole sources
    # on a top-mounted part with an In2.Cu+B.Cu class routed 0/16 while
    # the same board with F.Cu allowed routed 16/16. With the native
    # layer allowed they stay extra seeds, so a route may leave the pad
    # from its far side with no via beside it (gr489175).
    if signal_layers and footprints:
        geoms = pad_geometry(ir, footprints)
        for pin in range(ir.n_pins):
            if pin >= len(geoms) or not geoms[pin].drill_mm:
                continue
            primary = pin_point(ir, pin)
            if primary is None:
                continue
            inst = int(ir.pin_instance[pin])
            native = _side_layer(ir, inst, signal_layers)
            key = (str(ir.instance_refdes[inst]), str(ir.pin_label[pin]))
            out.setdefault(key, []).extend(
                (
                    pcb_connectivity.FixedTerminal(
                        point=primary,
                        layer=str(ir.stackup[i].get("name")),
                        kind="pad_through",
                    ),
                    i,
                )
                for i in signal_layers
                if i != native
            )
    return {key: tuple(terms) for key, terms in out.items()}


def _merge_terminals(
    *by_pin: dict[tuple[str, str], _EndTerminals],
) -> dict[tuple[str, str], _EndTerminals]:
    """Concatenate per-pin terminal maps, earlier maps' terminals first."""
    out: dict[tuple[str, str], _EndTerminals] = {}
    for mapping in by_pin:
        for key, terms in mapping.items():
            out[key] = out.get(key, ()) + terms
    return out


def _seg_island_terminals(
    ir: PcbIR, ids: list[int], by_pin: dict[tuple[str, str], _EndTerminals]
) -> dict[int, tuple[_EndTerminals, _EndTerminals]]:
    """Restate :func:`_island_terminals_by_pin`'s per-PIN answer at
    per-SEGMENT granularity — ``(pin-a's terminals, pin-b's terminals)`` —
    which is what :func:`_realize_vector` actually needs to hand
    :meth:`vroute.Space.route`'s
    ``extra_start_terminals``/``extra_goal_terminals``. A segment with
    neither pin in ``by_pin`` (the common case, always true when
    ``fixed_copper`` was empty to begin with) is absent from the result."""
    if not by_pin:
        return {}
    out: dict[int, tuple[_EndTerminals, _EndTerminals]] = {}
    for seg_id in ids:
        a, b = int(ir.seg_pin_a[seg_id]), int(ir.seg_pin_b[seg_id])
        key_a = (str(ir.instance_refdes[int(ir.pin_instance[a])]), str(ir.pin_label[a]))
        key_b = (str(ir.instance_refdes[int(ir.pin_instance[b])]), str(ir.pin_label[b]))
        term_a = by_pin.get(key_a, ())
        term_b = by_pin.get(key_b, ())
        if term_a or term_b:
            out[seg_id] = (term_a, term_b)
    return out


def _terminal_noun(term: pcb_connectivity.FixedTerminal) -> str:
    """``"fixed via"``/``"fixed track"`` for authored copper, ``"same-pin
    pad"`` for :func:`_sibling_pad_terminals_by_pin`'s terminals."""
    return "same-pin pad" if term.kind == "pad" else f"fixed {term.kind}"


def _split_fixed_realized(
    ir: PcbIR,
    ids: list[int],
    footprints: dict[str, dict[str, Any]] | None,
    fixed_copper: list[dict[str, Any]] | None,
) -> tuple[list[int], list[int]]:
    """Partition ``ids`` into ``(still needs a route, already realized by
    fixed copper)`` — pcb-pre-place-route-blocks Slice 1's "Realize
    seam", router side, item 2: a segment counts as fixed-realized only
    when its OWN two pins already sit in one connectivity component of
    ``fixed_copper`` alone, via :func:`~precis.pcb.connectivity.
    connected_pin_pairs`.

    **Whole-net short-circuit is deliberately out of scope.** This only
    ever drops a segment whose specific two endpoints are already one
    piece, never an entire net because ONE of its segments qualified — a
    net with 3+ pins where fixed copper bridges only two of them still
    routes its other segment(s) normally. See :attr:`RealizeResult.
    fixed_realized`'s own docstring for the free-end residue this leaves."""
    if not fixed_copper:
        return list(ids), []
    model = _fixed_copper_connectivity_model(ir, footprints, fixed_copper)
    connected = pcb_connectivity.connected_pin_pairs(model)
    if not connected:
        return list(ids), []
    route_ids: list[int] = []
    fixed_realized: list[int] = []
    for seg_id in ids:
        a, b = int(ir.seg_pin_a[seg_id]), int(ir.seg_pin_b[seg_id])
        key_a = (
            str(ir.instance_refdes[int(ir.pin_instance[a])]),
            str(ir.pin_label[a]),
        )
        key_b = (
            str(ir.instance_refdes[int(ir.pin_instance[b])]),
            str(ir.pin_label[b]),
        )
        if key_a != key_b and frozenset((key_a, key_b)) in connected:
            fixed_realized.append(seg_id)
        else:
            route_ids.append(seg_id)
    return route_ids, fixed_realized


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
    config: RealizeConfig = RealizeConfig(),
    footprints: dict[str, dict[str, Any]] | None = None,
    fixed_copper: list[dict[str, Any]] | None = None,
) -> RealizeResult:
    """Recompute exactly ``seg_ids``' tracks (and vias) and merge them into
    a COPY of ``result`` — every other segment's :class:`RealizedTrack` is
    the same object, untouched, never re-derived. Congestion warnings are
    recomputed from the merged track set's segment ids (cheap — a
    dict-bucket pass, not a geometry re-derivation) since a re-realized
    segment can change which gap it binds.

    The segments re-route on a Space seeded with everything EXCEPT their
    own old copper (pads + keep-outs + fixed copper + the other
    segments' tracks/vias), through the same per-segment helper the
    whole-board pass uses (:func:`_route_seg_vector`)."""
    seg_set = set(seg_ids)
    ctx = _route_context(ir, seg_ids, config, footprints, fixed_copper)
    replaced: dict[int, list[RealizedTrack]] = {}
    replaced_vias: dict[int, list[RealizedVia]] = {}
    island_notes = dict(result.island_terminals)
    if ctx is not None:
        names, index, n = _layer_index(ir)
        obstacles = _static_obstacles(ir, ctx, config, footprints, fixed_copper)
        obstacles += _copper_obstacles(
            [t for t in result.tracks if t.seg_id not in seg_set],
            [v for v in result.vias if v.seg_id not in seg_set],
            ir,
            index,
            n,
            names,
        )
        space = vroute.Space(obstacles, _space_clearance(ir, ctx))
        spent = [0]
        exhausted: set[int] = set()
        for s in seg_ids:
            pa_ = pin_point(ir, int(ir.seg_pin_a[s]))
            pb_ = pin_point(ir, int(ir.seg_pin_b[s]))
            if pa_ is None or pb_ is None:
                continue
            got = _route_seg_vector(
                space,
                ir,
                s,
                ctx,
                config,
                index,
                n,
                names,
                spent,
                island_notes,
                exhausted,
            )
            if got is not None:
                replaced[s], replaced_vias[s] = got
    tracks: list[RealizedTrack] = [t for t in result.tracks if t.seg_id not in seg_set]
    tracks += [t for s in seg_ids for t in replaced.get(s, [])]
    # Vias are keyed off their originating seg_id — drop every via that
    # belonged to a segment this call re-realized, then append the fresh
    # ones. Untouched segments' vias are the same objects, same
    # discipline as their tracks.
    vias: list[RealizedVia] = [v for v in result.vias if v.seg_id not in seg_set]
    vias += [v for s in seg_ids for v in replaced_vias.get(s, [])]
    warnings = tuple(_gap_usage(ir, [t.seg_id for t in tracks], config))
    # See rip_net's own comment: stitching status is a whole-board
    # diagnostic this per-segment recompute has no new information about.
    return RealizeResult(
        tuple(tracks),
        tuple(vias),
        warnings,
        unstitched=result.unstitched,
        island_terminals=island_notes,
    )


# ── gerber.py hand-off ────────────────────────────────────────────────────


def result_copper_rows(
    result: RealizeResult,
    ir: PcbIR,
    layers: list[str],
    *,
    track_width_mm: float | None = None,
    derived: bool = False,
) -> list[dict[str, Any]]:
    """The router's tracks, vias and pours as :mod:`precis.pcb.gerber`-shaped
    copper rows — :func:`to_gerber_model`'s copper half, factored out so the
    post-route DRC gate (:func:`precis.pcb.session.routed_drc_findings`)
    checks the SAME rows a fab receives. ``derived=True`` tags each row
    ``"derived": True`` so a DRC finding's ``objects`` can tell router copper
    from authored (``fixed``) copper; off for the gerber model, which has no
    use for the key."""
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
    if derived:
        copper = [{**row, "derived": True} for row in copper]
    return copper


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
    copper = result_copper_rows(result, ir, layers, track_width_mm=track_width_mm)
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
    own "where is this pad" claim).

    ``poly`` (pcb-ewod-multitile Slice 1) is set only when ``shape ==
    "polygon"`` — the pad's vertex ring, footprint-local mm, RELATIVE TO
    THE PIN'S OWN CENTER (same convention :attr:`~precis.pcb.ir.PcbIR.
    pin_dx`/``pin_dy`` already use for a pin's offset), unrotated by the
    instance's pose. :func:`pads_for_ir` is what rotates+translates it
    into board space — the one place every OTHER shape's placement
    already happens, so a polygon pad is not a second transform path.

    ``role``/``mask``/``paste``/``drill_mm`` (gr341578) are the SAME
    optional authoring fields :func:`~precis.pcb.padplace.
    place_footprint_pads` already threads onto a real footprint's placed
    pad dict — carried here, unmolested, only when the source pad (real
    footprint or authored local footprint) actually set them. ``None``
    for every synthesized pad (no real footprint at all — there is no
    fact to carry, the same "no key" default a bare real pad with none of
    these authored gets from ``place_footprint_pads`` itself).

    ``axis_aligned`` (gripe 346962) is ``True`` only for a REAL pad whose
    total rotation (instance + the source footprint's own per-pad
    ``rot``) landed on an exact 90°-multiple — the one case
    :func:`~precis.pcb.padplace.place_footprint_pads` already swapped
    ``w_mm``/``h_mm`` into true board orientation for, so ``w_mm``/
    ``h_mm`` are only trustworthy as an axis-aligned board-space rect
    when this is ``True``. Defaults ``True`` (a synthesized pad's w/h are
    footprint-local, never board-rotated by this field at all — see
    :func:`_realize_vector`'s own pad-shape builder, which resolves a
    synthesized rect pad's board orientation from the instance's own
    rotation directly rather than through this flag)."""

    w_mm: float
    h_mm: float
    shape: str
    synthesized: bool
    poly: list[tuple[float, float]] | None = None
    role: str | None = None
    mask: str | None = None
    paste: str | None = None
    drill_mm: float | None = None
    axis_aligned: bool = True
    #: The source footprint pad's own ``rot`` (0 for a synthesized pad) —
    #: only :func:`oblique_pad_ring` reads it, to build the true outline of
    #: a rect/obround pad whose total rotation is not a 90-multiple.
    pad_rot_deg: float = 0.0
    #: A slotted hole's two end-centre points RELATIVE to the pad centre,
    #: board frame (``padplace`` ``slot`` minus the pad's x/y); ``None`` for
    #: a round hole. ``drill_mm`` is then the slot width.
    slot_off: list[list[float]] | None = None


#: Which pin a raw footprint pad belongs to — :func:`precis.pcb.padplace.
#: pad_label`, the one implementation, re-exported under this module's own
#: name because three copies of the rule had accumulated (two of them in
#: this file). Aliased rather than wrapped so there is nothing here to
#: drift.
_footprint_pad_label = padplace.pad_label


def _real_pad_sizes(
    ir: PcbIR, inst_id: int, fp: dict[str, Any]
) -> dict[
    str,
    tuple[
        float,
        float,
        str,
        list[tuple[float, float]] | None,
        str | None,
        str | None,
        str | None,
        float | None,
        bool,
        float,
        list[list[float]] | None,
    ],
]:
    """This one instance's REAL per-pin pad size (plus
    role/mask/paste/drill, gr341578), keyed by NETLIST pin name, sourced
    from a cached ``part_footprints`` row (``fp`` in that row's own
    shape: ``{"pads": [...], "pin_map": {...}}``, the exact dict
    :func:`precis.pcb.padplace.place_footprint_pads` already consumes for
    the fab-export path).

    Delegates the mirror/rotate/90-degree-swap-for-rect-pads resolution to
    :func:`~precis.pcb.padplace.place_footprint_pads` itself rather than
    re-deriving a second copy of that logic here (the exact "two call
    sites, drifted" defect :func:`pads_for_ir`'s own docstring names) —
    the trick is asking it to resolve each pad's ``net`` field as the
    pin's own NAME (``pin_to_net={name: name for ...}``), which smuggles
    pin identity through the one field of its output that survives the
    transform unmolested, letting real w/h/shape (and the rotation/side
    invariant role/mask/paste/drill flags, gr341578) be read back out
    keyed by the same pin name this module already indexes everything
    else by. Position is irrelevant here (only size and those flags are
    read back), so the probe instance is placed at the origin regardless
    of where the real instance actually sits.

    A ``shape == "polygon"`` pad's vertex ring is read straight off the
    RAW footprint pad (never through the probe above, which — usefully
    for w/h — pre-rotates by the instance's pose; a polygon needs the
    UNROTATED ring instead, since :func:`pads_for_ir` rotates it itself,
    once, at final placement) — matched to the same pin name via the same
    ``pin_map`` indirection, offset to be relative to the pad's own
    center.

    **``axis_aligned`` (gripe 346962)** is the one fact the probe's own
    output cannot answer: ``place_footprint_pads`` already swapped w/h
    into board orientation when the pad's total rotation (instance +
    the raw pad's own ``rot``) is a 90°-multiple, but its return dict
    carries no rotation, so a caller cannot tell "already swapped, exact"
    apart from "any other angle, left alone, inexact" without asking the
    SAME question again here — read straight off the raw pad (mirroring
    ``raw_poly_by_name`` right below), never re-derived from the probe's
    transformed output.
    """
    pin_names = {
        str(ir.pin_label[p])
        for p in range(ir.n_pins)
        if int(ir.pin_instance[p]) == inst_id
    }
    if not pin_names:
        return {}
    rot = float(ir.inst_rot[inst_id])
    inst_rot = 0.0 if math.isnan(rot) else rot
    probe_inst = {"x": 0.0, "y": 0.0, "rot": inst_rot}
    pads, _drills = padplace.place_footprint_pads(
        fp.get("pads") or [],
        probe_inst,
        layers=["_pad_geometry_probe"],
        pin_map=fp.get("pin_map"),
        pin_to_net={name: name for name in pin_names},
    )
    pin_map = fp.get("pin_map") or {}
    raw_poly_by_name: dict[str, list[tuple[float, float]]] = {}
    for raw in fp.get("pads") or []:
        if not raw.get("poly"):
            continue
        name = _footprint_pad_label(raw, pin_map)
        if name in raw_poly_by_name:
            # FIRST poly-bearing raw pad for this pin wins — matches the
            # `out[name]` "first wins" convention two lines below exactly,
            # which this dict feeds. Before this guard, a pin with MORE
            # THAN ONE polygon-shaped raw pad sharing its name (e.g.
            # :mod:`precis.pcb.generators`' EWOD electrode: one raw pad for
            # the electrode BODY's crenellated ring, a second for its own
            # escape-stub taper — "nothing stops two pads sharing the same
            # pin number", that module's own docstring) silently kept
            # whichever one iterated LAST, overwriting the electrode's own
            # true zigzag ring with its own tiny stub taper for every
            # downstream consumer of this function (:func:`pads_for_ir` —
            # DRC clearance, connectivity, the router). The stub happens to
            # sit close to a diagonal neighbour by design (it is EN ROUTE
            # to a shared via plaza), so this read as spurious sub-floor
            # clearance between two electrodes whose real (electrode-body)
            # geometry the round-2/3/4 zigzag work already proved correct
            # (``test_zigzag_gap_between_row_neighbours_is_constant``,
            # which never caught this because it asserts on the GENERATOR's
            # own raw geometry directly, never through this DRC-facing
            # reconstruction) — found round 7, stress-testing the
            # ``ewod-dogfood-1`` vehicle's own real ``view='drc'`` output at
            # grid sizes (7x7/8x8/9x9) no earlier round's DRC test happened
            # to exercise.
            continue
        cx, cy = float(raw["x"]), float(raw["y"])
        raw_poly_by_name[name] = [
            (float(vx) - cx, float(vy) - cy) for vx, vy in raw["poly"]
        ]
    # `total_rot` (instance + this raw pad's own ``rot``) per pin name --
    # the SAME figure `place_footprint_pads` used internally to decide
    # whether a rect/obround pad's w/h needed swapping, re-derived here
    # (not threaded out of it) because its own return dict carries no
    # rotation. "First raw pad per pin name wins", same convention as
    # `raw_poly_by_name`/`out[name]` below -- for a THT pad every
    # layer-entry shares one raw pad so this cannot disagree with itself.
    raw_rot_by_name: dict[str, float] = {}
    for raw in fp.get("pads") or []:
        name = _footprint_pad_label(raw, pin_map)
        if name in raw_rot_by_name:
            continue
        raw_rot_by_name[name] = float(raw.get("rot") or 0.0)
    # Slot end-centres relative to the pad centre, through the instance's
    # REAL side: the probe above is never mirrored, but an oblique slot on
    # a bottom-side part must match padplace's (fab/Excellon) diagonal.
    side_inst = {
        "x": 0.0,
        "y": 0.0,
        "rot": inst_rot,
        "layer": "bottom" if bool(ir.inst_bottom[inst_id]) else "top",
    }
    raw_slot_by_name: dict[str, list[list[float]] | None] = {}
    for raw in fp.get("pads") or []:
        name = _footprint_pad_label(raw, pin_map)
        if name not in raw_slot_by_name:
            raw_slot_by_name[name] = padplace._slot_centre_line(
                raw, side_inst, 0.0, 0.0
            )
    out: dict[
        str,
        tuple[
            float,
            float,
            str,
            list[tuple[float, float]] | None,
            str | None,
            str | None,
            str | None,
            float | None,
            bool,
            float,
            list[list[float]] | None,
        ],
    ] = {}
    for pad in pads:
        name = str(pad.get("net") or "")
        if not name or name in out:
            continue  # a THT pad emits one entry per target layer -- first wins
        w = float(pad["w"])
        shape = str(pad["shape"])
        poly = raw_poly_by_name.get(name) if shape == "polygon" else None
        # `place_footprint_pads` already threads role/mask/paste/drill
        # straight off the raw pad onto this placed one (its own
        # docstring) -- reading them back here off ITS output, not a
        # second raw-pad scan, keeps this the same one-hop indirection
        # the w/h/shape/poly reads above already use.
        role = pad.get("role")
        mask = pad.get("mask")
        paste = pad.get("paste")
        drill = pad.get("drill")
        total_rot = inst_rot + raw_rot_by_name.get(name, 0.0)
        out[name] = (
            w,
            float(pad.get("h", w)),
            shape,
            poly,
            str(role) if role is not None else None,
            str(mask) if mask is not None else None,
            str(paste) if paste is not None else None,
            float(drill) if drill is not None else None,
            padplace.pad_axis_aligned(total_rot),
            raw_rot_by_name.get(name, 0.0),
            raw_slot_by_name.get(name),
        )
    return out


def pad_geometry(
    ir: PcbIR, footprints: dict[str, dict[str, Any]] | None = None
) -> list[PadGeom]:
    """Every pin's resolved pad SIZE, index-aligned with ``ir.pin_*`` —
    the one place :func:`pads_for_ir` and the vector router's pad-claim
    stamping (:func:`_realize_vector`) both read pad size from, so they cannot
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
    real_by_inst: dict[
        int,
        dict[
            str,
            tuple[
                float,
                float,
                str,
                list[tuple[float, float]] | None,
                str | None,
                str | None,
                str | None,
                float | None,
                bool,
                float,
                list[list[float]] | None,
            ],
        ],
    ] = {}
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
            (
                w,
                h,
                shape,
                poly,
                role,
                mask,
                paste,
                drill,
                axis_aligned,
                pad_rot,
                slot_off,
            ) = real
            out.append(
                PadGeom(
                    w,
                    h,
                    shape,
                    synthesized=False,
                    poly=poly,
                    role=role,
                    mask=mask,
                    paste=paste,
                    drill_mm=drill,
                    axis_aligned=axis_aligned,
                    pad_rot_deg=pad_rot,
                    slot_off=slot_off,
                )
            )
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


def _footprint_pad_rows(
    ir: PcbIR,
    layers: list[str],
    footprints: dict[str, dict[str, Any]] | None,
) -> list[tuple[FootprintPad, dict[str, Any]]]:
    """``(pad, placed_pad)`` for every NON-PRIMARY pad of the IR's pad set
    (:func:`~precis.pcb.ir.footprint_pad_set`) on a placed instance — the
    pads ``ir.pin_*`` does not describe, placed into board space.

    **A pad is physical.** It occupies board space and nothing may route
    through it whether or not a net names it: a manufacturer's NC pin, an
    unconnected corner land, the second half of a split thermal slug, a
    connector's second tab numbered like the first. Everything that asks
    "where are the pads" walks ``ir.pin_*`` — one pad per pin, the
    PRIMARY one — so before this those pads existed in exactly ONE place:
    the fab output, where :func:`precis.pcb.padplace.board_pads` flashes
    every pad the cached footprint carries. Copper could therefore be
    routed straight through a pad the gerbers then make: a real short
    (gr451276).

    **A pinned pad keeps its pin's net; an unpinned pad is net-less.**
    The second GND lead carries ``net="GND"`` — so it is its own net's
    copper to the router (routable to, not a foreign obstacle) and to DRC
    (no clearance finding against its own net), and connectivity treats
    the pads of one pin as one node (:mod:`precis.pcb.connectivity`, the
    part bonds them internally). An earlier version handed a pin's extra
    pads out with ``net=""``, which dropped them from their net's pad set
    AND made them foreign obstacles to the very net they belong to —
    caught by ``test_dogfood_route_op_routes_real_geometry...`` on GND.

    **Two consumers, because there are two pad paths.**
    :func:`_extra_footprint_pads` reshapes these for :func:`pads_for_ir`
    — DRC's ``_drc_pads``, the fab SVG, the plane pours
    (:func:`_pad_blockers`), the fixed-copper connectivity model,
    :func:`to_gerber_model`. :func:`_footprint_pad_claims` reshapes the
    SAME rows for the vector router, which builds its own claim list from
    :func:`pad_geometry` over ``ir.pin_*`` and would otherwise never see
    them. Detection without that second half only REPORTS the short after
    the router has drawn it — measured on the EWOD dogfood, 24
    ``track[...] <-> pad[]`` clearance errors at 0.000mm.

    The ``(pad, placed_pad)`` shape is what lets both share one answer:
    the router half needs each pad's raw ``rot`` and its pin, which the
    placed dict drops, and the instance id to resolve its mount side.

    An unplaced instance (NaN pose) contributes nothing, matching
    :func:`precis.pcb.padplace.board_pads`'s own rule; which instances
    have a pad set at all (real footprint, join succeeded) is
    :func:`~precis.pcb.ir.footprint_pad_set`'s decision, not repeated
    here.
    """
    if not footprints or not layers:
        return []
    extras_of: dict[int, list[FootprintPad]] = {}
    for pad in footprint_pad_set(ir, footprints):
        if not pad.primary:
            extras_of.setdefault(pad.instance, []).append(pad)
    rows: list[tuple[FootprintPad, dict[str, Any]]] = []
    for inst_id, extras in extras_of.items():
        x = float(ir.inst_x[inst_id])
        y = float(ir.inst_y[inst_id])
        if math.isnan(x) or math.isnan(y):
            continue
        refdes = str(ir.instance_refdes[inst_id])
        rot = float(ir.inst_rot[inst_id])
        inst = {
            "x": x,
            "y": y,
            "rot": 0.0 if math.isnan(rot) else rot,
            "refdes": refdes,
            "layer": "bottom" if bool(ir.inst_bottom[inst_id]) else "top",
        }
        pin_to_net: dict[str, str] = {}
        for pad in extras:
            if pad.pin == NO_PIN:
                continue
            net_id = int(ir.pin_net[pad.pin])
            # Same NO_NET-never-indexes-`net_name` rule as `pads_for_ir`.
            pin_to_net[pad.label] = "" if net_id == NO_NET else str(ir.net_name[net_id])
        # ONE outer layer, not the whole stackup: a drilled pad makes
        # `place_footprint_pads` emit a row per copper layer, but
        # `pads_for_ir`'s per-pin loop emits a single row carrying `drill`
        # and lets `drc.py::clearance_pairs_indexed` read that as "spans
        # every layer". Passing the whole list here would give a
        # non-primary THT pad a different shape from a primary one and
        # multiply every finding about it by the layer count.
        placed, _drills = padplace.place_footprint_pads(
            [pad.raw for pad in extras],
            inst,
            layers=[_side_layer(ir, inst_id, layers)],
            pin_map=(footprints.get(refdes) or {}).get("pin_map"),
            pin_to_net=pin_to_net,
        )
        part_lcsc = ir.instance_part_lcsc[inst_id]
        for pad, placed_pad in zip(extras, placed, strict=True):
            # Real measured geometry by construction -- this path exists
            # only where a cached footprint does, so `export_fab`'s
            # synthesized-geometry refusal must never trip on these.
            placed_pad["synthesized"] = False
            if part_lcsc:
                placed_pad["part_lcsc"] = str(part_lcsc)
            if pad.pin == NO_PIN:
                # No declared pin claims this land (Slice 2): keep it in
                # the model as always — it is real copper — but flag it so
                # `drc.py::check_undeclared_pads` can complain that the
                # netlist has no name for it (and so a same-named sibling
                # pad is told apart from a never-declared one). `pad`'s
                # footprint number rides along so the finding can name
                # the physical land, not only the resolved label.
                placed_pad["undeclared"] = True
                placed_pad["pad_number"] = pad.number
            rows.append((pad, placed_pad))
    return rows


def _footprint_pad_claims(
    ir: PcbIR,
    layers: list[str],
    signal_layers: list[int],
    footprints: dict[str, dict[str, Any]] | None,
    n_board_layers: int,
) -> list[tuple[Point, int, float, tuple[int, ...]]]:
    """The ROUTER-shaped half of :func:`_footprint_pad_rows` — every
    non-primary footprint pad as a claim :func:`_route_context` appends
    to its ``pads`` list, so the vector search's pads are never routed
    through the land the fab output flashes (gr451276's second half).

    A pinned pad is claimed for its pin's NET — the same owner the pin's
    primary pad already claims, so the net may land on either and nothing
    else may cross it — or, for an NC pin, that pin's own sentinel
    (``ir.n_nets + pid``, :func:`_route_context`'s per-pin scheme). An
    unpinned pad gets its own owner in the next free id band:
    ``ir.n_nets + ir.n_pins + k``. Distinct per pad, so two unpinned
    lands are never read as one shared net and nothing — including
    another unpinned land — may route through either.

    The radius carried is the ENCLOSING circle ``hypot(w, h) / 2`` off
    the PLACED dict (already board coordinates — ``place_footprint_pads``
    has done the mirror/rotate): the tuple feeds only distance probes
    (``_constricting_pad_names``, ``_pour_pieces``), which take the
    conservative circle on purpose. The vector search's actual pad
    obstacles come from :func:`pads_for_ir` → :func:`vroute.obstacles_of`
    with the true shape; the two never need to agree because only the
    Space's copy is a keep-out."""
    claims: list[tuple[Point, int, float, tuple[int, ...]]] = []
    base = int(ir.n_nets) + int(ir.n_pins)
    for k, (pad, placed) in enumerate(_footprint_pad_rows(ir, layers, footprints)):
        inst_id = pad.instance
        x, y = float(placed["x"]), float(placed["y"])
        w = float(placed.get("w") or 0.0)
        h = float(placed.get("h") or w)
        if pad.pin == NO_PIN:
            owner = base + k
        else:
            net_id = int(ir.pin_net[pad.pin])
            owner = net_id if net_id != NO_NET else int(ir.n_nets) + pad.pin
        layer_idxs = (
            tuple(range(n_board_layers))
            if placed.get("drill")
            else (_side_layer(ir, inst_id, signal_layers),)
        )
        claims.append(((x, y), owner, math.hypot(w, h) / 2.0, layer_idxs))
    return claims


def _extra_footprint_pads(
    ir: PcbIR,
    layers: list[str],
    footprints: dict[str, dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """The model-shaped half of :func:`_footprint_pad_rows` — every
    non-primary footprint pad as a :mod:`precis.pcb.gerber` pad dict.

    Keyed by pad number and never promoted to a pin: no IR pin id of its
    own. A pinned pad carries its pin's net (and ``pin`` label); an
    unpinned one carries ``net=""``, the same "a pad with no net is
    skipped" convention :mod:`precis.pcb.connectivity` already applies to
    an NC pin's pad — an obstacle and a clearance subject, nothing more,
    with ``pin`` carrying the footprint's own label purely so a DRC
    finding can name the thing it hit.
    """
    return [placed for _pad, placed in _footprint_pad_rows(ir, layers, footprints)]


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

    **The whole footprint pad set is in here (gr451276)** — the per-pin
    loop emits each pin's PRIMARY pad, and every other pad of a real
    cached footprint follows: pads no netlist pin names (NC pins, dead
    corner lands) and a pin's further same-named pads (a split thermal
    slug's second half, a connector's second tab). They are physical:
    they take up space and the gerbers flash them, so every consumer of
    this function must see them or it is checking a different board than
    the one that gets made. For a part with a cached real footprint the
    emitted pad count equals the footprint's pad count, however many nets
    reference it. See :func:`_extra_footprint_pads`; a pinned pad keeps
    its pin's net, an unpinned one has none, and none is promoted to a
    pin. The vector router does NOT read its obstacles from this function (it
    stamps :func:`pad_geometry` over ``ir.pin_*`` directly), so the same
    rows reach it through :func:`_footprint_pad_claims` — see
    :func:`_footprint_pad_rows`.

    **``paste``/``mask``/``role``/``drill`` ride along too (gr341578)**,
    sourced the same way :func:`~precis.pcb.padplace.place_footprint_pads`
    already carries them for the fab-export path — added only when the
    source pad set them, never invented. These are additive: every prior
    consumer keying off ``layer``/``net``/``shape``/``x``/``y``/``w``
    (``/h``/``poly``)/``synthesized``/``refdes``/``pin`` alone is
    unaffected.
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
        inst_id = int(ir.pin_instance[pid])
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
        # gr341516 — a pad's IR layer used to be `layers[PAD_LAYER]`
        # (always index 0, F.Cu) for every pin regardless of which side
        # of the board its instance actually mounts on
        # (`ir.py::from_graph` now reads `pcb_instances.layer` into
        # `PcbIR.inst_bottom`, the same predicate the real gerber path
        # (`padplace.place_footprint_pads`) already used) -- so a
        # bottom-side part's pads read, to DRC/preview, as sitting on the
        # SAME copper as whatever top-side part happened to be above it.
        # `layers[0]`/`layers[-1]` are the board's own two outer entries
        # (`layers` is index-aligned to `ir.stackup` -- module docstring's
        # "layers are integer indexes" convention, and DEFAULT_STACKUP's
        # own F.Cu-first/B.Cu-last ordering), never a stackup-index lookup
        # of a hardcoded name. A drilled (through-hole) pad's own land is
        # real copper on every layer, not just the one reported here (see
        # `place_footprint_pads`'s identical `drill` handling) -- DRC's
        # clearance/via-keepout rules treat `pad["drill"]` as the "spans
        # every layer" signal instead (`drc.py::clearance_pairs_indexed`),
        # so this single-entry `layer` value stays honest as "this pad's
        # OUTER copper flash lands here" without this function needing a
        # second, per-layer pad-dict shape only THT pins would use.
        # `_side_layer` (module-level, shared with `_realize_vector`'s
        # router-side pad list) is the one implementation of this
        # ternary now -- see its own docstring for why a second hand
        # copy over a different `layers` element type stayed a live risk.
        pad_layer = _side_layer(ir, inst_id, layers)
        # Board-space w/h through the SHARED :func:`pad_board_wh`, never
        # `geom.w_mm`/`geom.h_mm` raw: a synthesized rect pad's are
        # footprint-local, so a 90/270-rotated instance's pads were
        # emitted here transposed from the ones the occupancy grid
        # claimed through `pads_for_ir`/`vroute.obstacles_of`
        # for the clearance error this produced on copper that is
        # actually clear.
        pad_w, pad_h = pad_board_wh(geom, float(ir.inst_rot[inst_id]))
        pad: dict[str, Any] = {
            "layer": pad_layer,
            "net": "" if net_id == NO_NET else str(ir.net_name[net_id]),
            "shape": geom.shape,
            "x": point[0],
            "y": point[1],
            "w": pad_w,
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
            "refdes": str(ir.instance_refdes[inst_id]),
            "pin": str(ir.pin_label[pid]),
        }
        # `part_lcsc` (gr341532 scoping fix) -- carried only when the
        # instance is a real catalog part (`PcbIR.instance_part_lcsc`,
        # never an empty string, `None` for an unlinked instance). This is
        # what lets `drc.py::check_synthesized_footprint` tell "a catalog
        # part with no cached footprint" (an actionable gap: go cache it)
        # apart from "a design-local/hand-authored pad the landpattern
        # module sized" (this module's normal, permanent path for
        # anything with no LCSC C-number -- a generator's own pads,
        # mounting holes, wires) which can never become "real" by caching
        # anything and must never be flagged as if it could.
        part_lcsc = ir.instance_part_lcsc[inst_id]
        if part_lcsc:
            pad["part_lcsc"] = str(part_lcsc)
        # A synthesized pad on an instance whose footprint IS cached is a
        # pin NAME that matches no pad, not a missing footprint — DRC
        # reports the two differently (`check_synthesized_footprint`).
        if geom.synthesized and footprints:
            fp = footprints.get(str(ir.instance_refdes[inst_id])) or {}
            if fp.get("pads"):
                pad["pin_unmatched"] = True
        if geom.shape != "circle":
            pad["h"] = pad_h
        if pad_is_oblique(geom, float(ir.inst_rot[inst_id])):
            # An obliquely-rotated rect/obround is NOT an axis-aligned w x
            # h box: emit its true rotated outline as a polygon pad so the
            # DRC, gerber, fab preview and connectivity all measure the
            # real land. ``w``/``h`` become the ring's bbox -- the
            # "informational bbox" a polygon pad carries (gerber.py's
            # module docstring), which keeps the silk's box tests
            # conservative. The router's claim stays the enclosing circle
            # (`vroute.obstacles_of`'s pad body), which contains this ring.
            ring = oblique_pad_ring(
                geom,
                float(ir.inst_rot[inst_id]),
                mirrored=bool(ir.inst_bottom[inst_id]),
            )
            xs = [rx for rx, _ in ring]
            ys = [ry for _, ry in ring]
            pad["shape"] = "polygon"
            # The land's own narrow dimension, before the bbox below
            # replaces w/h: the annular-ring rule must keep judging the
            # true land, not its (wider) rotated bbox.
            pad["land_min_mm"] = round(min(float(pad["w"]), float(pad["h"])), 4)
            pad["w"] = round(max(xs) - min(xs), 4)
            pad["h"] = round(max(ys) - min(ys), 4)
            pad["poly"] = [
                [round(point[0] + rx, 4), round(point[1] + ry, 4)] for rx, ry in ring
            ]
        elif geom.shape == "polygon" and geom.poly:
            # `point` is already the placed pin CENTER (`pin_point`); the
            # ring is stored relative to that same center, so it only needs
            # the SAME mirror (bottom side) + rotation via `pose` (never a
            # second translate) before adding `point` back on -- the rule
            # `padplace.place_footprint_pads` applies on the fab path.
            inst_rot = float(ir.inst_rot[inst_id])
            inst_rot = 0.0 if math.isnan(inst_rot) else inst_rot
            pad["poly"] = [
                [round(point[0] + rvx, 4), round(point[1] + rvy, 4)]
                for rvx, rvy in (
                    pose.rel_to_world(
                        vx,
                        vy,
                        inst_rot,
                        mirrored=bool(ir.inst_bottom[inst_id]),
                    )
                    for vx, vy in geom.poly
                )
            ]
        # role/mask/paste/drill (gr341578) — the SAME optional keys
        # :func:`precis.pcb.padplace.place_footprint_pads` already emits
        # for the fab-export path, added only when the source pad
        # actually set them (a synthesized pad, or a real pad that never
        # authored one, carries none of these — same "no key" convention
        # `place_footprint_pads` itself uses for a bare pad, see its own
        # docstring). Without these, `gerber.solderpaste_gerber`'s own
        # `paste == "none"` skip (module docstring) never fires for a
        # pad built off this IR path, so an electrode meant to get NO
        # stencil opening got one anyway — the F_Paste layer came out
        # byte-identical to F_Cu on every board whose fab preview reaches
        # pads only through here (this function's own docstring: "every
        # consumer that needs pad geometry calls this").
        if geom.role is not None:
            pad["role"] = geom.role
        if geom.mask is not None:
            pad["mask"] = geom.mask
        if geom.paste is not None:
            pad["paste"] = geom.paste
        if geom.drill_mm:
            pad["drill"] = geom.drill_mm
            if geom.slot_off:
                pad["slot"] = [
                    [round(point[0] + ox, 4), round(point[1] + oy, 4)]
                    for ox, oy in geom.slot_off
                ]
        out.append(pad)
    # A PLATED mounting hole's copper annulus (a solder-on nut's land) is
    # real pad copper on EVERY copper layer — emitted here because this
    # function is "the single answer to where are the pads" (docstring
    # above): render, gerber, clearance DRC and the via-pad keep-out all
    # inherit the ring from this one loop. ``net=""`` follows the NC-pad
    # convention (checkable for clearance, attributed to no net); the
    # synthesized flag is False — the ring is authored dimension, not a
    # landpattern bound, so fab export must not refuse it. A bare NPTH
    # hole adds nothing here: no copper, and ``check_npth_clearance``
    # owns its rule via ``model['drills']``.
    for k, hole in enumerate(ir.mounting_holes):
        if not hole.plated or float(hole.ring_dia_mm) <= 0.0:
            continue
        for layer_name in layers:
            out.append(
                {
                    "layer": layer_name,
                    "net": "",
                    "shape": "circle",
                    "x": float(hole.x),
                    "y": float(hole.y),
                    "w": float(hole.ring_dia_mm),
                    "synthesized": False,
                    "refdes": f"MH{k + 1}",
                    "pin": "1",
                }
            )
    # The footprint pads the per-pin loop above does not describe
    # (gr451276) — see :func:`_extra_footprint_pads`. Appended here rather
    # than folded into the loop because they are NOT pins: no `pid` of
    # their own, no entry in any `ir.pin_*` array, and the one thing every
    # consumer of this function needs from them is that they occupy space
    # (and, for a pinned one, whose net's copper they are).
    out.extend(_extra_footprint_pads(ir, layers, footprints))
    return out


__all__ = [
    "PAD_LAYER",
    "CongestionWarning",
    "PadGeom",
    "RealizeConfig",
    "RealizeResult",
    "RealizedTrack",
    "RealizedVia",
    "UnroutedReason",
    "UnstitchedNet",
    "fixed_copper_pours",
    "pad_geometry",
    "pads_for_ir",
    "pin_topology",
    "re_realize_segments",
    "realize",
    "rip_net",
    "to_gerber_model",
]
