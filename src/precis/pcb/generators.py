"""Computed-component generators — pcb-ewod-multitile Slice 2, now emitting
its escape fabric as real copper (docs/backlog/pcb-pre-place-route-blocks.md
Slice 2).

A ``generators`` block on ``put(kind='pcb')`` (:meth:`precis.store.
_pcb_ops.PcbMixin._pcb_apply`) names a generator call — ``{name, generator,
params}`` — that gets *expanded*, deterministically and in pure Python, into
the same shapes the manual authoring surface already accepts: components,
nets, connections, local footprints, features, and (as of pcb-pre-place-
route-blocks Slice 2) authored fixed copper. That reuse is the whole
architecture here: expansion never talks to the database directly, and
nothing downstream (padplace/DRC/gerber/SVG) needs to know a component was
generated rather than hand-authored — a :class:`GeneratorExpansion` is just
a batch :meth:`_pcb_apply` was going to process anyway.

**One component, one pad per pin.** The spec's own framing (docs/backlog/
pcb-ewod-multitile.md, "the array generator emits the integrated unit") is
"the whole array is one component whose pins are the electrode nets" — so
``ewod_pad_array`` below emits exactly ONE ``components[]`` entry (one
refdes) whose local footprint's ``pads`` list carries every electrode's
BODY polygon, one pad per pin, addressed by pin name. **This is a change
from pcb-ewod-multitile Slice 2's original shape**, which additionally gave
each driven electrode a second (F.Cu neck stub) and third (drilled plaza
via) pad row sharing the same pin — "one via per electrode, capacitive
load, negligible current" was real, but a stub/via pad is fake geometry
from the router's and the fab's point of view (see the "pcb-pre-place-
route-blocks Slice 2" section below for why, and gr339236 for the router
symptom this reverses). The neck and the via are now real ``track``/``via``
rows in :attr:`GeneratorExpansion.copper` instead — the electrode BODY pad
is, and stays, each pin's ONLY pad.

**pcb-pre-place-route-blocks Slice 2 — the escape fabric is real copper,
not pad geometry.** Every DRIVEN electrode (one with a usable plaza escape)
gets exactly three :attr:`GeneratorExpansion.copper` rows: one ``track`` on
F.Cu from the electrode body's own boundary anchor to its plaza via centre
(:func:`_stub_track_row`), one ``via`` at the plaza slot, spanning F.Cu to
B.Cu (:func:`_via_row`), and (Rulings 2026-09-19 item 11) one B.Cu ``track``
breaking OUT from the via, along the slot's own direction, to a point
``slot_a`` further from the plaza centre (:func:`_breakout_track_row` /
:func:`_breakout_far_point`) — a short, pre-solved landing past the
plaza's own crowded interior, closing gripe 347037's congestion race (the
router's own occupancy search was starving inside the plaza before it
could even leave it). The F.Cu neck is CONSTANT-width (``stub_width``, the
same sizing figure the old tapered pad used) — the taper existed only to
clear a diagonal escape's own pinch point against a NEIGHBOUR electrode's
flat corner, and :func:`_electrode_polygon`'s ``plaza_corner_chamfer``
(round 4) already does that clearance job on the ELECTRODE side, so the
track itself needs no taper of its own; the B.Cu breakout is the same
constant ``stub_width``, the fab minimum trace width. All three rows carry
an ``envelope`` (:func:`_fabric_envelope`) — the capability floor (layer
count, clearance/track-width/via-size) the fabric was solved under — so a
re-apply into a design whose rules have since moved refuses honestly
rather than silently keeping copper that may no longer be legal
(:meth:`precis.store._pcb_ops.PcbMixin._pcb_fixed_copper_envelope_mismatch`).
A merged pad may never cover a plaza (:func:`_parse_pad_sizes` refuses that
whole apply outright — 8 OTHER nets' escapes live there); a ``reserve``'d
slot suppresses its via/stub/breakout the same as it always suppressed the
old pad pair. All non-emissions are COUNTED, in the ledger's new ``fabric``
section (per-tile ``{emitted, refused, suppressed}`` plus a flat reason
list) — see :func:`_expand_ewod_pad_array`'s own fabric-bookkeeping
comment; each emitted breakout's far end is also recorded per pin
(``ledger["pads"][pin]["breakout"]``). **B.Cu fan-out from the breakout's
own far end to the tile's sink footprint is still explicitly OUT OF SCOPE
this slice** (``ledger["fabric"]["fan"] == "router"``): this module is
pure and has no DB access to the sink's real pin positions, so it cannot
compute that geometry; a sibling slice teaches the router to treat this
generator's fixed copper as pre-existing obstacles/connectivity and finish
the B.Cu run itself — the breakout only gets that run OUT of the plaza's
own congestion, it does not replace it.

**Round 8 (gripe 338983 fixed), and what pcb-pre-place-route-blocks Slice 2
closes on top of it.** The router/DRC pad source (``precis.pcb.realize.
pads_for_ir``) used to place every pin at ``ir.py``'s SYNTHESIZED
``pin_dx``/``pin_dy`` regardless of the real footprint, so for THIS
module's own custom-grid component it measured and routed wildly wrong
virtual positions. Real per-pin positions now reach the IR (``precis.pcb.
session.apply_real_pin_offsets``, wired into ``build_ir``), so escape
routing IS driven off the real electrode copper — verified end-to-end on
the ``ewod-dogfood-1`` fixture (``tests/test_pcb_ewod_dogfood.py``). Round
8 then found **the IR's one-position-per-pin model** turned an electrode's
three pads (body + stub + via) into ONE IR pad — the body, first-wins — so
the plaza via itself was invisible to the router (gr339236). Moving the
stub/via OFF the pad list and onto real ``pcb_fixed_copper`` rows (this
slice) removes the multi-pad-per-pin collapse at the source: an electrode
has exactly one pad again, and the via/track are geometry the router reads
as pre-existing fixed copper instead of a pad it never saw. Whether
``op='route'`` has actually been taught to consume ``pcb_fixed_copper`` yet
is tracked outside this module (docs/backlog/pcb-pre-place-route-blocks.md,
the router/connectivity slices) — this generator's own job, emitting
correct fabric, is done regardless of when that lands.

**Idempotency contract.** :func:`expand` is a pure function of
``(generator, name, params)`` — same inputs, byte-identical output, no
randomness, no DB reads. The store layer (:mod:`precis.store._pcb_ops`)
owns the "same params -> no-op, changed params -> retire + reinsert"
policy by comparing this module's own :attr:`GeneratorExpansion.
canonical_params` against the ``pcb_generators`` row from the PREVIOUS
apply; this module never decides whether to write anything.

**What is deliberately NOT here yet** (docs/backlog/pcb-ewod-multitile.md's
decisions log records which "still open" items this slice resolved —
plaza slot capacity, via :func:`_plaza_capacity` below — and which remain
open; the ones below are this module's own scope boundary, not
necessarily still-open spec questions):

- **DRC integration — round 3's "plaza via stays a footprint pad" decision
  is REVERSED by pcb-pre-place-route-blocks Slice 2.** Round 3 kept the
  plaza via a drilled THT footprint pad specifically because
  ``check_annular_ring`` only iterated ``model["copper"]`` vias and a pad
  never reaches that list — fixed there instead (extending ``drc.py`` to
  also ring-check drilled footprint pads) rather than by promoting the via
  to real copper. That trade-off inverted once the router-visibility cost
  of a pad-shaped via became load-bearing (gr339236: the IR's one-
  position-per-pin model makes a same-pin pad invisible the instant a
  BODY pad exists on that pin too) — real copper fixes the router gap by
  construction (:mod:`precis.pcb.drc`'s existing ``check_annular_ring``
  extension for drilled pads keeps working for every OTHER drilled pad in
  the kind; it simply has nothing left to check here, because this
  generator's own via is a ``copper`` row now, which
  ``check_annular_ring`` already iterated regardless).
  ``check_via_pad_keepout`` (a ROUTER-placed via must clear every pad) is
  unaffected either way: it protects PADS from a foreign via, and the
  electrode BODY is still a pad on the exact same terms, polygon shape
  included (``tests/test_pcb_ewod_generator_drc.py``).
- **Electrode-gap net-class clearance floor — resolved round 3.** Every
  electrode net gets ``net_class = f"ewod_{name}"`` with a dedicated
  ``pcb_net_classes`` rule (``clearance_mm = gap``), upserted by
  :meth:`precis.store._pcb_ops.PcbMixin._pcb_apply` in the same
  transaction as the rest of the expansion. Without it, every ordinary
  electrode-adjacency pair would carry a spurious WARN against the fab's
  flat ``trace_spacing_mm`` house_default tier (0.15mm at the default
  4-layer capability, above the 0.10mm default ``gap``) on every board —
  the override only changes that WARN threshold; the ERROR floor (fab
  ``jlc_min``) is untouched and still binds a ``gap`` an author sets below
  what the fab can actually make.
- **Escape net-class layer lock — Rulings 2026-09-19 item 7.** A pin that
  actually got a plaza via/stub is tagged ``f"ewod_{name}_escape"``
  instead — the SAME ``clearance_mm`` floor, plus ``"layers": ["B.Cu"]``,
  which :func:`precis.pcb.realize._net_class_layers` resolves into a hard
  per-net routing constraint ("the escape is plaza via -> B.Cu track ->
  sink pad, nothing else", closing gr347037's congestion race together
  with the pin-swap feed below). A pin with no via (no adjacent plaza, or
  a reserved slot) has no B.Cu track to lock and keeps the plain
  electrode-gap class.
- **Capability map — resolved round 5.** ``get(kind='pcb', view=
  'capability')`` (:meth:`precis.handlers.pcb.PcbHandler._render_capability`)
  renders this module's own ``ledger`` dict (unchanged shape) as either an
  SVG schematic (:func:`precis.pcb.svg.render_capability_map`) or a
  ``format='ledger'`` agent-facing table — this module itself emits
  nothing new; the ledger dict already returned here was always the
  "machine-readable ledger" the spec asked for, the gap was only that
  nothing surfaced it through ``get()`` yet.
- **Merged ``pad_sizes`` — resolved round 6.** :func:`_parse_pad_sizes`
  validates a ``pad_sizes: [{"cells": [[r, c], ...]}]`` entry into a solid
  m x n rectangle of cells, rejects any entry that would cover a via
  plaza or (``variant='rim'``) hollow cell outright (spec's own "never
  relocate the plaza" rule), and the main loop emits exactly ONE
  electrode pad plus (pcb-pre-place-route-blocks Slice 2) ONE stub track
  and ONE via copper row for the whole span — "one via suffices, the
  electrode is a capacitor" (spec decision), picked as the
  FIRST plaza-adjacent cell/direction found across the span
  (:func:`_find_plaza_escape`), same determinism the single-cell case
  already had. :func:`_electrode_polygon` itself is the load-bearing
  generalisation: its 4 walls now walk a RANGE of unit cells rather than
  exactly one, concatenating each unit's own (unchanged) crenellated-or-
  flat segment — no new "seam filler" logic needed, because a segment's
  own zero-deflection ends (the existing corner-flattening machinery)
  already connect cleanly to the next unit's, which is exactly the solid
  copper a merged pad's own internal seam should be.
- **Sink-grid emission — resolved round 7, mechanically; rebalanced by
  chain order per the 2026-09-18 ruling.** ``sink_grid``
  (:class:`_SinkGrid`) emits one bottom-side component instance per
  ``channels_per_sink`` USABLE electrode escapes, assigned in
  **serpentine (boustrophedon) chain order** — the discovery loop's own
  row-major order, direction alternating on odd rows — and split into
  as-equal-as-possible shares (``docs/backlog/pcb-ewod-multitile.md``
  "Rulings 2026-09-18" item 2: square-block ``per_tiles`` binning could
  land a 9x9 field's 72 electrodes as a lopsided 64+8 across two
  64-channel sinks; balancing by chain order instead gives 36+36).
  ``per_tiles`` is REMOVED, forward-only (no silent alias — a spatial
  block count cannot map onto a channel count); ``_parse_sink_grid``
  refuses it by name. Sinks are chained DIN->DOUT in chain-index order,
  and (optionally) tied into a shared top-plate/complement-rail net. It
  is deliberately PART-AGNOSTIC (:func:`expand` is pure, no DB reads, so
  it cannot look up a real part's own pin names) — the caller names
  every pin this module needs to wire. **Layer-aware pads (gr341516,
  landed) fixed the engine limitation this note used to describe**: a
  sink instance's own ``layer='bottom'`` is honest (gerber/silk already
  read ``pcb_instances.layer``), and ``ir.py::from_graph`` now reads it
  too — into :attr:`~precis.pcb.ir.PcbIR.inst_bottom` — so every pad's IR
  layer reflects the instance's real board side (:func:`precis.pcb.
  realize.pads_for_ir`). ``rules.py::PAD_LAYER`` still exists, but only as
  the router's own via F.Cu/B.Cu transition reference, not a pad-layer
  override; a sink placed directly under the array is correctly read as a
  different physical side by courtyard-overlap/clearance checks. Pin-to-
  channel assignment order is documented in the ledger (chain order, see
  above; the pre-place-route block spec will revisit the whole scheme).
- ``corner_radius`` (the electrode's own OUTER 4 corners — a separate
  concept from the tooth TRANSITION rounding :func:`_s_curve` now does
  internally, radius=``tooth_depth``, not user-tunable) is accepted
  (recorded in ``canonical_params``) but not drawn — natural PCB corner
  rounding at fab is what the literature survey actually credits (module
  docstring's own citation), so this is a documented no-op, not a
  silent gap.
- Rim-variant via placement is a simple, documented mechanical default
  (short straight reach off each rim pad's own inward edge, see
  ``_rim_via_point``) — NOT the "closed-form global routing" the full-grid
  plaza scheme gives; good enough to place vias without collision, not
  claimed optimal.
"""

from __future__ import annotations

import functools
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from shapely.geometry import LineString  # type: ignore[import-untyped]

from precis.pcb import DEFAULT_STACKUP
from precis.pcb.capabilities import CapabilityRow, capability_for, conductor_spacing_mm

Point = tuple[float, float]

#: The fab process the derived-sizing defaults below are pinned to — every
#: EWOD board today is the 4-layer default stackup (``pcb.DEFAULT_STACKUP``);
#: lift this once ``put(stackup=...)`` authoring (Slice 3) exists.
_FAB_PROCESS = "4layer"

#: pcb-ewod-multitile decisions log: gap 0.10mm (electrode gap — advisory
#: only, HV separation applies to plaza internals/B.Cu escapes instead),
#: edge tooth_depth 0.06mm / tooth_pitch 0.25mm (Frontiers Phys. 2020
#: survey figures, docs/backlog's literature grounding section). Pitch
#: default raised 2.0mm -> 2.25mm ("Rulings 2026-09-19", the ruling on the
#: two levers) to clear the derived plaza floor at the now-standard 250V
#: drive voltage (min_pitch 2.233mm there) without an author having to
#: know that number -- a lower pitch still works fine at a lower or
#: undeclared voltage, `resolve_ewod_sizing`'s own floor check is what
#: actually gates it either way.
_DEFAULT_PITCH_MM = 2.25
_DEFAULT_GAP_MM = 0.10
_DEFAULT_TOOTH_DEPTH_MM = 0.06
_DEFAULT_TOOTH_PITCH_MM = 0.25
_DEFAULT_STUB_WIDTH_MM = 0.20

#: A diagonal escape's neck departs from the electrode's own flat corner,
#: which sits at perpendicular distance exactly ``gap/sqrt(2)`` from the
#: TWO flanking (cardinal-escaping) neighbours' own flat corners — a pure
#: trigonometric fact of the 45-degree escape path and the axis-aligned
#: corner offsets (see :func:`_electrode_polygon`'s plaza-corner-chamfer
#: block for the derivation), independent of pitch/via/hv_separation.
#: ``gap/sqrt(2)`` (~0.0707mm at the 0.10mm default) sits BELOW the fab's
#: own absolute copper-spacing floor (jlc_min ``trace_spacing_mm``,
#: ~0.09mm at 4-layer) — no taper WIDTH redesign can rescue this (round 4
#: finding: even a hypothetically zero-width path through that exact
#: pinch point is still too close), because the flat corners themselves,
#: not the stub's own copper, are what's too close together. The fix
#: retreats (chamfers) each flanking corner along its own two walls by
#: :func:`_electrode_polygon`'s ``plaza_corner_chamfer``, cutting the
#: corner INWARD (never protruding outward, so it cannot newly violate
#: any OTHER already-tuned clearance) until the corridor reopens.
#:
#: **docs/backlog/pcb-ewod-multitile.md "Rulings 2026-09-18" item 1 —
#: the corridor is now RULE-DERIVED, not a fixed margin on top of ``gap``.**
#: Round 4's number (a flat +0.01mm on top of ``gap``, formerly a module
#: constant here) was calibrated for a TAPERED footprint-pad neck
#: (:func:`_stub_polygon`, retired), whose width right at the pinch point
#: was ~0. pcb-pre-place-route-blocks Slice 2 replaced the taper with a
#: CONSTANT-width track (:func:`_stub_track_row`) the full length, so the
#: copper occupies ``stub_width/2`` of the corridor everywhere, including
#: at the corner — at that fixed +0.01mm margin the corridor (``gap +
#: 0.01`` = 0.11mm at the default ``gap``) could not host a fab-minimum
#: track plus two fab clearances (0.09 + 2x0.09 = 0.27mm), so the deficit
#: was pinned as a KNOWN, documented gap rather than hidden. Two fixes
#: were tried against that FIXED margin and both rejected: WIDENING IT (a
#: naive constant bump closed the gap but opened a worse, unrelated
#: zigzag-wall regression, because :func:`_electrode_polygon`'s meshing
#: wall still measured its own zero-deflection flat run purely off
#: ``tooth_pitch`` — see ``_meshing_wall``'s ``margin_t0``/``margin_t1``
#: below for the re-solve that this ruling required alongside it), and
#: NARROWING THE TRACK below the fab's minimum trace width (emitted
#: 0.038mm copper JLC cannot etch — a lie the ``trace_width`` DRC rule and
#: the fixed-copper envelope both expose). The ruling replaces the fixed
#: margin with a corridor sized directly from what has to fit through it —
#: ``resolve_ewod_sizing``'s own ``plaza_corner_chamfer`` derivation is the
#: actual formula now; this constant is gone.

#: 1 micron. The electrode-gap net class's ``clearance_mm`` (below) is set
#: from the analytic ``gap`` value, but the ACTUAL manufactured gap DRC
#: measures is the shapely distance between two polygons whose vertices
#: :func:`precis.store._pcb_ops._normalize_local_footprint_pad` and
#: :func:`precis.pcb.padplace.place_footprint_pads` both round to 4
#: decimal places (0.1um) — an accumulation of independent per-vertex
#: rounding on BOTH sides of the gap that can shave a few 0.01um off the
#: exact analytic value at some point along a zigzag wall (round-3
#: stress-test finding: observed deficits of 0.00006-0.00008mm on a
#: 3x3 array). Requiring the manufactured gap to meet the exact analytic
#: value, with zero slack, makes EVERY EWOD board spuriously warn on
#: coordinate-rounding noise a real fab process could not even measure —
#: exactly the false-positive this net class exists to avoid in the first
#: place. Subtracting this slack is 10x the observed deficit while still
#: negligible against any real `gap` (1% of the 0.10mm default).
_GEOMETRY_ROUNDING_SLACK_MM = 0.001


@dataclass
class GeneratorExpansion:
    """One generator call's expansion — the exact shapes
    :meth:`precis.store._pcb_ops.PcbMixin._pcb_apply` already accepts in
    its ``components``/``nets``/``connections``/``footprints``/``features``
    batch args, plus the identity/idempotency bookkeeping the store layer
    needs and a plain-dict capability ledger for the caller to read back."""

    refdes: str
    generator: str
    version: int
    #: JSON-safe, fully-defaulted params — what :mod:`precis.store._pcb_ops`
    #: diffs against the PREVIOUS apply's stored row to decide no-op vs.
    #: retire-and-reinsert. Never re-derive this from ``params`` again once
    #: computed — the caller's raw ``params`` may omit fields this fills in.
    canonical_params: dict[str, Any]
    components: list[dict[str, Any]]
    nets: list[dict[str, Any]]
    connections: list[dict[str, Any]]
    footprints: list[dict[str, Any]]
    features: list[dict[str, Any]]
    ledger: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    #: ``pcb_net_classes`` rows this expansion wants (name -> rules,
    #: :meth:`precis.store._pcb_ops.PcbMixin._pcb_upsert_net_classes`'s own
    #: shape) — ``ewod_pad_array`` uses this to give its electrode nets a
    #: DEDICATED clearance floor equal to the authored ``gap``, upserted
    #: alongside the rest of the expansion's rows rather than merely
    #: recorded and never applied.
    net_classes: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: docs/backlog/pcb-pre-place-route-blocks.md Slice 1 — real, fixed
    #: copper this expansion wants routed into ``pcb_fixed_copper`` (an
    #: AUTHORED input parallel to ``pcb_planes``, never ``pcb_copper``
    #: directly — that table is DERIVED and would have this fabric deleted
    #: by the next realize run; see :meth:`precis.store._pcb_ops.PcbMixin.
    #: _pcb_apply`'s routing of this field). Each dict: ``{ctype: 'track'|
    #: 'via', layer: <layer NAME, e.g. 'F.Cu'>, net: <net name>, geom:
    #: {...}, envelope?: {...}, meta?: {...}}``. ``geom`` uses the exact
    #: same per-``ctype`` shape ``pcb_copper.geom`` already carries
    #: (:mod:`precis.workers.job_types.pcb_route`'s writer): a ``track``'s
    #: ``{segments: [{shape, start:[x,y], end:[x,y]}, ...], width_mm,
    #: length_mm?, is_dogbone?}``; a ``via``'s ``{x, y, dia_mm, drill_mm,
    #: span: [layer_name_lo, layer_name_hi]}`` — a via's real layer
    #: membership is ``span``, never the top-level ``layer`` (which is a
    #: schema-satisfying placeholder only, same convention ``pcb_copper``
    #: itself uses). Coordinates are absolute BOARD mm — the generator
    #: resolves its own instance placement (grid origin, rotation) before
    #: emitting these, the same way it already resolves electrode/pad
    #: positions; nothing downstream re-offsets this geometry. ``envelope``,
    #: when given, is the rule envelope (layer count / clearance / track /
    #: via floor) the fabric was solved under — the store layer refuses the
    #: whole apply if it disagrees with the board's current rules rather
    #: than silently keeping copper that may no longer be legal. Empty by
    #: default (a generator with no escape fabric of its own, e.g. every
    #: pin unusable, simply never appends here); ``ewod_pad_array``
    #: (pcb-pre-place-route-blocks Slice 2, :func:`_stub_track_row`/
    #: :func:`_via_row`) is the first real emitter.
    copper: list[dict[str, Any]] = field(default_factory=list)


def expand(generator: str, name: str, params: dict[str, Any]) -> GeneratorExpansion:
    """Dispatch to the named generator's pure expansion function. Raises
    ``ValueError`` (the handler's ``put`` already translates that to
    ``BadInput``) for an unknown generator type or invalid params — never
    silently produces a partial/garbage expansion."""
    name = str(name or "").strip()
    if not name:
        raise ValueError("pcb generator needs a name")
    fn = _REGISTRY.get(generator)
    if fn is None:
        raise ValueError(
            f"pcb generator {name!r}: unknown generator type {generator!r}; "
            f"known: {sorted(_REGISTRY)}"
        )
    return fn(name, dict(params or {}))


# ── plaza slot capacity ───────────────────────────────────────────────────
#: docs/backlog/pcb-ewod-multitile.md "Rulings 2026-09-19" item 8 —
#: absolute step (not relative to ``half``) for the small, deterministic
#: numeric search :func:`_plaza_family` runs to pick ``(a, b)``. Small
#: enough that ``min_pitch`` moves by well under the ruling's own 0.05mm
#: budget between two consecutive candidates; coarse enough that the
#: search stays fast (a few hundred thousand float ops per
#: :func:`_plaza_capacity` call, cached — see its own decorator).
_PLAZA_FAMILY_GRID_STEP_MM = 0.005


def _pair_floor(via_dia: float, hv_separation: float) -> float:
    """The via-via centre-to-centre floor every one of the family's 28
    slot pairs (:func:`_family_min_pair_chord`) must clear — unchanged
    from round 4's own adjacent-ring-chord floor, just no longer scoped
    to "adjacent" (the axis-aligned family has no uniform ring to be
    adjacent ON): ``via_dia + hv_separation``, padded by
    :data:`_GEOMETRY_ROUNDING_SLACK_MM` for the same coordinate-rounding
    reason the ring version's own docstring documented."""
    return via_dia + hv_separation + _GEOMETRY_ROUNDING_SLACK_MM


def _foreign_req(via_dia: float, hv_separation: float) -> float:
    """The floor a slot's via EDGE (not centre) must clear from any
    FOREIGN electrode's own nominal flat corner — round 2's own
    constraint 2 threshold, unchanged: ``via_dia/2 + hv_separation``,
    padded the same way."""
    return via_dia / 2.0 + hv_separation + _GEOMETRY_ROUNDING_SLACK_MM


def _family_min_pair_chord(a: float, b: float) -> float:
    """The minimum centre-to-centre distance over all 28 pairs among the
    family's 8 slots (cardinals at ``(+-a, 0)``/``(0, +-a)``, diagonals at
    ``(+-b, +-b)``) — reduced, by the family's own 8-fold symmetry, to the
    3 distinct chord shapes that can ever be the minimum: two cardinals
    90 degrees apart (``a*sqrt(2)``), two diagonals sharing one axis sign
    (``2*b``), and a cardinal next to its nearer diagonal neighbour
    (``hypot(b, a-b)``). Every OTHER pair — opposite cardinals (``2a``),
    opposite diagonals (``2*sqrt(2)*b``), a cardinal against its FARTHER
    diagonal neighbour — is strictly larger than one of these three for
    any ``a, b >= 0``; verified once against the full 28-pair brute-force
    enumeration rather than re-derived by hand at every call
    (``tests/test_pcb_ewod_generator_drc.py``'s own all-28-pairs test)."""
    return min(a * math.sqrt(2.0), 2.0 * b, math.hypot(b, a - b))


def _family_foreign_clearance(a: float, b: float, half: float, gap: float) -> float:
    """The minimum distance from ANY of the family's 8 slots to ANY
    FOREIGN electrode's own nominal (un-chamfered) flat corner — the
    identity round 2's own constraint 2 used, generalised from the single
    ring radius to two free parameters. By the family's own 8-fold
    symmetry this reduces to two closed forms, each independent of the
    OTHER parameter — but each has TWO candidate corners, not one,
    because (unlike round 2's ring, where every slot sat at the same
    radius) ``a``/``b`` are now free to grow past ``half``:

    - A cardinal slot (say E, at ``(a, 0)``) is nearest EITHER N/S's own
      near-plaza corner (board-frame offset ``(+-half, -+(half+gap))``
      from the plaza centre — the corner every flat wall uses) while
      ``a <= half + gap/2``, or NE/SE's own near-plaza corner (offset
      ``(half+gap, -+(half+gap))``) once ``a`` grows past that —
      ``min(hypot(half-a, half+gap), hypot(half+gap-a, half+gap))``. The
      crossover ``a = half + gap/2`` is where the two candidate distances
      are exactly equal (solving ``hypot(half-a, half+gap) =
      hypot(half+gap-a, half+gap)`` for ``a``).
    - A diagonal slot (say NE, at ``(b, b)`` in magnitude) is nearest the
      N (or E) electrode's own near-plaza corner — round 2's own
      constraint 2 identity, ``u`` replaced by the free ``b``:
      ``hypot(half - b, half + gap - b)``. Unlike the cardinal case, no
      SECOND candidate ever wins here for ``b`` up to at least
      ``half + gap`` (verified the same way, below).

    Both reduce EXACTLY to the brute-force 8-slots x 7-foreign-electrodes
    x 4-corners minimum for ``a, b`` in ``[0, half + gap]`` — checked
    against 2000 random ``(half, gap, a, b)`` draws in that range while
    deriving this function, zero mismatches — which is why
    :func:`_plaza_family`'s own search never lets ``a``/``b`` exceed
    ``half + gap`` (a real physical bound too: beyond it a slot has left
    its own plaza's hollow interior and entered a foreign electrode's own
    copper outline, which no corner-distance formula alone protects
    against)."""
    cardinal = min(
        math.hypot(half - a, half + gap), math.hypot(half + gap - a, half + gap)
    )
    diagonal = math.hypot(half - b, half + gap - b)
    return min(cardinal, diagonal)


def _plaza_family(
    gap: float, via_dia: float, hv_separation: float, half: float
) -> tuple[float, float, float]:
    """The best ``(a, b)`` at a GIVEN ``half`` — a small grid search (step
    :data:`_PLAZA_FAMILY_GRID_STEP_MM`) over ``a`` in ``[a0, half + gap]``,
    ``b`` in ``[b0, half + gap]``, where ``a0``/``b0`` are each axis's OWN
    independent pairwise floor (below which even same-type slots can't
    clear :func:`_pair_floor` regardless of the other axis — shrinking the
    search range's LOWER end below what the ruling's own text suggests,
    since nothing below ``a0``/``b0`` is ever feasible) and ``half + gap``
    is the search range's UPPER end (:func:`_family_foreign_clearance`'s
    own docstring — both the limit of its closed forms' validity and the
    real physical boundary of the plaza's own hollow interior). Returns
    ``(a, b, clearance)``; ``clearance`` is ``-1.0`` if ``half`` is too
    small to even host ``a0``/``b0`` (an empty search range, not a real
    candidate — the caller's own :func:`_plaza_capacity` search must grow
    ``half`` past this before it can find anything)."""
    d = _pair_floor(via_dia, hv_separation)
    a0 = d / math.sqrt(2.0)
    b0 = d / 2.0
    upper = half + gap
    if upper < a0 - 1e-9 or upper < b0 - 1e-9:
        return (a0, b0, -1.0)
    step = _PLAZA_FAMILY_GRID_STEP_MM
    best = -1.0
    best_a, best_b = a0, b0
    a = a0
    while a <= upper + 1e-9:
        b = b0
        while b <= upper + 1e-9:
            if _family_min_pair_chord(a, b) >= d - 1e-9:
                clearance = _family_foreign_clearance(a, b, half, gap)
                if clearance > best:
                    best = clearance
                    best_a, best_b = a, b
            b += step
        a += step
    return (best_a, best_b, best)


@functools.lru_cache(maxsize=256)
def _plaza_capacity(
    gap: float, via_dia: float, hv_separation: float
) -> dict[str, float]:
    """The REAL plaza floor, chosen from a two-parameter AXIS-ALIGNED
    slot family (docs/backlog/pcb-ewod-multitile.md "Rulings 2026-09-19"
    item 8) rather than round 2/4's single uniform 8-slot ring: cardinal
    slots (N/E/S/W) at distance ``a`` from the plaza centre on the axes,
    diagonal slots (NE/NW/SE/SW) at ``(+-b, +-b)``. The ring was chosen
    because a uniform 3x3 SQUARE sub-grid was unsatisfiable at the spec's
    own numbers (this function's own earlier history); the ring itself
    ties the cardinal and diagonal positions TOGETHER (both sit at the
    same radius ``R``, cardinal directly, diagonal via ``u = R/sqrt(2)``
    on each axis), even though they face DIFFERENT nearest-foreign-corner
    geometry (:func:`_family_foreign_clearance`) and only share ONE
    constraint (the via-via floor between them). Decoupling them into two
    free parameters and letting the search below explore both AND the
    fact that a cardinal's own foreign-clearance curve is NOT monotone
    (:func:`_family_foreign_clearance`'s own docstring — it dips to a
    minimum at ``a = half`` and rises on either side) finds a strictly
    better combined floor than the ring ever could — verified against the
    ring's own formula for the default via/hv numbers
    (``tests/test_pcb_ewod_generator_drc.py``).

    **The search.** For a candidate ``half``, :func:`_plaza_family` grid-
    searches ``(a, b)`` for the pair-floor-feasible combination that
    MAXIMISES :func:`_family_foreign_clearance` — the smallest ``half``
    at which that best-achievable clearance reaches :func:`_foreign_req`
    is ``min_half``, found by doubling-then-bisecting on ``half`` (both
    :func:`_family_foreign_clearance` terms are non-decreasing in
    ``half`` for FIXED ``a``/``b``, and the feasible ``(a, b)`` region
    itself never depends on ``half`` at all, so the best-achievable
    clearance is monotone non-decreasing in ``half`` — bisection is
    valid). The ``(a, b)`` found AT ``min_half`` is what
    :func:`resolve_ewod_sizing` uses for ANY actual (larger) ``half`` a
    real board ends up with — exactly how the superseded ring's own
    ``R`` was fixed independent of ``half`` too: a bigger pitch only ever
    buys MORE clearance for the same ``(a, b)``, never requires a
    different one. ``@functools.lru_cache``: this is a pure function of
    three floats with no history dependence (the module's own
    "Idempotency contract"), and the search itself is not free — most
    calls across one process share the same (default or house) via/gap/
    hv_separation, so the cache turns a repeat sizing call (every
    ``resolve_ewod_sizing`` invocation makes one) into a dict lookup.

    ``min_pitch = gap + 2*min_half`` is what :func:`resolve_ewod_sizing`
    actually validates ``pitch`` against — the same contract the ring
    version held, still true here (the ruling's own requirement)."""
    req = _foreign_req(via_dia, hv_separation)
    d = _pair_floor(via_dia, hv_separation)
    a0 = d / math.sqrt(2.0)
    b0 = d / 2.0
    lo_half = max(a0, b0)
    _, _, clearance = _plaza_family(gap, via_dia, hv_separation, lo_half)
    if clearance >= req:
        min_half = lo_half
    else:
        hi_half = max(lo_half, req)
        _, _, clearance = _plaza_family(gap, via_dia, hv_separation, hi_half)
        while clearance < req:
            hi_half *= 1.5
            _, _, clearance = _plaza_family(gap, via_dia, hv_separation, hi_half)
        # `lo_half` is confirmed infeasible above, `hi_half` feasible --
        # bisect down to the grid's own step resolution (finer serves no
        # purpose: `_plaza_family`'s own (a, b) choice is already only
        # that precise).
        for _ in range(48):
            mid = (lo_half + hi_half) / 2.0
            _, _, mid_clearance = _plaza_family(gap, via_dia, hv_separation, mid)
            if mid_clearance >= req:
                hi_half = mid
            else:
                lo_half = mid
        min_half = hi_half
    a, b, _ = _plaza_family(gap, via_dia, hv_separation, min_half)
    return {
        "slot_a": a,
        "slot_b": b,
        # Alias for any reader still keyed on the pre-ruling-8 ring name
        # (docs/backlog/pcb-ewod-multitile.md's own instruction) --
        # `precis.pcb.svg`'s capability-map view draws a single schematic
        # ring off this and is explicitly NOT the fab-accurate geometry
        # (its own docstring), so approximating every slot at radius `a`
        # there is an acceptable, unchanged simplification.
        "slot_radius": a,
        "min_half": min_half,
        "min_pitch": gap + 2.0 * min_half,
    }


# ── sizing / grid resolution ─────────────────────────────────────────────
def resolve_ewod_sizing(params: dict[str, Any]) -> dict[str, Any]:
    """Derive every ``ewod_pad_array`` sizing figure from ``params``,
    filling documented defaults for anything unset, and validate
    ``pitch`` against the plaza-capacity floor (:func:`_plaza_capacity`)
    — the "Plaza slot capacity must be computed, not assumed" open item.
    """
    cap = capability_for(_FAB_PROCESS)
    via = params.get("via") or {}
    # JLC-MIN, deliberately not `design_value`'s house_default tier: the
    # spec's own "3x3 JLC-min vias (0.45/0.2) at 0.63mm grid fit with real
    # margin" (decisions log) is written against the published floor, not
    # the 1.5x-margined design tier — a plaza via is small and isolated
    # (no annular-ring current/reliability case for margining it up the
    # way a structural via would be). `design_value`'s house_default ->
    # jlc_min -> fallback chain is right for a MARGINED design figure;
    # this one wants the floor itself, with the spec's own cited numbers
    # as the fallback when the capability row has nothing.
    via_dia = float(via.get("dia") or cap.jlc_min.get("via_diameter_mm") or 0.45)
    # round-3 fix: `via_drill` used to default to a bare 0.2mm literal,
    # decoupled from `via_dia` -- but `via_dia`'s own jlc_min figure is
    # ITSELF derived as `drill_mm + 2*annular_ring_mm` at this same tier
    # (capabilities.py's own module docstring; capability_for() already
    # applies that coupling, so `cap.jlc_min["via_diameter_mm"]` above is
    # 0.45mm at 4-layer, not the raw 0.25mm floor the JSON file stores).
    # Pairing that derived diameter with an UNRELATED 0.2mm drill gave a
    # ring of (0.45-0.2)/2 = 0.125mm -- under this capability's own
    # 0.15mm annular_ring_mm floor, on every default-sized plaza via, on
    # every EWOD board, forever. Caught by round 3's `check_annular_ring`
    # extension actually being ABLE to check a drilled footprint pad for
    # the first time (previously this check only ever saw router copper
    # vias, so a plaza via's own ring floor was never validated at all).
    # Defaulting `via_drill` from the SAME `drill_mm` figure that produced
    # `via_dia` reproduces the file's own coupling exactly: ring lands AT
    # the floor (0.15mm at 4-layer), not under it -- `check_annular_ring`
    # no longer errors, though it still WARNS (floor-sized, zero margin
    # against house_default): the deliberate, already-documented
    # consequence of choosing the jlc_min tier for via sizing at all
    # (this function's own `via_dia` comment above), not a new gap. An
    # author who wants a quiet board passes an explicit, larger `via`.
    via_drill = float(via.get("drill") or cap.jlc_min.get("drill_mm") or 0.2)
    gap = float(params.get("gap", _DEFAULT_GAP_MM))
    if gap <= 0:
        raise ValueError(f"ewod_pad_array: gap must be positive, got {gap}")

    drive_voltage_v = params.get("drive_voltage_v")
    hv_row: str | None = None
    if params.get("hv_separation") is not None:
        hv_separation = float(params["hv_separation"])
    elif drive_voltage_v is not None:
        # A DECLARED voltage is looked up against IPC-2221B Table 6-1's
        # B4 column (external conductor, permanently coated) --
        # "Rulings 2026-09-18/2026-09-19" items 3/10: plaza internals, the
        # B.Cu escape stubs/tracks and the sink fan all sit under
        # soldermask + parylene, and ANY actuation pattern may put two
        # electrodes at full differential (no reliance on sequential-
        # neighbour switching), so the FULL drive voltage -- not a
        # switched fraction of it -- is the working voltage this looks up.
        hv_row = "B4"
        hv_separation = conductor_spacing_mm(
            float(drive_voltage_v), layer="external", coated=True
        )
    else:
        # No voltage declared at all: fall back to the fab's own ordinary
        # copper-isolation floor (jlc_min trace_spacing_mm), not an
        # invented HV number — "derived, not invented" for the case where
        # there is genuinely nothing to derive an HV figure FROM yet.
        hv_separation = float(cap.jlc_min.get("trace_spacing_mm") or 0.09)

    pitch = float(params.get("pitch", _DEFAULT_PITCH_MM))
    capacity = _plaza_capacity(gap, via_dia, hv_separation)
    min_pitch = capacity["min_pitch"]
    if pitch < min_pitch - 1e-9:
        raise ValueError(
            f"ewod_pad_array: pitch={pitch}mm is under the derived plaza-"
            f"capacity floor {min_pitch:.3f}mm for gap={gap}mm, "
            f"via_dia={via_dia}mm, hv_separation={hv_separation}mm (see "
            "precis.pcb.generators._plaza_capacity) -- the plaza cannot "
            "fit its 8-slot escape family at this pitch; raise pitch, or "
            "override via/gap/hv_separation explicitly"
        )

    # JLC-min again (not design_value's house_default), same reasoning as
    # via_dia above: a plaza neck is a short, isolated, low-current
    # feature. The house_default TIER's own trace width is comfortably
    # ABOVE the default `gap` (round-2 stress-test finding: it always got
    # silently capped back down to `gap` at the via end anyway --
    # see the `stub_width = min(...)` clamp below -- turning "default"
    # into "default, plus a warning every single call"), so start from
    # the floor instead.
    _trace_spacing_floor = float(cap.jlc_min.get("trace_spacing_mm") or 0.09)
    _trace_width_floor = float(
        cap.jlc_min.get("trace_width_mm") or _DEFAULT_STUB_WIDTH_MM
    )
    stub_width_uncapped = float(params.get("stub_width", _trace_width_floor))
    # A diagonal escape's via slot sits only `gap*sqrt(2)` from the
    # diagonally-adjacent electrode's own corner (round-2 stress-test
    # finding). A neck wider than `gap` at that end clips the neighbour --
    # capped here (once; :func:`_expand_ewod_pad_array` no longer repeats
    # this clamp).
    stub_width = min(stub_width_uncapped, gap)
    # docs/backlog/pcb-ewod-multitile.md "Rulings 2026-09-18" item 1 --
    # there is NO corridor cap here any more (the corridor itself is now
    # sized FROM the fab minimum, below -- see `plaza_corner_chamfer`),
    # only the fab's own absolute floor: copper narrower than this is not
    # "extra clearance", it is trace JLC will not etch, regardless of why
    # an author asked for it (an explicit `stub_width` override below the
    # floor is raised back up here, not silently accepted then flagged
    # elsewhere by `check_trace_width`).
    stub_width = max(stub_width, _trace_width_floor)
    edge = params.get("edge") or {}
    tooth_depth = float(edge.get("tooth_depth", _DEFAULT_TOOTH_DEPTH_MM))
    tooth_pitch = float(edge.get("tooth_pitch", _DEFAULT_TOOTH_PITCH_MM))
    if tooth_pitch <= 0:
        raise ValueError("ewod_pad_array: edge.tooth_pitch must be positive")
    if tooth_pitch <= 2.0 * tooth_depth:
        raise ValueError(
            f"ewod_pad_array: edge.tooth_pitch={tooth_pitch}mm must exceed "
            f"2x tooth_depth ({2.0 * tooth_depth}mm) -- a transition (see "
            "precis.pcb.generators._s_curve) needs its own tooth_pitch-scale "
            "room to round smoothly, on top of the flat plateau either side"
        )
    if tooth_depth < gap / 2.0:
        raise ValueError(
            f"ewod_pad_array: edge.tooth_depth={tooth_depth}mm must be >= "
            f"gap/2 ({gap / 2.0}mm) -- the crenellated centreline's own "
            "transition corners are rounded with radius=tooth_depth "
            "(precis.pcb.generators._s_curve), and offsetting a curve by "
            "gap/2 on its concave side is only well-defined when the "
            "curve's own radius is at least that large; a shallower tooth "
            "would zero out the constant-gap channel at every transition"
        )
    corner_radius = float(params.get("corner_radius", 0.0))
    external_edge = str(params.get("external_edge") or "mesh")
    if external_edge not in ("mesh", "straight"):
        raise ValueError(
            f"ewod_pad_array: external_edge must be 'mesh' or 'straight', "
            f"got {external_edge!r}"
        )
    tenting = str(params.get("tenting") or "on")
    if tenting not in ("on", "off"):
        raise ValueError(
            f"ewod_pad_array: tenting must be 'on' or 'off', got {tenting!r}"
        )
    half = (pitch - gap) / 2.0

    # Plaza-corner chamfer -- docs/backlog/pcb-ewod-multitile.md "Rulings
    # 2026-09-18" item 1: RULE-DERIVED, not a fixed margin on top of
    # `gap` (round 4's superseded formula, see _stub_track_row's sibling
    # notes and this module's own history for the fixed-margin version
    # and why it was pinned as a KNOWN gap rather than fixed by widening
    # the margin -- that regressed the meshing wall, see `_edge_sign`'s
    # own `margin_t0`/`margin_t1` docstring for the re-solve this ruling
    # ALSO required).
    #
    # The corridor a diagonal escape's stub threads through is the
    # perpendicular gap between the two flanking (cardinal-escaping)
    # neighbours' own chamfered corners. For a fab-legal stub to fit,
    # that corridor needs to be at least `stub_width + 2 *
    # trace_spacing_floor` wide (the track's own width plus a fab
    # clearance on each side) -- the ruling fixes `stub_width` here to
    # the FAB MINIMUM specifically (not whatever an author may have
    # overridden `stub_width` to, above -- the chamfer is a board-wide
    # geometric feature computed once, sized for the standard case; a
    # WIDER author override just spends some of the resulting margin,
    # same as it always could), plus the usual coordinate-rounding slack.
    #
    # `plaza_corner_chamfer`'s relationship to that corridor target is
    # the SAME geometric identity round 4 derived (only the target
    # changed): retreating each flanking corner by `plaza_corner_chamfer`
    # along its own two flat/mesh walls grows its distance to the
    # escape's 45-degree centreline from the un-chamfered `gap/sqrt(2)`
    # up to `corridor_target/sqrt(2)` -- i.e. `corridor_target =
    # sqrt(2)*(gap + plaza_corner_chamfer) / sqrt(2)`... solved for the
    # chamfer: `plaza_corner_chamfer = sqrt(2)*corridor_target - gap`.
    _corridor_target = (
        _trace_width_floor + 2.0 * _trace_spacing_floor + _GEOMETRY_ROUNDING_SLACK_MM
    )
    plaza_corner_chamfer = math.sqrt(2.0) * _corridor_target - gap
    if plaza_corner_chamfer >= half:
        raise ValueError(
            f"ewod_pad_array: derived plaza_corner_chamfer={plaza_corner_chamfer:.3f}mm "
            f"(from gap={gap}mm) would exceed the electrode's own half-pitch "
            f"({half:.3f}mm) -- raise pitch or lower gap"
        )

    return {
        "pitch": pitch,
        "gap": gap,
        "via_dia": via_dia,
        "via_drill": via_drill,
        "hv_separation": hv_separation,
        "hv_row": hv_row,
        "stub_width": stub_width,
        "stub_width_uncapped": stub_width_uncapped,
        "tooth_depth": tooth_depth,
        "tooth_pitch": tooth_pitch,
        "slot_a": capacity["slot_a"],
        "slot_b": capacity["slot_b"],
        "slot_radius": capacity["slot_radius"],
        "corner_radius": corner_radius,
        "external_edge": external_edge,
        "tenting": tenting,
        "min_pitch": min_pitch,
        "half": half,
        "plaza_corner_chamfer": plaza_corner_chamfer,
    }


def _resolve_grid(params: dict[str, Any]) -> tuple[int, int]:
    grid = params.get("grid")
    if grid is not None:
        if len(grid) != 2:
            raise ValueError("ewod_pad_array: 'grid' needs exactly [rows, cols]")
        rows, cols = int(grid[0]), int(grid[1])
    else:
        pads = params.get("pads")
        if pads is None:
            raise ValueError(
                "ewod_pad_array: needs 'grid': [rows, cols] or 'pads': N "
                "(N must be a perfect square for a square field)"
            )
        n = int(pads)
        side = round(math.sqrt(n))
        if side * side != n:
            raise ValueError(
                f"ewod_pad_array: pads={n} is not a perfect square -- pass "
                "'grid': [rows, cols] explicitly for a non-square field"
            )
        rows = cols = side
    if rows < 1 or cols < 1:
        raise ValueError("ewod_pad_array: grid rows/cols must both be >= 1")
    return rows, cols


def _default_plaza(r: int, c: int) -> bool:
    """Auto plaza-position rule (spec: "at every third interior grid
    position"): the CENTRE cell of every 3x3 block, ``r % 3 == 1 and
    c % 3 == 1``. This tiles a 3x3-electrode "block + 1 plaza" pattern
    across a grid of ANY size (not just multiples of 3) — a grid whose
    last block is truncated (e.g. 8 rows -> blocks of 3,3,2) simply has
    some boundary electrodes with no plaza neighbour, which the escape
    scan below marks ``unusable`` rather than silently misplacing a via.
    This is the "grids not divisible into 3x3" open item's mechanical
    default: documented here, not resolved as a general rule."""
    return r % 3 == 1 and c % 3 == 1


# ── zigzag edge geometry ──────────────────────────────────────────────────
def _tooth_sign(t: float, tooth_pitch: float) -> int:
    """+1/-1 square wave of period ``2*tooth_pitch``, keyed on the
    ABSOLUTE board coordinate ``t`` (not a per-edge-local one). Using the
    same global function for every boundary in the array is what makes
    two neighbours' shared edge (which is queried at the SAME ``t`` by
    construction — see module docstring) automatically agree without any
    boundary-identity bookkeeping."""
    return 1 if math.floor(t / tooth_pitch) % 2 == 0 else -1


def _edge_sign(
    t: float,
    tooth_pitch: float,
    t0: float,
    t1: float,
    *,
    margin_t0: float = 0.0,
    margin_t1: float = 0.0,
) -> int:
    """:func:`_tooth_sign`, but forced to 0 (no deflection) within one
    tooth_pitch of either end of the edge ``[t0, t1]`` — or within
    ``margin_t0``/``margin_t1`` (whichever is larger), when the caller
    passes one.

    Without this, two edges meeting at a pad's corner each compute their
    OWN independent wave (one is a function of x, the perpendicular one a
    function of y) — they agree everywhere along a shared straight edge
    (by construction, both query the same absolute coordinate) but have
    no reason to agree AT the corner point itself, where they meet
    end-to-end rather than side-by-side. Clamping every wall to the
    pad's plain, non-deflected corner (this returns 0 there, giving the
    same nominal ``+-half`` corner every flat wall already uses) is what
    keeps the polygon a single closed, non-self-intersecting ring.

    **``margin_t0``/``margin_t1`` — docs/backlog/pcb-ewod-multitile.md
    "Rulings 2026-09-18" item 1's own wall re-solve.** A meshing wall
    whose ``[t0, t1]`` is already the CHAMFERED (shrunk) extent
    (:func:`_electrode_polygon`'s ``_chamfer_inset``) used to get its
    zero-deflection buffer from ``tooth_pitch`` alone, same as any other
    wall — fine while ``plaza_corner_chamfer`` was a small fixed margin,
    but once the chamfer is sized to fit a real fab-legal stub through
    the corridor it opens (``resolve_ewod_sizing``'s own derivation) it
    can exceed ``tooth_pitch`` several times over: with only
    ``tooth_pitch`` of flat run past the (now much farther-retreated)
    corner, the wall's first real ZIGZAG tooth pokes back out toward the
    escape corridor the chamfer just widened, partially undoing it — the
    exact, previously-unexplained mechanism behind the "widening the
    margin regressed the zigzag wall" finding this module's history
    records (the margin grew, the wall's own flat run never followed).
    ``_electrode_polygon`` passes the ACTUAL chamfer amount applied at
    each end (0 where that end wasn't chamfered at all, the old,
    unaffected behaviour) so the flat run always reaches at least as far
    as the chamfer itself does."""
    lo, hi = (t0, t1) if t0 <= t1 else (t1, t0)
    m_t0, m_t1 = max(tooth_pitch, margin_t0), max(tooth_pitch, margin_t1)
    m_lo, m_hi = (m_t0, m_t1) if t0 <= t1 else (m_t1, m_t0)
    half_len = (hi - lo) / 2.0
    m_lo = min(m_lo, half_len)
    m_hi = min(m_hi, half_len)
    if t <= lo + m_lo + 1e-9 or t >= hi - m_hi - 1e-9:
        return 0
    return _tooth_sign(t, tooth_pitch)


def _breakpoints(t0: float, t1: float, tooth_pitch: float) -> list[float]:
    """Every tooth_pitch-multiple crossed between ``t0`` and ``t1``,
    inclusive of both endpoints, ordered in the direction of travel
    (``t1`` may be less than ``t0``)."""
    lo, hi = (t0, t1) if t0 <= t1 else (t1, t0)
    k0 = math.floor(lo / tooth_pitch)
    k1 = math.ceil(hi / tooth_pitch)
    pts = sorted({round(k * tooth_pitch, 9) for k in range(k0, k1 + 1)})
    pts = [p for p in pts if lo - 1e-9 <= p <= hi + 1e-9]
    if not pts or pts[0] > lo + 1e-9:
        pts.insert(0, lo)
    if pts[-1] < hi - 1e-9:
        pts.append(hi)
    pts[0], pts[-1] = lo, hi
    if t1 < t0:
        pts.reverse()
    return pts


#: Sample points per arc of an S-curve transition (:func:`_s_curve`) —
#: coarse enough to be cheap, fine enough that a straight-line polygon
#: edge through the samples tracks the true arc within fab tolerance.
_ARC_SEGMENTS = 12


def _s_curve(
    t_c: float, d_prev: float, d_next: float, radius: float, n: int = _ARC_SEGMENTS
) -> list[tuple[float, float]]:
    """A "reverse curve" (two equal-``radius`` arcs, tangent to each
    other and to the flat deflection on either side) connecting flat
    deflection ``d_prev`` to flat deflection ``d_next``, symmetric about
    ``t_c`` — ``n`` sampled points per arc, INCLUDING both flat-tangent
    endpoints ``(t_c - horiz, d_prev)`` and ``(t_c + horiz, d_next)``
    (``horiz`` derived internally from ``radius``/the deflection change)
    — the caller does NOT add its own flat-segment point at the
    transition; this function owns the full transition.

    **Why a rounded transition at all** (not the sharp right-angle jog
    an earlier attempt used): a 90-degree corner has zero radius of
    curvature, and offsetting a curve by ``gap/2`` on the CONCAVE side
    of a zero-radius corner is undefined — shapely trims the resulting
    self-intersection, which collapses that side's boundary onto (or
    past) the corner, killing the ``gap`` separation from the convex
    side's own offset right there (round-2 stress-test finding: this
    reproduced with EVERY ``offset_curve`` join style, because the
    defect is in the INPUT curve's sharpness, not the join). Rounding
    the shared centreline's own corners with radius ``R >= gap/2``
    (validated in :func:`resolve_ewod_sizing`) keeps BOTH sides'
    offsets non-self-intersecting, so the perpendicular distance
    between them stays exactly ``gap`` through the transition too.

    Requires ``abs(d_next - d_prev) <= 2*radius`` (the two arcs, each
    contributing half the deflection change, cannot together span more
    than ``2*radius`` of vertical travel) — always true here since the
    biggest change asked of this function is a full tooth flip,
    ``2*tooth_depth``, and the radius used is ``tooth_depth`` itself
    (see the module's callers)."""
    change = d_next - d_prev
    if abs(change) < 1e-12:
        return []
    half_d = abs(change) / 2.0
    cosphi = max(-1.0, min(1.0, 1.0 - half_d / radius))
    phi = math.acos(cosphi)
    horiz = radius * math.sin(phi)
    half_pi = math.pi / 2.0
    if change > 0:  # curving upward
        c1 = (t_c - horiz, d_prev + radius)
        c2 = (t_c + horiz, d_next - radius)
        a1_start, a1_end = -half_pi, -half_pi + phi
        a2_start, a2_end = half_pi + phi, half_pi
    else:  # curving downward
        c1 = (t_c - horiz, d_prev - radius)
        c2 = (t_c + horiz, d_next + radius)
        a1_start, a1_end = half_pi, half_pi - phi
        a2_start, a2_end = 3.0 * half_pi - phi, 3.0 * half_pi
    pts: list[tuple[float, float]] = []
    for k in range(0, n + 1):
        a = a1_start + (a1_end - a1_start) * k / n
        pts.append((c1[0] + radius * math.cos(a), c1[1] + radius * math.sin(a)))
    for k in range(1, n + 1):
        a = a2_start + (a2_end - a2_start) * k / n
        pts.append((c2[0] + radius * math.cos(a), c2[1] + radius * math.sin(a)))
    return pts


def _centerline_points(
    t0: float,
    t1: float,
    *,
    tooth_pitch: float,
    depth: float,
    margin_t0: float = 0.0,
    margin_t1: float = 0.0,
) -> list[tuple[float, float]]:
    """The SHARED crenellated centreline — ``(t, deflection)`` pairs, a
    right-angle-square-wave SHAPE (flat plateaus at ``+-depth``) but
    with each transition rounded into a reverse-curve S (:func:`_s_curve`,
    radius = ``depth``) rather than an instant jog — deflection only (no
    gap offset, no midline — those are the caller's job).

    This is a template curve, not a pad boundary: :func:`_meshing_wall`
    derives EACH side's actual boundary from it via a proper
    perpendicular polyline offset (shapely ``offset_curve``).
    ``margin_t0``/``margin_t1`` pass straight through to
    :func:`_edge_sign` (its own docstring has the ruling-1 re-solve this
    exists for)."""
    pts = _breakpoints(t0, t1, tooth_pitch)
    n_intervals = len(pts) - 1
    # One flat deflection level per interval [pts[i], pts[i+1]].
    levels = [
        depth
        * _edge_sign(
            (pts[i] + pts[i + 1]) / 2.0,
            tooth_pitch,
            t0,
            t1,
            margin_t0=margin_t0,
            margin_t1=margin_t1,
        )
        for i in range(n_intervals)
    ]

    # `_s_curve` is defined low-t-to-high-t (``d_prev`` at the lower t);
    # `pts` may run either direction (``_breakpoints`` reverses to match
    # the caller's own t0/t1 order) -- swap prev/next and reverse the
    # curve's own points when walking high-to-low, or the S-curve comes
    # back POINT-REFLECTED rather than simply retraced (round-2
    # stress-test finding: a west/south wall, which walks high-to-low,
    # got a mirror-image transition and the two neighbours' curves
    # stopped tracking each other through it).
    ascending = pts[0] <= pts[-1]
    out: list[tuple[float, float]] = [(pts[0], levels[0])]
    for i in range(n_intervals):
        boundary_t = pts[i + 1]
        if i + 1 < n_intervals and abs(levels[i + 1] - levels[i]) > 1e-9:
            if ascending:
                out += _s_curve(boundary_t, levels[i], levels[i + 1], radius=depth)
            else:
                out += list(
                    reversed(
                        _s_curve(boundary_t, levels[i + 1], levels[i], radius=depth)
                    )
                )
        else:
            out.append((boundary_t, levels[i]))
    return out


def _meshing_wall(
    t0: float,
    t1: float,
    *,
    tooth_pitch: float,
    depth: float,
    mid_axis: float,
    gap: float,
    side: float,
    axis: str,
    margin_t0: float = 0.0,
    margin_t1: float = 0.0,
) -> list[Point]:
    """One pad's boundary along a meshing wall: the shared, ROUNDED
    crenellated centreline (:func:`_centerline_points`, already smooth —
    radius=``depth`` at every transition — at ``x`` or ``y`` =
    ``mid_axis + deflection`` depending on ``axis``) offset perpendicular
    by ``side * gap / 2`` via shapely's ``offset_curve`` (round join —
    matters only for the offset's OWN corner smoothness now, since the
    input centreline no longer has any zero-radius corner for either
    side's offset to self-intersect against — see
    :func:`_centerline_points`/:func:`_s_curve` for why a SHARP
    crenellation cannot support this at all). ``axis='x'``: the wall
    varies in y with deflection in x (a vertical wall); ``axis='y'``:
    varies in x with deflection in y (a horizontal wall). ``side`` is
    whichever sign reproduces this pad's own known flat-corner value at
    zero deflection — empirically, all four walls (W/N/E/S) turn out to
    want ``side=1.0`` given how each one's own ``t0``/``t1``/``mid_axis``
    are set up in :func:`_electrode_polygon`, rather than a symbolic
    +/-1 derived from shapely's left/right convention. ``margin_t0``/
    ``margin_t1`` — the amount ``t0``/``t1`` was itself already retreated
    by a plaza-corner chamfer, or 0 — pass straight through to
    :func:`_centerline_points`/:func:`_edge_sign` (ruling-1 wall
    re-solve, see the latter's own docstring)."""
    centerline = _centerline_points(
        t0,
        t1,
        tooth_pitch=tooth_pitch,
        depth=depth,
        margin_t0=margin_t0,
        margin_t1=margin_t1,
    )
    if axis == "x":
        xy = [(mid_axis + d, t) for t, d in centerline]
    else:
        xy = [(t, mid_axis + d) for t, d in centerline]
    line = LineString(xy)
    offset = line.offset_curve(side * gap / 2.0, join_style="round")
    # Rare but possible even on a smooth input (e.g. a very short flat
    # plateau relative to gap): concatenate every piece's coords in
    # order rather than assuming a single LineString comes back.
    parts = list(offset.geoms) if offset.geom_type == "MultiLineString" else [offset]
    out: list[Point] = []
    for part in parts:
        for x, y in part.coords:
            p = (float(x), float(y))
            if not out or (
                abs(out[-1][0] - p[0]) > 1e-9 or abs(out[-1][1] - p[1]) > 1e-9
            ):
                out.append(p)
    return out


# ── pad-field construction ───────────────────────────────────────────────
@dataclass
class _Layout:
    rows: int
    cols: int
    variant: str
    sizing: dict[str, Any]
    plaza_set: set[tuple[int, int]]

    def cx(self, c: float) -> float:
        # `float`, not `int` -- a pad_sizes-merged span's own centroid
        # (round 6) is a fractional column/row (e.g. the midpoint of a
        # 2-cell span), not just a single grid index.
        return (c - (self.cols - 1) / 2.0) * self.sizing["pitch"]

    def cy(self, r: float) -> float:
        return (r - (self.rows - 1) / 2.0) * self.sizing["pitch"]

    def cell_kind(self, r: int, c: int) -> str:
        if not (0 <= r < self.rows and 0 <= c < self.cols):
            return "outside"
        if self.variant == "rim":
            on_rim = r in (0, self.rows - 1) or c in (0, self.cols - 1)
            return "electrode" if on_rim else "hollow"
        return "plaza" if (r, c) in self.plaza_set else "electrode"


#: The 8 directions a plaza slot maps to, plus the centre spare -- fixed
#: iteration order so the ledger and slot-id strings are stable.
_DIRECTIONS: tuple[tuple[str, int, int], ...] = (
    ("N", -1, 0),
    ("S", 1, 0),
    ("E", 0, 1),
    ("W", 0, -1),
    ("NE", -1, 1),
    ("NW", -1, -1),
    ("SE", 1, 1),
    ("SW", 1, -1),
)

#: Reverse of :data:`_DIRECTIONS` -- ``(dr, dc) -> name`` -- so
#: :func:`_rim_via_point` (round-4 rewrite) can name the direction from a
#: rim pad to its own virtual one-cell plaza and hand it straight to
#: :func:`_plaza_slot_point`'s already-proven ring math, the same as any
#: real plaza consumer.
_DIR_BY_DELTA: dict[tuple[int, int], str] = {
    (dr, dc): name for name, dr, dc in _DIRECTIONS
}


def _needs_plaza_corner_chamfer(kind_a: str, kind_b: str) -> bool:
    """A corner needs chamfering (round-4 fix, see
    ``resolve_ewod_sizing``'s own ``plaza_corner_chamfer`` derivation)
    exactly where one
    adjoining wall is ``flat`` (facing a plaza or a rim's hollow interior
    — no interlocking partner there) and the OTHER is ``mesh`` (an
    internal electrode neighbour, or an external boundary meshing for
    tiling): that mesh wall's far side is where a DIFFERENT electrode's
    own diagonal escape threads past this exact corner. A flat-flat
    corner (shouldn't occur given the 3x3 auto-plaza spacing, but is
    harmless if it ever does) or a flat-``straight`` corner (an external
    edge with no interlocking partner either) needs no chamfer -- nothing
    protrudes past either of those to be threatened by."""
    return {kind_a, kind_b} == {"flat", "mesh"}


def _needs_diagonal_margin_widen(kind_a: str, kind_b: str, kind_diag: str) -> bool:
    """The MIRROR side of :func:`_needs_plaza_corner_chamfer` — this
    electrode's OWN corner isn't chamfered (both of ITS adjoining walls
    are ``mesh``, nothing on its own side to cut), but the cell DIAGONAL
    to it at this exact corner (``kind_diag``) is ``flat`` — a plaza or
    hollow interior sitting one cell further out, diagonally. That means
    ONE of the two cardinal cells between this electrode and that
    diagonal plaza (whichever actually borders it) has ITS OWN flat wall
    there and IS chamfering this same physical corner point (see
    :func:`_electrode_polygon`'s own docstring for why this electrode's
    wall margin must widen to match, even though its shape does not)."""
    return kind_a == "mesh" and kind_b == "mesh" and kind_diag == "flat"


def _chamfer_inset(
    t0: float, t1: float, *, at_start: bool, at_end: bool, amount: float
) -> tuple[float, float]:
    """Shrink a wall's own ``[t0, t1]`` domain (which may run either
    direction) by ``amount`` at whichever end(s) need a plaza-corner
    chamfer -- always INWARD (toward the wall's own interior, never
    outward), so this can only ever gain clearance against a foreign
    corner, never newly violate some other already-tuned one."""
    if amount <= 0.0 or (not at_start and not at_end):
        return t0, t1
    direction = 1.0 if t1 >= t0 else -1.0
    new_t0 = t0 + direction * amount if at_start else t0
    new_t1 = t1 - direction * amount if at_end else t1
    return new_t0, new_t1


def _electrode_polygon(
    layout: _Layout, r0: int, c0: int, r1: int | None = None, c1: int | None = None
) -> tuple[list[Point], int]:
    """One electrode's polygon, and the number of its own 4 OUTER
    corners that got a plaza-corner chamfer (0-2 for the auto 3x3 rule's
    usual single-flat-wall case, up to 4 for a hand-authored ``plazas``
    layout with a plaza on more than one side — the ledger's own
    ``chamfer_loss_mm2`` is ``count * plaza_corner_chamfer**2 / 2``, see
    below).

    A single grid cell (``r0==r1, c0==c1``,
    the only shape before round 6) OR a ``pad_sizes``-merged rectangular
    SPAN of ``r1-r0+1`` x ``c1-c0+1`` cells (round 6): 4 walls (W, N, E,
    S), each either crenellated against a same-status neighbour (another
    electrode, or — for ``external_edge='mesh'`` — the array's own outer
    boundary, which gets the SAME formula so a tiled neighbour array
    meshes against it) or flat (facing a plaza/hollow interior, where
    there is no interlocking partner and the neck stub — emitted
    separately — does the real work).

    **Merged spans (round 6).** A span's own 4 OUTER walls may run past
    MULTIPLE unit cells (e.g. a 1x3 merge's north wall spans 3 columns) —
    each unit along a wall can face a DIFFERENT neighbour kind (part of
    the wall might mesh against an ordinary electrode while another part
    faces a plaza), so each wall is built by walking its own unit range
    in the SAME south-to-north / west-to-east / etc. order the single-cell
    case already used, computing each unit's own crenellated-or-flat
    segment with the EXACT single-cell logic (mid_axis/flat position keyed
    off the span's own OUTER row/column, not the per-unit one, since the
    whole wall sits on ONE straight line), and simply concatenating the
    per-unit point lists in order. This needs no extra "filler" code: a
    unit's own segment already starts/ends at zero deflection (the
    single-cell corner-flattening ``_meshing_wall``/``_edge_sign`` machinery
    already provides, unchanged) unless it is chamfered, so two adjacent
    units' concatenated segments automatically connect with a plain
    straight edge — which is exactly the merged pad's own solid copper
    filling what would otherwise be the ``gap``-wide seam between two
    separate pads at that same latitude/longitude. Chamfering (below) is
    the only place a span's own OUTER 4 corners get special treatment;
    internal unit-to-unit transitions along the SAME wall never chamfer —
    there is no diagonal escape geometry threading past an internal seam,
    only past the span's own true corners, the same as a single pad's.

    **Plaza-corner chamfer (round 4, generalised round 6, RULE-DERIVED
    since docs/backlog/pcb-ewod-multitile.md "Rulings 2026-09-18" item
    1).** Where a FLAT wall (facing a plaza) meets a MESH wall (facing an
    electrode neighbour) at one of the span's own 4 OUTER corners, that
    corner is exactly the point a DIFFERENT electrode's own diagonal
    escape stub has to thread past on its way to the same plaza — and
    un-chamfered, that corner sits at perpendicular distance ``gap/
    sqrt(2)`` from the escape's centreline, independent of how the stub
    itself is shaped (below the fab's own absolute copper-spacing floor
    at any sizing that needs a real track through there — see
    ``resolve_ewod_sizing``'s own ``plaza_corner_chamfer`` derivation for
    why no taper redesign alone can fix it, and for the corridor-width
    target the current chamfer is solved from). Both walls meeting such a
    corner retreat INWARD along their own axis by ``plaza_corner_chamfer``
    — this only ever removes copper (can't newly violate anything else)
    and reopens the corridor those two walls' corner and its mirror twin
    bound to. The area each such corner loses is reported per-electrode
    in the ledger as ``chamfer_loss_mm2`` (``chamfer**2 / 2`` per
    chamfered corner — a right isoceles triangle)."""
    if r1 is None:
        r1 = r0
    if c1 is None:
        c1 = c0
    s = layout.sizing
    pitch, gap, depth, tp = s["pitch"], s["gap"], s["tooth_depth"], s["tooth_pitch"]
    half = s["half"]
    chamfer = float(s.get("plaza_corner_chamfer", 0.0))
    external = s["external_edge"]

    def classify(kind: str) -> str:
        if kind == "electrode":
            return "mesh"
        if kind == "outside":
            return str(external)
        return "flat"  # plaza or hollow interior

    def kind_w(r: int) -> str:
        return classify(layout.cell_kind(r, c0 - 1))

    def kind_e(r: int) -> str:
        return classify(layout.cell_kind(r, c1 + 1))

    def kind_n(c: int) -> str:
        return classify(layout.cell_kind(r0 - 1, c))

    def kind_s(c: int) -> str:
        return classify(layout.cell_kind(r1 + 1, c))

    # Corner naming matches the single-cell case exactly (span=1 reduces
    # to it verbatim): <wall-A>-end meets <wall-B>-start in point-list
    # order (W -> N -> E -> S -> back to W), evaluated at the span's own
    # OUTER corner unit on each side.
    chamfer_wn = _needs_plaza_corner_chamfer(kind_w(r0), kind_n(c0))
    chamfer_ne = _needs_plaza_corner_chamfer(kind_n(c1), kind_e(r0))
    chamfer_es = _needs_plaza_corner_chamfer(kind_e(r1), kind_s(c1))
    chamfer_sw = _needs_plaza_corner_chamfer(kind_s(c0), kind_w(r1))

    # docs/backlog/pcb-ewod-multitile.md "Rulings 2026-09-18" item 1's own
    # wall re-solve, PART TWO (the mirror side of a chamfered corner).
    # `chamfer_*` above is TRUE only for the electrode that itself owns a
    # flat wall at that corner (e.g. a plaza's cardinal N/S/E/W neighbour)
    # -- but a corner electrode DIAGONAL to the plaza (e.g. the array's
    # own R0C0-style corner, whose own two adjoining walls are BOTH mesh)
    # shares that EXACT physical corner point with its cardinal neighbour,
    # who DOES chamfer there. Both sides of one shared mesh wall query the
    # SAME absolute coordinate (`_tooth_sign`'s own docstring), so if only
    # ONE side widens its zero-deflection margin, the OTHER side's zigzag
    # can still put a real tooth right where its neighbour has already
    # retreated -- this is the exact, previously mis-attributed "widening
    # the margin regressed the zigzag wall" finding (two electrode
    # BODIES, no stub, down to 0.04mm): the fixed-margin version's
    # neighbour-side never widened AT ALL, so growing only one side's
    # margin made the mismatch bigger, not the margin itself the problem.
    #
    # This has to be a PER-UNIT check, not just a span-outer-corner one
    # (round 6's merged spans generalise it): a merged span's own mesh
    # wall can run past SEVERAL of a NEIGHBOUR's own cells (or, for a
    # 2+-cell merge, the mirror-worthy neighbour might sit below/beside an
    # INTERNAL unit of the span, not either of its two true outer
    # corners), so each unit checks its OWN two local ends independently
    # of whether it happens to be the span's first/last unit --
    # ``_needs_diagonal_margin_widen`` reduces to "the neighbour ACROSS
    # this wall has a plaza on the far side of this exact position", using
    # this wall's own row/column for the (always-mesh) cardinal side of
    # that check (see the function's own docstring for the full 4-cell
    # corner identity this collapses from).
    def wall_widen_fns(
        fixed_kind_fn: Callable[[int], str],
        neighbour_at: Callable[[int, int], str],
        delta_t0: int,
        delta_t1: int,
    ) -> tuple[Callable[[int], bool], Callable[[int], bool]]:
        """``neighbour_at(unit, delta)`` names the wall's foreign
        neighbour cell's own kind one step further along the wall's own
        axis (``delta`` = -1/+1) from ``unit`` -- e.g. for a south wall,
        ``neighbour_at(c, -1)`` is ``classify(layout.cell_kind(r1 + 1, c
        - 1))``. ``delta_t0``/``delta_t1`` are the EXPLICIT deltas that
        correspond to each wall's own ``t0``/``t1`` (which one is
        "toward larger coordinate" vs "smaller" differs per wall's own
        ``get_extent`` -- passed explicitly at each call site rather than
        inferred, to keep this one small function correct for all 4
        orientations rather than four hand-derived copies)."""

        def widen_t0(unit: int) -> bool:
            return _needs_diagonal_margin_widen(
                fixed_kind_fn(unit), "mesh", neighbour_at(unit, delta_t0)
            )

        def widen_t1(unit: int) -> bool:
            return _needs_diagonal_margin_widen(
                fixed_kind_fn(unit), "mesh", neighbour_at(unit, delta_t1)
            )

        return widen_t0, widen_t1

    def wall_run(
        units: list[int],
        get_extent: Callable[[int], tuple[float, float]],
        kind_fn: Callable[[int], str],
        *,
        mid_axis: float,
        flat_axis: float,
        axis: str,
        chamfer_start: bool,
        chamfer_end: bool,
        widen_t0: Callable[[int], bool],
        widen_t1: Callable[[int], bool],
    ) -> list[Point]:
        out: list[Point] = []
        n = len(units)
        for i, unit in enumerate(units):
            t0, t1 = get_extent(unit)
            at_start = i == 0 and chamfer_start
            at_end = i == n - 1 and chamfer_end
            t0c, t1c = _chamfer_inset(
                t0, t1, at_start=at_start, at_end=at_end, amount=chamfer
            )
            if kind_fn(unit) == "mesh":
                # A self-chamfered end's own zero-deflection zone is
                # measured from its ALREADY-RETREATED endpoint, so 1x
                # `chamfer` reaches `nominal - 2*chamfer` absolute --
                # the point a MIRROR (widen-only, no self-chamfer) end
                # must ALSO reach, but measured from its OWN un-retreated
                # nominal endpoint instead, hence 2x there.
                m_start = (
                    chamfer if at_start else (2.0 * chamfer if widen_t0(unit) else 0.0)
                )
                m_end = (
                    chamfer if at_end else (2.0 * chamfer if widen_t1(unit) else 0.0)
                )
                out += _meshing_wall(
                    t0c,
                    t1c,
                    tooth_pitch=tp,
                    depth=depth,
                    mid_axis=mid_axis,
                    gap=gap,
                    side=1.0,
                    axis=axis,
                    margin_t0=m_start,
                    margin_t1=m_end,
                )
            elif axis == "x":
                out += [(flat_axis, t0c), (flat_axis, t1c)]
            else:
                out += [(t0c, flat_axis), (t1c, flat_axis)]
        return out

    pts: list[Point] = []
    # West wall: rows r1 -> r0 (south to north), each row's own y-extent
    # (cy+half) down to (cy-half) -- start is the SW corner, end the WN
    # corner, matching the single-cell case's own point order. t0 = south
    # (larger y), t1 = north (smaller y); the west neighbour at row r is
    # (r, c0-1), so "widen at t0/south" checks that neighbour's own SOUTH
    # side (r+1, c0-1), "widen at t1/north" its NORTH side (r-1, c0-1).
    w_widen_t0, w_widen_t1 = wall_widen_fns(
        kind_w, lambda r, d: classify(layout.cell_kind(r + d, c0 - 1)), 1, -1
    )
    pts += wall_run(
        list(range(r1, r0 - 1, -1)),
        lambda r: (layout.cy(r) + half, layout.cy(r) - half),
        kind_w,
        mid_axis=layout.cx(c0) - pitch / 2.0,
        flat_axis=layout.cx(c0) - half,
        axis="x",
        chamfer_start=chamfer_sw,
        chamfer_end=chamfer_wn,
        widen_t0=w_widen_t0,
        widen_t1=w_widen_t1,
    )
    # North wall: cols c0 -> c1 (west to east). t0 = west, t1 = east; the
    # north neighbour at col c is (r0-1, c) -- "widen at t0/west" checks
    # its WEST side (r0-1, c-1), "widen at t1/east" its EAST side
    # (r0-1, c+1).
    n_widen_t0, n_widen_t1 = wall_widen_fns(
        kind_n, lambda c, d: classify(layout.cell_kind(r0 - 1, c + d)), -1, 1
    )
    pts += wall_run(
        list(range(c0, c1 + 1)),
        lambda c: (layout.cx(c) - half, layout.cx(c) + half),
        kind_n,
        mid_axis=layout.cy(r0) - pitch / 2.0,
        flat_axis=layout.cy(r0) - half,
        axis="y",
        chamfer_start=chamfer_wn,
        chamfer_end=chamfer_ne,
        widen_t0=n_widen_t0,
        widen_t1=n_widen_t1,
    )
    # East wall: rows r0 -> r1 (north to south). t0 = north, t1 = south;
    # the east neighbour at row r is (r, c1+1) -- "widen at t0/north"
    # checks its NORTH side (r-1, c1+1), "widen at t1/south" its SOUTH
    # side (r+1, c1+1).
    e_widen_t0, e_widen_t1 = wall_widen_fns(
        kind_e, lambda r, d: classify(layout.cell_kind(r + d, c1 + 1)), -1, 1
    )
    pts += wall_run(
        list(range(r0, r1 + 1)),
        lambda r: (layout.cy(r) - half, layout.cy(r) + half),
        kind_e,
        mid_axis=layout.cx(c1) + pitch / 2.0,
        flat_axis=layout.cx(c1) + half,
        axis="x",
        chamfer_start=chamfer_ne,
        chamfer_end=chamfer_es,
        widen_t0=e_widen_t0,
        widen_t1=e_widen_t1,
    )
    # South wall: cols c1 -> c0 (east to west). t0 = east, t1 = west; the
    # south neighbour at col c is (r1+1, c) -- "widen at t0/east" checks
    # its EAST side (r1+1, c+1), "widen at t1/west" its WEST side
    # (r1+1, c-1).
    s_widen_t0, s_widen_t1 = wall_widen_fns(
        kind_s, lambda c, d: classify(layout.cell_kind(r1 + 1, c + d)), 1, -1
    )
    pts += wall_run(
        list(range(c1, c0 - 1, -1)),
        lambda c: (layout.cx(c) + half, layout.cx(c) - half),
        kind_s,
        mid_axis=layout.cy(r1) + pitch / 2.0,
        flat_axis=layout.cy(r1) + half,
        axis="y",
        chamfer_start=chamfer_es,
        chamfer_end=chamfer_sw,
        widen_t0=s_widen_t0,
        widen_t1=s_widen_t1,
    )

    # De-dup consecutive identical points (a straight wall's own start
    # coincides with the previous wall's own end -- true whether or not
    # either side was chamfered, since a chamfered corner simply leaves
    # the two walls' own endpoints DIFFERENT, connected by the ordinary
    # straight edge between two consecutive list entries).
    out: list[Point] = []
    for p in pts:
        if not out or (abs(out[-1][0] - p[0]) > 1e-9 or abs(out[-1][1] - p[1]) > 1e-9):
            out.append(p)
    if (
        len(out) > 1
        and abs(out[0][0] - out[-1][0]) < 1e-9
        and abs(out[0][1] - out[-1][1]) < 1e-9
    ):
        out.pop()
    chamfer_count = sum([chamfer_wn, chamfer_ne, chamfer_es, chamfer_sw])
    return out, chamfer_count


#: The rule-envelope keys :meth:`precis.store._pcb_ops.PcbMixin.
#: _pcb_fixed_copper_envelope_mismatch` reads off a copper row's own
#: ``envelope`` dict -- built once per :func:`_expand_ewod_pad_array` call
#: (the fabric is a pure function of the SAME capability floor
#: :func:`resolve_ewod_sizing` already reads, never a per-row recompute)
#: and stamped onto every emitted row, so an apply against a board whose
#: rules have since moved refuses honestly instead of keeping fabric that
#: may no longer be legal (docs/backlog/pcb-pre-place-route-blocks.md,
#: "Rule envelope is a hard gate").
def _fabric_envelope(cap: CapabilityRow) -> dict[str, Any]:
    return {
        "layers": len(DEFAULT_STACKUP),
        "min_clearance_mm": cap.jlc_min.get("trace_spacing_mm"),
        "min_track_mm": cap.jlc_min.get("trace_width_mm"),
        "via_drill_mm": cap.jlc_min.get("drill_mm"),
        "via_diameter_mm": cap.jlc_min.get("via_diameter_mm"),
    }


def _stub_track_row(
    net_name: str, anchor: Point, via_pt: Point, width: float, envelope: dict[str, Any]
) -> dict[str, Any]:
    """One driven electrode's F.Cu neck as REAL copper
    (docs/backlog/pcb-pre-place-route-blocks.md Slice 2) — a straight,
    CONSTANT-width track from the electrode body's own boundary anchor to
    its plaza via centre, on ``net_name``. This REPLACES the TAPERED
    footprint-pad neck (:func:`_stub_polygon`, pre-Slice-2) the module
    docstring's earlier rounds describe: the taper existed only to clear a
    diagonal escape's own pinch point against a NEIGHBOUR electrode's flat
    corner, and :func:`_electrode_polygon`'s ``plaza_corner_chamfer``
    (round 4) already does that clearance job on the ELECTRODE side —
    retreating the neighbour's own corner rather than narrowing this
    track — so a constant-width track needs no taper of its own to stay
    clear. ``geom`` matches ``pcb_copper.geom``'s own ``ctype='track'``
    shape exactly (:class:`GeneratorExpansion.copper`'s docstring)."""
    (ax, ay), (vx, vy) = anchor, via_pt
    return {
        "ctype": "track",
        "layer": "F.Cu",
        "net": net_name,
        "geom": {
            "segments": [{"shape": "line", "start": [ax, ay], "end": [vx, vy]}],
            "width_mm": width,
        },
        "envelope": envelope,
    }


def _via_row(
    net_name: str, via_pt: Point, sizing: dict[str, Any], envelope: dict[str, Any]
) -> dict[str, Any]:
    """One driven electrode's plaza via as REAL copper, on ``net_name`` —
    replaces the drilled-THT-pad via the module docstring's earlier
    rounds describe (docs/backlog/pcb-pre-place-route-blocks.md Slice 2
    reverses that round-3 decision; see the module docstring's own
    updated notice). ``span`` carries the via's real layer membership
    (F.Cu to B.Cu, this generator's escape is B.Cu-only per the module
    docstring's scope note); the top-level ``layer`` is a schema-
    satisfying placeholder only, the same convention ``pcb_copper`` itself
    uses (:class:`GeneratorExpansion.copper`'s own docstring)."""
    vx, vy = via_pt
    return {
        "ctype": "via",
        "layer": "F.Cu",
        "net": net_name,
        "geom": {
            "x": vx,
            "y": vy,
            "dia_mm": sizing["via_dia"],
            "drill_mm": sizing["via_drill"],
            "span": ["F.Cu", "B.Cu"],
        },
        "envelope": envelope,
    }


#: ``(dr, dc)`` unit-ish delta for each of :data:`_DIRECTIONS`' names, so
#: :func:`_breakout_far_point` (Rulings 2026-09-19 item 11) can turn a
#: slot's own direction name into a real (x, y) unit vector without
#: re-deriving the lookup :func:`_plaza_slot_point`/:func:`_edge_anchor`
#: each already build locally.
_DELTA_BY_DIR: dict[str, tuple[int, int]] = {
    name: (dr, dc) for name, dr, dc in _DIRECTIONS
}


def _direction_unit(direction: str) -> Point:
    """The unit (x, y) vector pointing in ``direction`` (a
    :data:`_DIRECTIONS` name). A cardinal's ``(dr, dc)`` is already unit
    length; a diagonal's has magnitude ``sqrt(2)`` (both components
    +-1), so it is normalised here rather than at each call site."""
    dr, dc = _DELTA_BY_DIR[direction]
    n = math.hypot(dc, dr)
    return (dc / n, dr / n)


def _breakout_far_point(via_pt: Point, direction: str, length: float) -> Point:
    """The far end of a plaza via's B.Cu breakout stub (docs/backlog/
    pcb-ewod-multitile.md "Rulings 2026-09-19" item 11, "radial B.Cu
    breakout stubs as fixed copper") -- ``length`` (the plaza's own
    ``slot_a``) outward from the via, along the SLOT's own direction: the
    unit vector from the plaza centre to the slot itself (the axis for a
    cardinal slot, the diagonal for a diagonal one) -- continuing past
    the via in the same direction it already sits from the plaza centre,
    not back toward the centre. A cardinal via sits at ``slot_a`` from
    centre, so its far end lands at ``slot_a + length`` (``2*slot_a`` at
    the default ``length=slot_a``); a diagonal via sits at ``slot_b`` on
    each axis, so its far end lands ``length/sqrt(2)`` further out on
    each axis -- the "~1.7mm ring, exits ~1.3mm apart" figures in the
    ruling's own two-levers paragraph are this identity evaluated at the
    default via/hv numbers, not a separately-tuned radius."""
    ux, uy = _direction_unit(direction)
    vx, vy = via_pt
    return (vx + ux * length, vy + uy * length)


def _breakout_track_row(
    net_name: str, via_pt: Point, far_pt: Point, width: float, envelope: dict[str, Any]
) -> dict[str, Any]:
    """The B.Cu breakout stub itself — same row shape as
    :func:`_stub_track_row` but fixed to ``B.Cu`` and running from the
    plaza via's own centre outward to :func:`_breakout_far_point`'s far
    end, on the same ``net_name`` the via/F.Cu neck already carry
    (docs/backlog/pcb-ewod-multitile.md "Rulings 2026-09-19" item 11).
    This is pre-solved fixed copper on the SAME "the fabric owns its
    pre-routed copper" contract the plaza vias/necks already use
    (pcb-pre-place-route-blocks Slice 2's own module-docstring section):
    the router's island terminals (:func:`precis.pcb.connectivity.
    fixed_copper_pin_terminals`) pick up the far end as a real landing
    point past the plaza's own crowded interior, closing gr347037's
    congestion race without any router change of its own — the far end
    unions onto the via's own B.Cu terminal by ordinary touching-copper
    connectivity (this track's OWN start point IS the via centre), the
    same mechanism that already offers a via's terminal on every layer
    it spans."""
    (vx, vy), (fx, fy) = via_pt, far_pt
    return {
        "ctype": "track",
        "layer": "B.Cu",
        "net": net_name,
        "geom": {
            "segments": [{"shape": "line", "start": [vx, vy], "end": [fx, fy]}],
            "width_mm": width,
        },
        "envelope": envelope,
    }


def _rim_virtual_plaza_cell(layout: _Layout, r: int, c: int) -> tuple[int, int]:
    """The hollow grid cell ``(r, c)``'s own rim via reaches toward --
    ``(r, c)`` itself when neither axis has an open direction (a
    degenerate 1-row/1-col layout has no interior to reach at all). Split
    out of :func:`_rim_via_point` (pcb-pre-place-route-blocks Slice 2) so
    the fabric ledger can key a tile by this cell the same way a real
    plaza is keyed by its own ``(row, col)`` -- two or three rim pads that
    share the SAME hollow cell (a rim's own 4 corners, see
    :func:`_rim_via_point`'s docstring) must land in ONE ledger tile, not
    one each."""
    dx = 1 if c == 0 else (-1 if c == layout.cols - 1 else 0)
    dy = 1 if r == 0 else (-1 if r == layout.rows - 1 else 0)
    return (r + dy, c + dx)


def _rim_via_point(layout: _Layout, r: int, c: int) -> Point:
    """Every rim pad reaches into the hollow interior toward its own
    VIRTUAL one-cell plaza -- the hollow grid cell one ``pitch`` away in
    its own open direction(s) (cardinal for a straight-edge pad, diagonal
    for a corner pad, which has no purely-cardinal hollow neighbour at
    all: both its cardinal neighbours are RIM pads, round-2 stress-test
    finding) -- and lands on :func:`_plaza_slot_point`'s own ring around
    that virtual plaza's centre, exactly the identity a REAL plaza's own
    consumers use.

    **Why the SAME ring, not a shorter ad hoc reach (round-4 rewrite).**
    At a rim's own 4 corners, the corner pad's virtual plaza and its TWO
    cardinal neighbours' virtual plazas are literally the SAME hollow
    cell (the one hollow cell diagonally/cardinally adjacent to all
    three) -- a genuine 3-consumer plaza in every way that matters,
    just short of the 8 a full-grid plaza can carry. Two earlier
    attempts got this wrong in complementary ways: a bare cardinal
    "half + reach" straight-line via (this function's own original
    version) placed each of the three consumers independently, with
    nothing guaranteeing they'd clear EACH OTHER; a diagonal reach
    measured from the corner pad's own centre (this function's round-3
    version, meant to fix a different, unrelated defect) undershot so
    badly the via landed back INSIDE the corner pad's own polygon --
    exactly the via-in-pad situation this whole generator exists to
    avoid, and short enough that it accidentally never got close enough
    to the cardinal vias to reveal THIS defect either (round-4
    stress-test finding, in that order). Routing every rim pad's via
    through the identical ring construction :func:`_plaza_capacity`
    already proves mutually clearance-safe for up to 8 simultaneous
    consumers closes both gaps at once: a corner's 3-consumer cell is
    just an under-subscribed real plaza, not a special case."""
    cx, cy = layout.cx(c), layout.cy(r)
    vr, vc = _rim_virtual_plaza_cell(layout, r, c)
    dy, dx = vr - r, vc - c
    if dx == 0 and dy == 0:
        return (cx, cy)
    direction = _DIR_BY_DELTA[(dy, dx)]
    return _plaza_slot_point(layout, vr, vc, direction)


#: Opposite of each direction key -- ``_expand_ewod_pad_array`` finds an
#: electrode's plaza by the direction FROM the electrode TO the plaza
#: (scanning its own 8 neighbours), but the slot inside that plaza must
#: sit on the side FACING that electrode, i.e. the opposite direction
#: from the plaza's own centre. Kept as one small lookup rather than
#: re-deriving ``(-dr, -dc)`` at each call site.
_OPPOSITE: dict[str, str] = {
    "N": "S",
    "S": "N",
    "E": "W",
    "W": "E",
    "NE": "SW",
    "SW": "NE",
    "NW": "SE",
    "SE": "NW",
}


def _plaza_slot_point(layout: _Layout, pr: int, pc: int, direction: str) -> Point:
    """The via-slot centre inside plaza ``(pr, pc)`` serving the
    electrode that lies in ``direction`` FROM the electrode's own
    perspective (i.e. this is the plaza-to-electrode direction the slot
    physically sits toward -- see :data:`_OPPOSITE`).

    docs/backlog/pcb-ewod-multitile.md "Rulings 2026-09-19" item 8 —
    the family is axis-aligned, not a uniform ring: a CARDINAL direction
    (exactly one of ``dr``/``dc`` non-zero, already unit length) sits at
    distance ``slot_a`` from the plaza centre; a DIAGONAL direction
    (``dr``, ``dc`` both +-1) sits at ``(+-slot_b, +-slot_b)`` -- since
    ``dr``/``dc`` are themselves +-1 there, multiplying by ``slot_b``
    directly (no normalising) already gives exactly that point, unlike
    the superseded uniform ring which normalised BOTH cases onto the
    same-radius circle (:func:`_plaza_capacity`'s own docstring has the
    two-parameter derivation)."""
    dr, dc = {d: (dr, dc) for d, dr, dc in _DIRECTIONS}[_OPPOSITE[direction]]
    r = layout.sizing["slot_a"] if (dr == 0 or dc == 0) else layout.sizing["slot_b"]
    return (layout.cx(pc) + dc * r, layout.cy(pr) + dr * r)


def _edge_anchor(layout: _Layout, r: int, c: int, direction: str) -> Point:
    """The point on electrode ``(r, c)``'s own nominal boundary nearest
    its plaza in ``direction`` -- an edge midpoint for a cardinal
    direction, the pad's own corner for a diagonal one."""
    half = layout.sizing["half"]
    cx, cy = layout.cx(c), layout.cy(r)
    dr, dc = {d: (dr, dc) for d, dr, dc in _DIRECTIONS}[direction]
    return (cx + dc * half, cy + dr * half)


@dataclass
class _Merge:
    """One ``pad_sizes`` entry, resolved: ``pin`` is the merged electrode's
    own pin/net name, ``cells`` the full solid-rectangle cell set (sorted
    row-major), ``span`` = ``(r0, c0, r1, c1)`` inclusive."""

    pin: str
    cells: list[tuple[int, int]]
    span: tuple[int, int, int, int]


def _parse_pad_sizes(
    params: dict[str, Any], layout: _Layout
) -> dict[tuple[int, int], _Merge]:
    """Validate and resolve the ``pad_sizes`` param (docs/backlog/
    pcb-ewod-multitile.md Slice 2: "a pad may span m x n grid cells --
    merged outline, zigzag preserved along its boundary, one net"), into a
    ``(row, col) -> _Merge`` lookup covering every cell any entry claims.

    Each entry is ``{"cells": [[r, c], ...], "name": <optional str>}`` --
    an explicit cell LIST rather than an anchor+span pair, so the caller
    can spell a merge out directly (matching the shape
    ``test_pcb_ewod_generator_geometry.py`` already pinned before this
    round's merging was built). The cells must form a SOLID rectangle (no
    holes, no L-shapes) -- ``_electrode_polygon``'s own wall-walking logic
    assumes one, and an m x n merge is what the spec actually asks for.

    **Hard rule enforced here, not left to the caller to get right (spec
    decision, 2026-09-13): a merged pad may NEVER cover a via plaza (or,
    for ``variant='rim'``, a hollow interior cell) -- the plaza carries 8
    OTHER nets' stubs and vias; copper over it shorts them all.** Checking
    every claimed cell's own ``layout.cell_kind() == "electrode"`` catches
    this generically (a plaza cell reports ``"plaza"``, a rim hollow cell
    reports ``"hollow"``) without needing a plaza-specific special case."""
    entries = params.get("pad_sizes") or []
    claimed: dict[tuple[int, int], str] = {}
    merges: dict[tuple[int, int], _Merge] = {}
    for i, entry in enumerate(entries):
        raw_cells = entry.get("cells") or []
        if len(raw_cells) < 2:
            raise ValueError(
                f"ewod_pad_array: pad_sizes[{i}] needs at least 2 'cells' "
                "(a single-cell entry is just an ordinary pad)"
            )
        cells = sorted({(int(rc[0]), int(rc[1])) for rc in raw_cells})
        r0 = min(r for r, _c in cells)
        r1 = max(r for r, _c in cells)
        c0 = min(c for _r, c in cells)
        c1 = max(c for _r, c in cells)
        expected = {(r, c) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)}
        if set(cells) != expected:
            raise ValueError(
                f"ewod_pad_array: pad_sizes[{i}] cells must form a solid "
                f"rectangle -- got {cells}, which does not fill the "
                f"{r1 - r0 + 1}x{c1 - c0 + 1} block it spans "
                f"({sorted(expected)})"
            )
        for r, c in cells:
            kind = layout.cell_kind(r, c)
            if kind != "electrode":
                raise ValueError(
                    f"ewod_pad_array: pad_sizes[{i}] cell ({r},{c}) is "
                    f"{kind!r}, not a pad position -- a merged pad may "
                    "never cover a via plaza or hollow interior cell "
                    "(would short the plaza's other 8 nets, or there is "
                    "no pad there at all)"
                )
            prior = claimed.get((r, c))
            if prior is not None:
                raise ValueError(
                    f"ewod_pad_array: cell ({r},{c}) is claimed by more "
                    f"than one pad_sizes entry ({prior!r} and entry {i})"
                )
        pin = str(entry.get("name") or f"R{r0}C{c0}")
        merge = _Merge(pin=pin, cells=cells, span=(r0, c0, r1, c1))
        for r, c in cells:
            claimed[(r, c)] = pin
            merges[(r, c)] = merge
    return merges


@dataclass
class _SinkGrid:
    """Resolved ``sink_grid`` param (round 7, docs/backlog/
    pcb-ewod-multitile.md's "sink_grid emission — part-agnostic" decision;
    channel assignment rebalanced by the 2026-09-18 ruling, "9x9 sink
    packing → balanced by chain order"): a set of bottom-side sink
    component instances, one per ``channels_per_sink`` USABLE electrode
    escapes assigned in serpentine CHAIN order (see the main loop's own
    ``chain_order`` comment), chained DIN->DOUT in that same chain-index
    order, and (optionally) tied into a shared top-plate/complement-drive
    rail net.

    **Deliberately part-agnostic.** :func:`expand` is pure -- no DB reads
    -- so it cannot look up an arbitrary LCSC part's (or local footprint's)
    REAL pin names the way a cached ``part_footprints``/
    ``pcb_local_footprints`` row would answer at IR-build time; the caller
    names them explicitly (``channel_pins``/``serial_in_pin``/
    ``serial_out_pin``/``top_plate_pin``/``power``). ``_pcb_pin_id``
    (:mod:`precis.store._pcb_ops`) creates a referenced pin ad hoc if it
    isn't already registered, so this module never needs to see the real
    footprint at all -- it just has to name pins consistently with
    whatever the real part/footprint calls them.

    **Channel assignment is balanced, not spatial**: ``sink_count =
    ceil(n_driven / channels_per_sink)``, then ``n_driven`` electrodes (in
    chain order) split into ``sink_count`` shares of ``n_driven //
    sink_count`` each, the FIRST ``n_driven % sink_count`` shares taking
    one extra -- never a greedy fill-then-spill (72 driven electrodes at
    ``channels_per_sink=64`` is 2 sinks of 36, not one at 64 and one at 8).
    ``channels_per_sink`` defaults to the whole part (``len(channel_pins)``)
    and is refused above that -- a sink cannot serve more channels than the
    part has pins for. The former square-block ``per_tiles`` param is
    REMOVED (forward-only; :func:`_parse_sink_grid` refuses it by name) --
    a spatial block count cannot map onto a channel count."""

    part: str | None
    footprint: str | None
    footprint_label: str
    channels_per_sink: int
    channel_pins: list[str]
    serial_in_pin: str
    serial_out_pin: str
    top_plate_pin: str | None
    top_plate_net: str
    power: dict[str, str]


def _parse_sink_grid(params: dict[str, Any], name: str) -> _SinkGrid | None:
    """Validate and resolve the ``sink_grid`` param, or ``None`` if unset.
    See :class:`_SinkGrid`'s own docstring for the design this implements."""
    cfg = params.get("sink_grid")
    if cfg is None:
        return None
    if "per_tiles" in cfg:
        raise ValueError(
            "ewod_pad_array: sink_grid.per_tiles was replaced by "
            "channels_per_sink (balanced by chain order, ruling "
            "2026-09-18) -- a spatial block count cannot map onto a "
            "channel count, so there is no silent alias; set "
            "sink_grid.channels_per_sink instead (default = "
            "len(channel_pins))"
        )
    part = cfg.get("part")
    footprint = cfg.get("footprint")
    if bool(part) == bool(footprint):
        raise ValueError(
            "ewod_pad_array: sink_grid needs EXACTLY one of 'part' (an LCSC "
            "C-number) or 'footprint' (a local footprint name authored via "
            "this same put()'s own 'footprints' block) -- the generator "
            "wires connections to the sink, it never authors the sink's "
            "own footprint geometry"
        )
    channel_pins = [str(p) for p in (cfg.get("channel_pins") or [])]
    if not channel_pins:
        raise ValueError(
            "ewod_pad_array: sink_grid.channel_pins must name at least one "
            "pin (the part-agnostic design has no other way to know what "
            "this part calls its channel pins)"
        )
    channels_per_sink = int(cfg.get("channels_per_sink") or len(channel_pins))
    if channels_per_sink < 1:
        raise ValueError("ewod_pad_array: sink_grid.channels_per_sink must be >= 1")
    if channels_per_sink > len(channel_pins):
        raise ValueError(
            f"ewod_pad_array: sink_grid.channels_per_sink ({channels_per_sink}) "
            f"exceeds channel_pins' own length ({len(channel_pins)}) -- a "
            "sink cannot serve more channels than the part has pins for"
        )
    top_plate_pin = cfg.get("top_plate_pin")
    return _SinkGrid(
        part=str(part) if part else None,
        footprint=str(footprint) if footprint else None,
        footprint_label=str(cfg.get("label") or footprint or f"sink:{part}"),
        channels_per_sink=channels_per_sink,
        channel_pins=channel_pins,
        serial_in_pin=str(cfg.get("serial_in_pin") or "DIN"),
        serial_out_pin=str(cfg.get("serial_out_pin") or "DOUT"),
        top_plate_pin=str(top_plate_pin) if top_plate_pin else None,
        top_plate_net=str(cfg.get("top_plate_net") or f"{name}_top_plate"),
        power={str(k): str(v) for k, v in dict(cfg.get("power") or {}).items()},
    )


def _find_plaza_escape(
    layout: _Layout, cells: list[tuple[int, int]]
) -> tuple[str, int, int, tuple[int, int]] | None:
    """The first (cell, direction, plaza) triple, scanning ``cells`` in
    order and each cell's own :data:`_DIRECTIONS` in their fixed order --
    "one via suffices regardless of size" (spec decision: the electrode is
    a capacitor, current is negligible), so a merged span with several
    candidate plaza-adjacent cells only ever claims the FIRST one, exactly
    generalising the single-cell case's own single candidate. Returns
    ``(direction, plaza_row, plaza_col, anchor_cell)`` or ``None`` if no
    cell in the span neighbours a plaza at all."""
    for r, c in cells:
        for d, dr, dc in _DIRECTIONS:
            if layout.cell_kind(r + dr, c + dc) == "plaza":
                return d, r + dr, c + dc, (r, c)
    return None


def _expand_ewod_pad_array(name: str, params: dict[str, Any]) -> GeneratorExpansion:
    rows, cols = _resolve_grid(params)
    variant = str(params.get("variant") or "full")
    if variant not in ("full", "rim"):
        raise ValueError(
            f"ewod_pad_array: variant must be 'full' or 'rim', got {variant!r}"
        )
    sizing = resolve_ewod_sizing(params)
    sink_cfg = _parse_sink_grid(params, name)
    if sink_cfg is not None and variant != "full":
        raise ValueError(
            "ewod_pad_array: sink_grid is not supported with variant='rim' "
            "yet -- a rim's hollow interior has no tracked plaza dict for a "
            "tile block to key off (round-7 scope; see docs/backlog/"
            "pcb-ewod-multitile.md)"
        )

    plazas_param = params.get("plazas")
    if variant == "rim":
        plaza_set: set[tuple[int, int]] = set()
    elif plazas_param is not None:
        plaza_set = {(int(p[0]), int(p[1])) for p in plazas_param}
    else:
        plaza_set = {
            (r, c) for r in range(rows) for c in range(cols) if _default_plaza(r, c)
        }

    layout = _Layout(
        rows=rows, cols=cols, variant=variant, sizing=sizing, plaza_set=plaza_set
    )
    merges = _parse_pad_sizes(params, layout)
    handled_merges: set[str] = set()

    reserve = {str(s).strip() for s in (params.get("reserve") or [])}
    unknown_reserve = set(reserve)

    pads: list[dict[str, Any]] = []
    pin_positions: dict[str, Point] = {}
    ledger_pads: dict[str, dict[str, Any]] = {}
    ledger_plazas: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    # `sink_grid`'s own chain roster, built up in the SAME row-major
    # electrode-escape scan below rather than a second pass --
    # [(anchor_row, anchor_col, pin), ...] in the exact order electrodes
    # are visited (row-major); reordered into serpentine chain order
    # further down, once every electrode's usability is known. Only ever
    # populated for USABLE escapes (an unusable pad has no via, so binding
    # a sink channel to it would wire a pad-to-pad net with no copper
    # between them).
    chain_pins: list[tuple[int, int, str]] = []
    x_anchor = float(params.get("x", 0.0))
    y_anchor = float(params.get("y", 0.0))

    # A diagonal escape's via slot sits only `gap*sqrt(2)` from the
    # diagonally-adjacent electrode's own corner (round-2 stress-test
    # finding: two abutting electrodes' corners approach that closely by
    # construction, half+half+gap=pitch). A neck wider than `gap` clips
    # the neighbour along its WHOLE length now (Slice 2's constant-width
    # track has no taper to thin out near the corner) -- `resolve_ewod_
    # sizing` already clamped `sizing["stub_width"]` to `gap` for this
    # reason; only the warning (needing the PRE-clamp value for its own
    # message) is this function's job.
    if sizing["stub_width"] < sizing["stub_width_uncapped"] - 1e-9:
        warnings.append(
            f"stub_width {sizing['stub_width_uncapped']}mm capped to gap "
            f"{sizing['gap']}mm along the whole constant-width track -- wider "
            "would clip a diagonally-adjacent electrode's corner (only "
            "gap*sqrt(2) away there, see _stub_track_row)"
        )

    # docs/backlog/pcb-pre-place-route-blocks.md Slice 2 -- the escape
    # fabric (neck track + plaza via, per driven electrode) is emitted as
    # REAL copper here, not footprint pads (see _stub_track_row/_via_row).
    # `fabric_envelope` is the rule floor every row below is stamped with
    # (once, not per-row -- it's a pure function of the SAME capability
    # `resolve_ewod_sizing` already read). `fabric_tiles` mirrors
    # `ledger_plazas`'s own per-plaza keying (a rim pad's virtual plaza,
    # `_rim_virtual_plaza_cell`, gets the SAME "rim:R{r}C{c}" tile key its
    # 2-3 co-located consumers share); `fabric_reasons` is a flat list so
    # a reader never has to walk every tile hunting for the few that
    # refused/suppressed something. B.Cu fan-out to the sink is NOT
    # emitted this slice (module docstring's scope note) -- the router
    # picks the via's B.Cu landing up as pre-existing copper instead.
    cap = capability_for(_FAB_PROCESS)
    fabric_envelope = _fabric_envelope(cap)
    copper: list[dict[str, Any]] = []
    fabric_tiles: dict[str, dict[str, int]] = {}
    fabric_totals = {"emitted": 0, "refused": 0, "suppressed": 0}
    fabric_reasons: list[dict[str, Any]] = []

    def _fabric_tile(tile_key: str) -> dict[str, int]:
        return fabric_tiles.setdefault(
            tile_key, {"emitted": 0, "refused": 0, "suppressed": 0}
        )

    def pin_name(r: int, c: int) -> str:
        return f"R{r}C{c}"

    # -- electrodes + their escapes -----------------------------------
    for r in range(rows):
        for c in range(cols):
            kind = layout.cell_kind(r, c)
            if kind != "electrode":
                continue
            merge = merges.get((r, c))
            if merge is not None:
                if merge.pin in handled_merges:
                    continue  # already emitted this merge's ONE pad
                handled_merges.add(merge.pin)
                pin = merge.pin
                r0, c0, r1, c1 = merge.span
                cells = merge.cells
            else:
                pin = pin_name(r, c)
                r0, c0, r1, c1 = r, c, r, c
                cells = [(r, c)]
            poly, chamfer_count = _electrode_polygon(layout, r0, c0, r1, c1)
            pads.append(
                {
                    "pin": pin,
                    "shape": "polygon",
                    "poly": poly,
                    "role": "electrode",
                    "mask": "covered",
                }
            )
            # Centroid of the span (== (cx(c), cy(r)) for an unmerged
            # single cell -- cx/cy are affine in their own row/col arg).
            pin_positions[pin] = (
                layout.cx((c0 + c1) / 2.0),
                layout.cy((r0 + r1) / 2.0),
            )
            ledger_pads[pin] = {"row": r0, "col": c0, "usable": True}
            if len(cells) > 1:
                ledger_pads[pin]["span"] = [r1 - r0 + 1, c1 - c0 + 1]
                ledger_pads[pin]["cells"] = [[cr, cc] for cr, cc in cells]
            if chamfer_count:
                # docs/backlog/pcb-ewod-multitile.md "Rulings 2026-09-18"
                # item 1: each plaza-corner chamfer removes a right
                # isoceles triangle of copper (legs = plaza_corner_chamfer)
                # from this electrode's own body -- reported so the
                # corridor-widening trade this ruling makes is visible per
                # electrode, not just in the aggregate sizing figure.
                chamfer_mm = float(sizing.get("plaza_corner_chamfer", 0.0))
                ledger_pads[pin]["chamfer_loss_mm2"] = (
                    chamfer_count * chamfer_mm * chamfer_mm / 2.0
                )

            if variant == "rim":
                # A merged span's via/anchor is placed off its FIRST cell
                # (sorted row-major, i.e. its own top-left corner) -- "one
                # via suffices" (spec decision), and rim's hollow interior
                # has no discrete slot budget the choice of cell could
                # collide against.
                via_r, via_c = cells[0]
                via_pt = _rim_via_point(layout, via_r, via_c)
                vr, vc = _rim_virtual_plaza_cell(layout, via_r, via_c)
                # Anchor the stub on that cell's own boundary, not its
                # centre -- project the via direction back onto the
                # nominal half-boundary.
                dx, dy = via_pt[0] - layout.cx(via_c), via_pt[1] - layout.cy(via_r)
                dn = math.hypot(dx, dy) or 1.0
                half = sizing["half"]
                anchor = (
                    layout.cx(via_c) + dx / dn * half,
                    layout.cy(via_r) + dy / dn * half,
                )
                net_name = f"{name}_{pin}"
                copper.append(
                    _stub_track_row(
                        net_name, anchor, via_pt, sizing["stub_width"], fabric_envelope
                    )
                )
                copper.append(_via_row(net_name, via_pt, sizing, fabric_envelope))
                ledger_pads[pin]["via"] = {"x": via_pt[0], "y": via_pt[1]}
                # Rulings 2026-09-19 item 11 -- the same B.Cu breakout stub
                # a real plaza's slots get below, keyed off the SAME
                # direction `_rim_via_point` itself placed this via along
                # (electrode -> virtual-plaza, opposite of the slot's own
                # outward direction). The degenerate 1x1-layout case (no
                # open direction at all, `_rim_virtual_plaza_cell`'s own
                # docstring) has no direction to break out along; skipped,
                # not a real board shape.
                rvy, rvx = vr - via_r, vc - via_c
                if (rvy, rvx) != (0, 0):
                    rim_slot_dir = _OPPOSITE[_DIR_BY_DELTA[(rvy, rvx)]]
                    far_pt = _breakout_far_point(via_pt, rim_slot_dir, sizing["slot_a"])
                    copper.append(
                        _breakout_track_row(
                            net_name,
                            via_pt,
                            far_pt,
                            sizing["stub_width"],
                            fabric_envelope,
                        )
                    )
                    ledger_pads[pin]["breakout"] = {
                        "x": far_pt[0],
                        "y": far_pt[1],
                        "layer": "B.Cu",
                    }
                rim_tile_key = f"rim:R{vr}C{vc}"
                _fabric_tile(rim_tile_key)["emitted"] += 1
                fabric_totals["emitted"] += 1
                continue

            # full variant: find this electrode's plaza neighbour --
            # exactly one candidate for an unmerged cell (at most one for
            # the auto/3x3 rule -- see _default_plaza's docstring for the
            # boundary cells that legitimately have none); for a merged
            # span, the FIRST candidate across all its cells
            # (_find_plaza_escape's own docstring: "one via suffices").
            escape = _find_plaza_escape(layout, cells)
            if escape is None:
                reason = "no adjacent plaza (array boundary)"
                ledger_pads[pin]["usable"] = False
                ledger_pads[pin]["reason"] = reason
                warnings.append(
                    f"{pin}: no adjacent via plaza -- marked unusable "
                    "(array-boundary effect of the 3x3 auto rule)"
                )
                # No plaza tile exists to attribute this to (that is
                # exactly the problem) -- board-total only, `tile: None`
                # in the reason list, same honesty the per-tile counts
                # give a genuine tile (docs/backlog/
                # pcb-pre-place-route-blocks.md Slice 2, acceptance
                # criterion "truncated edge tiles report honestly").
                fabric_totals["refused"] += 1
                fabric_reasons.append({"pin": pin, "tile": None, "reason": reason})
                continue
            d, pr, pc, (er, ec) = escape
            # Ledger/slot-id naming uses the PLAZA's own frame (the
            # direction the slot physically sits toward, i.e. toward
            # this electrode) -- `d` itself is the opposite, the
            # electrode's own direction to the plaza (see _OPPOSITE's
            # docstring).
            slot_dir = _OPPOSITE[d]
            slot_id = f"P{pr}_{pc}:{slot_dir}"
            slot_key = f"P{pr}_{pc}"
            plaza_ledger = ledger_plazas.setdefault(
                slot_key, {"row": pr, "col": pc, "slots": {}}
            )
            if slot_id in reserve:
                unknown_reserve.discard(slot_id)
                plaza_ledger["slots"][slot_dir] = {"status": "reserved"}
                ledger_pads[pin]["usable"] = False
                ledger_pads[pin]["reason"] = f"slot {slot_id} reserved"
                # 'reserve' suppresses this slot's via/stub -- counted,
                # not just silently skipped (docs/backlog/
                # pcb-pre-place-route-blocks.md Slice 2 item 2/3).
                _fabric_tile(slot_key)["suppressed"] += 1
                fabric_totals["suppressed"] += 1
                fabric_reasons.append(
                    {"pin": pin, "tile": slot_key, "reason": f"slot {slot_id} reserved"}
                )
                warnings.append(
                    f"{pin}: escape slot {slot_id} withheld by 'reserve' -- "
                    "marked unusable"
                )
                continue
            via_pt = _plaza_slot_point(layout, pr, pc, d)
            # Anchor off the SPECIFIC cell that actually touches this
            # plaza (`(er, ec)`, not the merge's arbitrary anchor `(r0,
            # c0)`) -- that cell's own edge/corner facing the plaza is
            # what sits on the merged polygon's real boundary there.
            anchor = _edge_anchor(layout, er, ec, d)
            net_name = f"{name}_{pin}"
            copper.append(
                _stub_track_row(
                    net_name, anchor, via_pt, sizing["stub_width"], fabric_envelope
                )
            )
            copper.append(_via_row(net_name, via_pt, sizing, fabric_envelope))
            # Rulings 2026-09-19 item 11 -- a B.Cu breakout stub, outward
            # from the via along the slot's OWN direction (`slot_dir`,
            # already the plaza-centre-to-slot direction this via itself
            # sits on -- see _plaza_slot_point's own docstring), length
            # `slot_a` (gr347037's "pre-solved breakout": the router's
            # island terminals then start past the plaza's crowded
            # interior instead of inside it).
            far_pt = _breakout_far_point(via_pt, slot_dir, sizing["slot_a"])
            copper.append(
                _breakout_track_row(
                    net_name, via_pt, far_pt, sizing["stub_width"], fabric_envelope
                )
            )
            ledger_pads[pin]["breakout"] = {
                "x": far_pt[0],
                "y": far_pt[1],
                "layer": "B.Cu",
            }
            plaza_ledger["slots"][slot_dir] = {"status": "used", "pin": pin}
            ledger_pads[pin]["via"] = {"x": via_pt[0], "y": via_pt[1]}
            ledger_pads[pin]["plaza"] = slot_key
            _fabric_tile(slot_key)["emitted"] += 1
            fabric_totals["emitted"] += 1
            if sink_cfg is not None:
                chain_pins.append((r0, c0, pin))

    # -- centre spare slots (every plaza gets one, whether or not any of
    # its 8 directional slots ended up used) --------------------------
    for pr, pc in sorted(plaza_set):
        slot_key = f"P{pr}_{pc}"
        plaza_ledger = ledger_plazas.setdefault(
            slot_key, {"row": pr, "col": pc, "slots": {}}
        )
        centre_id = f"{slot_key}:C"
        if centre_id in reserve:
            unknown_reserve.discard(centre_id)
            plaza_ledger["slots"]["C"] = {"status": "reserved"}
        else:
            plaza_ledger["slots"].setdefault("C", {"status": "free"})

    for leftover in unknown_reserve:
        warnings.append(
            f"reserve: {leftover!r} does not name a real plaza slot -- ignored"
        )

    # -- sink grid: bottom-side switch/connector instances, BALANCED BY
    # CHAIN ORDER (docs/backlog/pcb-ewod-multitile.md "Rulings 2026-09-18"
    # item 2 -- replaces the old square-block `per_tiles` binning, which
    # could land a 9x9 field's 72 driven electrodes as a lopsided 64+8
    # across two 64-channel sinks instead of a balanced 36+36). Emitted
    # here (rather than folded into the main loop above) because the
    # daisy chain and the equal-share split both need every driven
    # electrode's identity decided FIRST.
    #
    # `chain_order` is the serpentine (boustrophedon) reordering of
    # `chain_pins`: grouped by each electrode's own anchor row `r0` (a
    # merged span's top row), each row's own already-ascending-`c0` items
    # are used as discovered on even rows and REVERSED on odd rows -- so
    # consecutive chain positions land on spatially adjacent electrodes
    # across a row's own end, not a jump back to column 0. `sink_count =
    # ceil(n_driven / channels_per_sink)`; the `n_driven` chain positions
    # then split into `sink_count` shares of `n_driven // sink_count`
    # each, the FIRST `n_driven % sink_count` shares taking one extra --
    # the standard as-equal-as-possible partition, never a greedy
    # fill-then-spill.
    chain_order: list[tuple[int, int, str]] = []
    if sink_cfg is not None:
        by_row: dict[int, list[tuple[int, int, str]]] = {}
        for r0, c0, pin in chain_pins:
            by_row.setdefault(r0, []).append((r0, c0, pin))
        for row_r0 in sorted(by_row):
            row_items = by_row[row_r0]
            chain_order.extend(reversed(row_items) if row_r0 % 2 else row_items)

    sink_components: list[dict[str, Any]] = []
    sink_connections: list[dict[str, Any]] = []
    ledger_sinks: dict[str, Any] = {}
    if sink_cfg is not None:
        n_driven = len(chain_order)
        sink_count = math.ceil(n_driven / sink_cfg.channels_per_sink) if n_driven else 0
        base, extra = divmod(n_driven, sink_count) if sink_count else (0, 0)
        prev_out_net: str | None = None
        cursor = 0
        for i in range(sink_count):
            share_n = base + (1 if i < extra else 0)
            share = chain_order[cursor : cursor + share_n]
            cursor += share_n
            sink_refdes = f"{name}_SINK_{i}"
            channel_map: dict[str, str] = {}
            pin_decls: list[dict[str, Any]] = []
            for j, (_r0, _c0, elec_pin) in enumerate(share):
                ch_pin = sink_cfg.channel_pins[j]
                channel_map[ch_pin] = elec_pin
                pin_decls.append({"name": ch_pin})
                sink_connections.append(
                    {"net": f"{name}_{elec_pin}", "refdes": sink_refdes, "pin": ch_pin}
                )
            pin_decls.append({"name": sink_cfg.serial_in_pin})
            pin_decls.append({"name": sink_cfg.serial_out_pin})
            if sink_cfg.top_plate_pin:
                pin_decls.append({"name": sink_cfg.top_plate_pin})
                sink_connections.append(
                    {
                        "net": sink_cfg.top_plate_net,
                        "refdes": sink_refdes,
                        "pin": sink_cfg.top_plate_pin,
                    }
                )
            for pn, net_name in sink_cfg.power.items():
                pin_decls.append({"name": pn})
                sink_connections.append(
                    {"net": net_name, "refdes": sink_refdes, "pin": pn}
                )

            mean_r0 = sum(r0 for r0, _c0, _pin in share) / len(share)
            mean_c0 = sum(c0 for _r0, c0, _pin in share) / len(share)
            comp: dict[str, Any] = {
                "refdes": sink_refdes,
                "label": f"{name} sink {i}",
                "footprint": sink_cfg.footprint_label,
                "x": layout.cx(mean_c0),
                "y": layout.cy(mean_r0),
                "rot": 0.0,
                "layer": "bottom",
                # A sink's whole reason to exist is sitting directly under
                # ITS OWN share of electrodes (the escape-locality
                # argument the spec's own "regular grid of HV switches
                # directly under the array" language makes) -- letting
                # the placer move it would defeat that, same as the
                # array's own `fixed='both'` above.
                "fixed": "both",
                "pins": pin_decls,
                "roles": ["ewod_sink"],
                # docs/backlog/pcb-ewod-multitile.md "Rulings 2026-09-19"
                # item 6: any electrode this sink drives may sit on any of
                # its OWN channel pins (the chain order is firmware's
                # problem, not this generator's) -- ONE admissible set of
                # every channel pin this instance actually wired (not
                # `sink_cfg.channel_pins` verbatim: a last, undersized
                # share declares fewer pins than the part has, and a pin
                # this instance never declared has no IR pin id to swap
                # at all), no exclusions. `pcb_route` resolves this into a
                # real `pinswap.PinSwapGroup` with footprint pad offsets;
                # this generator only ever states the admissible-set
                # judgment, never the geometry.
                "pin_swap_groups": [list(channel_map)],
            }
            if sink_cfg.part:
                comp["part"] = sink_cfg.part
            if sink_cfg.footprint:
                comp["footprint"] = sink_cfg.footprint
            sink_components.append(comp)

            # DIN->DOUT daisy: the first sink's DIN and the last sink's
            # DOUT are left as externally-facing nets (one connection
            # each so far) -- a board-level `connections` entry in the
            # SAME or a later apply() wires the real serial-bus connector
            # to those exact net names (recorded in the ledger below).
            in_net = prev_out_net if prev_out_net is not None else f"{name}_serial_in"
            sink_connections.append(
                {"net": in_net, "refdes": sink_refdes, "pin": sink_cfg.serial_in_pin}
            )
            out_net = f"{name}_serial_{i}"
            sink_connections.append(
                {"net": out_net, "refdes": sink_refdes, "pin": sink_cfg.serial_out_pin}
            )
            prev_out_net = out_net

            ledger_sinks[sink_refdes] = {
                "index": i,
                "x": comp["x"],
                "y": comp["y"],
                "channels": channel_map,
                "share": share_n,
            }
        if ledger_sinks:
            ledger_sinks["_serial_in_net"] = f"{name}_serial_in"
            ledger_sinks["_serial_out_net"] = prev_out_net
            if sink_cfg.top_plate_pin:
                ledger_sinks["_top_plate_net"] = sink_cfg.top_plate_net
        else:
            warnings.append(
                "sink_grid: configured but no electrode claimed a usable "
                "escape -- check channels_per_sink/plaza layout"
            )

    if not pads:
        raise ValueError(
            "ewod_pad_array: expansion produced zero pads -- check grid/variant"
        )

    footprint_name = f"__gen_{name}"
    footprint = {"name": footprint_name, "pads": pads}

    component: dict[str, Any] = {
        "refdes": name,
        "label": f"EWOD pad array ({rows}x{cols} {variant})",
        "footprint": footprint_name,
        "x": x_anchor,
        "y": y_anchor,
        "rot": 0.0,
        "fixed": "both",
        "pins": [{"name": p} for p in sorted(pin_positions)],
        "roles": ["ewod_array"],
    }

    # A dedicated net class, keyed to THIS generator call, whose only rule
    # is `clearance_mm = gap` — the authored electrode-to-electrode
    # adjacency floor (spec: "checked against the authored gap, not
    # trace_spacing_mm"). Without this, `resolve_net_rules` falls through
    # to the fab's generic `trace_spacing_mm` house_default tier (0.15mm
    # at the default 4-layer capability) for every electrode net, and the
    # default gap (0.10mm) sits BELOW that generic house tier -- so every
    # ordinary electrode-adjacency pair would carry a spurious WARN on
    # every board, not because the gap is unmanufacturable (it clears
    # jlc_min, the hard floor, which stays untouched and still binds) but
    # only because the flat default doesn't know this net class has its
    # OWN intentional target. `resolve_net_rules`'s own clamp to the fab
    # MINIMUM (never below what the fab can make) is what turns an
    # authored gap that undercuts even jlc_min into the real DRC error the
    # spec wants -- this override changes the WARN threshold, never the
    # ERROR one.
    net_class = f"ewod_{name}"
    gap_clearance_mm = max(0.0, sizing["gap"] - _GEOMETRY_ROUNDING_SLACK_MM)
    # Rulings 2026-09-19 item 7 -- a SECOND class, next to the one above,
    # for the nets that actually got a plaza via/stub (`ledger_pads[pin]`
    # carries a `"via"` key ONLY when this pin's escape was usable --
    # `_find_plaza_escape`/the reserve check above, both of which leave
    # a pin with no via at all, never this class): "the escape is plaza
    # via -> B.Cu track -> sink pad, nothing else" (ruling 7, verbatim) is
    # a per-net LAYER LOCK, not merely a clearance floor, so it needs its
    # own class -- a pin with no via has no B.Cu track to lock at all and
    # stays on the plain electrode-gap class above. Carries the SAME
    # `clearance_mm` (the electrode-to-electrode floor still applies to
    # this net's own copper) plus `"layers": ["B.Cu"]`, resolved by
    # `precis.pcb.realize._net_class_layers` into the router's per-net
    # `maze.route(layers=...)` argument.
    escape_class = f"ewod_{name}_escape"
    nets: list[dict[str, Any]] = []
    connections: list[dict[str, Any]] = []
    for pin in sorted(pin_positions):
        net_name = f"{name}_{pin}"
        pin_class = escape_class if "via" in ledger_pads.get(pin, {}) else net_class
        nets.append({"name": net_name, "net_class": pin_class})
        connections.append({"net": net_name, "refdes": name, "pin": pin})

    half_extent_x = cols * sizing["pitch"] / 2.0 + sizing["gap"]
    half_extent_y = rows * sizing["pitch"] / 2.0 + sizing["gap"]
    mask_poly = [
        [x_anchor - half_extent_x, y_anchor - half_extent_y],
        [x_anchor + half_extent_x, y_anchor - half_extent_y],
        [x_anchor + half_extent_x, y_anchor + half_extent_y],
        [x_anchor - half_extent_x, y_anchor + half_extent_y],
    ]
    features = [
        {
            "ftype": "mask_open",
            "layer": "top",
            "geom": {"polygon": mask_poly},
            "note": f"generator:{name}",
        }
    ]

    n_usable = sum(1 for v in ledger_pads.values() if v.get("usable", True))
    n_unusable = len(ledger_pads) - n_usable
    ledger = {
        "generator": "ewod_pad_array",
        "grid": [rows, cols],
        "variant": variant,
        "pads": ledger_pads,
        "plazas": ledger_plazas,
        "summary": {
            "pads_total": len(ledger_pads),
            "pads_usable": n_usable,
            "pads_unusable": n_unusable,
            "plazas": len(plaza_set),
        },
        # docs/backlog/pcb-pre-place-route-blocks.md Slice 2 -- the
        # per-tile escape-fabric report: `tiles` keyed the same way
        # `plazas` is ("P{row}_{col}", plus "rim:R{row}C{col}" for a
        # rim's virtual plazas), each `{emitted, refused, suppressed}`;
        # `totals` sums across every tile PLUS every tile-less refusal
        # (a boundary electrode with no adjacent plaza at all has no
        # tile to attribute to -- see the `tile: None` reason entries);
        # `reasons` is the flat, greppable list behind every non-zero
        # refused/suppressed count. "merged-over" (a merged pad covering
        # a plaza) never appears here: `_parse_pad_sizes` refuses that
        # whole apply before any expansion is built at all, so it is a
        # hard validation error, not a per-tile fabric count. `fan` states
        # the scope boundary out loud: the B.Cu run from a via's own
        # landing to the tile's sink footprint is the ROUTER's job (a
        # sibling slice teaches it to start from fixed copper), never
        # this generator's -- it has no DB access to the sink's real pin
        # positions to route to.
        "fabric": {
            "tiles": fabric_tiles,
            "totals": fabric_totals,
            "reasons": fabric_reasons,
            "fan": "router",
        },
    }
    if sink_cfg is not None:
        ledger["sink_grid"] = ledger_sinks

    unique_merges = {m.pin: m for m in merges.values()}
    canonical_pad_sizes = [
        {"name": m.pin, "cells": [[cr, cc] for cr, cc in m.cells]}
        for m in sorted(unique_merges.values(), key=lambda m: m.span)
    ]

    canonical_params: dict[str, Any] = {
        "grid": [rows, cols],
        "variant": variant,
        "x": x_anchor,
        "y": y_anchor,
        "reserve": sorted(reserve),
        "plazas": sorted([list(p) for p in plaza_set]),
        "pad_sizes": canonical_pad_sizes,
        "sink_grid": (
            {
                "part": sink_cfg.part,
                "footprint": sink_cfg.footprint,
                "channels_per_sink": sink_cfg.channels_per_sink,
                "channel_pins": sink_cfg.channel_pins,
                "serial_in_pin": sink_cfg.serial_in_pin,
                "serial_out_pin": sink_cfg.serial_out_pin,
                "top_plate_pin": sink_cfg.top_plate_pin,
                "top_plate_net": sink_cfg.top_plate_net,
                "power": sink_cfg.power,
            }
            if sink_cfg is not None
            else None
        ),
        **sizing,
    }

    return GeneratorExpansion(
        refdes=name,
        generator="ewod_pad_array",
        version=1,
        canonical_params=canonical_params,
        components=[component, *sink_components],
        nets=nets,
        connections=connections + sink_connections,
        footprints=[footprint],
        features=features,
        ledger=ledger,
        warnings=warnings,
        copper=copper,
        net_classes={
            net_class: {"clearance_mm": gap_clearance_mm},
            escape_class: {"clearance_mm": gap_clearance_mm, "layers": ["B.Cu"]},
        },
    )


_REGISTRY: dict[str, Callable[[str, dict[str, Any]], GeneratorExpansion]] = {
    "ewod_pad_array": _expand_ewod_pad_array,
}


__all__ = [
    "GeneratorExpansion",
    "expand",
    "resolve_ewod_sizing",
]
