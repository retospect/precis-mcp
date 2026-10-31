"""Sketch → copper geometry: the pad model and the stored-copper model.

The router is gone (Reto 2026-10-25: the topological router —
``topo.py``/``topoplace`` — is the only router; ``vroute`` and this
module's ``realize()`` engine, plane fan-out, pour stitching and route
diagnosis were deleted; the last vroute state is tagged
``vroute-final``). What remains here is what the surviving surfaces
share:

- **Pad geometry** — :class:`PadGeom`, :func:`pad_geometry`,
  :func:`pads_for_ir`: where each pin's land is and how big it is, the
  single answer placement, DRC, export and import all read.
- **The stored-copper data model** — :class:`RealizedTrack`,
  :class:`RealizedVia`, :class:`RealizeResult` and friends: the shape a
  pre-deletion route run stored (``pcb_copper`` rows still render
  through :func:`result_copper_rows`/:func:`to_gerber_model`).
- **Authored-copper pours** — :func:`fixed_copper_pours` and its
  ``_pour_planes``/``_drop_bare_islands`` tail: the fills an imported
  board's authored plane assignment still gets in ``view='drc'``'s model
  (``check_run``). No NEW router pours are produced anywhere.
- **Rule resolution** — :func:`_resolve_track_rules`/:func:`_seg_rules`/
  :func:`_signal_layers`: what the congestion view's capacity map reads
  for per-net width/clearance.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from shapely.geometry import Point as _ShapelyPoint
from shapely.geometry import Polygon as _ShapelyPolygon

from precis.pcb import geom, landpattern, padplace, pose
from precis.pcb.capabilities import CapabilityRow, capability_for
from precis.pcb.geom import Point
from precis.pcb.ir import (
    NO_NET,
    NO_PIN,
    FootprintPad,
    PcbIR,
    footprint_pad_set,
    pin_point,
    plane_layers_of,
    routable_layers,
)
from precis.pcb.planes import (
    PourQuality,
    PourSpec,
    ThermalSpec,
    plane_pours,
)
from precis.pcb.rules import (
    PAD_LAYER,
    NetRules,
    layer_is_outer,
    net_current_a_or_none,
    resolve_net_rules,
    seg_current_a_or_none,
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


@dataclass(frozen=True, slots=True)
class RealizeConfig:
    clearance_mm: float = 0.15
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
    #: Thermal reliefs on pour-connected pads
    #: (:class:`precis.pcb.planes.ThermalSpec`), the board profile's
    #: ``thermal_spokes``/``thermal_spoke_width_mm``/``thermal_gap_mm``
    #: (prototype: 4 x 0.30 mm spokes over a 0.25 mm gap; ``None`` is off —
    #: prod's solid pour, today's behaviour exactly). A net whose class
    #: rules set ``thermal: 'solid'`` is exempt: the class value wins over
    #: the profile.
    thermal: ThermalSpec | None = None


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
      REAL search hit the expansion cap (:func:`_realize_vector` records
      it — reported directly, never re-probed, since a pads-only probe
      that finds a corridor would mislabel the budget failure
      ``'congestion'``; gr489909, 2026-10-20), or the diagnosis's own
      probe exhausted, leaving "walled in" and "too far to search"
      untold apart. The probe case was reported as ``'no_path'`` until
      2026-10-01; the real-search case as ``'congestion'`` until
      2026-10-20 — on a fine grid over a large board each conflation is
      the common case, not a corner.
    - ``'keepout'`` — the search reached the goal's layer but every leg
      into the goal (or out of the start) was refused ONLY by non-copper
      obstacles — a ``track_only`` reserved region (a derived power
      area), a ``via_only`` silk keep-out, or a fiducial/NPTH keep-out —
      not by another net's copper. Different fix from ``'congestion'``:
      the reservation, not a race, has to move. Witness-sampled
      (``fail_witness``'s ``goal_blockers``/``start_blockers``,
      gr503666).
    - ``'via_blocked'`` — the search roamed its allowed layers but never
      REACHED the goal's layer: every layer transition was refused (no
      legal via site in reach, or no route to one that cleared). The
      layer-transition refusal of gr503666's vocabulary; without it this
      case read as ``'no_path'`` even though a corridor exists on each
      layer and only the hop between them is missing.
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


def pad_board_wh(geom: PadGeom, inst_rot: float) -> tuple[float, float]:
    """One pad's width/height in BOARD space — the single answer to "how
    wide is this pad, along the board's own x", for every consumer that
    reads ``PadGeom.w_mm``/``h_mm``.

    A REAL pad arrives already board-oriented: :func:`~precis.pcb.
    padplace.place_footprint_pads` swaps w/h itself when the pad's total
    rotation (instance + the pad's own ``rot``) is 90 degrees or 270, so
    the flashed aperture matches the rotated land, and
    :attr:`PadGeom.axis_aligned` records that it did. Any w/h still in
    the FOOTPRINT-LOCAL frame (:attr:`PadGeom.footprint_local` — every
    ``ir.pin_*`` fallback row, bound or real-sized) gets its swap here.

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
    if geom.footprint_local and padplace.rect_swaps_wh(rot):
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
    if geom.footprint_local:
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


def _pad_blockers(
    ir: PcbIR,
    layers: list[str],
    footprints: dict[str, dict[str, Any]] | None,
    pads: list[dict[str, Any]] | None = None,
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

    ``pads`` is :func:`pads_for_ir`'s output precomputed by the caller —
    ``_pour_planes`` needs the same list for thermal reliefs, and
    ``pad_geometry`` is not free.
    """
    out: list[dict[str, Any]] = []
    for pad in pads if pads is not None else pads_for_ir(ir, layers, footprints):
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
    thermal: ThermalSpec | None = None,
    class_rules: dict[str, dict[str, Any]] | None = None,
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
        pads = pads_for_ir(ir, layer_names, footprints)
        # A class's own ``thermal: 'solid'`` wins over the profile
        # (pcb-board-profile-prototype): power pads that carry current.
        solid_nets = {
            str(ir.net_name[n])
            for n in range(ir.n_nets)
            if (class_rules or {}).get(str(ir.net_class[n]), {}).get("thermal")
            == "solid"
        }
        copper = (
            to_gerber_model(interim, ir, layers=layer_names, outline=[])["copper"]
            + _pad_blockers(ir, layer_names, footprints, pads)
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
            thermal=thermal,
            pads=pads,
            solid_nets=solid_nets,
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
        thermal=config.thermal,
        class_rules=config.class_rules,
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


@dataclass(frozen=True, slots=True)
class DroppedIsland:
    """A poured piece removed after stitching because nothing of its net
    reaches it and no via or jumper could (pcb-floating-pour-island: stitch,
    drop only what cannot be reached, and say so)."""

    net: str
    layer: str
    area_mm2: float
    centroid: tuple[float, float]


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
    rotation directly rather than through this flag).

    ``footprint_local`` is the FRAME marker ``synthesized`` used to be
    borrowed for: ``True`` when ``w_mm``/``h_mm`` are in the pad's
    footprint-local frame and still need the instance-rotation swap
    (:func:`pad_board_wh`), ``False`` for a real footprint pad, which
    arrives already board-oriented. The two flags differ for a pin whose
    REAL size came through :meth:`~precis.pcb.ir.PcbIR.set_pin_pad` —
    not a bound (``synthesized=False``) but still footprint-local; a
    caller keying the swap off ``synthesized`` emits that pad
    transposed on every 90°-rotated instance."""

    w_mm: float
    h_mm: float
    shape: str
    synthesized: bool
    footprint_local: bool = False
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
                    # Real size or bound, the IR fallback's w/h are always
                    # footprint-local — the instance-rotation swap is
                    # still owed (:func:`pad_board_wh`).
                    footprint_local=True,
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
