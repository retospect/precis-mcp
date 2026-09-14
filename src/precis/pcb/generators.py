"""Computed-component generators — pcb-ewod-multitile Slice 2.

A ``generators`` block on ``put(kind='pcb')`` (:meth:`precis.store.
_pcb_ops.PcbMixin._pcb_apply`) names a generator call — ``{name, generator,
params}`` — that gets *expanded*, deterministically and in pure Python, into
the same shapes the manual authoring surface already accepts: components,
nets, connections, local footprints, features. That reuse is the whole
architecture here: expansion never talks to the database directly, and
nothing downstream (padplace/DRC/gerber/SVG) needs to know a component was
generated rather than hand-authored — a :class:`GeneratorExpansion` is just
a batch :meth:`_pcb_apply` was going to process anyway.

**Why one component, many pads.** The spec's own framing (docs/backlog/
pcb-ewod-multitile.md, "the array generator emits the integrated unit") is
"the whole array is one component whose pins are the electrode nets" — so
``ewod_pad_array`` below emits exactly ONE ``components[]`` entry (one
refdes) whose local footprint's ``pads`` list carries every electrode
square, every F.Cu neck stub, and every plaza via as its own pad row, all
addressed by pin name. :func:`precis.pcb.padplace.place_footprint_pads`
already places every pad in a footprint's ``pads`` list independently and
resolves each one's net through ``pin_map`` — nothing stops two (or three)
pads sharing the SAME pin number (an electrode pad + its neck stub + its
via all being electrically the same net), which is exactly the "one via
per electrode, capacitive load, negligible current" contract. A drilled
pad (``drill`` set) already lands on every copper layer
(:func:`place_footprint_pads`), so the via pad alone gives the electrode
copper on B.Cu with zero extra machinery — the existing router
(``op='route'``) picks that B.Cu landing up like any other pad on that
net. **This is why slice 2's "B.Cu-only escape" needs no new routing
code**: escape routing IS routing, on an ordinary net, once the via pad
exists.

**Round 8 (gripe 338983 fixed) — what that claim now does and does not
cover.** The router/DRC pad source (``precis.pcb.realize.pads_for_ir``)
used to place every pin at ``ir.py``'s SYNTHESIZED ``pin_dx``/``pin_dy``
regardless of the real footprint, so for THIS module's own custom-grid
component it measured and routed wildly wrong virtual positions. Real
per-pin positions now reach the IR (``precis.pcb.session.
apply_real_pin_offsets``, wired into ``build_ir``), so escape routing IS
driven off the real electrode copper — verified end-to-end on the
``ewod-dogfood-1`` fixture (``tests/test_pcb_ewod_dogfood.py``), which
routes escapes to a real bottom-side sink and DRCs the result. **The
residue that remains is the IR's one-position-per-pin model**: an
electrode's three pads (body + stub + via) collapse to ONE IR pad — the
body, first-wins — so the plaza via itself is invisible to the router,
which drops its OWN via for a layer change rather than reusing the
authored one, and DRC through the IR path never sees stub/via copper at
all (the exact-geometry coverage for those lives in
``tests/test_pcb_ewod_generator_drc.py``, which drives ``padplace.
board_pads`` directly — the full, all-pads path the gerber writer uses).
Teaching the IR several pads per pin is an architecture round of its own;
see docs/backlog/pcb-ewod-multitile.md's round-8 decisions log.

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

- **DRC integration — resolved round 3, decision: the plaza via STAYS a
  drilled THT footprint pad, never a persistent ``model["copper"]`` via
  row.** Round 2's own docstring here got the risk backwards: it worried
  ``check_via_pad_keepout`` (a ROUTER-placed via must clear every pad) was
  blind to the plaza via because a footprint pad is not a ``model
  ["copper"]`` ``ctype == "via"`` item — true, but irrelevant, because
  that check's whole job is protecting pads from a router-inserted via
  landing on them, and a plaza via is never the VIA side of that question;
  it is one of the PADS every real router via still gets checked against
  (``model["pads"]``, generically — polygon electrodes included, via
  their authored bbox), unchanged, no waiver needed
  (``tests/test_pcb_ewod_generator_drc.py``). The REAL gap round 3 found
  and fixed instead: ``check_annular_ring`` iterated
  ``model["copper"]`` vias only, so a plaza via's own drilled hole
  (a THT footprint pad, never a ``copper`` via row by this design's own
  decision) had its annular ring computed by nothing — extended in
  ``drc.py`` to also ring-check every drilled footprint pad, deduplicated
  across the one-flash-per-copper-layer repetition
  :mod:`precis.pcb.padplace` emits for a THT pad.
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
  electrode pad + ONE stub + ONE via for the whole span — "one via
  suffices, the electrode is a capacitor" (spec decision), picked as the
  FIRST plaza-adjacent cell/direction found across the span
  (:func:`_find_plaza_escape`), same determinism the single-cell case
  already had. :func:`_electrode_polygon` itself is the load-bearing
  generalisation: its 4 walls now walk a RANGE of unit cells rather than
  exactly one, concatenating each unit's own (unchanged) crenellated-or-
  flat segment — no new "seam filler" logic needed, because a segment's
  own zero-deflection ends (the existing corner-flattening machinery)
  already connect cleanly to the next unit's, which is exactly the solid
  copper a merged pad's own internal seam should be.
- **Sink-grid emission — resolved round 7, mechanically.** ``sink_grid``
  (:class:`_SinkGrid`) now emits one bottom-side component instance per
  ``per_tiles`` x ``per_tiles`` tile block, wired to that block's own
  USABLE electrode escape nets (row-major channel assignment against
  ``channel_pins``), chained DIN->DOUT across tiles in row-major order,
  and (optionally) tied into a shared top-plate/complement-rail net. It
  is deliberately PART-AGNOSTIC (:func:`expand` is pure, no DB reads, so
  it cannot look up a real part's own pin names) — the caller names
  every pin this module needs to wire. **Known engine limitation, NOT
  fixed here (out of round-7 scope, per the round-6 handoff)**: a sink
  instance's own ``layer='bottom'`` is honest (gerber/silk already read
  ``pcb_instances.layer``), but ``rules.py::PAD_LAYER`` still forces
  EVERY pad's IR layer to 0 regardless of instance side
  (``ir.py::from_graph`` ignores ``pcb_instances.layer`` entirely) — so a
  sink placed directly under the array (its correct real-world position)
  reads, to DRC, as sitting on the SAME copper layer as the electrode
  field above it, which a courtyard-overlap/clearance check has no way
  to know is actually a different physical side. This is a pre-existing,
  kind-wide gap (slice 3's own scope, docs/backlog/pcb-ewod-multitile.md
  target list), not something ``sink_grid`` introduced; it bites HARDER
  here than anywhere else in the kind because "directly under the array"
  is the whole point of a sink grid. Pin-to-channel assignment order is
  documented in the ledger (auto row-major; the pre-place-route block
  spec will revisit the whole scheme).
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

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from shapely.geometry import LineString  # type: ignore[import-untyped]

from precis.pcb.capabilities import capability_for

Point = tuple[float, float]

#: The fab process the derived-sizing defaults below are pinned to — every
#: EWOD board today is the 4-layer default stackup (``pcb.DEFAULT_STACKUP``);
#: lift this once ``put(stackup=...)`` authoring (Slice 3) exists.
_FAB_PROCESS = "4layer"

#: pcb-ewod-multitile decisions log: pitch default 2.0mm, gap 0.10mm
#: (electrode gap — advisory only, HV separation applies to plaza
#: internals/B.Cu escapes instead), edge tooth_depth 0.06mm / tooth_pitch
#: 0.25mm (Frontiers Phys. 2020 survey figures, docs/backlog's literature
#: grounding section).
_DEFAULT_PITCH_MM = 2.0
_DEFAULT_GAP_MM = 0.10
_DEFAULT_TOOTH_DEPTH_MM = 0.06
_DEFAULT_TOOTH_PITCH_MM = 0.25
_DEFAULT_STUB_WIDTH_MM = 0.20
#: PLACEHOLDER coated-conductor HV clearance — the spec's decisions log
#: leaves "which IPC-2221-style coated row applies" explicitly OPEN. This
#: is a documented mechanical default (round-2 scope note), not a resolved
#: engineering figure: 0.3mm flat floor, scaled up 0.002mm/V above it when
#: ``drive_voltage_v`` is given. Revisit when that decision lands.
_DEFAULT_HV_SEPARATION_MM = 0.3
_HV_SEPARATION_V_SCALE = 0.002

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
#: any OTHER already-tuned clearance) until the corridor reopens to the
#: same ``gap`` the rest of the array's design already targets everywhere
#: else. This margin is added on top of that geometric target to absorb
#: the tapered stub's own residual half-width at its closest approach (a
#: separate, much smaller effect — round-3's own measured deficit was a
#: few hundredths of a mm) and ordinary coordinate-rounding noise.
_PLAZA_CORNER_CHAMFER_MARGIN_MM = 0.01

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
#: 2*sin(22.5deg) — the chord-length factor for 8 points evenly spaced
#: (45 degrees apart) on a circle: chord = 2*R*sin(half the angle step).
_RING_CHORD_FACTOR = 2.0 * math.sin(math.pi / 8.0)


def _plaza_capacity(
    gap: float, via_dia: float, hv_separation: float
) -> dict[str, float]:
    """The REAL plaza floor (round-2 stress-test finding, replacing an
    earlier "3x3 sub-grid at spacing=half" model that turned out
    infeasible at the spec's own default numbers — see the module
    docstring's own history note if this reads like a second attempt,
    because it is one).

    **Slot layout.** All 8 escapes sit on ONE ring of radius ``R`` around
    the plaza centre, at 45-degree spacing (N/NE/E/SE/S/SW/W/NW) — not a
    3x3 square sub-grid. A square grid forces the SAME spacing (``half``)
    to serve two very different jobs at once (adjacent-slot clearance
    AND diagonal-slot-to-foreign-electrode clearance), and at the spec's
    own cited via/pitch numbers those two jobs need DIFFERENT spacings —
    the square model is unsatisfiable there. A ring decouples them: ``R``
    is picked purely from adjacent-slot spacing, then validated
    separately against the (fixed, `R`-independent-until-plugged-in)
    foreign-corner clearance.

    **Two real constraints, ``half`` is the one free variable:**

    1. Adjacent same-ring slots (45 degrees apart) must clear
       ``via_dia + hv_separation`` centre-to-centre: chord
       ``= R * 2*sin(22.5deg) >= via_dia + hv_separation``, giving
       ``R = (via_dia + hv_separation) / (2*sin(22.5deg))`` (the smallest
       ring that satisfies every adjacent pair at once — cardinal-cardinal
       and cardinal-diagonal chords are equal on a uniform ring).
    2. A DIAGONAL slot (radius component along each axis ``u = R/sqrt(2)``)
       must clear the two neighbouring electrodes it does NOT serve — e.g.
       the plaza's NW slot serves the NW electrode (same net, free to be
       close) but sits near the N electrode's own SW corner, at board-frame
       offset ``(-half, -(half+gap))`` from the plaza centre (the SAME
       geometric identity every OTHER electrode's own flat corner uses).
       Requiring ``sqrt((half-u)^2 + (half+gap-u)^2) >= via_dia/2 +
       hv_separation`` and solving the resulting quadratic for the
       SMALLEST ``half`` that satisfies it (larger ``half`` moves that
       corner farther away, so this is monotone) gives ``min_half``.

    ``min_pitch = gap + 2*min_half`` is what :func:`resolve_ewod_sizing`
    actually validates ``pitch`` against.

    **Round-4 fix: adjacent-ring-slot chord carries a rounding-slack
    margin.** When no ``drive_voltage_v`` is declared, ``hv_separation``
    itself falls back to the fab's OWN ``jlc_min`` ``trace_spacing_mm``
    (:func:`resolve_ewod_sizing`'s own fallback) — the SAME value
    :func:`precis.pcb.drc.check_clearance`'s ERROR tier checks against.
    Constraint 1 above then places adjacent via slots at EXACTLY that
    floor, zero margin, by design — but the placed vias' actual board
    coordinates go through the same 4-decimal-place rounding as every
    other pad (:func:`precis.pcb.padplace.place_footprint_pads`), which
    can shave a few 0.00001mm off the exact analytic chord (same
    mechanism ``_GEOMETRY_ROUNDING_SLACK_MM``'s own docstring documents
    for the electrode-gap net class) — enough, at zero margin, to flip a
    genuinely-manufacturable design into a spurious ``check_clearance``
    ERROR (found stress-testing this round's diagonal-stub fix: once that
    fix stopped dominating the findings list, THIS zero-margin pair was
    what showed up next). Padding the chord requirement by the same
    :data:`_GEOMETRY_ROUNDING_SLACK_MM` used elsewhere keeps every
    adjacent via pair comfortably above the floor without materially
    loosening ``min_pitch`` (the slack is 1% of the default ``gap`` and
    far smaller than any real via/hv_separation figure)."""
    req = via_dia / 2.0 + hv_separation + _GEOMETRY_ROUNDING_SLACK_MM
    ring_radius = (
        via_dia + hv_separation + _GEOMETRY_ROUNDING_SLACK_MM
    ) / _RING_CHORD_FACTOR
    u0 = ring_radius / math.sqrt(2.0)
    # 2*a^2 + 2*g*a + (g^2 - req^2) >= 0, a = half - u0 -- see docstring's
    # constraint 2. The upper root is the boundary; `a` must be AT LEAST
    # that (a is increasing in `half`).
    disc = 2.0 * req * req - gap * gap
    a_hi = req if disc < 0 else (-gap + math.sqrt(disc)) / 2.0
    min_half = u0 + a_hi
    return {
        "slot_radius": ring_radius,
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
    if params.get("hv_separation") is not None:
        hv_separation = float(params["hv_separation"])
    elif drive_voltage_v is not None:
        # A DECLARED voltage gets a real (if placeholder-derived, see
        # module docstring) HV margin above ordinary fab spacing.
        hv_separation = max(
            _DEFAULT_HV_SEPARATION_MM, float(drive_voltage_v) * _HV_SEPARATION_V_SCALE
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
            "fit its 8-slot escape ring at this pitch; raise pitch, or "
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
    stub_width = float(
        params.get(
            "stub_width", cap.jlc_min.get("trace_width_mm") or _DEFAULT_STUB_WIDTH_MM
        )
    )
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

    # Plaza-corner chamfer (round-4 fix, see _PLAZA_CORNER_CHAMFER_MARGIN_MM's
    # docstring for the full derivation): retreat = sqrt(2)*(target - gap/
    # sqrt(2)) where target = gap + margin -- i.e. the corner moves along
    # each of its own two flat/mesh walls by just enough that its distance
    # to the escape's 45-degree centreline grows from the un-chamfered
    # gap/sqrt(2) up to the array's own uniform `gap` design target, plus
    # a fixed safety margin.
    plaza_corner_chamfer = (
        math.sqrt(2.0) * (gap + _PLAZA_CORNER_CHAMFER_MARGIN_MM) - gap
    )
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
        "stub_width": stub_width,
        "tooth_depth": tooth_depth,
        "tooth_pitch": tooth_pitch,
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


def _edge_sign(t: float, tooth_pitch: float, t0: float, t1: float) -> int:
    """:func:`_tooth_sign`, but forced to 0 (no deflection) within one
    tooth_pitch of either end of the edge ``[t0, t1]``.

    Without this, two edges meeting at a pad's corner each compute their
    OWN independent wave (one is a function of x, the perpendicular one a
    function of y) — they agree everywhere along a shared straight edge
    (by construction, both query the same absolute coordinate) but have
    no reason to agree AT the corner point itself, where they meet
    end-to-end rather than side-by-side. Clamping every wall to the
    pad's plain, non-deflected corner (this returns 0 there, giving the
    same nominal ``+-half`` corner every flat wall already uses) is what
    keeps the polygon a single closed, non-self-intersecting ring."""
    lo, hi = (t0, t1) if t0 <= t1 else (t1, t0)
    margin = min(tooth_pitch, (hi - lo) / 2.0)
    if t <= lo + margin + 1e-9 or t >= hi - margin - 1e-9:
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
    t0: float, t1: float, *, tooth_pitch: float, depth: float
) -> list[tuple[float, float]]:
    """The SHARED crenellated centreline — ``(t, deflection)`` pairs, a
    right-angle-square-wave SHAPE (flat plateaus at ``+-depth``) but
    with each transition rounded into a reverse-curve S (:func:`_s_curve`,
    radius = ``depth``) rather than an instant jog — deflection only (no
    gap offset, no midline — those are the caller's job).

    This is a template curve, not a pad boundary: :func:`_meshing_wall`
    derives EACH side's actual boundary from it via a proper
    perpendicular polyline offset (shapely ``offset_curve``)."""
    pts = _breakpoints(t0, t1, tooth_pitch)
    n_intervals = len(pts) - 1
    # One flat deflection level per interval [pts[i], pts[i+1]].
    levels = [
        depth * _edge_sign((pts[i] + pts[i + 1]) / 2.0, tooth_pitch, t0, t1)
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
    +/-1 derived from shapely's left/right convention."""
    centerline = _centerline_points(t0, t1, tooth_pitch=tooth_pitch, depth=depth)
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
    ``_PLAZA_CORNER_CHAMFER_MARGIN_MM``'s docstring) exactly where one
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
) -> list[Point]:
    """One electrode's polygon — a single grid cell (``r0==r1, c0==c1``,
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

    **Plaza-corner chamfer (round 4, generalised round 6).** Where a FLAT
    wall (facing a plaza) meets a MESH wall (facing an electrode
    neighbour) at one of the span's own 4 OUTER corners, that corner is
    exactly the point a DIFFERENT electrode's own diagonal escape stub has
    to thread past on its way to the same plaza — and at default sizing
    that corner sits geometrically closer to the escape's centreline than
    the fab's own absolute copper-spacing floor allows, independent of how
    the stub itself is shaped (see ``_PLAZA_CORNER_CHAMFER_MARGIN_MM``'s
    docstring for the full derivation and why no taper redesign alone can
    fix it). Both walls meeting such a corner retreat INWARD along their
    own axis by ``plaza_corner_chamfer`` — this only ever removes copper
    (can't newly violate anything else) and reopens the corridor those two
    walls' corner and its mirror twin bound to (at least) the array's own
    uniform ``gap`` design target."""
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
    ) -> list[Point]:
        out: list[Point] = []
        n = len(units)
        for i, unit in enumerate(units):
            t0, t1 = get_extent(unit)
            t0c, t1c = _chamfer_inset(
                t0,
                t1,
                at_start=(i == 0 and chamfer_start),
                at_end=(i == n - 1 and chamfer_end),
                amount=chamfer,
            )
            if kind_fn(unit) == "mesh":
                out += _meshing_wall(
                    t0c,
                    t1c,
                    tooth_pitch=tp,
                    depth=depth,
                    mid_axis=mid_axis,
                    gap=gap,
                    side=1.0,
                    axis=axis,
                )
            elif axis == "x":
                out += [(flat_axis, t0c), (flat_axis, t1c)]
            else:
                out += [(t0c, flat_axis), (t1c, flat_axis)]
        return out

    pts: list[Point] = []
    # West wall: rows r1 -> r0 (south to north), each row's own y-extent
    # (cy+half) down to (cy-half) -- start is the SW corner, end the WN
    # corner, matching the single-cell case's own point order.
    pts += wall_run(
        list(range(r1, r0 - 1, -1)),
        lambda r: (layout.cy(r) + half, layout.cy(r) - half),
        kind_w,
        mid_axis=layout.cx(c0) - pitch / 2.0,
        flat_axis=layout.cx(c0) - half,
        axis="x",
        chamfer_start=chamfer_sw,
        chamfer_end=chamfer_wn,
    )
    # North wall: cols c0 -> c1 (west to east).
    pts += wall_run(
        list(range(c0, c1 + 1)),
        lambda c: (layout.cx(c) - half, layout.cx(c) + half),
        kind_n,
        mid_axis=layout.cy(r0) - pitch / 2.0,
        flat_axis=layout.cy(r0) - half,
        axis="y",
        chamfer_start=chamfer_wn,
        chamfer_end=chamfer_ne,
    )
    # East wall: rows r0 -> r1 (north to south).
    pts += wall_run(
        list(range(r0, r1 + 1)),
        lambda r: (layout.cy(r) - half, layout.cy(r) + half),
        kind_e,
        mid_axis=layout.cx(c1) + pitch / 2.0,
        flat_axis=layout.cx(c1) + half,
        axis="x",
        chamfer_start=chamfer_ne,
        chamfer_end=chamfer_es,
    )
    # South wall: cols c1 -> c0 (east to west).
    pts += wall_run(
        list(range(c1, c0 - 1, -1)),
        lambda c: (layout.cx(c) + half, layout.cx(c) - half),
        kind_s,
        mid_axis=layout.cy(r1) + pitch / 2.0,
        flat_axis=layout.cy(r1) + half,
        axis="y",
        chamfer_start=chamfer_es,
        chamfer_end=chamfer_sw,
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
    return out


def _stub_polygon(anchor: Point, target: Point, width: float) -> list[Point]:
    """A TAPERED neck from ``anchor`` (a point on/near the electrode's own
    boundary) to ``target`` (the via slot centre) — a point (zero width)
    at ``anchor``, widening linearly to ``width`` at ``target``, rather
    than a uniform-width rectangle.

    The taper matters for a DIAGONAL escape specifically: its anchor is
    the electrode's own corner, which sits only ``gap*sqrt(2)`` from the
    diagonally-adjacent electrode's own corner — closer than a full-width
    stub's own half-width would clear. A stub that starts at zero width
    right at the corner and only reaches full width once it has travelled
    away from that pinch point (same idea as a trace-to-pad teardrop
    fillet) tracks the real available clearance; a uniform-width
    rectangle does not and clips the neighbour there (round-2 stress-test
    finding). Same-net overlap with the electrode's OWN polygon at the
    anchor end is still fine (redundant copper, not a short) — the fix
    here is only about NEIGHBOURING electrodes."""
    ax, ay = anchor
    bx, by = target
    dx, dy = bx - ax, by - ay
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return [(ax, ay), (ax, ay), (ax, ay), (ax, ay)]
    ux, uy = dx / length, dy / length
    px, py = -uy * width / 2.0, ux * width / 2.0
    return [
        (ax, ay),
        (bx + px, by + py),
        (bx - px, by - py),
    ]


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
    dx = 1 if c == 0 else (-1 if c == layout.cols - 1 else 0)
    dy = 1 if r == 0 else (-1 if r == layout.rows - 1 else 0)
    if dx == 0 and dy == 0:
        return (cx, cy)
    direction = _DIR_BY_DELTA[(dy, dx)]
    return _plaza_slot_point(layout, r + dy, c + dx, direction)


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
    physically sits toward -- see :data:`_OPPOSITE`). All 8 slots sit on
    ONE ring of radius ``slot_radius`` (:func:`_plaza_capacity`) at
    45-degree spacing -- a cardinal direction's unit vector is already
    unit length, a diagonal one has magnitude sqrt(2) and is normalised
    here so every slot ends up the same distance from the plaza centre."""
    dr, dc = {d: (dr, dc) for d, dr, dc in _DIRECTIONS}[_OPPOSITE[direction]]
    norm = math.hypot(dr, dc)
    r = layout.sizing["slot_radius"]
    return (layout.cx(pc) + dc / norm * r, layout.cy(pr) + dr / norm * r)


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
    pcb-ewod-multitile.md's "sink_grid emission — part-agnostic" decision):
    a regular grid of bottom-side sink component instances, one per
    ``per_tiles`` x ``per_tiles`` block of electrode CELLS, each wired to
    the escape nets of the usable electrodes its own block owns, chained
    DIN->DOUT in row-major tile order, and (optionally) tied into a shared
    top-plate/complement-drive rail net.

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

    **Channel assignment is mechanical, not optimal**: row-major over the
    tile's own USABLE (escaped, non-reserved) electrodes, zipped against
    ``channel_pins`` in the order given -- "the block spec will revisit"
    (docs/backlog/pcb-ewod-multitile.md's own round-6 handoff note). A
    tile needing more channels than ``channel_pins`` provides is a hard
    error (the part cannot serve that many escapes); a tile needing fewer
    just leaves the tail of ``channel_pins`` unused for that sink."""

    part: str | None
    footprint: str | None
    footprint_label: str
    per_tiles: int
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
    per_tiles = int(cfg.get("per_tiles") or 0)
    if per_tiles < 1:
        raise ValueError("ewod_pad_array: sink_grid.per_tiles must be >= 1")
    channel_pins = [str(p) for p in (cfg.get("channel_pins") or [])]
    if not channel_pins:
        raise ValueError(
            "ewod_pad_array: sink_grid.channel_pins must name at least one "
            "pin (the part-agnostic design has no other way to know what "
            "this part calls its channel pins)"
        )
    top_plate_pin = cfg.get("top_plate_pin")
    return _SinkGrid(
        part=str(part) if part else None,
        footprint=str(footprint) if footprint else None,
        footprint_label=str(cfg.get("label") or footprint or f"sink:{part}"),
        per_tiles=per_tiles,
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
    # `sink_grid`'s own per-tile channel roster, built up in the SAME
    # row-major electrode-escape scan below rather than a second pass --
    # (tile_row, tile_col) -> [(anchor_row, anchor_col, pin), ...] in the
    # exact order electrodes are visited (row-major), which is the
    # "row-major channel assignment" the module docstring promises. Only
    # ever populated for USABLE escapes (an unusable pad has no via, so
    # binding a sink channel to it would wire a pad-to-pad net with no
    # copper between them).
    tile_pin_lists: dict[tuple[int, int], list[tuple[int, int, str]]] = {}
    x_anchor = float(params.get("x", 0.0))
    y_anchor = float(params.get("y", 0.0))

    # A diagonal escape's via slot sits only `gap*sqrt(2)` from the
    # diagonally-adjacent electrode's own corner (round-2 stress-test
    # finding: two abutting electrodes' corners approach that closely by
    # construction, half+half+gap=pitch). A neck wider than `gap` at that
    # end WILL clip the neighbour despite the taper (_stub_polygon's own
    # docstring) -- cap it here, once, rather than at each of the two
    # call sites below.
    stub_width = min(sizing["stub_width"], sizing["gap"])
    if stub_width < sizing["stub_width"] - 1e-9:
        warnings.append(
            f"stub_width {sizing['stub_width']}mm capped to gap {sizing['gap']}mm "
            "at the via end -- wider would clip a diagonally-adjacent electrode's "
            "corner (only gap*sqrt(2) away there)"
        )
    sizing["stub_width"] = stub_width

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
            poly = _electrode_polygon(layout, r0, c0, r1, c1)
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

            if variant == "rim":
                # A merged span's via/anchor is placed off its FIRST cell
                # (sorted row-major, i.e. its own top-left corner) -- "one
                # via suffices" (spec decision), and rim's hollow interior
                # has no discrete slot budget the choice of cell could
                # collide against.
                via_r, via_c = cells[0]
                via_pt = _rim_via_point(layout, via_r, via_c)
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
                pads.append(
                    {
                        "pin": pin,
                        "shape": "polygon",
                        "poly": _stub_polygon(anchor, via_pt, sizing["stub_width"]),
                        "role": "electrode",
                        "mask": "covered",
                    }
                )
                pads.append(
                    {
                        "pin": pin,
                        "shape": "circle",
                        "x": via_pt[0],
                        "y": via_pt[1],
                        "w": sizing["via_dia"],
                        "drill": sizing["via_drill"],
                        "role": "electrode",
                        "mask": "covered",
                    }
                )
                ledger_pads[pin]["via"] = {"x": via_pt[0], "y": via_pt[1]}
                continue

            # full variant: find this electrode's plaza neighbour --
            # exactly one candidate for an unmerged cell (at most one for
            # the auto/3x3 rule -- see _default_plaza's docstring for the
            # boundary cells that legitimately have none); for a merged
            # span, the FIRST candidate across all its cells
            # (_find_plaza_escape's own docstring: "one via suffices").
            escape = _find_plaza_escape(layout, cells)
            if escape is None:
                ledger_pads[pin]["usable"] = False
                ledger_pads[pin]["reason"] = "no adjacent plaza (array boundary)"
                warnings.append(
                    f"{pin}: no adjacent via plaza -- marked unusable "
                    "(array-boundary effect of the 3x3 auto rule)"
                )
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
            pads.append(
                {
                    "pin": pin,
                    "shape": "polygon",
                    "poly": _stub_polygon(anchor, via_pt, sizing["stub_width"]),
                    "role": "electrode",
                    "mask": "covered",
                }
            )
            pads.append(
                {
                    "pin": pin,
                    "shape": "circle",
                    "x": via_pt[0],
                    "y": via_pt[1],
                    "w": sizing["via_dia"],
                    "drill": sizing["via_drill"],
                    "role": "electrode",
                    "mask": "covered",
                }
            )
            plaza_ledger["slots"][slot_dir] = {"status": "used", "pin": pin}
            ledger_pads[pin]["via"] = {"x": via_pt[0], "y": via_pt[1]}
            ledger_pads[pin]["plaza"] = slot_key
            if sink_cfg is not None:
                tile_key = (r0 // sink_cfg.per_tiles, c0 // sink_cfg.per_tiles)
                tile_pin_lists.setdefault(tile_key, []).append((r0, c0, pin))

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

    # -- sink grid (round 7): bottom-side switch/connector instances, one
    # per `per_tiles` x `per_tiles` block, wired to the block's own
    # escaped electrode nets + a DIN->DOUT daisy chain + an optional
    # shared top-plate/complement rail -- see _SinkGrid's own docstring
    # for the full design. Emitted here (rather than folded into the main
    # loop above) because the daisy chain needs every tile's identity
    # decided FIRST, in row-major tile order, not electrode-discovery
    # order (a tile with its escape-adjacent electrode near the far edge
    # of the block could otherwise be discovered out of tile order).
    sink_components: list[dict[str, Any]] = []
    sink_connections: list[dict[str, Any]] = []
    ledger_sinks: dict[str, Any] = {}
    if sink_cfg is not None:
        prev_out_net: str | None = None
        for tile_key in sorted(tile_pin_lists):
            tr, tc = tile_key
            channel_pins_here = tile_pin_lists[tile_key]  # already row-major
            if len(channel_pins_here) > len(sink_cfg.channel_pins):
                raise ValueError(
                    f"ewod_pad_array: sink_grid tile ({tr},{tc}) needs "
                    f"{len(channel_pins_here)} escape channels but "
                    f"sink_grid.channel_pins only names "
                    f"{len(sink_cfg.channel_pins)} -- this part cannot "
                    "serve that many electrodes per tile"
                )
            r_lo = tr * sink_cfg.per_tiles
            r_hi = min(rows - 1, r_lo + sink_cfg.per_tiles - 1)
            c_lo = tc * sink_cfg.per_tiles
            c_hi = min(cols - 1, c_lo + sink_cfg.per_tiles - 1)
            sink_refdes = f"{name}_SINK_{tr}_{tc}"
            channel_map: dict[str, str] = {}
            pin_decls: list[dict[str, Any]] = []
            for i, (_r0, _c0, elec_pin) in enumerate(channel_pins_here):
                ch_pin = sink_cfg.channel_pins[i]
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

            comp: dict[str, Any] = {
                "refdes": sink_refdes,
                "label": f"{name} sink ({tr},{tc})",
                "footprint": sink_cfg.footprint_label,
                "x": layout.cx((c_lo + c_hi) / 2.0),
                "y": layout.cy((r_lo + r_hi) / 2.0),
                "rot": 0.0,
                "layer": "bottom",
                # A sink's whole reason to exist is sitting directly under
                # ITS OWN tile block (the escape-locality argument the
                # spec's own "regular grid of HV switches directly under
                # the array" language makes) -- letting the placer move it
                # would defeat that, same as the array's own `fixed=
                # 'both'` above.
                "fixed": "both",
                "pins": pin_decls,
                "roles": ["ewod_sink"],
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
            out_net = f"{name}_serial_{tr}_{tc}"
            sink_connections.append(
                {"net": out_net, "refdes": sink_refdes, "pin": sink_cfg.serial_out_pin}
            )
            prev_out_net = out_net

            ledger_sinks[sink_refdes] = {
                "tile": [tr, tc],
                "x": comp["x"],
                "y": comp["y"],
                "channels": channel_map,
            }
        if ledger_sinks:
            ledger_sinks["_serial_in_net"] = f"{name}_serial_in"
            ledger_sinks["_serial_out_net"] = prev_out_net
            if sink_cfg.top_plate_pin:
                ledger_sinks["_top_plate_net"] = sink_cfg.top_plate_net
        else:
            warnings.append(
                "sink_grid: configured but no tile claimed any usable "
                "escape -- check per_tiles/plaza layout"
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
    nets: list[dict[str, Any]] = []
    connections: list[dict[str, Any]] = []
    for pin in sorted(pin_positions):
        net_name = f"{name}_{pin}"
        nets.append({"name": net_name, "net_class": net_class})
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
                "per_tiles": sink_cfg.per_tiles,
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
        net_classes={
            net_class: {
                "clearance_mm": max(0.0, sizing["gap"] - _GEOMETRY_ROUNDING_SLACK_MM)
            }
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
