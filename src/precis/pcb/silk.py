"""Silkscreen draws for a board — the ONE builder every silk consumer calls.

Closes the gap :mod:`precis.handlers.pcb` used to document explicitly:
``model["silkscreen"]`` was always ``{"top": [], "bottom": []}`` because
"there is no silkscreen table yet". This module is the generator half —
a pure function of a settled :class:`precis.pcb.ir.PcbIR` (refdes, pose,
pin offsets) plus the board's real flashed pad geometry, same
"regenerate, don't hand-author" discipline :mod:`precis.pcb.realize`
already applies to copper.

**Per placed instance, three kinds of silk:**

1. a **reference-designator label** (:mod:`precis.pcb.stroke_font`), sized
   from ``height_mm``. **Outside the part's outline, always** (gr495394,
   Reto 2026-10-17: "label outside white line, say to the bottom left
   corner of the component. Unless unavailable." — the default moved to
   the centred bottom edge 2026-10-30, Reto: "center labels near part and
   pick a pref direction (below center)"; gr496552, Reto
   2026-10-18: "it still should not be printed under parts, on vias."):
   the bottom side's centre by default, then a fixed order of corners and
   sides (:data:`LABEL_SPOT_ORDER`, :func:`_outline_spot_candidates`),
   each two clear line widths off the outline; then the radial ladder
   further out (:data:`_CANDIDATES`, a ring sweep that walks outward
   from bottom-left). **There is no inside
   last resort** — a label under the assembled part is invisible ink,
   so a part with no clear outside spot gets its label DROPPED (the
   ``silk_missing`` error names it), not hidden. "We can move it": a
   part's spot is overridable per instance via ``build_silk``'s
   ``label_spots`` argument (``op='move'``'s ``label_spot`` writes it).
   Failing every spot it is DROPPED — "a fab scrapes
   silk off pads, so text under a pad is silently lost" (task brief).
   Never emitted blind. A label never overlaps a pad, a via, another
   label, or any part's outline (:func:`_label_blocked`).
   **Read from one side, at any part rotation.** Glyph orientation is
   pinned to 0 degrees regardless of the part's own rotation — a label
   never goes vertical or upside-down, so nobody ever turns the board to
   read a refdes. The label's SPOT is chosen in the board frame too: every
   candidate is keyed off the courtyard as placed (rotated and mirrored),
   so "below" is below on the board at any rotation. A part-frame spot
   with board-frame text put a rot-180 0402's label above it, overlapping
   it, and labelled a 180-symmetric part differently at rot 0 and rot 180
   (heater-base-test, 2026-10-02). Bottom-side text is still
   mirrored (``mirror=True`` into :func:`precis.pcb.stroke_font.layout_text`)
   because ``B.Silkscreen`` is viewed through the board — "one orientation
   per side" is the rule, not "bottom silk reads from the top".
2. a **courtyard/body outline** — the POLYGON
   :func:`precis.pcb.ir.instance_courtyard_polygon` derives from this
   instance's OWN pad outlines (their union, offset outward — concavity
   kept since gr524238), never
   a fixed constant tuned to nothing and, since 2026-08-30, never a
   square either: a square sized by a pin-offset RADIUS over-reserves a
   1x20 edge connector 8-fold while UNDER-reserving a SOIC-8, and no
   single radius fixes both (that function's own docstring carries the
   measured table). See :attr:`precis.pcb.ir.PcbIR.pin_w`'s docstring for
   the neighbouring defect class ("every pad the same 0.4mm disc") the
   pad-size term sidesteps.
   The offset is walked down the fab chain (:func:`silk_clearance_mm`:
   mask expansion -> silk-to-mask clearance -> half the drawn stroke) from
   this board's capability row, plus two line widths of clear board where
   there is room (:func:`outline_offsets_mm`) — so the outline cannot
   overlap its own pads by construction. Its final shape is settled for
   the whole board at once (:func:`part_outlines`): it bends in toward,
   never across, a close neighbour and the board edge, and an edge-mated
   part's outline gets one clean gap at the edge (gr495393/gr495293).
3. a **pin-1 marker** — a **dot** beside pin 1
   (:func:`_pin1_dot_candidates`), placed outside the courtyard next to
   pin 1's own land-pattern offset (or the first declared pin, when no pin
   is literally named ``"1"``).

   The other spelling, a tick cut at the nearest courtyard VERTEX, is only
   the fallback for when no dot placement is clear. It reads far worse
   than its ubiquity suggests: the tick IS a cut of the courtyard outline,
   so it prints along that outline rather than beside it — measured on the
   40mm fixture, all 20 ticked parts sat 0.0000mm from their own courtyard
   — and ink that coincides with the line it annotates conveys nothing.
   Being inside the courtyard, it is also under the part once assembled.
   :func:`precis.pcb.drc.check_silk_missing` scored all 20 as present
   throughout, because it proves a draw EXISTS and cannot see that it is
   invisible.

   **Not every part needs one.** A pin-1 mark exists to disambiguate an
   orientation the assembler could otherwise get backwards — meaningless
   for a part with no polarity at all. A passive whose refdes FAMILY
   (leading letters before the first digit, uppercased —
   :func:`_refdes_family`) is R/C/L/FB gets no attempt UNLESS the caller's
   ``polarized`` set names it (an electrolytic/tantalum cap, a polarized
   inductor): see :func:`build_silk`'s own ``polarized`` parameter. The
   skip still gets a census row (``outcome="not_applicable"``), never a
   silent absence, so :func:`precis.pcb.drc.check_silk_missing` and a
   human reading the report both see a deliberate policy, not a dropped
   mark.

**Suppression, not silent loss.** Every drawn stroke — text, outline,
tick alike — is checked against the passed-in ``pads`` (real flashed pad
geometry, e.g. :func:`precis.pcb.padplace.board_pads`'s output, or the
synthesized-bound fallback :func:`precis.pcb.realize.pads_for_ir`) AND
``vias`` (realized copper's ``ctype == 'via'`` items — same "a fab scrapes
silk off exposed copper" hazard a pad has, and just as silently lost if
skipped) before it survives into the result. A pad is checked at its
soldermask OPENING, not its copper outline (:func:`_mask_openings`): the
opening is already swelled past the copper, and ink inside it still prints
onto solderable metal — checking the copper alone left every clearance
test in this module one expansion optimistic. A via has no opening (they
are tented) so its bare annulus is the obstacle. An overlapping outline/tick
segment is dropped outright (there is no sensible "relocate a courtyard
box"); an overlapping refdes tries the candidate list first.

**The board's own cut edge was a separate, unchecked question until
2026-08-31.** ``pads``/``vias``/``reserved`` are OBSTACLES; none of them is
the board itself, so a build with no outline gap between a part and the
board edge used to print silk past it with no margin at all. An optional
``outline`` argument (:func:`build_silk`) closes this for the two
primitives with somewhere else to go: a refdes candidate and a pin-1 dot
candidate are now also rejected when they would land outside ``outline``
or inside its silk-to-edge margin (:func:`_silk_edge_margin_mm`, the same
``board_edge_clearance_vcut_mm`` figure :func:`precis.pcb.drc.
check_board_edge_clearance` grades copper against). The courtyard ring
has no candidates to choose among; since gr495293 it yields to the same
margin when it is settled (:func:`part_outlines`), and where its floor
offset still crosses the margin (an edge-mated connector) the stretch past
it is cut away as one gap. :func:`precis.pcb.drc.check_outline_containment`
and :func:`~precis.pcb.drc.check_silk_edge_clearance` still check every
draw at DRC time.
:class:`SilkResult` carries a structured ``census`` (one
:class:`SilkPlacement` per courtyard/pin-1/refdes item considered) plus
``dropped``/``relocated`` human-readable messages DERIVED from it — never
swallowed (task brief, verbatim), and never a second independent record of
the same fact (:class:`SilkPlacement`'s own docstring). A via
only obstructs the side(s) its plating barrel actually reaches
(:func:`_via_reaches_side`) — a blind/buried via on the far side of the
board from a given silk layer is not a hazard to it.

**Avoidance is GLOBAL, not per-instance (2026-08-29 fix).** Every
placed instance used to check its own silk against its own courtyard/
pin-1 tick and nothing else — a part's silk had no way to know any other
part had drawn anything at all, so a dense cluster produced overlapping,
illegible ink between NEIGHBOURING parts (the defect this fixed: 7 parts
in a ~25x6mm strip, with the bottom-most part's courtyard, pin-1 tick
AND label all dropped by the time its turn came, purely because it went
last against an empty obstacle set every earlier part also saw empty).
Fixed by making ``reserved`` — the SAME board-level obstacle list
:func:`build_fiducials`/:func:`build_title_block` already feed —
the ONE obstacle mechanism every check reads, and growing it as this
function goes: each side's obstacle list starts at ``pads + that side's
via annuli + the IR's mounting holes + the caller's reserved geometry``
and every tick/
refdes-label that survives its own check is folded straight back into
that SAME list (via :func:`obstacle_from_bbox`, a conservative
axis-aligned bounding box of what was just drawn — never under-flagging,
same idiom :func:`_segment_box` already uses) before the NEXT check
runs, so every part after it checks against everything committed so far
— one growing set, not a per-instance one plus a separate cross-part
one. Outlines are the exception (gr495393): they are settled before any
other silk and every mark checks them exactly, so they never join the
list as solid boxes — a box that once cut a neighbour's outline into
pieces.

**Order is instance-independent: natural refdes order, not array
index.** Once avoidance is global, whichever part is processed FIRST
gets the least-obstructed pick of its candidate spots and whichever goes
LAST gets whatever survives — so the processing order is a real,
visible decision, not an accident of whatever order ``ir.instance_refdes``
happens to carry (itself just upstream netlist/DB iteration order, not
anything spatial or meaningful). This function sorts instances by
NATURAL refdes order (``C1, C3, C4, C5, C9, C14, D2`` — numeric-aware,
the order a human reads a BOM in, never raw string order which would
put "C10" before "C2") specifically so who wins a contested spot is
predictable and reproducible from the refdes alone, independent of
upstream row order. Combined with board-level ``reserved`` geometry
seeded BEFORE the first instance runs (fiducials and the title block
always win), the full sequence is one deterministic chain: reserved seed,
then instances in natural-refdes order — never two independently-ordered
passes.

The per-side pad/via/committed-silk obstacle list is indexed in 5 mm
board-space buckets for candidate checks. It remains insertion ordered:
the index only rejects obstacles whose bounding boxes are too far away,
so the exact checks see the same candidates and first-hit diagnostics.
Moving a label rebuilds the index rather than leaving stale buckets.

**Body outlines are exempt from that order.** Predictable is not the same
as right, and one class of contest should never have been settled by
refdes at all: a pin-1 dot or a refdes label committed early could land
where a part processed LATER needed to draw its courtyard, and the later
part lost the whole outline. Every instance's courtyard ring is therefore
resolved BEFORE the loop (``courtyard_ring``), and both marks yield to all
of them — so an outline outranks a label whichever way round the two parts
happen to sort. An outline is the larger and far less relocatable mark;
a dot has 8 directions and 3 distances still to try. Measured on the 40mm
fixture when the dot became the primary marker: without this, R3 and U3
each lost their entire courtyard to a neighbour's mark.

**A row of identical parts shares one label spot.** Greedy per-part
placement gives the end of a column the one spot its neighbours block for
everyone else (nothing sits below the last part, so it labels below while
the rest label to the right): heater-base-test, 2026-10-02, a column of
0402s at 1.38 mm pitch. Before the loop, :func:`_aligned_label_groups`
finds rows and columns of parts with the same board-frame courtyard, and
the first spot that clears every member (against the pre-silk obstacles,
every courtyard, and each other) becomes that group's first choice. A
member whose shared spot was taken by silk committed earlier falls back on
its own; nothing else about the order changes.

**A pin-1 tick never survives alone (2026-08-29 decision).** The tick is
a corner-cut of the courtyard outline — it has no meaning except as an
annotation ON that outline. When the courtyard itself is dropped (global
obstacle collision), drawing the tick anyway leaves a small L-shaped mark
with nothing around it to say what it is: on a dense board it reads as
stray ink or a pad defect, not a pin-1 marker (measured: C3 on the
7-part reference cluster kept exactly this after losing its courtyard
AND its label). Deliberately tied together: a courtyard that survives is
a prerequisite for even attempting its own tick, reported in ``dropped``
same as any other suppression, never silently paired with a courtyard
that isn't there.

That reasoning is about the TICK only — the DOT survives (gr503436,
Reto 2026-10-22: "keep the pin-1 dot when the courtyard is dropped;
assemblers rely on it"). A dot sits outside the courtyard beside pin 1's
own land, so it still points at real geometry when the outline is gone,
and it is a polarity marker, not decoration. So a dropped courtyard now
means: still attempt the dot (against the same pads, vias, silk and
other parts' outlines as always, and still outside the part's own body
region), and only the corner-tick fallback is withheld — the tick is the
mark that is meaningless without the outline it is a cut of. A part
whose courtyard drops AND whose every dot candidate is blocked still
gets no mark, reported in ``dropped`` as before.

**Provenance, for a future ingested-silk merge.** Every draw this module
emits carries ``"source": "synthesized"`` (an extra key both
:func:`precis.pcb.gerber.silkscreen_gerber` and
:mod:`precis.pcb.svg`'s ``_stroke_el`` already ignore — neither reads
anything but ``width_mm``/``segments``). A refdes label derived from a
land-pattern BOUND is cosmetic, not a fabrication hazard the way a
synthesized pad is (:func:`precis.pcb.gerber.export_fab` refuses those
outright) — but real silk primitives already sit unparsed in
``part_footprints.raw`` (EasyEDA ``TEXT``/``CIRCLE``/``ARC``/
``SOLIDREGION``, a separate future slice — see
``docs/backlog/pcb-engine-plan.md``), and a merged board should be able to
tell a generated refdes apart from an ingested outline on the same side.
This flag is that seam, not a promise anything reads it yet.

**Per-instance board side in the IR (gr341516, landed).**
:class:`precis.pcb.ir.PcbIR` now carries it —
:attr:`~precis.pcb.ir.PcbIR.inst_bottom`, read off ``pcb_instances.layer``
at :func:`precis.pcb.ir.from_graph` time — and
:func:`precis.pcb.realize.pads_for_ir` flashes each pad on the real outer
layer for its own instance's side instead of a single hardcoded one.
``instance_sides`` here stays a SEPARATE lookup this module accepts
(refdes -> ``'top'``/``'bottom'``, the same string convention
:func:`precis.pcb.padplace.is_bottom_instance` uses) rather than reading
the IR directly — this module builds silk from the design's own
``instances`` list, not an IR object — with an instance missing from it
defaulting to ``'top'``.

**Two things every real board has, that a settled IR does not carry at
all: fiducials and an identification (title) block.** Both are
BOARD-level, not per-instance, so neither lives in the per-instance loop
above — see :func:`build_fiducials` and :func:`build_title_block`.

- A fiducial is an optical alignment target a pick-and-place machine
  locates the board with — copper, so it needs a mask opening AND has to
  stay clear of a pour the same way a real pad does (:mod:`precis.pcb.
  planes`' antipad mechanism doesn't know about ``pcb_features`` at all;
  see :func:`build_fiducials`'s own docstring for exactly what a caller
  still has to wire up for that guarantee to reach a poured layer).
  Conventionally they belong on ``pcb_features`` (``ftype`` is already
  the board-level discriminator ``outline``/``mounting_hole`` use) — but
  :func:`precis.pcb.session.outline_from_features` reads ONLY
  ``ftype == 'outline'`` and silently drops every other feature type, so
  a caller that stores an ``ftype='fiducial'`` row and expects it back
  out of THAT function gets nothing. This module never round-trips
  through it: :func:`build_fiducials` computes fiducial geometry
  directly from the outline + real pads, the same "regenerate, don't
  hand-author" discipline every other draw in this module already
  follows.
- A title block is silk text (:mod:`precis.pcb.stroke_font`) naming the
  board, so it is physically legible on the fabricated part — never a
  ``datetime.now()`` call (this renderer's determinism tests require two
  renders of the same input to be byte-identical; the caller must pass a
  ``date`` string that ITSELF came from the model, or omit it — see
  :func:`build_title_block`).
"""

from __future__ import annotations

import functools
import itertools
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, cast

from shapely import union_all as _shapely_union_all
from shapely import voronoi_polygons as _shapely_voronoi_polygons
from shapely.geometry import LineString as _ShapelyLineString
from shapely.geometry import MultiPoint as _ShapelyMultiPoint
from shapely.geometry import Point as _ShapelyPoint
from shapely.geometry import Polygon as _ShapelyPolygon
from shapely.geometry.polygon import orient as _shapely_orient
from shapely.ops import linemerge as _shapely_linemerge
from shapely.ops import unary_union as _shapely_unary_union

from precis.pcb import stroke_font
from precis.pcb.capabilities import CapabilityRow, design_value
from precis.pcb.geom import (
    convex_polygons_overlap,
    dist_point_to_segment,
    point_in_polygon,
)
from precis.pcb.gerber import DEFAULT_SILK_WIDTH_MM, SOLDERMASK_EXPANSION_MM
from precis.pcb.ir import COURTYARD_MITRE_LIMIT, PcbIR, instance_courtyard_polygon
from precis.pcb.landpattern import place_points

Point = tuple[float, float]

#: Typical fab-default refdes silk height (mm) — the same status as
#: :data:`precis.pcb.gerber.DEFAULT_SILK_WIDTH_MM`: a documented default,
#: not a courtyard-class "tuned to nothing" constant (this sizes TEXT, a
#: cosmetic/typographic choice every silk generator makes, not a part's
#: physical footprint).
DEFAULT_REFDES_HEIGHT_MM = 1.0

#: How many rings out the refdes ladder walks, and how many directions it
#: tries on each — 1 + 3x12 = 37 placements per label.
#:
#: **These are measured, not chosen.** The ladder was six fixed spots
#: (centred, above, below, left, right, and one far fallback) until
#: 2026-08-30, and on the ESP32-C3 reference board EVERY remaining
#: ``silk_missing`` refdes finding turned out to be an artifact of it: 8 of
#: 8 there and 16 of 16 on the squeezed 40mm fixture were recovered by a
#: ring sweep, so the room existed and nothing was looking for it. The
#: knee is here: 2x8 leaves one finding on the 40mm board, 4x16 recovers
#: nothing 3x12 does not. Re-measure both fixtures before changing either
#: number — a label relocated to a NEW spot becomes an obstacle for every
#: part processed after it, so the effect is not confined to labels (at
#: 2x8 the residual finding is a courtyard, not a refdes).
#:
#: **Re-measured 2026-10-17 (gr495393/gr495394), now 4x16.** Outlines grew
#: two line widths and labels keep two line widths off every outline, so
#: the room a label needs moved out. Refdes drops on the routed nano / nano
#: r2 fixtures: 3x12 38/59, 4x12 36/55, 3x16 38/55, 4x16 35/51, 5x16 32/50
#: (old geometry at 3x12: 36/52). 4x16 is the smallest that is no worse
#: than before on both; 5x16 floats labels five outline-reaches away.
#:
#: **Now 5x16 (gr496552).** Reto 2026-10-18: "better algo should not
#: require any label missing. 100% is our quest" — a label one more ring
#: out is still legible and attributable; a dropped one is a DRC error.
#: The 5 mm step cap (:data:`_LADDER_STEP_CAP_MM`) keeps ring 5 from
#: floating the way an uncapped reach did.
_REFDES_RINGS = 5
_REFDES_DIRECTIONS = 16

#: The most one ladder ring steps out past the one before it, mm. A ring
#: steps by the outline's own reach, so on a large part ring 4 sat four
#: part-widths away (83 mm from a 33 mm part's centre), a label nobody
#: could tie to its part. Measured 2026-10-17 on the nano / nano r2
#: fixtures: a 5 mm cap drops no more labels than none (35/51); 3 mm
#: drops 38/53.
_LADDER_STEP_CAP_MM = 5.0

#: How far off an axis a direction must lean before the text stops being
#: centred on that axis and starts being pushed to one side of the anchor.
#: A label offset UP wants its baseline on the anchor and its glyphs
#: centred horizontally; one offset up-and-right wants to start at the
#: anchor and run away from the part. At :data:`_REFDES_DIRECTIONS` = 16
#: the components are 0, 0.38, 0.71, 0.92 and 1.0, so any deadband in
#: (0, 0.38) gives the same answer — the value is a readable middle, not a
#: tuned threshold.
_REFDES_ALIGN_DEADBAND = 0.25


def _angular_dist(a: float, target: float) -> float:
    """Circular distance between two angles (radians), independent of
    which side of the 0/2*pi wrap either one falls on — a plain ``abs(a -
    target)`` is wrong for a target near either end of the ``[0, 2*pi)``
    range :func:`_refdes_candidates` sweeps its angles over."""
    d = abs(a - target) % (2.0 * math.pi)
    return min(d, 2.0 * math.pi - d)


def _refdes_candidates(
    n_rings: int, per_ring: int
) -> tuple[tuple[float, float, str, str, str], ...]:
    """Candidate refdes placements, tried in order, as ``(dx_units,
    dy_units, h_align, v_align, label)`` in the BOARD frame, relative to the
    part origin (module docstring, "Read from one side"). ``h_align`` is
    for top-side text; :func:`build_silk` swaps left/right for mirrored
    bottom-side glyphs.

    ``dx``/``dy`` are scaled by the courtyard's reach IN THAT DIRECTION
    plus a gap before use (:func:`_courtyard_support_mm`), and their
    MAGNITUDE is the ring number, so ring 2 sits twice as far out as ring
    1 — the same meaning the old hand-written ``(0, -2)`` fallback had —
    except that no ring steps more than :data:`_LADDER_STEP_CAP_MM` past
    the one before it (:func:`_label_candidates`).

    Order is each ring outward, and within a ring the directions nearest
    the bottom-left diagonal (angle -3*pi/4 — gr496552: the ladder walks
    outward from the bottom-left bearing, and none of its rungs is centred
    — a label on the part origin prints under the package and the
    assembled part hides it; the centred bottom EDGE leads the named-spot
    pass instead, :data:`LABEL_SPOT_ORDER` since 2026-10-30), then nearest
    BELOW (-y, angle -pi/2),
    then everything else. A single "up is where a reader expects a
    refdes" preference is superseded by this side-consistency rule: a
    repeated part in an array relocates its label to the SAME relative
    spot every time, which is what actually reads as tidy across a
    populated board (task brief, verbatim, 2026-09-01). Ties (a
    direction equidistant from both preferred sides) break toward the
    smaller angle, so the sweep is deterministic rather than
    dict-ordered.

    Landing outside a
    part's courtyard is still an unambiguous, readable label; more than a
    dropped one is (task brief: "prefer moving the label to shrinking the
    outline"). A candidate is rejected against a pad/via, board-level
    ``reserved`` geometry, or silk ANY part — including this same
    instance's own courtyard and pin-1 mark — has already committed, all
    folded into the SAME growing obstacle list (module docstring's
    "Avoidance is GLOBAL")."""
    out: list[tuple[float, float, str, str, str]] = []
    for ring in range(1, n_rings + 1):
        angles = [2.0 * math.pi * k / per_ring for k in range(per_ring)]
        # Nearest bottom-left (angle -3*pi/4, i.e. 5*pi/4) first, then
        # nearest BELOW (angle -pi/2); the remaining tie breaks toward
        # the smaller angle so the order never depends on float ties.
        angles.sort(
            key=lambda a: (
                round(_angular_dist(a, 5.0 * math.pi / 4.0), 9),
                round(_angular_dist(a, -math.pi / 2.0), 9),
                a,
            )
        )
        for theta in angles:
            du, dv = math.cos(theta) * ring, math.sin(theta) * ring
            h = (
                "left"
                if du > _REFDES_ALIGN_DEADBAND * ring
                else "right"
                if du < -_REFDES_ALIGN_DEADBAND * ring
                else "center"
            )
            v = (
                "baseline"
                if dv > _REFDES_ALIGN_DEADBAND * ring
                else "top"
                if dv < -_REFDES_ALIGN_DEADBAND * ring
                else "middle"
            )
            out.append((du, dv, h, v, f"ring {ring} at {math.degrees(theta):.0f}deg"))
    return tuple(out)


_CANDIDATES: tuple[tuple[float, float, str, str, str], ...] = _refdes_candidates(
    _REFDES_RINGS, _REFDES_DIRECTIONS
)


def _label_gap_mm(stroke_width_mm: float) -> float:
    """Outline CENTRELINE to a refdes label's text box: half the outline's
    stroke, :data:`OUTLINE_GAP_STROKES` widths of clear board, and half a
    glyph stroke (glyph centrelines reach the box edge). gr495394: a label
    keeps the same two line widths off an outline that two outlines keep
    off each other."""
    return (1.0 + OUTLINE_GAP_STROKES) * stroke_width_mm


def refdes_label_slot_mm(
    clearance_mm: float,
    *,
    height_mm: float = DEFAULT_REFDES_HEIGHT_MM,
    stroke_width_mm: float = DEFAULT_SILK_WIDTH_MM,
) -> float:
    """The band between a part's courtyard hull and the nearest
    non-silk obstacle (board furniture) that a refdes label hung ABOVE or
    BELOW the outline needs in order to fit: the outline's own target
    offset off the hull (``clearance_mm`` plus :data:`OUTLINE_GAP_STROKES`
    widths, :func:`outline_offsets_mm`), the label's gap off the outline
    (:func:`_label_gap_mm`), the label's text height, and a silk clearance
    beyond it. A caller keeping
    furniture off courtyards (``handlers/pcb.py::_board_furniture``) must
    leave at least this much, else a part with nothing free on its other
    sides loses its label (``silk_missing``) — ewod-dogfood-6's bottom
    sink sat 1.385 mm from a furniture rect against a ~1.75 mm need."""
    return (
        height_mm
        + 2.0 * clearance_mm
        + OUTLINE_GAP_STROKES * stroke_width_mm
        + _label_gap_mm(stroke_width_mm)
    )


#: A refdes label's spots around its part's outline, in try order
#: (gr495394, Reto 2026-10-17: "label outside white line, say to the bottom
#: left corner of the component. Unless unavailable." — superseded
#: 2026-10-30, Reto: "center labels near part and pick a pref direction
#: (below center)": the centred bottom edge leads, corners follow, "top"
#: stays last). Board frame, read as the board is viewed from the top on
#: either side. A corner spot sits just outside that side of the outline,
#: flush with the outline's own left or right edge; a side spot is centred
#: on that side. The skill precis-pcb-fab-help documents this order; keep
#: the two in step.
LABEL_SPOT_ORDER: tuple[str, ...] = (
    "bottom",
    "bottom-left",
    "bottom-right",
    "top-left",
    "top-right",
    "left",
    "right",
    "top",
)


def _outline_spot_candidates(
    box_board: list[Point], stroke_width_mm: float
) -> tuple[tuple[Point, str, str, str], ...]:
    """The :data:`LABEL_SPOT_ORDER` spots as ``(anchor, h_align, v_align,
    spot)``, relative to the part origin in the board frame, each text box
    :func:`_label_gap_mm` clear of ``box_board``'s bounding box.

    Keyed to the outline's LITERAL bounding box, never
    :func:`_courtyard_support_mm`'s directional support (the radial
    ladder's helper): support drifts off the true edge on an asymmetric
    outline, which is where "flush with the left edge" matters most.
    ``v_align="top"`` hangs a label below the outline (cap at the anchor,
    growing down); ``"baseline"`` stands one on top (this alphabet has no
    descenders, so the baseline is the box's bottom). Empty for a
    courtyard-less instance."""
    if not box_board:
        return ()
    xs = [p[0] for p in box_board]
    ys = [p[1] for p in box_board]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    xc, yc = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    g = _label_gap_mm(stroke_width_mm)
    spots = {
        "bottom-left": ((x0, y0 - g), "left", "top"),
        "bottom-right": ((x1, y0 - g), "right", "top"),
        "top-left": ((x0, y1 + g), "left", "baseline"),
        "top-right": ((x1, y1 + g), "right", "baseline"),
        "left": ((x0 - g, yc), "right", "middle"),
        "right": ((x1 + g, yc), "left", "middle"),
        "top": ((xc, y1 + g), "center", "baseline"),
        "bottom": ((xc, y0 - g), "center", "top"),
    }
    return tuple((*spots[name], name) for name in LABEL_SPOT_ORDER)


@dataclass(frozen=True, slots=True)
class SilkPlacement:
    """One census row per silk item :func:`build_silk` considered — a
    refdes label, a courtyard outline, or a pin-1 tick (the three
    per-instance silk primitives the module docstring's "Per placed
    instance, three kinds of silk" section names). **The structured
    record ``dropped``/``relocated`` are now DERIVED from** (see
    :func:`_prose_from_census`, called at the bottom of :func:`build_silk`
    itself) — this subsystem's own recurring, named defect is one rule
    (here, "what silk got dropped and why") implemented at two
    independently-maintained call sites that then drift apart, and a
    human-readable prose tuple built ALONGSIDE a structured record, rather
    than FROM it, is exactly that shape. :mod:`precis.pcb.drc`'s
    ``check_silk_missing``/``check_silk_printability`` read this census
    directly — a DRC rule that had to re-derive "what got dropped" by
    re-parsing prose strings would be a second implementation of the same
    rule, not a consumer of the first.

    ``kind`` is one of ``"refdes"`` (the reference-designator label),
    ``"courtyard"`` (the body outline), or ``"pin1"`` (the corner tick).
    ``outcome`` is ``"placed"`` (drawn, no relocation needed —
    ``"relocated"`` below is reserved for the refdes label's own
    multi-candidate ladder; a courtyard/pin-1 tick has no such ladder, only
    placed-or-dropped), ``"relocated"`` (a refdes label landed on a
    candidate other than the default bottom-left one), ``"dropped"``
    (never drawn at all — the fact :func:`precis.pcb.drc.
    check_silk_missing` turns into a DRC error), or, ``kind="pin1"`` only,
    ``"not_applicable"`` (never ATTEMPTED — an unpolarized R/C/L/FB, see
    the module docstring's "Not every part needs one" — deliberately NOT
    ``"dropped"``, so :func:`~precis.pcb.drc.check_silk_missing` never
    turns a policy decision into a DRC error).

    ``reason`` is populated whenever ``outcome`` is not ``"placed"`` — the
    SAME text a human-readable ``dropped``/``relocated`` message carries,
    minus its own ``"{refdes}: "`` prefix (this dataclass already carries
    ``refdes`` as its own field, so repeating it inside ``reason`` would be
    a second definition of the same fact, the identical class of drift this
    dataclass exists to close).

    ``stroke_width_mm`` is the pen width this item was drawn (or, for a
    dropped item, would have been drawn) with — always populated, the
    field :func:`precis.pcb.drc.check_silk_printability` reads against a
    fab's declared minimum printable silk width. ``height_mm`` is the text
    cap height an item was drawn at — populated ONLY for ``kind="refdes"``
    (a courtyard/pin-1 tick is a stroke, not text; "height" carries no
    legibility meaning for either one)."""

    refdes: str
    kind: str  # "refdes" | "courtyard" | "pin1"
    side: str  # "top" | "bottom"
    outcome: str  # "placed" | "relocated" | "dropped" | "not_applicable" (pin1 only)
    reason: str | None = None
    stroke_width_mm: float = 0.0
    height_mm: float | None = None
    #: Where a DRAWN refdes label sits: the bottom-left corner of its text
    #: box in board mm (+Y up) and the glyph angle (always 0 — "read from
    #: one side"). Populated only for ``kind="refdes"`` with outcome
    #: ``placed``/``relocated``; an export that writes the designator as
    #: editable text (EasyEDA ``ATTR``, pcb-epro-export slice 2e) anchors
    #: it here instead of at the part origin, so the retypeable label lands
    #: where the stroked one would have printed.
    x_mm: float | None = None
    y_mm: float | None = None
    angle_deg: float = 0.0


def _prose_from_census(
    census: list[SilkPlacement],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(dropped, relocated)`` human-readable prose, DERIVED from
    ``census`` — the ONE place either tuple is assembled (see
    :class:`SilkPlacement`'s own docstring for why "derived, not built
    alongside" is load-bearing here). Each message is ``"{refdes}:
    {reason}"`` — exactly the format every pre-census caller of
    :func:`build_silk` already parses (e.g. ``"U3" in msg`` in this
    module's own tests), so this refactor changes WHERE the string is
    assembled, never its shape."""
    dropped = tuple(f"{c.refdes}: {c.reason}" for c in census if c.outcome == "dropped")
    relocated = tuple(
        f"{c.refdes}: {c.reason}" for c in census if c.outcome == "relocated"
    )
    return dropped, relocated


@dataclass(frozen=True, slots=True)
class SilkResult:
    """``draws`` is exactly :mod:`precis.pcb.gerber`'s
    ``model["silkscreen"]`` shape (``{"top": [<draw>, ...], "bottom": [...]}``,
    see that module's docstring for ``<draw>``). ``census`` is one
    :class:`SilkPlacement` per courtyard/pin-1/refdes item this build
    considered — the structured record. ``dropped``/``relocated`` are
    human-readable — never silently swallowed — and, as of this dataclass
    gaining ``census``, DERIVED from it (:func:`_prose_from_census`), not
    built independently alongside it."""

    draws: dict[str, list[dict[str, Any]]]
    census: tuple[SilkPlacement, ...] = ()
    dropped: tuple[str, ...] = ()
    relocated: tuple[str, ...] = ()


# ─────────────────────────────────────────────────────────────────────
# pure 2D overlap primitives — no shapely; a rotated text/outline box
# against an axis-aligned circle/rect pad is exactly SAT + point-to-
# polygon distance (both from :mod:`precis.pcb.geom`), both a few lines.
# ─────────────────────────────────────────────────────────────────────
def _polygon_overlaps_circle(poly: list[Point], center: Point, radius: float) -> bool:
    if radius <= 0:
        return point_in_polygon(center, poly)
    if point_in_polygon(center, poly):
        return True
    n = len(poly)
    return any(
        dist_point_to_segment(center, poly[i], poly[(i + 1) % n]) <= radius
        for i in range(n)
    )


def _pad_rect_polygon(pad: dict[str, Any]) -> list[Point]:
    x, y = float(pad["x"]), float(pad["y"])
    w = float(pad["w"])
    h = float(pad.get("h", pad["w"]))
    return [
        (x - w / 2, y - h / 2),
        (x + w / 2, y - h / 2),
        (x + w / 2, y + h / 2),
        (x - w / 2, y + h / 2),
    ]


def _mask_opening(pad: dict[str, Any], expansion_mm: float) -> dict[str, Any]:
    """A pad widened to its soldermask OPENING — ``expansion_mm`` per
    side, the same swell :func:`precis.pcb.gerber.soldermask_gerber`
    writes the mask film with.

    **This is the shape silk actually has to clear, and checking against
    bare copper is an under-check.** Every clearance test in this module
    used to run against the pad outline, leaving all of them
    ``expansion_mm`` optimistic: silk laid inside the opening but outside
    the copper still prints onto solderable metal, and a fab either
    scrapes it or ships an unwettable pad.

    A via is deliberately NOT put through this: vias are tented (that
    same writer's own docstring), so a via has no mask opening at all and
    its bare annulus is the real obstacle. ``w`` alone is widened where
    the dict carries no ``h`` — :func:`_pad_rect_polygon` and
    :func:`_box_overlaps_pad` both read ``h`` as defaulting to ``w``, so
    that stays a square/circle rather than becoming an implicit
    rectangle."""
    if expansion_mm <= 0.0:
        return pad
    out = dict(pad)
    out["w"] = float(pad["w"]) + 2.0 * expansion_mm
    if "h" in pad:
        out["h"] = float(pad["h"]) + 2.0 * expansion_mm
    return out


def soldermask_expansion_mm(capability: CapabilityRow | None) -> float:
    """This board's soldermask swell per side, off its capability row.

    Public, and read from BOTH ends of the same physical edge: the silk
    side (:func:`_mask_openings`, :func:`silk_clearance_mm`) and the mask
    film itself (``model["soldermask_expansion_mm"]``, which
    :func:`precis.pcb.gerber.soldermask_gerber` draws the openings with).
    A board whose mask was drawn at one expansion while its silk was
    cleared for another has two numbers for one edge, and neither DRC nor
    a render can see the disagreement — so there is one function."""
    return design_value(
        capability, "soldermask_expansion_mm", fallback=DEFAULT_SOLDERMASK_EXPANSION_MM
    )


def _mask_openings(
    pads: list[dict[str, Any]], capability: CapabilityRow | None
) -> list[dict[str, Any]]:
    """Every pad widened to its mask opening, at this board's expansion —
    the ONE seam each silk builder turns caller-supplied pad geometry into
    the obstacle set it may check against. Three builders draw silk
    (:func:`build_silk`, :func:`build_title_block`, :func:`build_sn_patch`)
    and one rule applied at only some of them is this subsystem's own
    named recurring defect.

    :func:`build_fiducials` is deliberately NOT a caller: it places COPPER
    dots, so the clearance it owes a pad is copper-to-copper — the mask
    opening is the wrong question there, not merely a stricter one."""
    expansion_mm = soldermask_expansion_mm(capability)
    return [_mask_opening(pad, expansion_mm) for pad in pads]


def _box_overlaps_pad(box: list[Point], pad: dict[str, Any]) -> bool:
    if str(pad.get("shape") or "circle") == "circle":
        cx, cy = float(pad["x"]), float(pad["y"])
        return _polygon_overlaps_circle(box, (cx, cy), float(pad["w"]) / 2.0)
    return convex_polygons_overlap(box, _pad_rect_polygon(pad))


def _segment_box(a: Point, b: Point, half_width: float) -> list[Point]:
    """A stroke segment thickened to a rectangle of ``half_width`` on
    each side, extended by ``half_width`` at both ends — a conservative
    (never-under-flagging) square-cap approximation of the Gerber
    writer's true round-cap stroke."""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length = math.hypot(dx, dy)
    ux, uy = (dx / length, dy / length) if length > 1e-9 else (1.0, 0.0)
    nx, ny = -uy, ux
    ex, ey = ux * half_width, uy * half_width
    ox, oy = nx * half_width, ny * half_width
    return [
        (ax - ex + ox, ay - ey + oy),
        (bx + ex + ox, by + ey + oy),
        (bx + ex - ox, by + ey - oy),
        (ax - ex - ox, ay - ey - oy),
    ]


def _stroke_hits(
    points: list[Point], pads: list[dict[str, Any]], stroke_width_mm: float
) -> dict[str, Any] | None:
    """The FIRST obstacle this stroke lands on, or ``None`` if it is
    clear. Returning the obstacle rather than a bool is what lets a drop
    say WHAT it hit (:func:`_obstacle_label`); a census row reading
    "overlaps a pad or via" sends its reader back to instrument a run
    before they can begin, which is the same complaint
    ``tests/test_pcb_fab_render_all_layers.py`` already records against a
    DRC message that named a rule and a count."""
    half = stroke_width_mm / 2.0
    for a, b in itertools.pairwise(points):
        box = _segment_box(a, b, half)
        for pad in pads:
            if _box_overlaps_pad(box, pad):
                return pad
    return None


def _stroke_overlaps_any_pad(
    points: list[Point], pads: list[dict[str, Any]], stroke_width_mm: float
) -> bool:
    return _stroke_hits(points, pads, stroke_width_mm) is not None


#: How finely a courtyard edge is walked when clipping it around an
#: obstacle (:func:`_clip_polyline`). This quantizes the GAP the clip
#: leaves, not the outline's own accuracy — a kept run always ends on a
#: sub-segment that cleared the obstacle with a full square cap, so a
#: coarser step only ever removes more ink, never less.
_CLIP_STEP_MM = 0.1

#: The shortest surviving run worth drawing, as a multiple of the stroke
#: width. Below this a "kept" piece is a dot, not a line: it reads as
#: debris rather than as part of an outline, and a fab's own registration
#: tolerance is the same order.
_CLIP_MIN_RUN_STROKES = 4.0

#: How much of a courtyard's own perimeter must survive clipping before
#: the broken outline is still worth drawing. Below it the remains no
#: longer read as a body outline and the courtyard is dropped whole — the
#: honest outcome, and the one :func:`precis.pcb.drc.check_silk_missing`
#: reports. Not zero: "draw whatever is left" would turn a genuinely
#: buried part into scattered ink and report success.
_COURTYARD_MIN_KEPT_FRACTION = 0.5

#: How many pieces a clipped courtyard outline may shatter into before
#: the remains are dropped whole instead. Every run clears
#: :data:`_CLIP_MIN_RUN_STROKES`, so past this cap the pieces are long
#: enough to draw but no longer read as ONE outline — on the board they
#: look like zigzag fragments wandering between obstacles (gr520792:
#: a courtyard clipped by an overlapping neighbour's copper came out as
#: confetti). Cleanly overlapped → clipped once or twice, or the
#: courtyard is dropped honestly and ``silk_missing`` reports it.
_COURTYARD_MAX_PIECES = 4


def _same_point(a: Point, b: Point) -> bool:
    """Two points that came out of the SAME arithmetic, compared for
    identity rather than proximity. Both callers below feed this endpoints
    that were either copied from one another or computed by the identical
    expression, so the tolerance guards float representation only — it is
    deliberately far tighter than any geometric feature on a board, so it
    can never mistake two genuinely distinct vertices for one."""
    return math.hypot(b[0] - a[0], b[1] - a[1]) < 1e-9


def _clip_polyline(
    points: list[Point], obstacles: list[dict[str, Any]], stroke_width_mm: float
) -> tuple[list[list[Point]], float, float]:
    """Break ``points`` into the maximal runs that clear every obstacle,
    returning ``(runs, kept_mm, total_mm)``.

    **Why break rather than drop.** A body outline that passes through one
    fan-out via is not an unrenderable outline; it is an outline with a
    gap in it, which is what every real silk generator emits and what a
    fab would have trimmed to anyway. Dropping the whole ring instead
    threw away the other 95% of a part's outline — and, via
    :func:`build_silk`'s "a pin-1 tick never survives alone" rule, its
    pin-1 marker with it (now the tick alone — gr503436 keeps the dot
    regardless; the module docstring carries the ruling). On the
    ESP32-C3 reference board that single
    policy accounted for most of the ``silk_missing`` population once
    courtyards became tight enough to pass NEAR a part's own vias instead
    of enclosing them.

    Runs carry across vertices, so a corner is not broken merely for
    being a corner. Sub-segments are walked at :data:`_CLIP_STEP_MM` and
    tested with the same square-capped :func:`_segment_box` every other
    check here uses. Runs shorter than :data:`_CLIP_MIN_RUN_STROKES`
    stroke widths are discarded as debris — their length still counts as
    removed, so the caller's kept fraction stays honest.

    **A closed ring is spliced across its own seam.** A courtyard arrives
    as a ring whose first and last point coincide, but the walk above is
    an OPEN one: the arc that happens to span that seam vertex comes out
    as two runs, the first starting there and the last ending there. They
    abut exactly, so nothing looks wrong in the numbers — but each run is
    drawn as its own polyline, so a seam landing on a CORNER loses the
    mitre join and shows a notch that no obstacle explains, and a seam
    stub shorter than ``min_run`` is deleted as debris, silently widening
    the neighbouring real gap. Measured on the 40mm fixture: 15 of 25
    clipped outlines carried a phantom break of the first kind. Splicing
    happens BEFORE the debris filter, which is the whole point — a stub
    that is too short alone is not too short once rejoined to the run it
    was always part of. Where the seam sits mid-gap there is nothing to
    splice and the ring is left alone.

    **A FULL stroke width of ink is tested, not the usual half.** Every
    other check here asks "does this stroke land on that obstacle", and a
    half-width box answers it. Clipping asks something different: a kept
    run deliberately ends as close to the obstacle as it legally can, so
    the margin it leaves IS the clearance. Several of these obstacles are
    other silk items, whose bounding box (:func:`obstacle_from_bbox`)
    bounds their CENTRELINE — their own ink reaches half a stroke further
    out. Half-width clipping therefore left neighbouring outlines running
    one half-stroke apart, which :func:`_stroke_crosses_stroke` correctly
    calls a collision. Testing a full width covers both halves."""
    half = stroke_width_mm
    min_run = stroke_width_mm * _CLIP_MIN_RUN_STROKES
    raw: list[list[Point]] = []
    current: list[Point] = []
    total = 0.0

    def flush() -> None:
        nonlocal current
        if len(current) >= 2:
            raw.append(current)
        current = []

    for a, b in itertools.pairwise(points):
        seg_len = math.hypot(b[0] - a[0], b[1] - a[1])
        total += seg_len
        n = max(1, math.ceil(seg_len / _CLIP_STEP_MM))
        for i in range(n):
            p = (a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n)
            q = (
                a[0] + (b[0] - a[0]) * (i + 1) / n,
                a[1] + (b[1] - a[1]) * (i + 1) / n,
            )
            box = _segment_box(p, q, half)
            if any(_box_overlaps_pad(box, pad) for pad in obstacles):
                flush()
                continue
            if not current:
                current = [p, q]
            else:
                current.append(q)
    flush()

    # Rejoin the arc that spans the ring's seam, before the debris filter
    # below can delete either half of it (see the docstring). Guarded on
    # the two halves actually REACHING the seam: if the walk was blocked
    # there, the seam sits inside a genuine gap and there is nothing to
    # rejoin.
    if (
        len(raw) >= 2
        and _same_point(points[0], points[-1])
        and _same_point(raw[0][0], points[0])
        and _same_point(raw[-1][-1], points[-1])
    ):
        raw[0] = raw[-1] + raw[0][1:]
        raw.pop()

    runs: list[list[Point]] = []
    kept = 0.0
    for run in raw:
        length = sum(
            math.hypot(q[0] - p[0], q[1] - p[1]) for p, q in itertools.pairwise(run)
        )
        if length >= min_run:
            runs.append(run)
            kept += length
    return runs, kept, total


def _near_obstacles(
    obstacles: list[dict[str, Any]], points: list[Point], margin_mm: float
) -> list[dict[str, Any]]:
    """The obstacles whose own bounding box comes within ``margin_mm`` of
    ``points``' bounding box — a broad phase for :func:`_clip_polyline`,
    which is otherwise every sub-segment against every obstacle on the
    side. Conservative by construction (a bbox contains its shape), so it
    can only ever admit an obstacle that a later exact test rejects,
    never exclude one that would have hit."""
    if not points:
        return []
    x0, y0, x1, y1 = _bbox(points)
    x0, y0, x1, y1 = x0 - margin_mm, y0 - margin_mm, x1 + margin_mm, y1 + margin_mm
    if isinstance(obstacles, _SideObstacles):
        return obstacles.near(x0, y0, x1, y1)
    out = []
    for pad in obstacles:
        px, py = float(pad["x"]), float(pad["y"])
        hw = float(pad["w"]) / 2.0
        hh = float(pad.get("h", pad["w"])) / 2.0
        if px + hw >= x0 and px - hw <= x1 and py + hh >= y0 and py - hh <= y1:
            out.append(pad)
    return out


class _SideObstacles(list[dict[str, Any]]):
    """Insertion-ordered obstacles with a conservative 5 mm grid broad phase.

    The silk pass appends marks as it commits them; label repair also replaces
    list slices. Both operations maintain the grid, while ``near`` preserves
    original list order so first-hit diagnostics and candidate choices do not
    change with board size.
    """

    _pitch = 5.0

    def __init__(self, items: list[dict[str, Any]]) -> None:
        super().__init__(items)
        self._cells: dict[tuple[int, int], list[int]] = {}
        for i, item in enumerate(self):
            self._insert(i, item)

    def _insert(self, i: int, item: dict[str, Any]) -> None:
        x, y = float(item["x"]), float(item["y"])
        hw = float(item["w"]) / 2.0
        hh = float(item.get("h", item["w"])) / 2.0
        for ix in range(
            math.floor((x - hw) / self._pitch), math.floor((x + hw) / self._pitch) + 1
        ):
            for iy in range(
                math.floor((y - hh) / self._pitch),
                math.floor((y + hh) / self._pitch) + 1,
            ):
                self._cells.setdefault((ix, iy), []).append(i)

    def append(self, item: dict[str, Any]) -> None:
        self._insert(len(self), item)
        super().append(item)

    def replace_all(self, items: list[dict[str, Any]]) -> None:
        """Reindex after label repair moves or removes a committed mark."""
        super().__setitem__(slice(None), items)
        self._cells.clear()
        for i, item in enumerate(self):
            self._insert(i, item)

    def near(self, x0: float, y0: float, x1: float, y1: float) -> list[dict[str, Any]]:
        hits: set[int] = set()
        for ix in range(math.floor(x0 / self._pitch), math.floor(x1 / self._pitch) + 1):
            for iy in range(
                math.floor(y0 / self._pitch), math.floor(y1 / self._pitch) + 1
            ):
                hits.update(self._cells.get((ix, iy), ()))
        out = []
        for i in sorted(hits):
            pad = self[i]
            px, py = float(pad["x"]), float(pad["y"])
            hw = float(pad["w"]) / 2.0
            hh = float(pad.get("h", pad["w"])) / 2.0
            if px + hw >= x0 and px - hw <= x1 and py + hh >= y0 and py - hh <= y1:
                out.append(pad)
        return out


def _obstacle_label(pad: dict[str, Any]) -> str:
    """One obstacle, named the way a human reads a board: a component pad
    by refdes/pin, anything else by what it is and where. ``obstacle`` is
    the marker this module stamps on the non-pad entries it synthesizes
    (via annuli, and each committed silk item it folds back into the
    shared list) — without it every one of them reads as an anonymous
    rectangle and a courtyard dropped by its own part's fanout via is
    indistinguishable from one dropped by a neighbour."""
    where = f"({float(pad.get('x', 0.0)):.2f}, {float(pad.get('y', 0.0)):.2f})"
    marker = str(pad.get("obstacle") or "")
    if marker:
        return f"{marker} at {where}"
    refdes = str(pad.get("refdes") or "")
    if refdes:
        pin = str(pad.get("pin") or "")
        return f"pad {refdes}.{pin} at {where}" if pin else f"pad {refdes} at {where}"
    net = str(pad.get("net") or "")
    return f"pad on net {net!r} at {where}" if net else f"pad at {where}"


def _stroke_crosses_stroke(
    points_a: list[Point], points_b: list[Point], stroke_width_mm: float
) -> bool:
    """Do two INKED strokes (not just their centrelines) touch, both
    inflated by ``stroke_width_mm``? A narrow, same-instance-only
    complement to the solid-obstacle checks above: a courtyard can't be
    folded into ``side_obstacles`` as a filled shape before this
    instance's own pin-1 tick draws ON the outline's corner (see the
    call sites) -- but a glyph stroke still can't cross the courtyard's
    own border line, hollow or not. Reuses the exact
    SAT/segment-box machinery :func:`_stroke_overlaps_any_pad` already
    uses."""
    half = stroke_width_mm / 2.0
    for a1, b1 in itertools.pairwise(points_a):
        box1 = _segment_box(a1, b1, half)
        for a2, b2 in itertools.pairwise(points_b):
            box2 = _segment_box(a2, b2, half)
            if convex_polygons_overlap(box1, box2):
                return True
    return False


def _obstacle_sort_key(o: dict[str, Any]) -> tuple[float, float, float, float, str]:
    """Canonical order for the seed obstacle lists (gr498680): the same
    copper handed in in a different list order must seed the same
    obstacle set, so the checks that scan it (:func:`_stroke_hits` names
    the FIRST hit) can't ride on caller order."""
    return (
        float(o.get("x", 0.0)),
        float(o.get("y", 0.0)),
        float(o.get("w", 0.0)),
        float(o.get("h", o.get("w", 0.0))),
        str(o.get("obstacle") or o.get("refdes") or ""),
    )


def _via_pad(via: dict[str, Any]) -> dict[str, Any]:
    """A via's exposed annulus, reshaped into a circle-``pads`` dict --
    the SAME shape :func:`_box_overlaps_pad`/:func:`_polygon_overlaps_circle`
    already handle for a round SMD pad, so a via rides the existing
    pad-overlap machinery instead of a second geometry convention. Uses the
    outer plated diameter (``dia_mm``), not the drill: the annulus is
    exposed copper end to end, and silk printed over the drilled centre
    fares no better than silk over the ring around it."""
    return {
        "x": float(via["x"]),
        "y": float(via["y"]),
        "w": float(via["dia_mm"]),
        "obstacle": "via",
    }


def via_obstacles(vias: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Vias reshaped into the circle-pad obstacle dicts every clearance
    check in this module reads (:func:`_via_pad`) — public so the handler
    can fold the routed board's vias into :func:`build_title_block` /
    :func:`build_sn_patch` ``avoid`` lists. Both are placed at render
    time, AFTER routing, and neither used to see vias at all — measured
    on the nano fixture as two rows of GND stitch vias marching straight
    through the S/N patch, the one silk feature that exists to be
    WRITTEN on (a tented via is a bump under the Sharpie; round-3 review
    item 2). Silk dodging copper is this module's normal direction —
    the refdes ladder already avoids vias the same way."""
    return [_via_pad(v) for v in vias]


def _mounting_hole_obstacles(
    ir: PcbIR, capability: CapabilityRow | None
) -> list[dict[str, Any]]:
    """One circle obstacle per IR mounting hole, in the circle-pad dict
    shape every clearance check here reads (``_via_pad``'s convention).

    2026-10-29, taz-leaf: R26's refdes printed INSIDE the 5 mm zip-tie
    hole at (133,30) -- the silk pass never learned holes exist. The
    obstacle's diameter is the hole's widest feature -- the drill, the
    plated annulus, or the hardware envelope above the board
    (:attr:`~precis.pcb.ir.MountingHole.head_dia_mm`) -- the same
    ``max(drill, ring, head)`` semantics as
    ``precis.pcb.optimize._hole_keepout_radius_mm`` (reimplemented, not
    imported: silk sits below optimize in the pcb module layering, and
    the import would cycle through cost/drc), then widened by this
    board's soldermask expansion, the same swell every pad obstacle gets
    (:func:`_mask_opening`). THT hardware spans the board -- the screw
    head sits above it, the solder nut below -- so the caller seeds BOTH
    sides with these regardless of ``plated``; and a hole blocks every
    part's label, its ``part`` owner's included, the same policy pads
    follow."""
    expansion_mm = soldermask_expansion_mm(capability)
    return [
        _mask_opening(
            {
                "x": float(hole.x),
                "y": float(hole.y),
                "w": max(hole.drill_mm, hole.ring_dia_mm, hole.head_dia_mm),
                "obstacle": "mounting hole",
            },
            expansion_mm,
        )
        for hole in ir.mounting_holes
    ]


def _via_reaches_side(span: Any, layer_names: list[str]) -> tuple[bool, bool]:
    """``(reaches_top, reaches_bottom)`` for a via's plating barrel, off
    its ``span`` layer-NAME pair (:func:`precis.pcb.realize.to_gerber_model`'s
    shape — index 0 of the stackup is the top copper layer, the last index
    is the bottom, same convention :mod:`precis.pcb.svg`'s
    ``_via_layer_names`` and :mod:`precis.pcb.drc`'s twin already use). A
    through via (span covers index 0 to the last index) reaches both; a
    blind/buried via that never touches index 0 does not threaten TOP silk,
    and likewise for the bottom. A malformed/unrecognised span (schema
    drift, or a caller that didn't set one) reaches both, same
    never-under-flag default this module uses everywhere else."""
    if not layer_names:
        return True, True
    try:
        lo, hi = span[0], span[1]
        i0, i1 = layer_names.index(str(lo)), layer_names.index(str(hi))
    except (TypeError, IndexError, ValueError):
        return True, True
    top_idx, bottom_idx = min(i0, i1), max(i0, i1)
    return top_idx == 0, bottom_idx == len(layer_names) - 1


def _pad_reaches_side(pad: dict[str, Any], layer_names: list[str]) -> tuple[bool, bool]:
    """``(reaches_top, reaches_bottom)`` for a pad obstacle — the pad
    counterpart of :func:`_via_reaches_side` (gr520373 item 2: bottom
    silk was being checked against TOP pads, so a bottom part whose
    courtyard overlapped a top pad lost its silk to a collision that
    cannot exist — ink on one face never touches copper on the other).
    A pad's ``layer`` names the OUTER copper its land flashes on
    (stackup index 0 = top, last = bottom, same convention as the via
    span); ``"top"``/``"bottom"`` literals are accepted for fixtures
    that never named a stackup. A ``drill`` pad (THT) exposes its
    annulus on BOTH faces, like a through via. A pad with no usable
    layer signal at all reaches both — the same never-under-flag
    default the via path takes on a malformed span."""
    if pad.get("drill"):
        return True, True
    if not layer_names or layer_names[0] == layer_names[-1]:
        return True, True
    layer = str(pad.get("layer") or "")
    if layer in (layer_names[0], "top"):
        return True, False
    if layer in (layer_names[-1], "bottom"):
        return False, True
    return True, True


# ─────────────────────────────────────────────────────────────────────
# per-instance geometry
# ─────────────────────────────────────────────────────────────────────
def _draw(
    points: list[Point], width_mm: float, *, role: str, refdes: str
) -> dict[str, Any]:
    segments = [
        {"shape": "line", "start": list(points[i]), "end": list(points[i + 1])}
        for i in range(len(points) - 1)
    ]
    return {
        "width_mm": width_mm,
        "segments": segments,
        "source": "synthesized",
        "role": role,
        "refdes": refdes,
    }


def _courtyard_support_mm(courtyard: list[Point], du: float, dv: float) -> float:
    """How far the courtyard polygon reaches from the instance origin
    along ``(du, dv)`` — its support function in that direction.

    This is what replaced a single scalar "reach" once the courtyard
    stopped being a square: an elongated part reaches much further along
    its own long axis than across it, and a label offset by the larger of
    the two floats away in BOTH directions (which one radius forces) lands
    a connector's refdes a centimetre off its own body. ``0.0`` for a
    degenerate direction or an empty polygon, so a caller can add its gap
    unconditionally."""
    norm = math.hypot(du, dv)
    if norm <= 0.0 or not courtyard:
        return 0.0
    ux, uy = du / norm, dv / norm
    return max(x * ux + y * uy for x, y in courtyard)


def _pin1_id(ir: PcbIR, inst: int, pins: list[int]) -> int:
    for pid in pins:
        if str(ir.pin_label[pid]) == "1":
            return pid
    return min(pins)


#: How much of a courtyard EDGE each leg of the pin-1 tick runs back
#: along. 0.15 of the full edge is the same proportion the square
#: courtyard's ``reach * 0.3`` was — ``reach`` was a HALF-extent, and the
#: distinction is not cosmetic: reading it as 0.3 of the whole edge
#: doubles the tick, whose bounding box is then folded into the shared
#: obstacle list and shoulders this part's own refdes label off its
#: default spot (measured on the two-part fixture in
#: ``tests/test_pcb_silk.py``, by 4 micrometres).
_PIN1_TICK_EDGE_FRACTION = 0.15


def _towards(frm: Point, to: Point, dist_mm: float) -> Point:
    dx, dy = to[0] - frm[0], to[1] - frm[1]
    length = math.hypot(dx, dy)
    if length <= 1e-9:
        return frm
    return (frm[0] + dx / length * dist_mm, frm[1] + dy / length * dist_mm)


def _pin1_tick(
    pin_xy: Point, courtyard: list[Point], stroke_width_mm: float
) -> list[Point]:
    """A small tick cutting whichever courtyard VERTEX sits nearest pin
    1 (``pin_xy``, in the same frame as ``courtyard``) — a two-segment
    ``L`` running back along the two edges that meet there.

    Sized off the courtyard's own geometry, never an invented mm constant:
    :data:`_PIN1_TICK_EDGE_FRACTION` of the shorter of the two adjacent
    edges, floored so it stays visible on a tiny part and capped at HALF
    that edge so the two legs can never meet in the middle of a side and
    read as a second outline. On the rectangle a square courtyard always
    produced this is the same corner-cut ``L`` as before; on a real hull
    it follows whatever shape the part's own pads make.

    ``courtyard`` is the CLOSED drawn outline (:func:`part_outlines`'
    ring, board frame) — the repeated final point is dropped here so a
    vertex is not considered twice."""
    ring = courtyard[:-1] if len(courtyard) > 1 else list(courtyard)
    if len(ring) < 3:
        return []
    dx, dy = pin_xy
    k = min(
        range(len(ring)), key=lambda i: math.hypot(ring[i][0] - dx, ring[i][1] - dy)
    )
    corner = ring[k]
    before, after = ring[(k - 1) % len(ring)], ring[(k + 1) % len(ring)]
    span = min(
        math.hypot(before[0] - corner[0], before[1] - corner[1]),
        math.hypot(after[0] - corner[0], after[1] - corner[1]),
    )
    tick_len = min(
        max(span * _PIN1_TICK_EDGE_FRACTION, stroke_width_mm * 4.0), span / 2
    )
    if tick_len <= 0:
        return []
    return [
        _towards(corner, before, tick_len),
        corner,
        _towards(corner, after, tick_len),
    ]


#: The pin-1 dot's diameter as a multiple of the silk stroke width, and how
#: many directions/distances the ladder tries around pin 1 before giving
#: up. 3x the pen is the smallest blob that still reads as a deliberate
#: mark rather than a printing defect, and it is drawn as a wider STROKE
#: rather than a filled region so it rides the same width/printability
#: checks (:func:`precis.pcb.drc.check_silk_printability`) as every other
#: mark here — a wider pen clears the fab's minimum, never undercuts it.
_PIN1_DOT_STROKES = 3.0
_PIN1_DOT_DIRECTIONS = 8
_PIN1_DOT_DISTANCES = 3


def _pin1_dot_candidates(
    ir: PcbIR,
    pin1: int,
    stroke_width_mm: float,
    clearance_mm: float,
    courtyard: list[Point],
) -> list[tuple[Point, float]]:
    """Where a pin-1 DOT may sit, best spot first, as
    ``(centre, dot_diameter)`` pairs in the instance's local frame.

    **The corner tick's fallback, not its replacement.** A tick marks a
    courtyard CORNER, and when a plane fan-out via occupies that exact
    corner there is nothing to negotiate: shrinking the tick keeps the
    same blocked corner point, sliding it to another vertex would mark
    the wrong pin, and clipping a two-segment ``L`` leaves debris rather
    than a marker. A dot beside pin 1 is the other convention the
    industry already uses for exactly this situation, and it has somewhere
    to go — 8 directions at 3 distances, which a tick does not.

    Ordered outward-from-the-part-centre first: that is where a reader
    looks, and it keeps the dot off the part's body. A pin sitting AT the
    instance origin (a single-pad part) has no outward direction, so the
    sweep starts at +x rather than dividing by zero.

    **Sized off the instance's own courtyard reach, not pin 1's own pad
    half-extent.** Every ring used to floor its distance at
    ``hypot(pad_hw, pad_hh)`` -- pin 1's own land-pattern half-size, which
    has nothing to do with how big the PART is. On any part whose pin 1
    pad is small relative to its own hull (measured: every multi-pin part
    whose pin 1 isn't at a hull extremum -- connectors, regulators with a
    tab), that floor left every ring landing inside the part's own body.
    :func:`_courtyard_support_mm` is the same directional-reach helper the
    refdes ladder already uses for exactly this "how far does THIS part's
    hull actually reach" question, called here per direction the sweep
    tries (never a single shared radius, for the same reason a courtyard
    isn't a circle: an elongated part reaches further along its long axis
    than across it). This still isn't a GUARANTEE the candidate clears the
    hull on a concave courtyard -- the acceptance check in
    :func:`build_silk` is the actual correctness gate; this only sizes the
    search so it usually doesn't have to fall through every ring first."""
    dia = stroke_width_mm * _PIN1_DOT_STROKES
    dx, dy = float(ir.pin_dx[pin1]), float(ir.pin_dy[pin1])
    reach = math.hypot(dx, dy)
    base = math.atan2(dy, dx) if reach > 1e-9 else 0.0
    out: list[tuple[Point, float]] = []
    for step in range(_PIN1_DOT_DISTANCES):
        for k in range(_PIN1_DOT_DIRECTIONS):
            # 0, -1, +1, -2, +2 ... around `base`: the outward direction
            # first, then each symmetric pair. Which member of a pair
            # comes first is arbitrary and does not change which spot is
            # chosen when both are clear — only that both are tried.
            half = (k + 1) // 2
            sign = 1 if k % 2 == 0 else -1
            theta = base + sign * half * 2.0 * math.pi / _PIN1_DOT_DIRECTIONS
            ux, uy = math.cos(theta), math.sin(theta)
            # Clear of the courtyard's own reach in THIS direction, plus
            # the fab clearance and the dot's own radius — the first ring
            # is the closest the dot may legally sit, and each further
            # ring adds one diameter. The support is measured from the
            # instance ORIGIN while the dot offsets from pin 1, so pin
            # 1's own projection in this direction comes off first —
            # omitting it placed the mark ~|pin 1| beyond the hull edge
            # (gr520373: PV1's dot on the hanger ring, ~11mm off the
            # part, because its pin 1 sits at local x=10.7).
            first = (
                max(
                    _courtyard_support_mm(courtyard, ux, uy) - dx * ux - dy * uy,
                    0.0,
                )
                + clearance_mm
                + dia / 2.0
            )
            radius = first + step * dia
            out.append(((dx + ux * radius, dy + uy * radius), dia))
    return out


#: Null fallbacks for the two looked-up terms of :func:`silk_clearance_mm`
#: — used ONLY where a process's capability row carries ``None`` for the
#: field (aluminum does for both; see that JSON's own convention that an
#: unpublished figure is deliberately absent rather than borrowed from the
#: FR-4 rows) or where a board's stackup resolves to no capability row at
#: all. JLC-typical numbers, stated here so the substitution is a named
#: constant in one place instead of a literal inside the chain.
DEFAULT_SOLDERMASK_EXPANSION_MM = SOLDERMASK_EXPANSION_MM
DEFAULT_SILK_TO_MASK_CLEARANCE_MM = 0.15


def silk_clearance_mm(
    capability: CapabilityRow | None, *, stroke_width_mm: float
) -> float:
    """How far a silk CENTRELINE must sit from a pad's copper edge, walked
    down the fab chain rather than asserted as a convention::

        pad copper edge
          + soldermask expansion    -> mask opening edge
          + silk-to-mask clearance  -> silk line edge
          + drawn stroke width / 2  -> silk CENTRELINE

    Each term answers a different question and none substitutes for
    another. The governing constraint is **fab printability** — silk must
    clear the soldermask OPENING, which is already swelled past the copper
    — not IPC-7351's assembly courtyard excess, which is what the flat
    0.25mm constant this replaces was measuring (the right kind of number
    for a different question).

    The last term is the *drawn* stroke width, ``stroke_width_mm``, NOT
    :data:`~precis.pcb.capabilities.FIELDS`' ``silk_width_mm``: that field
    is the printability FLOOR (what the fab can hold), this is what the
    pen actually lays down. They are numerically coincident at
    ``house_default`` today and would silently swap without notice.

    Tier is pinned by :func:`~precis.pcb.capabilities.design_value`
    (``house_default``, then ``jlc_min``); a ``None`` figure or a ``None``
    row falls back to this module's own constants. ~0.375mm at 4-layer
    house-default numbers (0.05 + 0.25 + 0.075).
    """
    expansion = design_value(
        capability, "soldermask_expansion_mm", fallback=DEFAULT_SOLDERMASK_EXPANSION_MM
    )
    silk_to_mask = design_value(
        capability,
        "silk_to_mask_clearance_mm",
        fallback=DEFAULT_SILK_TO_MASK_CLEARANCE_MM,
    )
    return expansion + silk_to_mask + stroke_width_mm / 2.0


def _place(
    points: list[Point], *, cx: float, cy: float, rot: float, mirror: bool
) -> list[Point]:
    """This module's spelling of :func:`precis.pcb.landpattern.place_points`
    — a thin alias, not a second implementation. The affine path a
    courtyard travels has to be the one its pads travel, and
    :mod:`precis.pcb.optimize` now reserves the same polygon this module
    draws."""
    return place_points(points, cx=cx, cy=cy, rot_deg=rot, mirrored=mirror)


def _side_for(inst_sides: dict[str, str], refdes: str) -> str:
    raw = str(inst_sides.get(refdes) or "top").lower()
    return "bottom" if raw in ("bottom", "bot", "b") else "top"


#: Refdes families with no INHERENT polarity — a resistor, capacitor,
#: inductor, or ferrite bead can be assembled either way around, so a
#: pin-1 mark on one asserts an orientation that does not exist. The
#: caller's ``polarized`` set (:func:`build_silk`) is the escape hatch for
#: the members of these families that DO carry real polarity (an
#: electrolytic/tantalum cap, a polarized inductor) — see
#: :mod:`precis.handlers.pcb`'s ``polarized`` build (``"polarized": true``
#: or a label matching ``ELEC|TANT|POL``).
_PIN1_EXEMPT_FAMILIES = frozenset({"R", "C", "L", "FB"})


def _refdes_family(refdes: str) -> str:
    """A refdes's FAMILY — its leading letters, before the first digit,
    uppercased (``"R12"`` -> ``"R"``, ``"LED3"`` -> ``"LED"``,
    ``"C1"`` -> ``"C"``). :data:`_PIN1_EXEMPT_FAMILIES` keys off this, not
    the raw refdes, so ``"R1"`` and ``"R100"`` are the same policy
    decision. A refdes with no leading letters at all (malformed input)
    falls back to its own uppercased self, which will simply never match
    any known family."""
    m = re.match(r"[A-Za-z]+", refdes)
    return m.group(0).upper() if m else refdes.upper()


# ─────────────────────────────────────────────────────────────────────
# part outlines — resolved for the whole board at once, before any other
# silk, and shared with the router's via keep-out (gr495393).
# ─────────────────────────────────────────────────────────────────────

#: Clear space a part OUTLINE keeps, in silk stroke widths (gr495393,
#: Reto 2026-10-17: "the white lines are at least 2 white line widths
#: apart"). It sizes the outline's breathing room off its own pads
#: (:func:`outline_offsets_mm`), the spacing between two parts' outlines
#: (:func:`part_outlines`), a refdes label's distance from any outline, and
#: the router's via keep-out around outline ink
#: (:func:`outline_via_keepouts`).
OUTLINE_GAP_STROKES = 2.0


def outline_offsets_mm(
    capability: CapabilityRow | None, *, stroke_width_mm: float
) -> tuple[float, float]:
    """``(floor, target)``: how far a part outline's CENTRELINE sits from
    its own pads' copper.

    ``target`` is :func:`silk_clearance_mm` plus :data:`OUTLINE_GAP_STROKES`
    stroke widths: the fab chain's legal clearance with two line widths of
    clear board on top, where the outline has room for it. ``floor`` is how
    far in the outline may yield to a neighbouring part or the board edge:
    never inside the legal clearance, and never with less than two line
    widths of clear board between its ink and a pad's copper. At JLC
    4-layer house-default figures both floor terms are 0.375 mm and the
    target is 0.675 mm."""
    legal = silk_clearance_mm(capability, stroke_width_mm=stroke_width_mm)
    gap = OUTLINE_GAP_STROKES * stroke_width_mm
    return max(legal, gap + stroke_width_mm / 2.0), legal + gap


def outline_pair_separation_mm(
    capability: CapabilityRow | None, *, stroke_width_mm: float = DEFAULT_SILK_WIDTH_MM
) -> float:
    """How far apart two parts' own-pad hulls (silk's outline base, zero
    extra clearance) must sit for BOTH to draw their FULL outline ring with
    the ruled outline-to-outline breathing room: each ring is its hull
    grown by the ``target`` offset (:func:`outline_offsets_mm`), and two
    rings keep :data:`OUTLINE_GAP_STROKES` stroke widths between them
    (gr495393, Reto 2026-10-17: "the white lines are at least 2 white line
    widths apart"). 1.80 mm with a real JLC 4-layer house-default
    capability row (legal 0.375 = 0.05 + 0.25 + 0.075); 1.60 mm under
    this module's no-capability fallbacks (silk-to-mask 0.15).

    This is the PLACEMENT-side statement of the same rule
    :func:`part_outlines` enforces draw-side (bend in, share one wall, or
    drop): a placer holding every same-side pair at or beyond this
    separation guarantees un-squished outlines and room for a refdes label
    at the first ladder ring, instead of discovering the squeeze after the
    silk pass. Consumers measuring on the placer's courtyard polygons
    (already inflated by ``ir.COURTYARD_CLEARANCE_MM``) subtract twice
    that clearance — one formula here, the inflation arithmetic at the
    consumer, same convention ``courtyard_overlap_pair_term`` documents."""
    _, target = outline_offsets_mm(capability, stroke_width_mm=stroke_width_mm)
    # One (1 + OUTLINE_GAP_STROKES) stroke term, not OUTLINE_GAP_STROKES:
    # ``part_outlines`` draws two full rings only when the hull gap reaches
    # 2*reach = 2*target + spacing (centreline to centreline). Priced with
    # the shorter 2*target + 2*sw, the rings would sit one stroke width
    # apart ink-to-ink and still merge into a shared wall.
    return 2.0 * target + (1.0 + OUTLINE_GAP_STROKES) * stroke_width_mm


@dataclass(frozen=True, slots=True)
class PartOutline:
    """One part's body-outline silk, before obstacle clipping.

    ``ring`` is the closed centreline (first point repeated last) in board
    coordinates. ``runs`` is what gets drawn: ``[ring]``, or, when even the
    floor offset crosses the board's silk edge margin (an edge-mated
    connector), the open run(s) left once that margin is cut away —
    ``edge_cut`` is then True and a part crossing one edge has exactly one
    run, i.e. one gap."""

    side: str
    ring: list[Point]
    runs: list[list[Point]]
    edge_cut: bool = False
    #: Length of ring the board-edge cut left open (mm).
    edge_open_mm: float = 0.0
    #: Refdes of earlier-drawn outlines this one overlaps; the stretch
    #: inside them is left out of ``runs`` (see :func:`part_outlines`).
    meets: tuple[str, ...] = ()
    #: Length of ring buried inside those earlier outlines (mm).
    buried_mm: float = 0.0
    #: Refdes of the neighbours this ring shares a coincident wall
    #: segment with (gr496366 comment 5): the split between the two hulls,
    #: drawn identically by both, never crossed by either.
    shared: tuple[str, ...] = ()
    #: Length of ring left open where the shared wall would sit inside
    #: the pad-clearance floor (mm) — the pair is packed tighter than two
    #: floors, so no legal line exists there and the facing sides stay
    #  open rather than invade.
    floor_open_mm: float = 0.0
    #: The routed vias this ring was redrawn around instead of printing on
    #: them (:func:`_bend_around_vias`), each named by
    #: :func:`_obstacle_label`.
    bent_around: tuple[str, ...] = ()
    #: Those of them the ring bulged out round, each ``(via label, x, y)``.
    bulged: tuple[tuple[str, float, float], ...] = ()


def _polyline_length(points: Sequence[Point]) -> float:
    return sum(
        math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in itertools.pairwise(points)
    )


def _clip_runs(
    runs: list[list[Point]],
    *,
    keep: Any = None,
    cut: Any = None,
    min_run_mm: float,
) -> list[list[Point]]:
    """``runs`` with every stretch outside ``keep`` or inside ``cut``
    removed, as open runs no shorter than ``min_run_mm``. A closed run is
    first rotated to start at a removed vertex, so the stretch crossing its
    seam is never split in two — a part crossing one board edge keeps one
    run and one gap."""

    def removed(p: Point) -> bool:
        pt = _ShapelyPoint(p)
        return (keep is not None and not keep.covers(pt)) or (
            cut is not None and cut.covers(pt)
        )

    out: list[list[Point]] = []
    for run in runs:
        pts = list(run)
        if len(pts) > 2 and pts[0] == pts[-1]:
            ring = pts[:-1]
            start = next((i for i, p in enumerate(ring) if removed(p)), 0)
            ring = ring[start:] + ring[:start]
            pts = [*ring, ring[0]]
        line: Any = _ShapelyLineString(pts)
        if keep is not None:
            line = line.intersection(keep)
        if cut is not None:
            line = line.difference(cut)
        lines = [
            g for g in getattr(line, "geoms", [line]) if g.geom_type == "LineString"
        ]
        if not lines:
            continue
        merged = _shapely_linemerge(lines) if len(lines) > 1 else lines[0]
        out += [
            [(float(x), float(y)) for x, y in g.coords]
            for g in getattr(merged, "geoms", [merged])
            if g.length >= min_run_mm
        ]
    return out


#: Spacing of the boundary samples :func:`_hull_cells` splits the board
#: with. The split between two parallel hull edges sampled this finely is
#: off the true midline by about ``step**2 / (8 * gap)`` — 2 um at a
#: 0.8 mm gap — far below a silk stroke.
_CELL_SAMPLE_MM = 0.1

#: The slack the sub-floor wall cut leaves under ``floor``, mm
#: (:func:`part_outlines`): a ring stretch on the split is left open only
#: when it sits at least this far INSIDE the pad-clearance floor — wide
#: enough that the floor ring itself (computed as the floor buffer's own
#: boundary, so its sampled distance lands exactly on it) is never in
#: doubt, and a wall's sub-floor stretch is still cut whole.
_WALL_FLOOR_EPS_MM = 0.01


def _hull_cells(hulls: list[tuple[int, Any]], *, margin_mm: float) -> dict[int, Any]:
    """Each hull's Voronoi cell among ``hulls``: the area closer to it than
    to any other hull. Built from points sampled along every hull's
    boundary (:data:`_CELL_SAMPLE_MM`), one Voronoi diagram for all of
    them, each point's cell unioned into its hull's. A sample shared by two
    touching hulls goes to the first; overlapping hulls get no clean
    split, and :func:`part_outlines`' draw-order cut handles them."""
    owner: dict[tuple[float, float], int] = {}
    for inst, hull in hulls:
        boundary = hull.boundary
        n = max(4, math.ceil(boundary.length / _CELL_SAMPLE_MM))
        for k in range(n):
            pt = boundary.interpolate(boundary.length * k / n)
            owner.setdefault((round(pt.x, 6), round(pt.y, 6)), inst)
    if len({*owner.values()}) < 2:
        return {}
    points = list(owner)
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    envelope = _ShapelyPolygon(
        [
            (min(xs) - margin_mm, min(ys) - margin_mm),
            (max(xs) + margin_mm, min(ys) - margin_mm),
            (max(xs) + margin_mm, max(ys) + margin_mm),
            (min(xs) - margin_mm, max(ys) + margin_mm),
        ]
    )
    diagram = _shapely_voronoi_polygons(
        _ShapelyMultiPoint(points), extend_to=envelope, ordered=True
    )
    pieces: dict[int, list[Any]] = {}
    for point, cell in zip(points, diagram.geoms, strict=True):
        pieces.setdefault(owner[point], []).append(cell)
    # Snapped to a micrometre grid: neighbouring Voronoi cells share edges
    # that float noise otherwise makes GEOS reject as a topology conflict.
    return {
        inst: _shapely_union_all([c.buffer(0) for c in cs], grid_size=1e-6)
        for inst, cs in pieces.items()
    }


def _via_to_silk_mm(capability: CapabilityRow | None) -> float:
    """Clear board silk owes a via's annulus. The fab publishes no via-to-silk
    figure; a via's annulus is exposed copper the way a mask opening is, so
    silk owes it the same ``silk_to_mask_clearance_mm`` an outline owes a
    pad (house default first)."""
    return design_value(
        capability,
        "silk_to_mask_clearance_mm",
        fallback=DEFAULT_SILK_TO_MASK_CLEARANCE_MM,
    )


@dataclass(slots=True)
class _BendRing:
    """One outline's working state for :func:`_bend_around_vias`."""

    inst: int
    side: str
    refdes: str
    poly: Any
    bent: list[str]
    #: The bulges kept, each ``(via label, x, y)``: what
    #: :func:`build_silk` may veto when one costs a label.
    bulged: list[tuple[str, float, float]]


def _bend_around_vias(
    rings: list[_BendRing],
    obstacles_by_side: dict[str, list[dict[str, Any]]],
    *,
    inside: Any,
    floor_zones: dict[str, Any],
    spacing_mm: float,
    stroke_width_mm: float,
    via_to_silk_mm: float,
    bulge_veto: frozenset[tuple[str, str]] | None = frozenset(),
) -> None:
    """Move each outline off the routed vias it would print on, in place
    (gr495393 option A, Reto's ruling 2026-10-18: silk yields, the router
    and the placer are untouched).

    A via TOUCHES an outline when the ring's centreline is within the via's
    radius plus half a stroke plus ``via_to_silk_mm``: the legal clearance.
    For each touching via, the ring is redrawn around it as a circular arc,
    one of two ways:

    - **dent**: the disk is cut out of the outline, so the via sits outside
      it. This is the move for a via under the part's own body between its
      pads (J2 on usbc-attiny-dogfood-b), where the ring can't grow outward
      without running into the neighbour on the far side.
    - **bulge**: the disk is added, so the outline encloses the via. This is
      the move for a via just outside the part.

    Dents run first across the whole board, then bulges, then each mode
    retries whatever is still touching. A neighbour that dents first leaves
    room for the bulge on its other side. Each move tries the largest radius
    first: via radius plus the legal clearance plus ``OUTLINE_GAP_STROKES``
    widths, plus the outline spacing; then without the spacing; then the
    legal clearance alone. A move is kept only if the redrawn ring

    - clears the via by the legal clearance,
    - hits no pad, fiducial or other furniture (each widened by the
      silk-to-mask clearance) that the old ring did not already hit, and
      brings no other via inside its legal clearance,
    - still encloses every one of the part's own pads,
    - stays inside the board's silk edge margin, and
    - keeps its new stretch out of every other same-side outline and at
      least ``spacing_mm`` off its ring.

    A kept move is then checked once more against ``floor_zones``
    (:func:`part_outlines`' pad-clearance floor union): a legal dent can
    leave its new stretch inside the floor, where the floor pass opens it
    — the via then buys a SECOND gap and the outline comes out in
    fragments (gr495393: J2 on dogfood-b dented round its under-body via
    into the floor and split in two). A move that adds floor-open falls
    back to whatever the OTHER mode's legal radii leave least inside the
    floor — there a bulge keeps the ring one line.

    ``bulge_veto`` names the ``(refdes, via label)`` bulges not to try;
    ``None`` tries no bulge at all. :func:`build_silk` sets it to give a
    label back the room a bulge took (gr495393 option c).

    A via that no move clears stays where it is: :func:`build_silk` cuts
    the outline around it, as before, and the census reason names the via
    (no DRC rule: the ruling 2026-10-18 kept these cuts silent, as they
    were)."""
    half = stroke_width_mm / 2.0
    legal = half + via_to_silk_mm
    radii = (
        legal + OUTLINE_GAP_STROKES * stroke_width_mm + spacing_mm,
        legal + OUTLINE_GAP_STROKES * stroke_width_mm,
        legal + 1e-3,
    )
    for side in ("top", "bottom"):
        side_obs = obstacles_by_side.get(side, [])
        ctx = _BendSide(
            rings=[r for r in rings if r.side == side],
            vias=[o for o in side_obs if o.get("obstacle") == "via"],
            hard=[
                _mask_opening(o, via_to_silk_mm)
                for o in side_obs
                if o.get("obstacle") != "via"
            ],
            legal_mm=legal,
            spacing_mm=spacing_mm,
            stroke_width_mm=stroke_width_mm,
            inside=inside,
            below=floor_zones.get(side),
        )
        if not ctx.rings or not ctx.vias:
            continue
        for mode, natural in (
            ("dent", True),
            ("bulge", True),
            ("dent", False),
            ("bulge", False),
        ):
            for ring in ctx.rings:
                for k in range(len(ctx.vias)):
                    if not ctx.touching(ring.poly, k):
                        continue
                    under = bool(ring.poly.covers(ctx.centre(k)))
                    if natural and under != (mode == "dent"):
                        continue
                    if mode == "bulge" and _bulge_vetoed(
                        bulge_veto, ring.refdes, ctx.vias[k]
                    ):
                        continue
                    _settle_bend(
                        ctx, ring, k, mode=mode, radii=radii, bulge_veto=bulge_veto
                    )


def _bulge_vetoed(
    bulge_veto: frozenset[tuple[str, str]] | None, refdes: str, via: dict[str, Any]
) -> bool:
    """Whether ``bulge_veto`` rules a bulge out for this (refdes, via):
    ``None`` rules every bulge out, a set the ones it names."""
    return bulge_veto is None or (refdes, _obstacle_label(via)) in bulge_veto


#: How much floor-open a bend candidate must save to displace the move
#: already taken, mm of centreline (:func:`_settle_bend`).
_BEND_OPEN_EPS_MM = 1e-6


def _settle_bend(
    ctx: _BendSide,
    ring: _BendRing,
    k: int,
    *,
    mode: str,
    radii: tuple[float, ...],
    bulge_veto: frozenset[tuple[str, str]] | None,
) -> bool:
    """Commit ``ring``'s redraw around via ``k``; return whether it cleared.

    The first legal ``mode`` radius is taken, as before — unless it lands
    the ring inside ``ctx.below`` (the pad-clearance floor), a stretch the
    floor pass opens anyway: the via then buys a second gap where another
    move would have added none (gr495393). In that case every legal move
    of the OTHER mode is tried and the one leaving the least ring inside
    the floor wins — the outline stays continuous, trimmed only where a
    pad, the board edge or the floor itself forces it."""
    moved = None
    for extra in radii:
        moved = _bend_move(ctx, ring, k, mode=mode, extra_mm=extra)
        if moved is not None:
            break
    if moved is None:
        return False
    best_mode, best = mode, moved
    if ctx.below is not None:
        old_open = ctx.below.intersection(ring.poly.exterior).length
        open_mm = ctx.below.intersection(moved.exterior).length
        if open_mm > old_open + _BEND_OPEN_EPS_MM:
            alt = "bulge" if mode == "dent" else "dent"
            if alt != "bulge" or not _bulge_vetoed(
                bulge_veto, ring.refdes, ctx.vias[k]
            ):
                for extra in radii:
                    cand = _bend_move(ctx, ring, k, mode=alt, extra_mm=extra)
                    if cand is None:
                        continue
                    cand_open = ctx.below.intersection(cand.exterior).length
                    if cand_open < open_mm - _BEND_OPEN_EPS_MM:
                        open_mm, best_mode, best = cand_open, alt, cand
    ring.poly = best
    label = _obstacle_label(ctx.vias[k])
    ring.bent.append(label)
    if best_mode == "bulge":
        ring.bulged.append((label, float(ctx.centre(k).x), float(ctx.centre(k).y)))
    return True


@dataclass(slots=True)
class _BendSide:
    """One board side's state for :func:`_bend_around_vias`: its outlines,
    the vias that reach it, and the copper and furniture a bent ring must
    not newly hit (widened by the silk-to-mask clearance)."""

    rings: list[_BendRing]
    vias: list[dict[str, Any]]
    hard: list[dict[str, Any]]
    legal_mm: float
    spacing_mm: float
    stroke_width_mm: float
    inside: Any
    #: This side's pad-clearance floor union (:func:`part_outlines`'
    #: ``subfloor``): where a redrawn stretch lands in it, the floor pass
    #: opens it — the measure :func:`_settle_bend` minimises.
    below: Any

    def centre(self, k: int) -> Any:
        return _ShapelyPoint(float(self.vias[k]["x"]), float(self.vias[k]["y"]))

    def radius(self, k: int) -> float:
        return float(self.vias[k]["w"]) / 2.0

    def touching(self, poly: Any, k: int) -> bool:
        reach = self.radius(k) + self.legal_mm - 1e-9
        return bool(poly.exterior.distance(self.centre(k)) < reach)


def _bend_move(
    ctx: _BendSide, ring: _BendRing, k: int, *, mode: str, extra_mm: float
) -> Any:
    """``ring`` redrawn around via ``k`` as one dent or bulge of radius
    via radius + ``extra_mm`` — the polygon, or ``None`` unless every
    check in :func:`_bend_around_vias` passes. Does not commit:
    :func:`_settle_bend` picks between the modes' answers."""
    centre, r = ctx.centre(k), ctx.radius(k)
    old = ring.poly
    old_line = old.exterior
    disk = centre.buffer(r + extra_mm, quad_segs=8)
    moved = old.difference(disk) if mode == "dent" else old.union(disk)
    if moved.is_empty or moved.geom_type != "Polygon":
        return None
    moved = _shapely_orient(_ShapelyPolygon(moved.exterior), 1.0)
    line = moved.exterior
    if line.distance(centre) < r + ctx.legal_mm - 1e-6:
        return None
    changed = line.difference(old_line.buffer(1e-4))
    if changed.is_empty:
        return None
    if ctx.inside is not None and not ctx.inside.buffer(1e-6).covers(changed):
        return None
    own = [
        p
        for p in ctx.hard
        if "obstacle" not in p and str(p.get("refdes")) == ring.refdes
    ]
    if not all(moved.covers(_ShapelyPoint(float(p["x"]), float(p["y"]))) for p in own):
        return None
    pts = [(float(x), float(y)) for x, y in line.coords]
    old_pts = [(float(x), float(y)) for x, y in old_line.coords]
    width = ctx.stroke_width_mm
    for o in _near_obstacles(ctx.hard, pts, 2.0 * width):
        if (
            _stroke_hits(pts, [o], width) is not None
            and _stroke_hits(old_pts, [o], width) is None
        ):
            return None
    for j in range(len(ctx.vias)):
        if j != k and ctx.touching(moved, j) and not ctx.touching(old, j):
            return None
    far = changed.difference(old_line.buffer(ctx.spacing_mm))
    for other in ctx.rings:
        if other is ring:
            continue
        if other.poly.buffer(-1e-6).intersects(changed):
            return None
        if (
            not far.is_empty
            and far.distance(other.poly.exterior) < ctx.spacing_mm - 1e-6
        ):
            return None
    return moved


def part_outlines(
    ir: PcbIR,
    *,
    capability: CapabilityRow | None,
    stroke_width_mm: float = DEFAULT_SILK_WIDTH_MM,
    instance_sides: dict[str, str] | None = None,
    outline: Sequence[Sequence[float]] | None = None,
    obstacles_by_side: dict[str, list[dict[str, Any]]] | None = None,
    bulge_veto: frozenset[tuple[str, str]] | None = frozenset(),
) -> dict[int, PartOutline]:
    """Every placed part's outline, keyed by instance — the ONE place an
    outline's shape is decided, read by :func:`build_silk` (which draws it)
    and by :func:`outline_via_keepouts` (the router's via keep-out,
    ``RealizeConfig.silk_via_keepout``, off until Reto rules on its cost).

    **Why the outline is resolved here and never clipped by other silk
    (gr495393).** The fragmented outlines Reto saw on usbc-attiny-dogfood-b
    were not cut by their own pads — the old hull offset already cleared
    those. U1 and J2 were cut around one routed via between them; U2 was
    cut by U1's committed courtyard, folded into the shared obstacle list
    as a solid bounding box that reached over U2's own outline. Pushing an
    outline outward cannot fix either cause by itself — it only meets a
    neighbour sooner — so the shape is settled here, against every other
    part at once:

    1. Grow the part's own pad hull to the ``target`` offset
       (:func:`outline_offsets_mm`): two line widths of clear board beyond
       the legal silk clearance.
    2. Where a neighbour on the same side is close, the two share ONE
       line on the split between them (gr496366 comment 5, Reto
       2026-10-19: "just overlap the lines, don;t invade the neighbour.
       So two things adjacent share one line if they are as close as
       possibel"). Silk adds no spacing of its own: each part keeps to
       its own side of the line equidistant from the two hulls (their
       Voronoi split, :func:`_hull_cells`) and is EXTENDED to it where its
       target ring would otherwise sit less than the outline spacing
       (``1 + OUTLINE_GAP_STROKES`` stroke widths centreline to
       centreline) off the neighbour's — so the two rings coincide as one
       shared segment rather than print as two nearly-touching lines.
       Neither ring ever crosses the split, and where the split itself
       sits inside ``floor`` (a pair packed tighter than two floors) the
       facing stretch is left open — the clearance floor still applies
       (``PartOutline.floor_open_mm``). A later outline running inside an
       earlier one's area still leaves that stretch out
       (``PartOutline.meets``), now a guard for degenerate splits rather
       than the common path.
    3. Yield to the board edge the same way: no further out than the silk
       edge margin (:func:`_silk_edge_margin_mm`, plus half a stroke).
    4. Never inside ``floor``. If ``floor`` itself crosses the edge margin
       (an edge-mated connector), the ring keeps its shape and the stretch
       past the margin is cut away: one clean gap at the edge instead of a
       silk_edge_clearance error per segment (gr495293).

    5. Given ``obstacles_by_side`` (:func:`build_silk`'s seed obstacle
       lists: pads, the vias reaching each side, furniture), redraw each ring
       around the routed vias it would print on (:func:`_bend_around_vias`,
       gr495393 option A), skipping the bulges ``bulge_veto`` names. The
       router never sees this: the :func:`outline_via_keepouts` call
       passes no obstacles.

    Other silk — refdes labels, pin-1 marks, title block — yields to these
    rings, never the other way round. Copper the outline still lands on
    (a via no bend clears, a foreign pad inside ``floor``) is
    :func:`build_silk`'s to clip.

    ``instance_sides`` is refdes -> ``'top'``/``'bottom'`` as
    :func:`build_silk` takes it; ``None`` reads the IR's own
    :attr:`~precis.pcb.ir.PcbIR.inst_bottom` (the router's view). Uses
    shapely: a hull clipped to its Voronoi cell is a polygon boolean, and
    :func:`precis.pcb.ir.instance_courtyard_polygon` already builds the
    ring with it."""
    floor, target = outline_offsets_mm(capability, stroke_width_mm=stroke_width_mm)
    spacing = (1.0 + OUTLINE_GAP_STROKES) * stroke_width_mm
    pins_of_inst: dict[int, list[int]] = {}
    for pid in range(ir.n_pins):
        pins_of_inst.setdefault(int(ir.pin_instance[pid]), []).append(pid)

    hulls: list[tuple[int, str, Any]] = []
    for inst in range(ir.n_instances):
        cx, cy = float(ir.inst_x[inst]), float(ir.inst_y[inst])
        if math.isnan(cx) or math.isnan(cy):
            continue
        local = instance_courtyard_polygon(
            ir, inst, clearance_mm=0.0, pins=pins_of_inst.get(inst, [])
        )
        if not local:
            continue
        if instance_sides is None:
            side = "bottom" if bool(ir.inst_bottom[inst]) else "top"
        else:
            side = _side_for(instance_sides, str(ir.instance_refdes[inst]))
        rot = float(ir.inst_rot[inst])
        world = _place(
            local,
            cx=cx,
            cy=cy,
            rot=0.0 if math.isnan(rot) else rot,
            mirror=side == "bottom",
        )
        # The courtyard union can be concave (gr524238); the Voronoi
        # split below wants one ring per part, so this pass still hulls.
        hull = _ShapelyMultiPoint(world).convex_hull
        hulls.append((inst, side, hull))

    # Natural refdes order, not ``ir`` array order (gr498680): the Voronoi
    # split below gives a sample point shared by two touching hulls to the
    # FIRST hull in this list, and the rings themselves are resolved in
    # list order — upstream netlist/DB iteration order deciding either is
    # the same unprincipled input-order dependence the module docstring's
    # "Order is instance-independent" section names for the label pass.
    hulls.sort(key=lambda h: _refdes_sort_key(str(ir.instance_refdes[h[0]])))

    inside = None
    if outline is not None and len(outline) >= 3:
        board = _ShapelyPolygon([(float(p[0]), float(p[1])) for p in outline]).buffer(0)
        inside = board.buffer(
            -(_silk_edge_margin_mm(capability) + stroke_width_mm / 2.0), join_style=2
        )

    # A crowded pair shares ONE line on the split between their hulls
    # (gr496366 comment 5, Reto 2026-10-19: "just overlap the lines, don;t
    # invade the neighbour. So two things adjacent share one line if they
    # are as close as possibel"). Silk adds no spacing: the outlines are
    # either far enough apart to keep ``spacing`` between them, or they
    # are the same segment. ``reach`` is how far onto the gap the merged
    # pair's wall reaches: the target offset plus half ``spacing``, so the
    # trigger "the two target rings can't hold the spacing" and the wall
    # fill agree on one number.
    reach = target + spacing / 2.0
    reach_limit = 2.0 * reach
    cells: dict[int, Any] = {}
    for side_name in ("top", "bottom"):
        cells.update(
            _hull_cells(
                [(inst, hull) for inst, side, hull in hulls if side == side_name],
                margin_mm=reach_limit,
            )
        )
    # The sampled Voronoi cells OVERLAP where two hulls sit close (the
    # union of boundary-sample cells is not a clean partition there), so
    # each part gets an EXCLUSIVE cell: its own minus the cells of
    # earlier-refdes same-side neighbours. The contested strip goes to the
    # earlier part — arbitrary but deterministic (gr498680) — and makes
    # the frontier between any pair a single line, `own ∩ other`, which
    # is the wall both rings then share. Subtracting raw would slit a
    # ring through the part's own body (the wander reaches inside its
    # hull), so the floor zone is protected: nothing inside ``base`` is
    # ever subtracted.
    exclusive: dict[int, Any] = {}
    for pos, (inst, side, hull) in enumerate(hulls):
        cell = cells.get(inst)
        if cell is None:
            continue
        lowers = [
            cells[other]
            for other, other_side, _h in hulls[:pos]
            if other_side == side and other in cells
        ]
        if not lowers:
            exclusive[inst] = cell
            continue
        protected = hull.buffer(floor, join_style=2, mitre_limit=COURTYARD_MITRE_LIMIT)
        rest = _shapely_unary_union(lowers).difference(protected)
        exclusive[inst] = cell.difference(rest) if not rest.is_empty else cell

    rings: dict[int, tuple[str, list[Point]]] = {}
    shared_with: dict[int, tuple[str, ...]] = {}
    for inst, side, hull in hulls:
        grown = hull.buffer(target, join_style=2, mitre_limit=COURTYARD_MITRE_LIMIT)
        base = hull.buffer(floor, join_style=2, mitre_limit=COURTYARD_MITRE_LIMIT)
        region = grown if inside is None else grown.intersection(inside)
        x0, y0, x1, y1 = hull.bounds
        own_cell = exclusive.get(inst)
        if own_cell is not None:
            # Never past the split ("don't invade the neighbour"): the
            # ring keeps to its own exclusive cell.
            region = region.intersection(own_cell)
        walls: list[Any] = []
        shared: list[str] = []
        for other, other_side, other_hull in hulls:
            if other == inst or other_side != side or other not in cells:
                continue
            ox0, oy0, ox1, oy1 = other_hull.bounds
            if (
                ox0 > x1 + reach_limit
                or ox1 < x0 - reach_limit
                or oy0 > y1 + reach_limit
                or oy1 < y0 - reach_limit
            ):
                continue
            gap = hull.distance(other_hull)
            if gap >= reach_limit:
                continue
            if own_cell is None:
                continue
            other_cell = exclusive.get(other)
            if other_cell is None:
                continue
            edge = own_cell.intersection(other_cell)
            if edge.is_empty or edge.length <= 1e-6:
                continue
            # ...and EXTENDS to the shared frontier: the strip from the
            # ring's own reach out to it fills in the same wall segment
            # the neighbour's ring is being given, so the two coincide.
            # Bounded by EITHER hull's reach so both parts draw the same
            # span — with only the nearer hull's reach the two walls
            # could leave a sliver of boundary undrawn on one side.
            wall = edge.buffer(spacing / 2.0).intersection(
                hull.buffer(reach).union(other_hull.buffer(reach))
            )
            if inside is not None:
                wall = wall.intersection(inside)
            wall = own_cell.intersection(wall)
            if not wall.is_empty:
                walls.append(wall)
                shared.append(str(ir.instance_refdes[other]))
        if walls:
            region = region.union(_shapely_unary_union(walls))
        # The floor-offset ring still applies where the part's own cell
        # allows it: never inside the pad-clearance floor off its own
        # pads, and never across the split.
        region = region.union(base if own_cell is None else base.intersection(own_cell))
        poly = max(
            getattr(region, "geoms", [region]),
            key=lambda g: g.intersection(base).area,
        )
        exterior = _shapely_orient(_ShapelyPolygon(poly.exterior), 1.0).exterior
        rings[inst] = (side, [(float(x), float(y)) for x, y in exterior.coords])
        if shared:
            shared_with[inst] = tuple(sorted(shared, key=_refdes_sort_key))

    # One union per side: anywhere within ``floor`` of a same-side pad
    # hull. A shared-wall stretch landing in it cannot print — the line
    # would sit inside someone's pad clearance — so it is left open on
    # both rings at once. The via bend needs it too: a redraw landing in
    # it buys a second gap where the other mode adds none (gr495393).
    subfloor: dict[str, Any] = {}
    for side_name in ("top", "bottom"):
        zones = [
            hull.buffer(floor - _WALL_FLOOR_EPS_MM)
            for _inst, s, hull in hulls
            if s == side_name
        ]
        if zones:
            subfloor[side_name] = _shapely_unary_union(zones)
    bent: dict[int, tuple[str, ...]] = {}
    bulged: dict[int, tuple[tuple[str, float, float], ...]] = {}
    if obstacles_by_side:
        work = [
            _BendRing(
                inst=inst,
                side=side,
                refdes=str(ir.instance_refdes[inst]),
                poly=_ShapelyPolygon(ring),
                bent=[],
                bulged=[],
            )
            for inst, (side, ring) in sorted(
                rings.items(),
                key=lambda kv: _refdes_sort_key(str(ir.instance_refdes[kv[0]])),
            )
        ]
        _bend_around_vias(
            work,
            obstacles_by_side,
            inside=inside,
            floor_zones=subfloor,
            spacing_mm=spacing,
            stroke_width_mm=stroke_width_mm,
            via_to_silk_mm=_via_to_silk_mm(capability),
            bulge_veto=bulge_veto,
        )
        for w in work:
            if w.bent:
                rings[w.inst] = (
                    w.side,
                    [(float(x), float(y)) for x, y in w.poly.exterior.coords],
                )
                bent[w.inst] = tuple(w.bent)
                bulged[w.inst] = tuple(w.bulged)

    # Draw order is natural refdes order, as everywhere in this module. A
    # later outline that runs INSIDE an earlier one leaves out that
    # stretch — with every ring already clipped to its own cell this can
    # only fire on degenerate splits, but it stays as the guard. A shared
    # midline wall is not cut: both rings run exactly along the same
    # split, so each stays one closed line and the wall prints as one.
    min_run = stroke_width_mm * _CLIP_MIN_RUN_STROKES
    order = sorted(rings, key=lambda i: _refdes_sort_key(str(ir.instance_refdes[i])))
    drawn: list[tuple[int, str, Any]] = []
    out: dict[int, PartOutline] = {}
    for inst in order:
        side, ring = rings[inst]
        line = _ShapelyLineString(ring)
        runs, edge_cut, edge_open = [ring], False, 0.0
        if inside is not None and not inside.covers(line):
            runs = _clip_runs(runs, keep=inside, min_run_mm=min_run)
            edge_cut = True
            edge_open = line.length - sum(_polyline_length(r) for r in runs)
        # The pad-clearance floor still applies to a shared wall: where
        # the split itself is closer than ``floor`` to EITHER part's pad
        # hull (a pair packed tighter than two floors), no line can print
        # there at all, so the facing stretch is left open rather than
        # invading the neighbour or breaking clearance. Checked against
        # EVERY same-side hull, not just this part's own — the sampled
        # split can wiggle off the true midline onto one side, and the
        # same stretch has to come off both rings for the wall to stay
        # coincident.
        below = subfloor.get(side)
        floor_open = 0.0
        if below is not None and below.intersects(line):
            clipped = _clip_runs(runs, cut=below, min_run_mm=min_run)
            floor_open = sum(_polyline_length(r) for r in runs) - sum(
                _polyline_length(r) for r in clipped
            )
            runs = clipped
        area = _ShapelyPolygon(ring).buffer(0)
        over = [
            (other, other_area)
            for other, other_side, other_area in drawn
            if other_side == side
            and other_area.distance(line) <= 1.5 * stroke_width_mm
            and line.intersection(other_area.buffer(-stroke_width_mm)).length
            > stroke_width_mm
        ]
        buried = 0.0
        if over:
            earlier = _shapely_unary_union([a for _o, a in over])
            buried = line.intersection(earlier.buffer(-stroke_width_mm)).length
            # 1.5 strokes: the cut ends keep half a stroke of clear board
            # off the earlier outline's ink instead of just touching it.
            runs = _clip_runs(
                runs, cut=earlier.buffer(1.5 * stroke_width_mm), min_run_mm=min_run
            )
        drawn.append((inst, side, area))
        out[inst] = PartOutline(
            side=side,
            ring=ring,
            runs=runs,
            edge_cut=edge_cut,
            edge_open_mm=edge_open,
            buried_mm=buried,
            meets=tuple(str(ir.instance_refdes[o]) for o, _a in over),
            shared=shared_with.get(inst, ()),
            floor_open_mm=floor_open,
            bent_around=bent.get(inst, ()),
            bulged=bulged.get(inst, ()),
        )
    return out


def outline_via_keepouts(
    ir: PcbIR,
    *,
    capability: CapabilityRow | None,
    stroke_width_mm: float = DEFAULT_SILK_WIDTH_MM,
) -> list[tuple[list[Point], float]]:
    """``(polyline, margin_mm)`` per drawn outline run: a via's COPPER edge
    must stay ``margin_mm`` off the polyline (gr495393 comment 1, Reto: vias
    must not land on part-outline silk — enforced where vias are placed,
    not by clipping silk afterwards).

    ``margin_mm`` is half the stroke (the ink edge), plus the via-to-silk
    clearance, plus :data:`OUTLINE_GAP_STROKES` stroke widths. The fab
    publishes no via-to-silk figure; a via's annulus is exposed copper the
    way a mask opening is, so silk owes it the same
    ``silk_to_mask_clearance_mm`` (house default first) the outline owes a
    pad. 0.625 mm at house-default figures. Read off the IR's own outline
    and sides, so the router sees the rings :func:`build_silk` draws on a
    board whose silk is built with the same outline and capability row."""
    via_to_silk = _via_to_silk_mm(capability)
    margin = stroke_width_mm / 2.0 + via_to_silk + OUTLINE_GAP_STROKES * stroke_width_mm
    outlines = part_outlines(
        ir,
        capability=capability,
        stroke_width_mm=stroke_width_mm,
        outline=ir.outline or None,
    )
    return [(run, margin) for _inst, o in sorted(outlines.items()) for run in o.runs]


# ─────────────────────────────────────────────────────────────────────
# board-level fiducials — optical alignment targets, NOT a per-instance
# concern (everything above this line is per-placed-part). See the
# module docstring's "Two things every real board has" for where these
# belong in the model and the known ``outline_from_features`` gap.
# ─────────────────────────────────────────────────────────────────────

#: Copper-dot / soldermask-opening diameters, mm. Convention followed:
#: the JLCPCB/KiCad-default fiducial pair — 1mm copper, 2mm mask opening
#: (0.5mm annular clearance each side). The other common spelling in the
#: wild keeps the SAME 1mm copper but opens the mask a full 1mm per side
#: instead (3mm total) for a more generous no-pour keepout on a
#: coarser-pitch board. Named constants (never bare literals in the
#: function below) specifically so a caller preferring the 3mm
#: convention can override ``mask_dia_mm`` at the call site without
#: touching this module.
FIDUCIAL_COPPER_DIA_MM = 1.0
FIDUCIAL_MASK_DIA_MM = 2.0

#: Three, never two: two fiducials leave a 180-degree rotation ambiguous
#: (the vision system can't tell the board apart from its own point
#: reflection about the line joining them), so a pick-and-place machine
#: needs a THIRD point off that line to resolve rotation as well as
#: offset. "Off that line" is asserted by this module's own test, not
#: trusted from the placement code below (task brief, verbatim).
FIDUCIAL_COUNT = 3

#: How far a fiducial's centre sits in from the outline bbox corner, mm —
#: clear of the board edge keepout a fab already reserves, expressed as
#: one shared constant so every corner uses the same inset rather than
#: four independently-tuned numbers.
FIDUCIAL_MARGIN_MM = 3.0

# Corner try-order: near (x0,y0), (x1,y1), (x0,y1), (x1,y0) — chosen so
# the first `count` candidates that SUCCEED are never collinear (any 3 of
# a rectangle's 4 corners form a right triangle; a degenerate zero-area
# bbox instead fails every candidate's containment check below, so it
# can never silently emit 3 collinear points). Each entry is
# ``(sx, sy)``: +1 insets FROM the low (x0/y0) edge, -1 insets from the
# high (x1/y1) edge.
_FIDUCIAL_CORNER_SIGNS: tuple[tuple[float, float], ...] = (
    (1.0, 1.0),
    (-1.0, -1.0),
    (1.0, -1.0),
    (-1.0, 1.0),
)


def _bbox(poly: list[Point]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


def _corner_point(
    sx: float, sy: float, x0: float, y0: float, x1: float, y1: float, margin: float
) -> Point:
    cx = x0 + margin if sx > 0 else x1 - margin
    cy = y0 + margin if sy > 0 else y1 - margin
    return (cx, cy)


def _circle_inside_polygon(center: Point, radius: float, poly: list[Point]) -> bool:
    """Exact containment for a disc of ``radius`` at ``center``: the centre
    inside ``poly`` and at least ``radius`` from every one of its edges.
    Not sampled: 12 points on the circumference reach only cos(15°) of the
    radius between samples, and a pin-1 dot accepted that way printed
    0.013 mm inside the silk edge margin (gr495395, r2 seed 6)."""
    if not point_in_polygon(center, poly):
        return False
    ring = poly if poly[0] == poly[-1] else [*poly, poly[0]]
    return all(
        dist_point_to_segment(center, a, b) >= radius
        for a, b in itertools.pairwise(ring)
    )


def _circle_overlaps_pad(center: Point, radius: float, pad: dict[str, Any]) -> bool:
    if str(pad.get("shape") or "circle") == "circle":
        pcx, pcy = float(pad["x"]), float(pad["y"])
        pr = float(pad["w"]) / 2.0
        return math.hypot(center[0] - pcx, center[1] - pcy) <= radius + pr
    return _polygon_overlaps_circle(_pad_rect_polygon(pad), center, radius)


@dataclass(frozen=True, slots=True)
class FiducialResult:
    """Board-level fiducial geometry — deliberately NOT part of
    :class:`SilkResult` (a fiducial belongs to the board, not to a placed
    instance; see the module docstring).

    ``pads`` is exactly :mod:`precis.pcb.gerber`'s ``model["pads"]``
    shape — fold it into the pads list a caller already builds and both
    the copper flash AND the (uniformly-swelled) soldermask opening fall
    out of the EXISTING pad pipeline for free (``copper_gerber``/
    ``soldermask_gerber`` read every ``model["pads"]`` entry identically
    regardless of what put it there — nothing here duplicates that).
    **One entry per fiducial PER LAYER in ``layers``**, all sized
    ``copper_dia_mm`` — a fiducial is a fab-wide registration mark, not a
    top-side-only one: the fab needs a flash on every copper layer to
    align inner layers to the outer films, and (since ``soldermask_gerber``
    reads pads on ``layers[0]``/``layers[-1]`` only) a bottom mask opening
    falls out of this same list automatically, for free, exactly the way
    the top one always has — no second, mask-specific entry shape.

    ``plane_blockers`` is a SEPARATE list, in the same "pad reshaped as a
    multi-layer fake via" ``ctype='via'`` shape
    :func:`precis.pcb.realize._pad_blockers` already uses to feed a real
    component pad into :func:`precis.pcb.planes.plane_pours` as an
    antipad obstacle (see that function's own docstring for why a pad
    has to be a blocker at all — an unlisted pad gets flooded straight
    over). ``layers`` names EVERY layer in ``layers`` (the module's own
    ``layers`` argument, not a single one) so :func:`precis.pcb.planes.
    cut_antipads` (which already reads a blocker's full ``layers`` list,
    see :func:`~precis.pcb.planes._blocker_layers`) cuts the ring into a
    pour on ANY of them, not just the one the copper flash used to sit
    on. Sized to ``mask_dia_mm``, not the bare copper disc, so the
    antipad clears the whole mask opening, not just the copper underneath
    it. **This module cannot fold it into ``plane_pours`` itself**:
    ``plane_pours`` runs at REALIZE time (:func:`precis.pcb.realize.
    _pour_planes`, before a pour is ever written to ``pcb_copper``), while
    this module only ever runs at render time, off already-realized
    copper — a fiducial does not exist yet when the plane it would need
    to avoid is poured. That gap does NOT leave a plane-poured fiducial
    flooded, though: :meth:`precis.handlers.pcb.PcbHandler.
    _board_furniture` (the one caller of :func:`build_fiducials`) feeds
    this same list straight into :func:`precis.pcb.planes.cut_antipads`
    immediately afterward, which punches the ring into the ALREADY-BUILT
    pour dicts instead — a second, render-time-shaped cut rather than the
    realize-time one ``plane_pours`` would have done, landing in the
    gerbers/DRC model/fab SVG identically because all three read the
    pours ``cut_antipads`` returns, not the ones ``plane_pours`` wrote.

    ``silk_keepouts`` is ``pads``-shaped too (radius ``mask_dia_mm``, no
    ``net``/``layer``) but for THIS module's own obstacle checks — pass
    it as (part of) ``build_title_block``'s ``avoid`` or fold it into
    ``build_silk``'s ``reserved`` so a title block or a refdes label never
    prints on top of a fiducial."""

    fiducials: tuple[Point, ...]
    pads: list[dict[str, Any]]
    plane_blockers: list[dict[str, Any]]
    silk_keepouts: list[dict[str, Any]]
    dropped: tuple[str, ...] = ()


def world_courtyard_rings(ir: PcbIR) -> list[list[Point]]:
    """Every PLACED instance's courtyard polygon in board coordinates —
    the per-part "thicket" pieces a fiducial must stay out of. A pinless
    placed instance (mounting hole) contributes nothing: it has no land
    pattern and a fiducial beside a mounting hole is fine. Bottom-side
    mirroring is ignored on purpose: a mirror flips a ring about its own
    centre, and the fiducial standoff test that consumes these rings
    carries millimetres of its own slack."""
    pins_of_inst: dict[int, list[int]] = {}
    for pid in range(ir.n_pins):
        pins_of_inst.setdefault(int(ir.pin_instance[pid]), []).append(pid)
    rings: list[list[Point]] = []
    for inst in range(ir.n_instances):
        cx, cy = float(ir.inst_x[inst]), float(ir.inst_y[inst])
        if math.isnan(cx) or math.isnan(cy):
            continue
        ring_local = instance_courtyard_polygon(
            ir, inst, clearance_mm=0.0, pins=pins_of_inst.get(inst, [])
        )
        if not ring_local:
            continue
        rot = float(ir.inst_rot[inst])
        rings.append(
            _place(
                ring_local,
                cx=cx,
                cy=cy,
                rot=0.0 if math.isnan(rot) else rot,
                mirror=False,
            )
        )
    return rings


def fiducial_candidate_sites(
    outline: list[tuple[float, float]] | list[list[float]],
    *,
    ir: PcbIR | None = None,
    margin_mm: float = FIDUCIAL_MARGIN_MM,
    mask_dia_mm: float = FIDUCIAL_MASK_DIA_MM,
) -> list[tuple[int, Point]]:
    """Every ``(corner_index, point)`` candidate :func:`build_fiducials`
    may ever choose, in its exact trial order, filtered only by outline
    containment — the obstacle-INDEPENDENT superset.

    This is the shared definition between the render-time mint
    (:func:`build_fiducials` consumes this list and applies its pad-
    overlap/spacing/count checks on top — every one of which only ever
    REJECTS candidates, so the chosen fiducials are always a subset) and
    the ROUTER's pre-claim (``precis.pcb.realize._claim_fiducial_
    keepouts``). Fiducials are minted at render/DRC time, AFTER routing;
    without a route-time claim of these sites, a legally-routed track or
    via could cross the corner the mint later lands on — found live
    2026-08-31 as a 0.000mm VCC3V3-track-to-fiducial short on the
    esp32c3 reference fixture, seed 3 (the root cause behind
    ``docs/backlog/pcb-plane-via-copper-claim-leak.md``'s "copper reaches
    the board without claiming its corridor" family)."""
    sites: list[tuple[int, Point]] = []
    if len(outline) < 3:
        return sites
    poly = [(float(p[0]), float(p[1])) for p in outline]
    x0, y0, x1, y1 = _bbox(poly)
    radius = mask_dia_mm / 2.0
    # A fiducial buried in the parts thicket is useless to the fab (an
    # optical target needs clear surroundings) AND, because it is minted
    # after routing, it lands on whatever copper routed through that spot
    # first — so the thicket is excluded from candidacy for the mint and
    # the router's pre-claim alike, off the same ``ir``. PER-COURTYARD
    # polygons, not a global parts bbox: a bbox over a placement that
    # (correctly, post-recentre) spreads across its board covers every
    # corner and yields the zero-fiducial board the 2026-09-01 review
    # caught on the nano fixture — the thicket is the parts, not their
    # hull. The standoff is one mask radius folded into the test circle:
    # a clear ring the size of the window itself between any courtyard
    # edge and the mask opening.
    rings = world_courtyard_rings(ir) if ir is not None else []
    # A full mask DIAMETER of standoff beyond the fiducial's own radius,
    # not the original half: the deeper rungs (below) walk candidates in
    # toward the parts, and at half-diameter a rung-5/6 fiducial could sit
    # close enough to a courtyard for its silk keep-out to clip the
    # courtyard OUTLINE below legibility — measured on the 40mm stress
    # fixture seed 2 as C14's courtyard (and with it the pin-1 tick)
    # dropped entirely. An optical target needs clear surroundings; a
    # spot that costs another feature its silk is not clear.
    standoff = radius + mask_dia_mm
    # Six rungs, not the original two: corner MOUNTING HARDWARE (an M4
    # solder nut claims ~10mm of every corner, fixtures since 2026-09-01)
    # sits exactly where the first rungs land, and a ladder that gives up
    # at 2x margin yields the zero-fiducial board the round-3 session
    # measured (and 2/3 at 4x — a part near a corner eats the freed rung).
    # The deeper rungs stay a bounded, obstacle-independent superset —
    # the router's pre-claim grows by a few never-used corner discs,
    # which is the documented cost of the superset trick (docstring
    # above).
    for idx, (sx, sy) in enumerate(_FIDUCIAL_CORNER_SIGNS):
        for m in (
            margin_mm,
            margin_mm * 2.0,
            margin_mm * 3.0,
            margin_mm * 4.0,
            margin_mm * 5.0,
            margin_mm * 6.0,
        ):
            cand = _corner_point(sx, sy, x0, y0, x1, y1, m)
            if not _circle_inside_polygon(cand, radius, poly):
                continue
            if any(_polygon_overlaps_circle(ring, cand, standoff) for ring in rings):
                continue
            sites.append((idx, cand))
    return sites


def build_fiducials(
    outline: list[tuple[float, float]] | list[list[float]],
    pads: list[dict[str, Any]],
    *,
    layers: list[str],
    ir: PcbIR | None = None,
    count: int = FIDUCIAL_COUNT,
    margin_mm: float = FIDUCIAL_MARGIN_MM,
    copper_dia_mm: float = FIDUCIAL_COPPER_DIA_MM,
    mask_dia_mm: float = FIDUCIAL_MASK_DIA_MM,
) -> FiducialResult:
    """``count`` (default 3) optical targets near non-collinear corners of
    ``outline``'s bounding box, spanning EVERY layer in ``layers`` —
    inset by ``margin_mm``, each checked clear of every REAL pad in
    ``pads`` (a component pad flooding a fiducial's mask opening is
    exactly as fab-fatal as a refdes label doing it — see this module's
    docstring) and fully inside ``outline`` (never straddling the board
    edge).

    A fiducial is a whole-stack registration feature, not a single-layer
    one: a fab aligns inner-layer films and BOTH soldermask films to the
    same optical target, so the geometry this mints carries a copper disc
    on every entry of ``layers`` (see ``FiducialResult.pads``'s own
    docstring for how the outer two of those double as the top/bottom
    mask openings) and one router keep-out — sized once, off ``layers``
    as a whole, in ``plane_blockers`` — that :func:`precis.pcb.planes.
    cut_antipads` applies per-layer against whatever pour actually exists
    there.

    The corner PICK itself is layer-independent (candidacy only ever
    checks the outline, the parts thicket and real pads, never which
    copper layer a fiducial will occupy), so ``layers`` affects only how
    many pad/blocker entries a chosen fiducial mints, never where the
    three land.

    Corners are tried in a fixed order (:data:`_FIDUCIAL_CORNER_SIGNS`)
    chosen so the first ``count`` that SUCCEED are never collinear — a
    caller's test should assert that property directly rather than trust
    this docstring (task brief, verbatim: "assert that property in a
    test rather than trusting the placement code").

    Never silently drops below ``count``: a board short a fiducial
    because every corner it tried was blocked is a real fact about that
    board (parts crowd every corner, or the outline is tiny relative to
    ``margin_mm``), reported in ``dropped`` — never smoothed over by
    placing one somewhere a pick-and-place machine wasn't told to look.
    """
    fids: list[Point] = []
    dropped: list[str] = []
    if len(outline) >= 3:
        radius = mask_dia_mm / 2.0
        candidates = fiducial_candidate_sites(
            outline, ir=ir, margin_mm=margin_mm, mask_dia_mm=mask_dia_mm
        )
        for idx in range(len(_FIDUCIAL_CORNER_SIGNS)):
            if len(fids) >= count:
                break
            placed_this_corner = False
            for cand_idx, cand in candidates:
                if cand_idx != idx:
                    continue
                if any(_circle_overlaps_pad(cand, radius, pad) for pad in pads):
                    continue
                if any(
                    math.hypot(cand[0] - fx, cand[1] - fy) < 2 * radius
                    for fx, fy in fids
                ):
                    continue
                fids.append(cand)
                placed_this_corner = True
                break
            if not placed_this_corner:
                dropped.append(
                    f"fiducial corner {idx}: no candidate rung clear of the "
                    "outline edge, the parts thicket, or a pad -- skipped"
                )
    if len(fids) < count:
        dropped.append(
            f"only {len(fids)}/{count} fiducial(s) placed -- the rest had no "
            "corner clear of a pad, another fiducial, or the outline edge"
        )
    fid_pads = [
        {
            "layer": layer_name,
            "net": "",
            "shape": "circle",
            "x": fx,
            "y": fy,
            "w": copper_dia_mm,
            "role": "fiducial",
        }
        for fx, fy in fids
        for layer_name in layers
    ]
    plane_blockers = [
        {
            "ctype": "via",
            "net": "",
            "x": fx,
            "y": fy,
            "dia_mm": mask_dia_mm,
            "layers": list(layers),
            "role": "fiducial",
        }
        for fx, fy in fids
    ]
    silk_keepouts = [
        {"shape": "circle", "x": fx, "y": fy, "w": mask_dia_mm, "role": "fiducial"}
        for fx, fy in fids
    ]
    return FiducialResult(
        fiducials=tuple(fids),
        pads=fid_pads,
        plane_blockers=plane_blockers,
        silk_keepouts=silk_keepouts,
        dropped=tuple(dropped),
    )


# ─────────────────────────────────────────────────────────────────────
# board identification (title block) — silk text, so it survives on the
# physical board; see the module docstring for why this never invents a
# date.
# ─────────────────────────────────────────────────────────────────────

#: Cap height for the identification block, mm — deliberately larger
#: than :data:`DEFAULT_REFDES_HEIGHT_MM` so it reads as a title, not
#: another part's label, at the SAME silk pen (``stroke_width_mm``) every
#: other draw in this module uses.
TITLE_HEIGHT_MM = 1.5

#: Vertical gap between the block's stacked lines, mm — proportional to
#: ``height_mm`` the same way ``build_silk``'s refdes candidate gap is
#: (``height_mm * 0.3``), not an independently-invented constant.
TITLE_LINE_GAP_MM = 0.5

#: Inset from the outline bbox's bottom-right corner, mm — the
#: conventional drawing-title-block corner, and (with the default
#: 3-fiducial count) the one corner :func:`build_fiducials`'s corner
#: order does NOT claim by default, so the two features don't compete
#: for the same spot before either has to relocate.
TITLE_MARGIN_MM = 2.0

#: The corner fallback ladder :func:`build_title_block` walks, tried in
#: this order — bottom-right FIRST (the conventional drawing-title-block
#: spot, and this ladder's previous only behaviour) before the remaining
#: three. Each entry is ``(name, x_at_max, y_at_max, h_align, v_align,
#: stack_sign)``:
#:
#: - ``x_at_max``/``y_at_max`` pick which bbox edge the corner insets
#:   from (``True`` -> ``x1``/``y1``, the high edge; ``False`` -> ``x0``/
#:   ``y0``, the low edge) — the same corner-parameterization
#:   :data:`_FIDUCIAL_CORNER_SIGNS` uses for fiducials, just spelled as
#:   bools instead of +-1 since every consumer here wants a branch, not
#:   arithmetic.
#: - ``h_align``/``v_align`` are the :mod:`precis.pcb.stroke_font` alignment
#:   that keeps a line's advance box growing AWAY from the corner's own
#:   edges and into the board — ``"right"`` at a right-hand corner (text
#:   ends at the anchor, extends left), ``"left"`` at a left-hand corner
#:   (mirrors the reasoning); ``v_align="baseline"`` at a bottom corner
#:   (glyphs sit above their baseline), ``v_align="top"`` at a top corner
#:   (glyphs hang below their cap line) — same "grow inward, never off the
#:   board" rule applied to the vertical axis. Fixed h_align="right" at a
#:   fixed corner was the original defect: at a LEFT corner that would
#:   run the text off the board edge instead of into it.
#: - ``stack_sign`` is which way successive stacked lines (name -> REV ->
#:   date) move: ``+1`` (up, toward the board interior) from a bottom
#:   corner, ``-1`` (down, toward the board interior) from a top corner —
#:   stacking upward from a TOP corner was the other half of the same
#:   defect (the second and third lines would walk off the top edge).
_TITLE_CORNERS: tuple[tuple[str, bool, bool, str, str, float], ...] = (
    ("bottom-right", True, False, "right", "baseline", 1.0),
    ("bottom-left", False, False, "left", "baseline", 1.0),
    ("top-right", True, True, "right", "top", -1.0),
    ("top-left", False, True, "left", "top", -1.0),
)


@dataclass(frozen=True, slots=True)
class TitleBlockResult:
    """``draws`` is a list of :func:`_draw`-shaped items — append them
    directly onto ``model["silkscreen"][side]`` alongside
    :class:`SilkResult`'s own ``draws``. ``bbox`` (closed 4-corner
    polygon, board frame) is the block's own footprint, for a caller that
    wants to fold it into :func:`build_silk`'s ``reserved`` so a refdes
    label placed AFTER the title block never lands on it — see
    :func:`obstacle_from_bbox`."""

    draws: list[dict[str, Any]]
    bbox: list[Point] | None
    dropped: tuple[str, ...] = ()


def obstacle_from_bbox(bbox: list[Point], *, label: str = "") -> dict[str, Any]:
    """A closed polygon's axis-aligned bounding rect, in the same
    ``{"shape":"rect","x","y","w","h"}`` obstacle shape this module's own
    pad-overlap checks already read — the one conversion between
    :class:`TitleBlockResult`'s corner-list ``bbox`` and
    :func:`build_silk`'s ``reserved`` obstacle list, so a caller never
    hand-rolls this arithmetic at the call site.

    ``label`` names the thing for :func:`_obstacle_label`, so a drop
    caused by it reads as "overlaps R3 courtyard silk at (…)" rather than
    as an anonymous rectangle. Optional because the geometry is the
    contract and a caller with nothing useful to say should say nothing
    rather than invent a name."""
    xs = [p[0] for p in bbox]
    ys = [p[1] for p in bbox]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    out: dict[str, Any] = {
        "shape": "rect",
        "x": (x0 + x1) / 2.0,
        "y": (y0 + y1) / 2.0,
        "w": x1 - x0,
        "h": y1 - y0,
    }
    if label:
        out["obstacle"] = label
    return out


def build_title_block(
    outline: list[tuple[float, float]] | list[list[float]],
    pads: list[dict[str, Any]],
    *,
    name: str,
    revision: str | None = None,
    date: str | None = None,
    avoid: list[dict[str, Any]] | None = None,
    side: str = "top",
    height_mm: float = TITLE_HEIGHT_MM,
    stroke_width_mm: float = DEFAULT_SILK_WIDTH_MM,
    margin_mm: float = TITLE_MARGIN_MM,
    capability: CapabilityRow | None = None,
) -> TitleBlockResult:
    """The board's name (required), revision and date (each OPTIONAL —
    only rendered when the caller actually has one), stacked in
    :mod:`precis.pcb.stroke_font`, anchored at an outline bbox CORNER —
    tried in :data:`_TITLE_CORNERS` order (bottom-right first, the
    conventional drawing-title-block spot, then the remaining three) with
    the existing per-corner margin ladder (``margin_mm``, ``1.5x``,
    ``2x``) retried at EACH corner before moving to the next. The first
    corner/margin combination where every line's box lands inside the
    outline and clear of every obstacle wins.

    **Alignment follows the corner, not a fixed "right".** A block
    anchored at a left-hand corner is left-aligned (and stacks in the
    down-into-the-board direction from a top corner) — right-aligning it
    unconditionally would run the text off the board edge the moment the
    ladder ever reached a left corner, and stacking upward from a top
    corner would walk the second/third line off the top edge the same
    way. See :data:`_TITLE_CORNERS` for exactly which alignment/stack
    direction each corner uses and why.

    **Placed FIRST, as an obstacle everything else avoids — not last,
    scavenging leftover space.** :func:`build_silk`'s own refdes ladder
    can relocate a label around a part it already owns the position of;
    a title block has no part to relocate relative to and is bigger than
    a refdes, so hunting for a gap AFTER every part's silk already
    landed would make its own placement depend on placement order (a
    part added late could strand it anywhere). Calling this BEFORE
    :func:`build_silk` and feeding its ``bbox`` into that function's
    ``reserved`` (via :func:`obstacle_from_bbox`) makes every part's
    label avoid the title block instead, deterministically, regardless
    of instance order.

    **Never fabricates a date.** ``date`` must come from the caller — a
    ``pcb_boards`` row (or the ref's own metadata) carries whatever
    actually exists; there is no authored "release date" field in the
    schema today (only a row's own ``created_at``, which is NOT the same
    thing but is at least real, persisted data rather than
    ``datetime.now()`` called at render time — the latter would make two
    renders of the same design differ, breaking the byte-identical
    determinism ``render_fab_svg``/the gerber writers are tested for).
    Passing ``date=None`` (the default) omits the date line entirely
    rather than inventing one.
    """
    lines = [name]
    if revision:
        lines.append(f"REV {revision}")
    if date:
        lines.append(date)
    if len(outline) < 3:
        return TitleBlockResult(
            draws=[],
            bbox=None,
            dropped=("title block: no board outline -- nothing to anchor it to",),
        )
    poly = [(float(p[0]), float(p[1])) for p in outline]
    x0, y0, x1, y1 = _bbox(poly)
    mirror = side == "bottom"
    # Vias in ``avoid`` (tagged by :func:`_via_pad`) are SOFT obstacles:
    # the first pass treats them as hard, but if no corner clears them
    # the same ladder reruns via-blind — title text over a tented via is
    # a cosmetic compromise, a silently DROPPED title block is a missing
    # feature (measured 2026-09-01: spreading the stitch sprinkle left
    # ~1 via in every corner candidate rect, so an all-hard check
    # dropped the furniture from the whole board).
    hard = _mask_openings(pads, capability) + [
        a for a in (avoid or ()) if a.get("obstacle") != "via"
    ]
    via_soft = [a for a in (avoid or ()) if a.get("obstacle") == "via"]
    passes: list[list[dict[str, Any]]] = [hard + via_soft]
    if via_soft:
        passes.append(hard)
    gap = height_mm + TITLE_LINE_GAP_MM
    # The slide rung: corner mounting hardware (M4 solder nuts, fixtures
    # since 2026-09-01) reaches ~10mm into every corner, and a purely
    # diagonal margin ladder (2/3/4mm) can never escape it. Sliding is
    # OUTERMOST (after the obstacle pass) so every plain corner is tried
    # before any slid position — a free corner still beats "same corner,
    # slid inward", preserving the original corner-preference order.
    for obstacles, slide, (
        corner_name,
        x_at_max,
        y_at_max,
        h_align,
        v_align,
        stack_sign,
    ) in (
        (obs, s, corner)
        for obs in passes
        for s in (0.0, 8.0, 14.0)
        for corner in _TITLE_CORNERS
    ):
        # `stroke_font`'s mirror negates the LOCAL x coordinate about the
        # anchor (mirror applied before rotate, same order `landpattern.
        # rotate_offset` pins everywhere else) -- so an unmirrored
        # "right"-aligned box (spanning [-width, 0] locally, anchor at its
        # right edge) becomes a mirrored box spanning [0, width] (anchor at
        # its LEFT edge instead), which for a corner near the board's high
        # x edge runs straight past it. Swapping left/right here, only for
        # the mirrored (bottom-side) case, cancels that negation so the
        # block still hugs the SAME corner side its `h_align` names,
        # regardless of side -- the glyphs themselves still mirror (that
        # flag is passed through unchanged below), only the block's own
        # anchor edge is compensated.
        draw_h_align = h_align
        if mirror:
            draw_h_align = "left" if h_align == "right" else "right"
        for m in (margin_mm, margin_mm * 1.5, margin_mm * 2.0):
            # `slide` (outer rung, see the comment above the loop) moves
            # the anchor inward along the horizontal edge, past corner
            # hardware the diagonal margin can never clear.
            anchor_x = x1 - m - slide if x_at_max else x0 + m + slide
            anchor_y = y1 - m if y_at_max else y0 + m
            boxes: list[list[Point]] = []
            strokes_all: list[list[Point]] = []
            ok = True
            for i, line in enumerate(lines):
                ay = anchor_y + i * gap * stack_sign
                corners = stroke_font.text_bbox_corners(
                    line,
                    anchor=(anchor_x, ay),
                    height_mm=height_mm,
                    rotation_deg=0.0,
                    mirror=mirror,
                    h_align=draw_h_align,
                    v_align=v_align,
                )
                if not all(point_in_polygon(c, poly) for c in corners):
                    ok = False
                    break
                if any(_box_overlaps_pad(corners, pad) for pad in obstacles):
                    ok = False
                    break
                strokes = stroke_font.layout_text(
                    line,
                    anchor=(anchor_x, ay),
                    height_mm=height_mm,
                    rotation_deg=0.0,
                    mirror=mirror,
                    h_align=draw_h_align,
                    v_align=v_align,
                )
                if any(
                    _stroke_overlaps_any_pad(pts, obstacles, stroke_width_mm)
                    for pts in strokes
                ):
                    ok = False
                    break
                boxes.append(corners)
                strokes_all.extend(strokes)
            if ok and strokes_all:
                all_corners = [c for box in boxes for c in box]
                bx0 = min(c[0] for c in all_corners)
                bx1 = max(c[0] for c in all_corners)
                by0 = min(c[1] for c in all_corners)
                by1 = max(c[1] for c in all_corners)
                bbox = [(bx0, by0), (bx1, by0), (bx1, by1), (bx0, by1)]
                draws = [
                    _draw(pts, stroke_width_mm, role="title", refdes="")
                    for pts in strokes_all
                ]
                return TitleBlockResult(draws=draws, bbox=bbox, dropped=())
    tried = ", ".join(c[0] for c in _TITLE_CORNERS)
    return TitleBlockResult(
        draws=[],
        bbox=None,
        dropped=(
            f"title block: no spot near any outline corner ({tried}) clear of "
            f"a pad within {margin_mm * 2.0:.1f}mm margin -- dropped",
        ),
    )


# ─────────────────────────────────────────────────────────────────────
# S/N label patch -- a filled silk box with "S/N" knocked out of it, next
# to the title block. A practical label: an assembler writes the real
# serial number onto it with a Sharpie, and the knocked-out "S/N" says
# what the blank patch is FOR. Board-level, same as the title block
# (there is no per-instance "serial number" concept), and deliberately
# built as this module's first NON-stroke-only silk primitive:
#
# - a solid ``G36``/``G37`` REGION for the box (:func:`_region_draw`) --
#   :func:`precis.pcb.gerber._emit_region` already writes exactly this
#   for a copper pour, and a region is legal on any layer, silkscreen
#   included; :func:`precis.pcb.gerber.silkscreen_gerber` recognises this
#   shape and reuses that same writer rather than a second one.
# - the letters, drawn with :mod:`precis.pcb.stroke_font` same as every
#   other text in this module, but each stroke carries
#   ``"polarity": "clear"`` (:func:`_clear_draw`) so
#   ``silkscreen_gerber`` wraps it in ``%LPC*%``/``%LPD*%`` -- the same
#   dark-then-clear-then-back-to-dark idiom ``_emit_region`` already uses
#   for a pour's antipad holes, just spelled with a stroke instead of a
#   hole ring (a knocked-out letter is a hole shaped like a glyph, not a
#   filled polygon). See :mod:`precis.pcb.gerber_view`'s module docstring
#   for why the VIEWER has to track ``%LPC*%`` on a stroke too, not only
#   on a region, for this to render correctly rather than diverging from
#   the gerber it was rendered from.
# ─────────────────────────────────────────────────────────────────────

#: The knocked-out label itself -- the task, verbatim: identify what a
#: blank patch is for.
SN_LABEL = "S/N"

#: Cap height of the "S/N" letters, mm -- the same documented-default
#: status as :data:`DEFAULT_REFDES_HEIGHT_MM`/:data:`TITLE_HEIGHT_MM`: a
#: cosmetic/typographic choice every silk generator makes, not a
#: fabrication figure.
SN_LABEL_HEIGHT_MM = 1.5

#: Padding inside the box, mm -- between the box edge and whatever is
#: closest to it (the "S/N" label, or the blank writing area), AND the
#: gap between the label and the writing area itself. Proportional to
#: the label height (the same "gap = height_mm * k" idiom
#: :func:`build_silk`'s own refdes candidate gap and
#: :data:`TITLE_LINE_GAP_MM` already use), floored so a tiny
#: ``label_height_mm`` still leaves a visually real margin rather than
#: letters butting straight against the box edge.
SN_BOX_PADDING_MM_FACTOR = 0.6
SN_BOX_PADDING_MIN_MM = 0.3

#: How many placeholder characters of BLANK writing space the box
#: reserves beside the label, sized at the label's own height -- "clear
#: writing space" (task brief) means room for this many real characters
#: an assembler can Sharpie on legibly, not an arbitrarily-chosen box
#: width. The box does not enforce this as a hard length limit, it is
#: just the budget the box is SIZED for.
SN_WRITE_SPACE_CHARS = 8

#: The knockout stroke's width, as a fraction of ``label_height_mm``,
#: when a caller doesn't override it -- an APPEARANCE/manufacturing-
#: process choice (a hairline knockout does not reliably survive the
#: silkscreen process and would fill back in), never a published fab
#: figure. Deliberately NOT sourced from ``capabilities.py``'s
#: ``silk_width_mm``: that field is the fab's printability FLOOR (what
#: survives the process at all), which :func:`drc.check_silk_printability`
#: now enforces as a floor. This factor is a legibility choice sitting
#: above it -- the two answer different questions and must not collapse.
SN_KNOCKOUT_WIDTH_FACTOR = 0.22

#: Floor under the knockout stroke width, mm -- the same "proportional,
#: but never below a manufacturable/legible floor" idiom
#: :func:`_pin1_tick`'s own ``tick_len`` already uses, so a caller
#: passing a tiny ``label_height_mm`` doesn't get an unfabricable
#: knockout.
SN_KNOCKOUT_MIN_WIDTH_MM = 0.15


@dataclass(frozen=True, slots=True)
class SnPatchResult:
    """Same contract as :class:`TitleBlockResult`: ``draws`` append
    directly onto ``model["silkscreen"][side]`` (mixed stroke/region
    shapes -- see the module docstring above), ``bbox`` (closed 4-corner
    polygon, board frame) is the patch's own footprint for a caller
    folding it into :func:`build_silk`'s ``reserved`` (via
    :func:`obstacle_from_bbox`) so a refdes label placed after it never
    prints across the patch, and a board with no room for it says so in
    ``dropped`` rather than silently omitting the patch."""

    draws: list[dict[str, Any]]
    bbox: list[Point] | None
    dropped: tuple[str, ...] = ()


def _region_draw(polygon: list[Point]) -> dict[str, Any]:
    return {
        "shape": "region",
        "polygon": [list(p) for p in polygon],
        "source": "synthesized",
        "role": "sn-box",
        "refdes": "",
    }


def _clear_draw(points: list[Point], width_mm: float) -> dict[str, Any]:
    """A knockout-letter stroke -- exactly :func:`_draw`'s shape, plus
    the one extra key :func:`precis.pcb.gerber.silkscreen_gerber` reads
    to wrap it in ``%LPC*%``/``%LPD*%`` instead of drawing it dark."""
    draw = _draw(points, width_mm, role="sn-text", refdes="")
    draw["polarity"] = "clear"
    return draw


def _candidate_rects(
    outline_bbox: tuple[float, float, float, float],
    title_bbox: list[Point] | None,
    w: float,
    h: float,
    gap: float,
) -> list[tuple[float, float]]:
    """Candidate ``(x0, y0)`` lower-left corners for a ``w`` x ``h`` box,
    tried in order: NEXT TO ``title_bbox`` first (task brief) -- above it
    (right- then left-aligned) and beside it (left-of, then right-of,
    both bottom-aligned) -- and THEN, whether because there is no title
    block at all or because every adjacent spot failed, each corner of
    the outline's own bbox (the same four-corner fallback
    :func:`build_title_block`/:func:`build_fiducials` already use for
    board-level silk with nothing else to anchor to)."""
    x0, y0, x1, y1 = outline_bbox
    out: list[tuple[float, float]] = []
    if title_bbox is not None:
        tx0, ty0, tx1, ty1 = _bbox(title_bbox)
        out += [
            (tx1 - w, ty1 + gap),  # above the title block, right-aligned to it
            (tx0, ty1 + gap),  # above it, left-aligned
            (tx0 - gap - w, ty0),  # left of it, bottom-aligned
            (tx1 + gap, ty0),  # right of it, bottom-aligned
        ]
    out += [
        (x1 - gap - w, y1 - gap - h),  # outline top-right
        (x0 + gap, y1 - gap - h),  # outline top-left
        (x1 - gap - w, y0 + gap),  # outline bottom-right
        (x0 + gap, y0 + gap),  # outline bottom-left
    ]
    # Edge-CENTRE fallbacks, after the corners: corner mounting hardware
    # (M4 solder nuts, fixtures since 2026-09-01) occupies every corner
    # of a board that authors it, and a ladder with only corner rungs
    # then drops the patch entirely. The middle of the bottom/top edge is
    # clear of the nuts by construction and still a conventional spot for
    # a serial-number field.
    cx = (x0 + x1 - w) / 2.0
    out += [
        (cx, y0 + gap),  # bottom edge, centred
        (cx, y1 - gap - h),  # top edge, centred
    ]
    return out


def build_sn_patch(
    outline: list[tuple[float, float]] | list[list[float]],
    pads: list[dict[str, Any]],
    *,
    title_bbox: list[Point] | None = None,
    avoid: list[dict[str, Any]] | None = None,
    side: str = "top",
    label_height_mm: float = SN_LABEL_HEIGHT_MM,
    write_chars: int = SN_WRITE_SPACE_CHARS,
    knockout_width_mm: float | None = None,
    capability: CapabilityRow | None = None,
) -> SnPatchResult:
    """A filled silk box with "S/N" knocked out of it -- a practical
    label patch (task brief, verbatim) an assembler writes a real serial
    number onto with a Sharpie, sized wider than the label alone:
    ``write_chars`` (default 8) placeholder characters' worth of BLANK
    space, at the label's own height, sit beside it -- real writing room,
    not just a tight label bbox (task brief: "must leave clear writing
    space beside/below the letters"). See the module docstring above
    this section for the two gerber-level pieces this emits (a solid
    region for the box, clear-polarity strokes for the letters).

    **Placement** is tried adjacent to ``title_bbox`` first (this patch
    belongs NEXT TO the title block, task brief) via
    :func:`_candidate_rects`, then at each outline-bbox corner as a
    fallback -- each candidate checked fully inside ``outline`` and clear
    of every real pad in ``pads`` plus the caller's ``avoid`` obstacles
    (a fiducial's ``silk_keepouts``, the title block's own bbox if the
    caller didn't already pass it as ``title_bbox``, etc.), same
    corner-ladder-with-obstacle-check discipline
    :func:`build_title_block` already uses. Never silently omitted: a
    board with no clear spot for it says so in ``dropped``, and this
    return's ``bbox`` is ``None`` -- exactly :class:`TitleBlockResult`'s
    own drop contract.

    **Not folded into ``build_silk``'s obstacle avoidance by this
    function.** Like the title block, the caller places this BEFORE
    :func:`build_silk` and feeds its ``bbox`` into that function's
    ``reserved`` (:func:`obstacle_from_bbox`) so every part's own silk
    avoids it, deterministically, regardless of instance order -- see
    :func:`build_title_block`'s own docstring for why "placed first, an
    obstacle everything else avoids" is the right order rather than
    hunting for a gap after every part's silk already landed.
    """
    if len(outline) < 3:
        return SnPatchResult(
            draws=[],
            bbox=None,
            dropped=("S/N patch: no board outline -- nothing to anchor it to",),
        )
    poly = [(float(p[0]), float(p[1])) for p in outline]
    outline_bbox = _bbox(poly)
    mirror = side == "bottom"
    # Same soft-via discipline as :func:`build_title_block`: vias (tagged
    # by :func:`_via_pad`) never HARD-reject a candidate — a board whose
    # stitch vias sprinkle everywhere would otherwise drop the patch
    # entirely. Here the compromise is REAL (a via bump under the writing
    # surface degrades it), so the ladder is SCORED: first via-free
    # candidate wins outright, otherwise the fewest-vias candidate is
    # taken and the count reported in ``dropped`` (the warnings channel).
    hard = _mask_openings(pads, capability) + [
        a for a in (avoid or ()) if a.get("obstacle") != "via"
    ]
    via_soft = [a for a in (avoid or ()) if a.get("obstacle") == "via"]

    pad_mm = max(label_height_mm * SN_BOX_PADDING_MM_FACTOR, SN_BOX_PADDING_MIN_MM)
    label_w = stroke_font.text_width_mm(SN_LABEL, label_height_mm)
    write_w = stroke_font.text_width_mm("0" * write_chars, label_height_mm)
    box_w = pad_mm * 3.0 + label_w + write_w  # left pad + label + gap + write area
    box_h = label_height_mm + pad_mm * 2.0
    knockout_w = (
        knockout_width_mm
        if knockout_width_mm is not None
        else max(label_height_mm * SN_KNOCKOUT_WIDTH_FACTOR, SN_KNOCKOUT_MIN_WIDTH_MM)
    )
    # A mirrored (bottom-side) box has the SAME rectangle -- only the
    # letters inside it need to mirror. "left"-aligned text anchored at
    # the box's own left edge, drawn through `layout_text`'s mirror,
    # would run off the LEFT of the box instead of into it (mirror
    # negates local x about the anchor before rotate -- see
    # `build_title_block`'s own comment on the identical issue);
    # swapping to "right" cancels that negation so the label still hugs
    # the box's left edge and grows into the box regardless of side.
    label_h_align = "right" if mirror else "left"

    candidates = list(_candidate_rects(outline_bbox, title_bbox, box_w, box_h, pad_mm))
    # Pass 1 walks the ladder with vias hard (first via-free spot wins).
    # Pass 2 re-walks it via-blind — but scored: of every candidate that
    # clears the HARD obstacles, take the one covering the FEWEST vias
    # (ladder order breaks ties), not the first. Ladder-order acceptance
    # here put the patch over 4 stitch vias when a later candidate had 1.
    best: tuple[int, int, float, float] | None = None  # (vias, order, x0, y0)
    for rank, (x0, y0) in enumerate(candidates):
        corners = [
            (x0, y0),
            (x0 + box_w, y0),
            (x0 + box_w, y0 + box_h),
            (x0, y0 + box_h),
        ]
        if not all(point_in_polygon(c, poly) for c in corners):
            continue
        if any(_box_overlaps_pad(corners, pad) for pad in hard):
            continue
        vias_here = sum(1 for v in via_soft if _box_overlaps_pad(corners, v))
        if vias_here == 0:
            best = (0, rank, x0, y0)
            break
        if best is None or vias_here < best[0]:
            best = (vias_here, rank, x0, y0)
    if best is not None:
        vias_under, _rank, x0, y0 = best
        corners = [
            (x0, y0),
            (x0 + box_w, y0),
            (x0 + box_w, y0 + box_h),
            (x0, y0 + box_h),
        ]
        label_anchor = (x0 + pad_mm, y0 + pad_mm)
        text_strokes = stroke_font.layout_text(
            SN_LABEL,
            anchor=label_anchor,
            height_mm=label_height_mm,
            rotation_deg=0.0,
            mirror=mirror,
            h_align=label_h_align,
            v_align="baseline",
        )
        draws = [
            _region_draw(corners),
            *[_clear_draw(pts, knockout_w) for pts in text_strokes],
        ]
        return SnPatchResult(
            draws=draws,
            bbox=corners,
            dropped=(
                (
                    f"S/N patch: no via-free spot -- placed over "
                    f"{vias_under} via(s) (the fewest of any candidate); "
                    "the writing surface will have bumps",
                )
                if vias_under
                else ()
            ),
        )

    return SnPatchResult(
        draws=[],
        bbox=None,
        dropped=(
            "S/N patch: no spot near the title block or any outline corner "
            "clear of a pad -- dropped",
        ),
    )


_REFDES_RE = re.compile(r"^([A-Za-z]*)(\d*)(.*)$")


def _refdes_sort_key(refdes: str) -> tuple[str, int, str]:
    """Natural (numeric-aware) sort key for a reference designator —
    ``C2`` before ``C10``, the order a human reads a BOM in, never raw
    string order (which would put ``"C10"`` before ``"C2"``). Always
    matches (the trailing ``(.*)`` group absorbs anything left over, so
    an unconventional refdes still sorts, deterministically, rather than
    raising): ``(letter prefix, numeric part or -1 if none, remainder)``."""
    m = _REFDES_RE.match(refdes)
    assert m is not None  # the pattern's trailing .* group always matches
    prefix, digits, rest = m.group(1), m.group(2), m.group(3)
    return (prefix, int(digits) if digits else -1, rest)


#: Fallback silk-to-board-edge margin, mm — used only when a board's
#: capability row carries no ``board_edge_clearance_vcut_mm`` figure (a
#: process JLC publishes none for) or there is no row at all, the same
#: "``None`` falls back to a documented constant" contract every other
#: capability-derived figure in this module already follows
#: (:func:`silk_clearance_mm`, :func:`soldermask_expansion_mm`). JLC's
#: 4-layer house-default V-cut figure — the same number
#: :func:`precis.pcb.realize._board_edge_min_mm` sizes the routing grid's
#: own inset with.
DEFAULT_BOARD_EDGE_CLEARANCE_MM = 0.6


def _silk_edge_margin_mm(capability: CapabilityRow | None) -> float:
    """How far silk must sit from the board's own cut edge, mm.

    Reads this board's ``board_edge_clearance_vcut_mm`` at
    :func:`~precis.pcb.capabilities.design_value`'s usual tier (house
    default, then JLC's published minimum, then
    :data:`DEFAULT_BOARD_EDGE_CLEARANCE_MM`) — the SAME field
    :func:`precis.pcb.realize._board_edge_min_mm` reads to inset the
    routing grid and :func:`precis.pcb.drc.check_board_edge_clearance`
    reads to grade copper, so a candidate this module accepts and the
    checker that grades the finished board never disagree about what
    "too close to the edge" means.

    **Re-derived here, not imported.** :mod:`precis.pcb.realize` does not
    import this module today, but :mod:`precis.handlers.pcb` calls both,
    and title-block/fiducial placement already lives here — a future
    ``realize -> silk`` import would turn a ``silk -> realize`` import
    back into a real cycle. A second small reader of the same capability
    field costs far less than that.
    """
    return design_value(
        capability,
        "board_edge_clearance_vcut_mm",
        fallback=DEFAULT_BOARD_EDGE_CLEARANCE_MM,
    )


def _box_inside_outline(
    corners: list[Point], outline: list[Point], margin_mm: float
) -> bool:
    """True iff every one of ``corners`` sits inside ``outline`` AND at
    least ``margin_mm`` clear of every one of its edges.

    The containment-plus-clearance test a generation-side candidate uses
    so silk never lands outside the board or hugs its cut edge.
    Corner-only, not the full stroke: ``corners`` is
    :func:`~precis.pcb.stroke_font.text_bbox_corners`'s bounding box (or
    an equivalent axis box a caller already built), which bounds every
    stroke a label draws — the same "check the box, not each glyph"
    shortcut :func:`_box_overlaps_pad` already takes at the very same call
    site. ``outline`` may be open or closed (repeating its first point at
    the end is not required)."""
    ring = outline if outline[0] == outline[-1] else [*outline, outline[0]]
    for c in corners:
        if not point_in_polygon(c, outline):
            return False
        if any(
            dist_point_to_segment(c, a, b) < margin_mm
            for a, b in itertools.pairwise(ring)
        ):
            return False
    return True


#: One refdes label candidate, resolved to the board: ``(spot, anchor,
#: corners, h_align, v_align)``. ``corners`` is the text bbox the
#: acceptance checks and the committed obstacle both use, so the two can
#: never disagree about where the label is. ``spot`` names the
#: :data:`LABEL_SPOT_ORDER` spot or the ladder's ``"ring N at Xdeg"``.
_LabelCandidate = tuple[str, Point, list[Point], str, str]


def _label_candidates(
    refdes: str,
    cx: float,
    cy: float,
    box_board: list[Point],
    *,
    mirror: bool,
    height_mm: float,
    stroke_width_mm: float,
) -> list[_LabelCandidate]:
    """Every refdes spot for one placed part, in try order, resolved to a
    world anchor and bbox ONCE (an acceptance test and a reorder test built
    from two independently computed boxes is this module's own named
    recurring defect). Order (gr495394/gr496552): the
    :data:`LABEL_SPOT_ORDER` corners and sides just outside the outline;
    then the quarter-edge anchors between each corner and side-centre
    ("a nearby free spot outside the courtyard"); then the radial ladder
    (:data:`_CANDIDATES`), further out but still outside. There is no
    inside last resort — a part with no clear
    outside spot drops its label rather than print it under the package
    (gr496552, Reto 2026-10-18: "it still should not be printed under
    parts"). A part with no outline (pinless) has nothing to be inside
    or outside of and walks the ladder from its centre.

    ``box_board`` is the courtyard as it sits ON THE BOARD (rotated and
    mirrored, still relative to the part origin), not the part's own
    frame: glyphs are laid out in the board frame, so a part-frame anchor
    put a rot-180 0402's "below" label above it, its v_align growing the
    text back over the part (heater-base-test, 2026-10-02). Two parts with
    the same ``box_board`` therefore get the same offsets, which is what
    lets an aligned group share a spot (:func:`_aligned_label_groups`)."""
    # Never nearer the outline than a label may sit (`_label_gap_mm`): a
    # ladder rung inside that gap is a candidate the acceptance test can
    # only reject.
    gap = max(height_mm * 0.3, _label_gap_mm(stroke_width_mm))
    raw: list[tuple[Point, str, str, str]] = list(
        _outline_spot_candidates(box_board, stroke_width_mm)
    )
    if box_board:
        # Edge-quarter anchors (gr496552, "a nearby free spot outside the
        # courtyard"): two more near-in spots per edge, between each
        # corner and the side-centre candidate, still `gap` off the
        # outline. In congestion a quarter-edge box threads where the
        # corner and side-centre boxes cannot.
        xs = [p[0] for p in box_board]
        ys = [p[1] for p in box_board]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        for f in (0.25, 0.75):
            mid_x = x0 + f * (x1 - x0)
            mid_y = y0 + f * (y1 - y0)
            raw += [
                ((mid_x, y0 - gap), "center", "top", f"bottom edge {f:.0%}"),
                ((x0 - gap, mid_y), "right", "middle", f"left edge {f:.0%}"),
                ((x1 + gap, mid_y), "left", "middle", f"right edge {f:.0%}"),
                ((mid_x, y1 + gap), "center", "baseline", f"top edge {f:.0%}"),
            ]
    for du, dv, h_align, v_align, spot in _CANDIDATES:
        # Directional, not one scalar radius: an elongated part reaches
        # much further along its own long axis than across it, and the
        # square courtyard this replaced pushed every label out by the
        # larger of the two (see `_courtyard_support_mm`).
        ring = math.hypot(du, dv)
        ux, uy = du / ring, dv / ring
        reach = _courtyard_support_mm(box_board, ux, uy) + gap
        off = reach + (ring - 1.0) * min(reach, _LADDER_STEP_CAP_MM)
        raw.append(((ux * off, uy * off), h_align, v_align, spot))

    out: list[_LabelCandidate] = []
    for board_offset, h_align, v_align, spot in raw:
        ax, ay = cx + board_offset[0], cy + board_offset[1]
        if mirror:
            # Bottom-side glyphs mirror about the anchor, which flips which
            # way a left/right-aligned label grows; swap so the text still
            # extends the way the spot intends.
            h_align = {"left": "right", "right": "left"}.get(h_align, h_align)
        corners = stroke_font.text_bbox_corners(
            refdes,
            anchor=(ax, ay),
            height_mm=height_mm,
            rotation_deg=0.0,
            mirror=mirror,
            h_align=h_align,
            v_align=v_align,
        )
        out.append((spot, (ax, ay), corners, h_align, v_align))
    return out


def _label_strokes(
    refdes: str, cand: _LabelCandidate, *, mirror: bool, height_mm: float
) -> list[list[Point]]:
    _spot, anchor, _corners, h_align, v_align = cand
    return stroke_font.layout_text(
        refdes,
        anchor=anchor,
        height_mm=height_mm,
        rotation_deg=0.0,
        mirror=mirror,
        h_align=h_align,
        v_align=v_align,
    )


@dataclass(frozen=True, slots=True)
class _RingGeom:
    """One part outline as the label checks read it: its side, the
    centreline, the filled area it encloses, and the bbox the broad phase
    reads first."""

    side: str
    line: Any
    area: Any
    bounds: tuple[float, float, float, float]


def _ring_geoms(rings: dict[int, tuple[str, list[Point]]]) -> dict[int, _RingGeom]:
    out: dict[int, _RingGeom] = {}
    for inst, (side, ring) in rings.items():
        line = _ShapelyLineString(ring)
        out[inst] = _RingGeom(
            side=side,
            line=line,
            area=_ShapelyPolygon(ring).buffer(0),
            bounds=line.bounds,
        )
    return out


#: How far, in em, any glyph's centreline leaves its advance box (the
#: box :func:`~precis.pcb.stroke_font.text_bbox_corners` returns): '#',
#: '(' and ')' reach 1/3 em below the baseline. A label box at least this
#: far inside the edge margin needs no per-stroke edge check. No glyph
#: leaves its box sideways today; ``-x`` still guards the left side.
_GLYPH_OVERHANG_EM = max(
    max(-y, y - 1.0, -x, 0.0)
    for glyph in stroke_font.GLYPHS.values()
    for stroke in glyph
    for x, y in stroke
)


def _label_blocked(
    corners: list[Point],
    strokes_of: Callable[[], list[list[Point]]],
    *,
    inst: int,
    side_name: str,
    obstacles: list[dict[str, Any]],
    rings: dict[int, _RingGeom],
    outline_ring: list[Point] | None,
    edge_margin_mm: float,
    stroke_width_mm: float,
) -> bool:
    """The ONE refdes acceptance test, shared by the per-part placement and
    the aligned-group spot choice so the two can never disagree about
    whether a spot is clear. ``strokes_of`` lays the text out only once the
    cheap bbox tests have passed.

    Against every part outline on this side, placed or not yet (gr495394:
    a label never overlaps another outline, and keeps
    :data:`OUTLINE_GAP_STROKES` clear line widths off any outline,
    :func:`_label_gap_mm`): the text box may not touch or sit inside ANY
    part's outline — its own included (gr496552: no label inside a
    courtyard, ever) — nor come within the gap of any outline line.
    Checked on the text BOX, not the glyphs: stricter, and the
    same box :func:`_outline_spot_candidates` spaces off the outline."""
    # Glyphs can protrude past their advance box; include that reach for
    # the broad phase as well as the ink's own stroke width.
    label_height = math.dist(corners[0], corners[3])
    nearby = _near_obstacles(
        obstacles, corners, stroke_width_mm + _GLYPH_OVERHANG_EM * label_height
    )
    if any(_box_overlaps_pad(corners, pad) for pad in nearby):
        return True
    # The DRC grades the INK against the edge, and the box is neither: it
    # bounds stroke centrelines, which reach its top and bottom while a
    # few glyphs ('_', 'Q', brackets) drop below or rise above it, and its
    # advance width carries letter spacing no ink fills. So the box only
    # has to sit inside the board; a box within the margin plus half a
    # stroke plus the widest overhang has its strokes measured below
    # (gr495395: J_N1's box cleared the margin, its ink printed 0.021 mm
    # and its '_' 0.116 mm inside it).
    if outline_ring is not None and not _box_inside_outline(corners, outline_ring, 0.0):
        return True
    ink_margin = edge_margin_mm + stroke_width_mm / 2.0
    near_edge = outline_ring is not None and not _box_inside_outline(
        corners,
        outline_ring,
        ink_margin + _GLYPH_OVERHANG_EM * math.dist(corners[0], corners[3]),
    )
    gap = _label_gap_mm(stroke_width_mm) - 1e-9
    box = _ShapelyPolygon(corners)
    bx0, by0, bx1, by1 = box.bounds
    for other, geom in rings.items():
        if geom.side != side_name:
            continue
        gx0, gy0, gx1, gy1 = geom.bounds
        if other != inst and (
            gx0 > bx1 + gap or gx1 < bx0 - gap or gy0 > by1 + gap or gy1 < by0 - gap
        ):
            continue
        if box.distance(geom.line) < gap:
            return True
        if box.intersects(geom.area):
            return True
    strokes = strokes_of()
    if near_edge and outline_ring is not None:
        edge = _ShapelyLineString(
            outline_ring
            if outline_ring[0] == outline_ring[-1]
            else [*outline_ring, outline_ring[0]]
        )
        if any(
            edge.distance(_ShapelyLineString(pts)) < ink_margin
            for pts in strokes
            if len(pts) >= 2
        ):
            return True
    # bbox cleared but a real stroke may not have
    return any(
        _stroke_overlaps_any_pad(pts, nearby, stroke_width_mm) for pts in strokes
    )


#: At most this many placed labels move to free one dropped label's spot
#: (:func:`_repair_dropped_labels`).
_LABEL_REPAIR_MAX_MOVERS = 2

#: Repair sweeps over the dropped labels; a sweep that frees nothing ends
#: it early. A later sweep can use a spot an earlier move opened.
_LABEL_REPAIR_PASSES = 3


@dataclass(slots=True)
class _LabelState:
    """One part's refdes label as the greedy pass left it, enough for
    :func:`_repair_dropped_labels` to move it: its candidates in try
    order, which one it took (``None`` when dropped), the obstacle it
    committed, and where its census row and draws live."""

    inst: int
    refdes: str
    side_name: str
    mirror: bool
    bucket: list[dict[str, Any]]
    side_obstacles: _SideObstacles
    candidates: list[_LabelCandidate]
    default_spot: str
    pinned_spot: str | None
    census_index: int
    taken: int | None = None
    obstacle: dict[str, Any] | None = None


def _repair_dropped_labels(
    states: list[_LabelState],
    census: list[SilkPlacement],
    *,
    rings: dict[int, _RingGeom],
    outline_ring: list[Point] | None,
    edge_margin_mm: float,
    stroke_width_mm: float,
    height_mm: float,
) -> None:
    """Give a dropped label a spot that only other labels block, by moving
    those labels to other clear spots of their own (gr495395).

    The greedy pass hands out spots in refdes order, first fit. A part
    that comes early takes its first clear spot even when a later
    neighbour had no other: measured on the nano board, 6-10 of the
    30-40 dropped labels per seed had a spot whose only blocker was
    another part's label. Each drop is retried here once all silk is
    committed: for each of its spots in order, if nothing but at most
    :data:`_LABEL_REPAIR_MAX_MOVERS` placed labels block it, and each of
    them (none pinned by a ``label_spot`` override) has another spot
    clear of everything else, the new label and the movers already
    planned, they move and the dropped label takes the freed spot. Repeats
    for :data:`_LABEL_REPAIR_PASSES` sweeps. Never drops a placed label,
    so the drop count can only fall. Mutates ``census`` and the states'
    buckets and obstacle lists in place."""

    def blocked(
        state: _LabelState, cand: _LabelCandidate, obstacles: list[dict[str, Any]]
    ) -> bool:
        return _label_blocked(
            cand[2],
            functools.partial(
                _label_strokes,
                state.refdes,
                cand,
                mirror=state.mirror,
                height_mm=height_mm,
            ),
            inst=state.inst,
            side_name=state.side_name,
            obstacles=obstacles,
            rings=rings,
            outline_ring=outline_ring,
            edge_margin_mm=edge_margin_mm,
            stroke_width_mm=stroke_width_mm,
        )

    def commit(state: _LabelState, k: int, reason: str | None) -> None:
        cand = state.candidates[k]
        spot, _anchor, corners, _h, _v = cand
        state.bucket[:] = [
            d
            for d in state.bucket
            if not (d.get("role") == "refdes" and d.get("refdes") == state.refdes)
        ]
        for pts in _label_strokes(
            state.refdes, cand, mirror=state.mirror, height_mm=height_mm
        ):
            state.bucket.append(
                _draw(pts, stroke_width_mm, role="refdes", refdes=state.refdes)
            )
        if state.obstacle is not None:
            state.side_obstacles.replace_all(
                [o for o in state.side_obstacles if o is not state.obstacle]
            )
        state.obstacle = obstacle_from_bbox(
            corners, label=f"{state.refdes} refdes silk"
        )
        state.side_obstacles.append(state.obstacle)
        state.taken = k
        census[state.census_index] = SilkPlacement(
            refdes=state.refdes,
            kind="refdes",
            side=state.side_name,
            outcome="placed" if spot == state.default_spot else "relocated",
            reason=None
            if spot == state.default_spot
            else f"refdes label moved off the default {state.default_spot} spot "
            f"(drawn at {spot}){reason or ''}",
            stroke_width_mm=stroke_width_mm,
            height_mm=height_mm,
            x_mm=min(p[0] for p in corners),
            y_mm=min(p[1] for p in corners),
        )

    def plan_moves(
        dropped: _LabelState, cand_a: _LabelCandidate
    ) -> list[tuple[_LabelState, int]] | None:
        owners = {id(s.obstacle): s for s in states if s.obstacle is not None}
        local = _near_obstacles(
            dropped.side_obstacles, cand_a[2], stroke_width_mm + height_mm
        )
        fixed = [o for o in local if id(o) not in owners]
        if blocked(dropped, cand_a, fixed):
            return None
        strokes_a = _label_strokes(
            dropped.refdes, cand_a, mirror=dropped.mirror, height_mm=height_mm
        )
        movers = sorted(
            (
                owners[id(o)]
                for o in local
                if id(o) in owners
                and (
                    _box_overlaps_pad(cand_a[2], o)
                    or any(
                        _stroke_overlaps_any_pad(p, [o], stroke_width_mm)
                        for p in strokes_a
                    )
                )
            ),
            # Natural refdes order, not obstacle-list order: which label
            # moves first cannot ride on where the caller happened to
            # append it (gr498680).
            key=lambda m: _refdes_sort_key(m.refdes),
        )
        if not movers or len(movers) > _LABEL_REPAIR_MAX_MOVERS:
            return None
        if any(m.candidates[m.taken or 0][0] == m.pinned_spot for m in movers):
            return None
        moving = {id(m.obstacle) for m in movers}
        planned = [obstacle_from_bbox(cand_a[2], label=f"{dropped.refdes} refdes silk")]
        moves: list[tuple[_LabelState, int]] = []
        for mover in movers:
            rest = [o for o in mover.side_obstacles if id(o) not in moving] + planned
            kb = next(
                (
                    k
                    for k, cand_b in enumerate(mover.candidates)
                    if k != mover.taken and not blocked(mover, cand_b, rest)
                ),
                None,
            )
            if kb is None:
                return None
            moves.append((mover, kb))
            planned.append(
                obstacle_from_bbox(
                    mover.candidates[kb][2], label=f"{mover.refdes} refdes silk"
                )
            )
        return moves

    for _pass in range(_LABEL_REPAIR_PASSES):
        repaired = False
        for dropped in [s for s in states if s.taken is None]:
            for ka, cand_a in enumerate(dropped.candidates):
                moves = plan_moves(dropped, cand_a)
                if moves is None:
                    continue
                for mover, kb in moves:
                    commit(mover, kb, f", making room for {dropped.refdes}'s label")
                names = ", ".join(m.refdes for m, _kb in moves)
                commit(dropped, ka, f", after {names} moved")
                repaired = True
                break
        if not repaired:
            break


#: Two parts sit on one row (or column) when their origins agree to this
#: tolerance across the row. Placements imported from an EDA tool or snapped
#: to a grid agree exactly; this only absorbs float noise.
_LABEL_GROUP_ALIGN_MM = 0.01

#: Neighbouring members of an aligned row stay one group while their
#: pitch is at most this many courtyard lengths along the row, i.e. at most
#: one empty part-slot between them. Further apart, the two are separate
#: parts that happen to share a coordinate, and forcing them onto one spot
#: would cost an isolated part its best spot for a tidiness nobody can see.
_LABEL_GROUP_MAX_PITCH_EXTENTS = 2.0


def _aligned_label_groups(
    shapes: dict[int, tuple[str, Point, list[Point]]],
) -> list[tuple[int, ...]]:
    """Rows and columns of identical parts whose labels should share one
    spot (heater-base-test, 2026-10-02: in a column of nine 0402s the last
    one labelled below itself, because only its below spot was not blocked
    by a neighbour, while the other eight labelled to the right).

    ``shapes`` maps an instance to ``(side, origin, box_board)``. Two parts
    are identical here when their board-frame courtyards are the same
    point set (:func:`_label_candidates` then gives them the same offsets),
    which already folds in footprint, rotation mod 180 for a symmetric
    part, and mirroring. A run is aligned when every origin agrees on one
    axis to :data:`_LABEL_GROUP_ALIGN_MM` and contiguous per
    :data:`_LABEL_GROUP_MAX_PITCH_EXTENTS`.

    A part in both a row and a column (a grid) joins the larger run; ties
    go to the column, then to the run whose first member sorts first.
    Members are returned in instance order within each run."""
    by_key: dict[tuple[Any, ...], list[int]] = {}
    for inst, (side, (x, y), box_board) in shapes.items():
        if not box_board:
            continue
        shape_key = tuple(sorted((round(px, 3), round(py, 3)) for px, py in box_board))
        by_key.setdefault(
            (0, side, shape_key, round(x / _LABEL_GROUP_ALIGN_MM)), []
        ).append(inst)
        by_key.setdefault(
            (1, side, shape_key, round(y / _LABEL_GROUP_ALIGN_MM)), []
        ).append(inst)

    runs: list[tuple[int, tuple[int, ...]]] = []
    for (axis, *_), members in by_key.items():
        if len(members) < 2:
            continue
        # A column (axis 0, shared x) runs along y; a row along x.
        along = 1 - axis
        box = shapes[members[0]][2]
        extent = max(p[along] for p in box) - min(p[along] for p in box)
        limit = _LABEL_GROUP_MAX_PITCH_EXTENTS * extent
        members.sort(key=lambda i: (shapes[i][1][along], shapes[i][1][axis]))
        run = [members[0]]
        for prev, cur in itertools.pairwise(members):
            if shapes[cur][1][along] - shapes[prev][1][along] > limit:
                if len(run) > 1:
                    runs.append((axis, tuple(run)))
                run = []
            run.append(cur)
        if len(run) > 1:
            runs.append((axis, tuple(run)))

    # Ordering ties geometrically (smallest member origin), never by
    # instance index — ``ir`` array order must not shape the silk
    # (gr498680).
    runs.sort(key=lambda r: (-len(r[1]), r[0], min(shapes[m][1] for m in r[1])))
    taken: set[int] = set()
    groups: list[tuple[int, ...]] = []
    for _axis, members_run in runs:
        # A member already claimed by a larger run breaks this one into the
        # pieces on either side of it; each piece of two or more is still a
        # contiguous row of its own.
        piece: list[int] = []
        # `members_run` is in order along the row, so a piece stays contiguous.
        for member in (*members_run, None):
            if member is not None and member not in taken:
                piece.append(member)
                continue
            if len(piece) > 1:
                groups.append(tuple(sorted(piece)))
            piece = []
        taken.update(members_run)
    return groups


def build_silk(
    ir: PcbIR,
    pads: list[dict[str, Any]],
    *,
    vias: list[dict[str, Any]] | None = None,
    instance_sides: dict[str, str] | None = None,
    reserved: dict[str, list[dict[str, Any]]] | None = None,
    height_mm: float = DEFAULT_REFDES_HEIGHT_MM,
    stroke_width_mm: float = DEFAULT_SILK_WIDTH_MM,
    capability: CapabilityRow | None = None,
    outline: list[tuple[float, float]] | None = None,
    polarized: frozenset[str] | None = None,
    unpolarized: frozenset[str] | None = None,
    label_spots: dict[str, str] | None = None,
) -> SilkResult:
    """Build ``{"top": [...], "bottom": [...]}`` silk draws for every
    PLACED instance in ``ir`` — the one builder :mod:`precis.handlers.pcb`
    calls for every gerber/svg-fab site that needs silk (module
    docstring's "one silk builder, never duplicated" discipline).

    ``polarized`` names the refdes of every instance that carries REAL
    polarity — the escape hatch from the module docstring's "Not every
    part needs one" pin-1 policy: an R/C/L/FB refdes (by FAMILY,
    :func:`_refdes_family`) gets no pin-1 attempt unless it appears here.
    Every other family is unaffected regardless of this set's contents.
    ``None`` (default) is the empty set — every pre-2026-09-01 caller's
    behaviour is unchanged for non-R/C/L/FB parts, and an R/C/L/FB part
    now gets ``outcome="not_applicable"`` instead of a pin-1 attempt.
    Determining WHICH refdes are polarized is the caller's job
    (:mod:`precis.handlers.pcb` reads it off a ``"polarized": true``
    component flag or a label match) — this module only knows refdes and
    geometry, never labels.

    ``unpolarized`` (gr520373) is the mirror set: refdes EXPLICITLY
    declared unpolarized (``"polarized": false`` on the component), which
    suppresses the pin-1 attempt for ANY family — not only the
    R/C/L/FB-exempt ones — and wins over ``polarized`` membership. The
    footprint-level "no pin-1 mark" the policy used to grant only by
    accident of refdes prefix. ``None`` = empty, unchanged behaviour.

    ``pads`` is the board's real flashed pad geometry, in
    :mod:`precis.pcb.gerber`'s ``model["pads"]`` shape — used ONLY to
    decide what silk would be scraped off, never to place anything (pad
    positions come from ``pads``, everything else comes from ``ir``).

    ``vias`` is a SEPARATE obstacle set, not folded into ``pads``: a via is
    round (``x``, ``y``, ``dia_mm``, ``drill_mm``) and carries a ``span``
    layer-name pair (:mod:`precis.pcb.realize`'s ``to_gerber_model``/
    ``pcb_copper_list`` shape) rather than a rectangular ``w``/``h`` —
    different enough geometry that folding it into ``pads`` would either
    lose the span or force every ``pads`` consumer to learn a new key. Same
    "a fab scrapes silk off exposed copper" reasoning as a pad: a via's
    annulus is exposed copper on whichever side(s) its plating barrel
    reaches (:func:`_via_reaches_side` — a blind/buried via does not
    threaten the side it never touches), so only vias reaching a given side
    obstruct silk drawn on THAT side.

    ``reserved`` is the caller's BOARD-level obstacle set, keyed by side,
    ``pads``-shaped (``{"shape": "circle"|"rect", "x", "y", "w", "h"?}``) —
    silk this function did not draw and must still avoid: a
    :func:`build_fiducials` fiducial's ``silk_keepouts``, or a
    :func:`build_title_block` block's footprint via
    :func:`obstacle_from_bbox`. This is also the ONE mechanism cross-part
    avoidance uses (module docstring's "Avoidance is GLOBAL" section):
    each side's obstacle list starts at ``pads + that side's via annuli +
    the IR's mounting holes + reserved`` and every
    courtyard/tick/refdes-label this function itself
    draws is folded straight back into that SAME growing list
    (:func:`obstacle_from_bbox`) the moment it survives its own check —
    so a part's own later silk, and every part processed after it, checks
    against everything committed so far on that side. Processing order is
    NATURAL refdes order (:func:`_refdes_sort_key`), not ``ir``'s raw
    instance-array order — see the module docstring for why that's the
    deterministic, reproducible choice.

    ``capability`` is this board's fab-capability row — the source of both
    terms of :func:`silk_clearance_mm` (what sizes every courtyard) and of
    the soldermask expansion each ``pads`` entry is widened by before any
    clearance test runs (:func:`_mask_opening`). ``None`` falls back to
    this module's own documented constants, so a caller that has no row
    (a stackup with no published process) still gets a board rather than
    a crash — see :func:`~precis.pcb.capabilities.design_value`.

    ``outline`` is this board's cut-edge polygon, world coordinates, the
    same shape :func:`build_title_block`/:func:`build_fiducials` already
    take — optional (``None`` keeps every pre-2026-08-31 caller's
    behaviour: no board edge to check, exactly as before). When given, a
    refdes candidate and a pin-1 dot candidate that would land outside
    ``outline`` or inside its silk-to-edge margin
    (:func:`_silk_edge_margin_mm`) are rejected, same as landing on a pad
    or on committed silk (module docstring's edge-margin note). The
    courtyard ring and the corner tick are NOT filtered by this — see
    that same note for why.

    A courtyard ring bulges out round a routed via only where that costs
    no silk (gr495393 option c, Reto 2026-10-18: "better algo should not
    require any label missing"). The board is built with every bulge, and
    again with none; while the bulged build drops an item the no-bulge
    build kept, the bulges within :data:`_BULGE_VETO_REACH_MM` of the
    dropped item's part are vetoed (the ring dents or is cut there
    instead) and the board rebuilt. If the widest reach still loses one,
    the no-bulge build stands.

    ``label_spots`` is the per-instance label override (gr496552: "we
    can move it"), refdes -> a :data:`LABEL_SPOT_ORDER` name; the named
    spot becomes that part's FIRST candidate, ahead of even an aligned
    group's shared spot, with the rest of the order unchanged behind it
    when the named spot is blocked. Names are the board-frame spots the
    module docstring defines; an unknown name is ignored (the write
    path, ``op='move'``'s ``label_spot``, validates). An overridden part
    sits out of :func:`_aligned_label_groups` — the spot its row shares
    is a tidiness default, and an explicit spot outranks it.
    """
    common: dict[str, Any] = {
        "vias": vias,
        "instance_sides": instance_sides,
        "reserved": reserved,
        "height_mm": height_mm,
        "stroke_width_mm": stroke_width_mm,
        "capability": capability,
        "outline": outline,
        "polarized": polarized,
        "unpolarized": unpolarized,
        "label_spots": label_spots,
    }
    result, bulges = _build_silk_pass(ir, pads, bulge_veto=frozenset(), **common)
    if not bulges:
        return result
    base, _none = _build_silk_pass(ir, pads, bulge_veto=None, **common)
    kept = {(c.refdes, c.kind) for c in base.census if c.outcome != "dropped"}
    centre = {
        str(ir.instance_refdes[i]): (float(ir.inst_x[i]), float(ir.inst_y[i]))
        for i in range(ir.n_instances)
    }
    veto: set[tuple[str, str]] = set()
    for reach in _BULGE_VETO_REACH_MM:
        lost = {
            c.refdes
            for c in result.census
            if c.outcome == "dropped" and (c.refdes, c.kind) in kept
        }
        if not lost:
            return result
        more = {
            (refdes, label)
            for refdes, items in bulges.items()
            for label, x, y in items
            if any(
                math.hypot(x - centre[r][0], y - centre[r][1]) <= reach
                for r in lost
                if r in centre
            )
        }
        if more <= veto:
            continue
        veto |= more
        result, bulges = _build_silk_pass(
            ir, pads, bulge_veto=frozenset(veto), **common
        )
        if not bulges:
            break
    if not any(
        c.outcome == "dropped" and (c.refdes, c.kind) in kept for c in result.census
    ):
        return result
    return base


#: How far from a part whose silk a bulge cost (mm, part origin to via
#: centre) :func:`build_silk` vetoes bulges, widening each round: the part's
#: own neighbourhood first, then its label ladder's reach.
_BULGE_VETO_REACH_MM = (2.0, 5.0, 10.0)


def _build_silk_pass(
    ir: PcbIR,
    pads: list[dict[str, Any]],
    *,
    vias: list[dict[str, Any]] | None,
    instance_sides: dict[str, str] | None,
    reserved: dict[str, list[dict[str, Any]]] | None,
    height_mm: float,
    stroke_width_mm: float,
    capability: CapabilityRow | None,
    outline: list[tuple[float, float]] | None,
    polarized: frozenset[str] | None,
    unpolarized: frozenset[str] | None,
    label_spots: dict[str, str] | None,
    bulge_veto: frozenset[tuple[str, str]] | None,
) -> tuple[SilkResult, dict[str, list[tuple[str, float, float]]]]:
    """One :func:`build_silk` pass with these bulges vetoed (``None``: no
    bulges); also returns the bulges kept, by refdes."""
    sides = instance_sides or {}
    polarized_set = polarized or frozenset()
    unpolarized_set = unpolarized or frozenset()
    label_spots = label_spots or {}
    top: list[dict[str, Any]] = []
    bottom: list[dict[str, Any]] = []
    census: list[SilkPlacement] = []
    reserved_by_side = reserved or {}

    # Both fab-derived, resolved ONCE: `clearance_mm` sets how far every
    # courtyard sits off its own pads, and each pad becomes the mask
    # OPENING every clearance test below actually runs against.
    clearance_mm = silk_clearance_mm(capability, stroke_width_mm=stroke_width_mm)
    _floor_mm, target_mm = outline_offsets_mm(
        capability, stroke_width_mm=stroke_width_mm
    )
    pads = _mask_openings(pads, capability)

    # Resolved ONCE, same as `clearance_mm` above: `None` when the caller
    # gave no outline at all, so every check below is a no-op and every
    # pre-2026-08-31 caller keeps its exact prior behaviour.
    outline_ring: list[Point] | None = (
        [(float(p[0]), float(p[1])) for p in outline] if outline else None
    )
    edge_margin_mm = _silk_edge_margin_mm(capability) if outline_ring else 0.0

    layer_names = [str(layer.get("name") or i) for i, layer in enumerate(ir.stackup)]
    top_via_pads: list[dict[str, Any]] = []
    bottom_via_pads: list[dict[str, Any]] = []
    for via in vias or ():
        reaches_top, reaches_bottom = _via_reaches_side(via.get("span"), layer_names)
        via_pad = _via_pad(via)
        if reaches_top:
            top_via_pads.append(via_pad)
        if reaches_bottom:
            bottom_via_pads.append(via_pad)

    # Mounting holes join the seed set on BOTH sides (2026-10-29,
    # taz-leaf: R26's refdes printed inside the 5 mm zip-tie hole at
    # (133,30) -- no silk check knew the hole was there). THT hardware
    # spans the board -- a screw head above it, a solder nut below -- so
    # `plated` is irrelevant here, and a part-owned hole (a connector's
    # standoff) blocks every label including its owner's, the same
    # policy pads follow.
    hole_obstacles = _mounting_hole_obstacles(ir, capability)

    # The ONE obstacle list per side (module docstring): seeded with the
    # real pads REACHING that side (gr520373: every pad used to seed BOTH
    # sides, so bottom silk could be scraped off by top copper -- a
    # collision that cannot exist; `_pad_reaches_side` is the pad
    # counterpart of the via split just above), that side's via annuli,
    # the mounting holes, and the caller's board-level `reserved`
    # geometry -- then grown in
    # place as each instance's own silk survives, so it is simultaneously
    # "what this instance must avoid" and "what the NEXT instance (and
    # this instance's later draws) must avoid".
    # Sorted canonically (:func:`_obstacle_sort_key`): the set is what
    # matters, and the first-hit naming in `_stroke_hits` shouldn't see
    # the caller's list order either.
    obstacles_by_side: dict[str, _SideObstacles] = {
        "top": _SideObstacles(
            sorted(
                [
                    *[p for p in pads if _pad_reaches_side(p, layer_names)[0]],
                    *top_via_pads,
                    *hole_obstacles,
                    *reserved_by_side.get("top", []),
                ],
                key=_obstacle_sort_key,
            )
        ),
        "bottom": _SideObstacles(
            sorted(
                [
                    *[p for p in pads if _pad_reaches_side(p, layer_names)[1]],
                    *bottom_via_pads,
                    *hole_obstacles,
                    *reserved_by_side.get("bottom", []),
                ],
                key=_obstacle_sort_key,
            )
        ),
    }

    pins_of_inst: dict[int, list[int]] = {}
    for pid in range(ir.n_pins):
        pins_of_inst.setdefault(int(ir.pin_instance[pid]), []).append(pid)

    # Natural refdes order, not raw array order (module docstring's
    # "Order is instance-independent" section) -- who gets first pick of
    # a contested spot is a real decision now that avoidance is global, so
    # it has to be a stated, reproducible one rather than upstream
    # netlist/DB iteration order.
    placed = [
        inst
        for inst in range(ir.n_instances)
        if not (
            math.isnan(float(ir.inst_x[inst])) or math.isnan(float(ir.inst_y[inst]))
        )
    ]
    order = sorted(placed, key=lambda i: _refdes_sort_key(str(ir.instance_refdes[i])))

    # Every placed instance's courtyard ring, in world coordinates,
    # resolved BEFORE the loop starts.
    #
    # A pin-1 dot joins the shared obstacle list the moment its own part is
    # processed, so on a crowded board it can land exactly where a part
    # processed LATER has to draw its body outline -- and the later part is
    # the one that loses, by nothing more principled than refdes order.
    # Measured on the 40mm fixture when the dot became the primary marker:
    # R3 and U3 each lost their ENTIRE courtyard to a neighbour's dot, at
    # 44% and 47% of perimeter kept against the 50% floor. Knowing all the
    # rings up front lets the dot yield to them instead, which is the right
    # precedence whichever order the parts happen to come in: an outline is
    # the larger and far less relocatable mark, and a dot has seven other
    # directions and three distances still to try.
    #
    # The rings themselves come from `part_outlines` (gr495393): settled
    # against every other part and the board edge at once, never clipped
    # by other silk afterwards, and redrawn around the routed vias they
    # would print on.
    outlines = part_outlines(
        ir,
        capability=capability,
        stroke_width_mm=stroke_width_mm,
        instance_sides=sides,
        outline=outline_ring,
        obstacles_by_side=cast(dict[str, list[dict[str, Any]]], obstacles_by_side),
        bulge_veto=bulge_veto,
    )
    bulges = {
        str(ir.instance_refdes[inst]): list(o.bulged)
        for inst, o in outlines.items()
        if o.bulged
    }
    courtyard_ring: dict[int, tuple[str, list[Point]]] = {
        inst: (o.side, o.ring) for inst, o in outlines.items()
    }

    # One label spot per aligned row/column of identical parts, chosen
    # BEFORE the loop. Placement is greedy per part, so without this the
    # one member whose preferred spot happens to be free (the end of a
    # column, nothing below it) labels differently from all the others.
    # The spot must clear every member against the pre-silk obstacles,
    # every courtyard, each other, and must not sit over any other part's
    # body. In the loop each member tries it first; if silk committed
    # before its turn took it, that member alone falls back.
    group_spot: dict[int, tuple[int, str, int]] = {}
    label_shapes: dict[int, tuple[str, Point, list[Point]]] = {}
    for inst in placed:
        if inst not in courtyard_ring:
            continue
        if str(ir.instance_refdes[inst]) in label_spots:
            # A per-part label_spot override outranks the shared spot a
            # group would pick (`build_silk`'s own docstring): the member
            # neither shapes the group's choice nor takes it first.
            continue
        rot = float(ir.inst_rot[inst])
        side_name = courtyard_ring[inst][0]
        label_shapes[inst] = (
            side_name,
            (float(ir.inst_x[inst]), float(ir.inst_y[inst])),
            _place(
                instance_courtyard_polygon(
                    ir, inst, clearance_mm=target_mm, pins=pins_of_inst.get(inst, [])
                ),
                cx=0.0,
                cy=0.0,
                rot=0.0 if math.isnan(rot) else rot,
                mirror=side_name == "bottom",
            ),
        )
    seed_obstacles = {
        side: _SideObstacles(list(obs)) for side, obs in obstacles_by_side.items()
    }
    ring_geoms = _ring_geoms(courtyard_ring)
    for group in _aligned_label_groups(label_shapes):
        group_side = label_shapes[group[0]][0]
        cands = {
            m: _label_candidates(
                str(ir.instance_refdes[m]),
                *label_shapes[m][1],
                label_shapes[m][2],
                mirror=group_side == "bottom",
                height_mm=height_mm,
                stroke_width_mm=stroke_width_mm,
            )
            for m in group
        }
        for k, (spot, *_rest) in enumerate(cands[group[0]]):
            boxes: list[list[Point]] = []
            for m in group:
                cand = cands[m][k]
                corners = cand[2]
                if any(convex_polygons_overlap(corners, b) for b in boxes):
                    break
                if _label_blocked(
                    corners,
                    functools.partial(
                        _label_strokes,
                        str(ir.instance_refdes[m]),
                        cand,
                        mirror=group_side == "bottom",
                        height_mm=height_mm,
                    ),
                    inst=m,
                    side_name=group_side,
                    obstacles=seed_obstacles[
                        "bottom" if group_side == "bottom" else "top"
                    ],
                    rings=ring_geoms,
                    outline_ring=outline_ring,
                    edge_margin_mm=edge_margin_mm,
                    stroke_width_mm=stroke_width_mm,
                ):
                    break
                boxes.append(corners)
            else:
                group_spot.update((m, (k, spot, len(group))) for m in group)
                break

    label_states: list[_LabelState] = []
    for inst in order:
        cx, cy = float(ir.inst_x[inst]), float(ir.inst_y[inst])
        refdes = str(ir.instance_refdes[inst])
        rot = float(ir.inst_rot[inst])
        rot = 0.0 if math.isnan(rot) else rot
        side_name = _side_for(sides, refdes)
        mirror = side_name == "bottom"
        bucket = bottom if mirror else top
        # Only the vias whose plating barrel reaches THIS side are an
        # obstacle to silk drawn on it (module docstring). Direct
        # reference, not a copy: appends below are visible to every
        # later check on this side, this instance's own and every other's.
        side_obstacles = obstacles_by_side["bottom" if mirror else "top"]

        pins = pins_of_inst.get(inst, [])
        # Empty for a pinless instance (mounting hole, fiducial) — no land
        # pattern, so no courtyard, rather than an invented size. The
        # undeformed target-offset polygon: it sizes and keys the pin-1 dot
        # and label searches, so identical parts get identical offsets
        # however their neighbours bent their drawn outlines.
        box_local = instance_courtyard_polygon(
            ir, inst, clearance_mm=target_mm, pins=pins
        )

        # 1) courtyard/body outline -- `part_outlines` already settled its
        # shape against the other parts and the board edge, so it is only
        # checked against COPPER and board furniture (the seed obstacles:
        # pads, this side's vias, `reserved`), never against silk committed
        # by an earlier part (gr495393: that is what cut U2's outline).
        # Never folded into `side_obstacles`: later silk checks the rings
        # exactly (`ring_geoms`), and the pin-1 tick below is designed to
        # touch this SAME outline's own corner.
        courtyard_kept = False
        box_pts: list[Point] = []
        part_outline = outlines.get(inst)
        if box_local and part_outline is not None:
            box_pts = part_outline.ring
            hard = seed_obstacles["bottom" if mirror else "top"]
            hit = None
            runs: list[list[Point]] = []
            clip_pieces = 0
            kept_mm = 0.0
            # A whole outline is not lost to one via. Break it around
            # whatever it meets and keep the rest -- what a real silk
            # generator emits, and what the fab would have trimmed to
            # anyway (`_clip_polyline`). Only a courtyard with too little
            # left to read as an outline still drops.
            for run in part_outline.runs:
                run_hit = _stroke_hits(run, hard, stroke_width_mm)
                if run_hit is None:
                    runs.append(run)
                    kept_mm += _polyline_length(run)
                    continue
                hit = hit or run_hit
                pieces, kept, _total = _clip_polyline(
                    run,
                    _near_obstacles(hard, run, 2.0 * stroke_width_mm),
                    stroke_width_mm,
                )
                runs += pieces
                clip_pieces += len(pieces)
                kept_mm += kept
            # What the outline draws on a bare board: the ring less the
            # designed gaps (board edge, and the below-floor stretch of a
            # shared wall). The share kept is measured against this, so
            # an edge connector's gap never counts as loss.
            designed_mm = (
                _polyline_length(box_pts)
                - part_outline.edge_open_mm
                - part_outline.floor_open_mm
            )
            causes = []
            if hit is not None:
                causes.append(f"overlaps {_obstacle_label(hit)}")
            if part_outline.meets:
                causes.append(
                    "lies on or inside the outline of "
                    + ", ".join(part_outline.meets)
                    + " (drawn first)"
                )
            # The piece cap counts what ``_clip_polyline`` PRODUCED — an
            # outline's designed gaps (edge/shared-wall openings already
            # broken into ``runs``) are not confetti it made.
            shattered = clip_pieces > _COURTYARD_MAX_PIECES
            too_little = (
                designed_mm <= 0 or kept_mm / designed_mm < _COURTYARD_MIN_KEPT_FRACTION
            )
            if not runs or (causes and (too_little or shattered)):
                tail = (
                    f"clipped into {clip_pieces} pieces that no longer "
                    f"read as one outline ({kept_mm:.2f} of "
                    f"{designed_mm:.2f}mm kept)"
                    if shattered and not too_little
                    else "leaving too little to read as an outline "
                    f"({kept_mm:.2f} of {designed_mm:.2f}mm)"
                )
                census.append(
                    SilkPlacement(
                        refdes=refdes,
                        kind="courtyard",
                        side=side_name,
                        outcome="dropped",
                        reason=(
                            "courtyard outline "
                            + (
                                " and ".join(causes)
                                if causes
                                else "lies wholly inside the board's silk edge margin"
                            )
                            + f", {tail} -- dropped"
                        ),
                        stroke_width_mm=stroke_width_mm,
                    )
                )
            else:
                for run in runs:
                    bucket.append(
                        _draw(run, stroke_width_mm, role="outline", refdes=refdes)
                    )
                courtyard_kept = True
                notes = []
                if part_outline.edge_cut:
                    notes.append(
                        "courtyard outline opened at the board edge "
                        f"({part_outline.edge_open_mm:.2f}mm left open to keep "
                        "silk inside the edge margin)"
                    )
                if part_outline.meets:
                    notes.append(
                        "courtyard outline leaves out the stretch on or inside "
                        "the outline of " + ", ".join(part_outline.meets)
                    )
                if part_outline.floor_open_mm:
                    notes.append(
                        "courtyard outline leaves open "
                        f"{part_outline.floor_open_mm:.2f}mm where the shared "
                        "line would sit inside the pad-clearance floor"
                    )
                if part_outline.bent_around:
                    notes.append(
                        "courtyard outline bent around "
                        + ", ".join(part_outline.bent_around)
                    )
                if hit is not None:
                    notes.append(
                        f"courtyard outline broken around {_obstacle_label(hit)}"
                    )
                if causes:
                    notes.append(
                        f"{len(runs)} piece(s) drawn "
                        f"({kept_mm:.2f} of {designed_mm:.2f}mm kept)"
                    )
                census.append(
                    SilkPlacement(
                        refdes=refdes,
                        kind="courtyard",
                        side=side_name,
                        outcome="relocated" if notes else "placed",
                        reason="; ".join(notes) or None,
                        stroke_width_mm=stroke_width_mm,
                    )
                )

        # 2) pin-1 marker -- the DOT is attempted whether or not this
        # instance's own courtyard survived (module docstring's "A pin-1
        # tick never survives alone" decision, narrowed by gr503436: the
        # dot is a polarity marker beside pin 1's own land, not an
        # annotation on the outline, so it still points at real geometry
        # with the outline gone). Only the corner-TICK fallback is tied
        # to the courtyard: a tick is a corner-cut of that outline,
        # meaningless -- and unreadable as stray ink -- without it.
        # Checked against the shared obstacle list -- a dot touching its
        # own courtyard is rejected by the body-region test below, not by
        # obstacle membership.
        tick_obstacle: dict[str, Any] | None = None
        # Policy check FIRST, ahead of the dot search below:
        # an unpolarized part gets no pin-1 attempt regardless of whether
        # its courtyard survived -- there is no mark to have lost, so
        # "the courtyard was dropped" would be the wrong reason to give.
        # Two ways to be unpolarized (gr520373): an R/C/L/FB part the
        # caller's `polarized` set does not name, OR an explicit
        # `polarized: false` declaration on ANY family (the `unpolarized`
        # set -- the footprint-level opt-out that used to exist only for
        # the exempt families). The explicit flag wins even over
        # `polarized`/`polarized_set` membership.
        family = _refdes_family(refdes)
        no_pin1_by_policy = bool(pins) and (
            refdes in unpolarized_set
            or (family in _PIN1_EXEMPT_FAMILIES and refdes not in polarized_set)
        )
        if no_pin1_by_policy:
            census.append(
                SilkPlacement(
                    refdes=refdes,
                    kind="pin1",
                    side=side_name,
                    outcome="not_applicable",
                    reason=(
                        "no polarity -- pin-1 mark not applicable "
                        "(declared 'polarized': false)"
                        if refdes in unpolarized_set
                        else f"no polarity -- pin-1 mark not applicable ({family} "
                        "parts are unpolarized unless declared polarized)"
                    ),
                    stroke_width_mm=stroke_width_mm,
                )
            )
        elif pins:
            pin1 = _pin1_id(ir, inst, pins)
            # **A DOT beside pin 1 first, and the corner tick only as a
            # fallback.** The tick is a corner-CUT of the courtyard
            # outline, so it is drawn along that outline rather than near
            # it: measured on the 40mm fixture, all 20 ticked parts had
            # their tick 0.0000mm from their own courtyard. A mark that
            # coincides exactly with the line it is meant to annotate adds
            # no ink a reader can distinguish, which is why the rendered
            # board appeared to have no pin-1 marker on those parts while
            # `check_silk_missing` reported every one of them present --
            # that rule proves a draw EXISTS, not that it is legible.
            #
            # The tick is also inside the courtyard, and a courtyard
            # encloses the part body, so even printed correctly it ends up
            # under the component once assembled. The dot sits outside,
            # beside the pin it names; the eight parts that reached it by
            # the old fallback path were the only visible pin-1 marks on
            # the board.
            dot_pts, dot_dia = None, stroke_width_mm
            for local_centre, dia in _pin1_dot_candidates(
                ir, pin1, stroke_width_mm, clearance_mm, box_local
            ):
                (dcx, dcy) = _place(
                    [local_centre], cx=cx, cy=cy, rot=rot, mirror=mirror
                )[0]
                # A dot is a zero-length stroke at the dot's own diameter
                # -- the gerber writer's round cap makes it a filled
                # circle, so it needs no new primitive.
                candidate = [(dcx, dcy), (dcx, dcy)]
                if _stroke_overlaps_any_pad(
                    candidate, _near_obstacles(side_obstacles, candidate, dia), dia
                ):
                    continue
                if outline_ring is not None and not _circle_inside_polygon(
                    (dcx, dcy), dia / 2.0 + edge_margin_mm, outline_ring
                ):
                    continue
                # Fully OUTSIDE this instance's own courtyard, not merely
                # "doesn't cross its border line". `_stroke_crosses_stroke`
                # is the border-touching half of the check; a candidate
                # entirely inside the polygon crosses no border, so it
                # must still be rejected outright -- the module
                # docstring's own "the dot sits outside, beside the pin
                # it names" (and, since gr496552, the same is true of the
                # refdes label: no candidate may sit inside any outline).
                # `_polygon_overlaps_circle` already covers both shapes of
                # failure (fully inside, or merely touching the boundary)
                # in one exact (non-sampled) test.
                #
                # Keyed on `box_pts`, not `courtyard_kept` (gr503436): the
                # ring still describes the part's body even when the
                # outline itself was dropped, and a dot inside it prints
                # under the assembled part -- invisible ink.
                if box_pts and _polygon_overlaps_circle(box_pts, (dcx, dcy), dia / 2.0):
                    continue
                # Yield to every OTHER part's body outline on this side,
                # whether or not that part has been processed yet (see
                # `courtyard_ring`): neither on its line nor inside it.
                dot = _ShapelyPoint(dcx, dcy)
                reach = dia / 2.0 + stroke_width_mm / 2.0
                if any(
                    other != inst
                    and geom.side == side_name
                    and geom.bounds[0] - reach <= dcx <= geom.bounds[2] + reach
                    and geom.bounds[1] - reach <= dcy <= geom.bounds[3] + reach
                    and geom.area.distance(dot) <= reach
                    for other, geom in ring_geoms.items()
                ):
                    continue
                dot_pts, dot_dia = candidate, dia
                break

            if dot_pts is not None:
                bucket.append(_draw(dot_pts, dot_dia, role="pin1", refdes=refdes))
                # A circle, not `obstacle_from_bbox`: that helper bounds a
                # POLYGON, and a dot's polygon is a single point -- it
                # would fold in as a zero-area rectangle that nothing
                # could ever collide with.
                tick_obstacle = {
                    "shape": "circle",
                    "x": dot_pts[0][0],
                    "y": dot_pts[0][1],
                    "w": dot_dia,
                    "obstacle": f"{refdes} pin-1 silk",
                }
                census.append(
                    SilkPlacement(
                        refdes=refdes,
                        kind="pin1",
                        side=side_name,
                        outcome="placed",
                        stroke_width_mm=dot_dia,
                    )
                )
            elif courtyard_kept:
                # Nowhere clear beside pin 1. The corner tick is a weak
                # mark for the reasons above, but it still says WHICH
                # corner to a reader who knows to look for it, and that
                # beats dropping the marker entirely. Only reachable with
                # the courtyard drawn: a lone tick reads as unanchored
                # ink (module docstring).
                pin1_xy = _place(
                    [(float(ir.pin_dx[pin1]), float(ir.pin_dy[pin1]))],
                    cx=cx,
                    cy=cy,
                    rot=rot,
                    mirror=mirror,
                )[0]
                tick_pts = _pin1_tick(pin1_xy, box_pts, stroke_width_mm)
                tick_hit = (
                    _stroke_hits(
                        tick_pts,
                        _near_obstacles(side_obstacles, tick_pts, stroke_width_mm),
                        stroke_width_mm,
                    )
                    if tick_pts
                    else None
                )
                # gr520373: the tick used to be the ONE mark never checked
                # against the board edge -- the dot candidates above answer
                # to outline+margin, but a tick beside a part sitting at
                # the edge could still print past it (the prod finding:
                # pin-1 ink 0.04mm off the cut). A mark must never land
                # outside the board or inside its silk-to-edge margin.
                if tick_pts and tick_hit is None and outline_ring is not None:
                    edge_line = _ShapelyLineString(
                        outline_ring
                        if outline_ring[0] == outline_ring[-1]
                        else [*outline_ring, outline_ring[0]]
                    )
                    if (
                        not all(point_in_polygon(p, outline_ring) for p in tick_pts)
                        or edge_line.distance(_ShapelyLineString(tick_pts))
                        < edge_margin_mm + stroke_width_mm / 2.0
                    ):
                        tick_hit = {
                            "obstacle": "the board edge",
                            "x": tick_pts[0][0],
                            "y": tick_pts[0][1],
                        }
                if tick_pts and tick_hit is None:
                    bucket.append(
                        _draw(tick_pts, stroke_width_mm, role="pin1", refdes=refdes)
                    )
                    tick_obstacle = obstacle_from_bbox(
                        tick_pts, label=f"{refdes} pin-1 silk"
                    )
                    census.append(
                        SilkPlacement(
                            refdes=refdes,
                            kind="pin1",
                            side=side_name,
                            outcome="relocated",
                            reason=(
                                "no dot beside pin 1 is clear -- fell back to "
                                "the corner tick, which prints along the "
                                "courtyard outline and reads weakly"
                            ),
                            stroke_width_mm=stroke_width_mm,
                        )
                    )
                else:
                    census.append(
                        SilkPlacement(
                            refdes=refdes,
                            kind="pin1",
                            side=side_name,
                            outcome="dropped",
                            reason=(
                                "no dot beside pin 1 is clear, and the corner "
                                + (
                                    f"tick overlaps {_obstacle_label(tick_hit)}"
                                    if tick_hit is not None
                                    else "tick has no courtyard to cut"
                                )
                                + " -- dropped"
                            ),
                            stroke_width_mm=stroke_width_mm,
                        )
                    )
            else:
                # The courtyard is gone, so the corner-tick fallback has
                # nothing to cut -- but the dot was still attempted above
                # (gr503436: a polarity marker, not decoration). Reaching
                # here means every dot candidate was blocked too, so the
                # part goes unmarked -- reported, never silently absent.
                census.append(
                    SilkPlacement(
                        refdes=refdes,
                        kind="pin1",
                        side=side_name,
                        outcome="dropped",
                        reason=(
                            "no dot beside pin 1 is clear, and the "
                            "courtyard it would annotate was dropped -- "
                            "a lone corner tick reads as unanchored ink"
                        ),
                        stroke_width_mm=stroke_width_mm,
                    )
                )

        # Fold this instance's own surviving pin-1 tick into the shared
        # obstacle list now, ahead of the refdes-label search below: the
        # tick is a small mark a label must not print over, same as any
        # foreign silk. The courtyard is deliberately NOT folded in yet
        # (see the comment above the refdes-label loop) -- it comes after.
        if tick_obstacle is not None:
            side_obstacles.append(tick_obstacle)

        unsupported = sorted({c for c in refdes if not stroke_font.supported(c)})
        if unsupported:
            # Not fatal (an unsupported character just draws nothing and
            # still advances the cursor -- see stroke_font's own docstring)
            # but a caller should know the refdes silk prints blank there,
            # same "don't silently swallow" discipline as a dropped label.
            census.append(
                SilkPlacement(
                    refdes=refdes,
                    kind="refdes",
                    side=side_name,
                    outcome="dropped",
                    reason=(
                        f"unsupported character(s) {''.join(unsupported)!r} "
                        "in refdes silk -- drawn as a gap, not a glyph"
                    ),
                    stroke_width_mm=stroke_width_mm,
                    height_mm=height_mm,
                )
            )

        # 3) refdes text -- try each candidate placement, drop if none is clear.
        # Glyph orientation is pinned to 0 regardless of the part's own
        # rotation -- "read from one side": every top-side label reads
        # upright without turning the board, at any angle the part sits at
        # (see module docstring, "Read from one side, at any part
        # rotation"). ONLY the
        # anchor point (via _place, above) follows the part's rotation --
        # where the label sits relative to its footprint still tracks the
        # footprint; the letters themselves never do.
        #
        # Spots and their order are gr495394's (`_label_candidates`); every
        # outline on this side, placed or not yet, is checked exactly
        # (`_label_blocked` against `ring_geoms`), so a part's outline never
        # needs folding into `side_obstacles` as a solid bounding box -- a
        # box that, on a rotated or neighbour-bent outline, blocked label
        # spots the outline itself leaves free.
        box_board = (
            _place(box_local, cx=0.0, cy=0.0, rot=rot, mirror=mirror)
            if box_local
            else []
        )
        candidates = _label_candidates(
            refdes,
            cx,
            cy,
            box_board,
            mirror=mirror,
            height_mm=height_mm,
            stroke_width_mm=stroke_width_mm,
        )
        default_spot = candidates[0][0]
        over_name = label_spots.get(refdes)
        over_k = next((k for k, c in enumerate(candidates) if c[0] == over_name), -1)
        shared_k, shared_spot, group_size = group_spot.get(inst, (-1, "", 0))
        # A per-part label_spot override goes first, then the aligned
        # group's shared spot, ahead of even the default; the rest keep
        # their order. Matched by index: group members share one
        # candidate list.
        scored = sorted(
            enumerate(candidates),
            key=lambda item: (
                0 if item[0] == over_k else 1 if item[0] == shared_k else 2
            ),
        )

        state = _LabelState(
            inst=inst,
            refdes=refdes,
            side_name=side_name,
            mirror=mirror,
            bucket=bucket,
            side_obstacles=side_obstacles,
            candidates=[cand for _k, cand in scored],
            default_spot=default_spot,
            pinned_spot=over_name if over_k >= 0 else None,
            census_index=-1,
        )
        label_states.append(state)
        placed_text = False
        for pos, (_k, cand) in enumerate(scored):
            spot, _anchor, corners, _h, _v = cand
            if _label_blocked(
                corners,
                functools.partial(
                    _label_strokes, refdes, cand, mirror=mirror, height_mm=height_mm
                ),
                inst=inst,
                side_name=side_name,
                obstacles=side_obstacles,
                rings=ring_geoms,
                outline_ring=outline_ring,
                edge_margin_mm=edge_margin_mm,
                stroke_width_mm=stroke_width_mm,
            ):
                continue
            for pts in _label_strokes(refdes, cand, mirror=mirror, height_mm=height_mm):
                bucket.append(_draw(pts, stroke_width_mm, role="refdes", refdes=refdes))
            state.obstacle = obstacle_from_bbox(corners, label=f"{refdes} refdes silk")
            side_obstacles.append(state.obstacle)
            state.taken = pos
            state.census_index = len(census)
            placed_text = True
            label_x = min(p[0] for p in corners)
            label_y = min(p[1] for p in corners)
            if spot != default_spot:
                if spot == over_name:
                    reason = (
                        f"refdes label drawn at its per-part label_spot "
                        f"override {spot} (default {default_spot})"
                    )
                else:
                    shared = (
                        f", the spot its aligned row of {group_size} identical parts shares"
                        if spot == shared_spot
                        else f"; its aligned row's shared spot {shared_spot} was "
                        "taken by silk committed earlier"
                        if group_size
                        else ""
                    )
                    reason = (
                        f"refdes label moved off the default {default_spot} "
                        "spot to clear a pad, a via, an outline or silk already "
                        f"committed (drawn at {spot}{shared})"
                    )
                census.append(
                    SilkPlacement(
                        refdes=refdes,
                        kind="refdes",
                        side=side_name,
                        outcome="relocated",
                        reason=reason,
                        stroke_width_mm=stroke_width_mm,
                        height_mm=height_mm,
                        x_mm=label_x,
                        y_mm=label_y,
                    )
                )
            else:
                census.append(
                    SilkPlacement(
                        refdes=refdes,
                        kind="refdes",
                        side=side_name,
                        outcome="placed",
                        stroke_width_mm=stroke_width_mm,
                        height_mm=height_mm,
                        x_mm=label_x,
                        y_mm=label_y,
                    )
                )
            break
        if not placed_text:
            state.census_index = len(census)
            census.append(
                SilkPlacement(
                    refdes=refdes,
                    kind="refdes",
                    side=side_name,
                    outcome="dropped",
                    reason=(
                        "refdes label dropped -- every spot outside its "
                        "outline overlaps a pad, a via, an outline or silk "
                        "already committed, and no label prints under a part "
                        "(gr496552)"
                    ),
                    stroke_width_mm=stroke_width_mm,
                    height_mm=height_mm,
                )
            )

    _repair_dropped_labels(
        label_states,
        census,
        rings=ring_geoms,
        outline_ring=outline_ring,
        edge_margin_mm=edge_margin_mm,
        stroke_width_mm=stroke_width_mm,
        height_mm=height_mm,
    )
    dropped, relocated = _prose_from_census(census)
    return SilkResult(
        draws={"top": top, "bottom": bottom},
        census=tuple(census),
        dropped=dropped,
        relocated=relocated,
    ), bulges


__all__ = [
    "DEFAULT_REFDES_HEIGHT_MM",
    "FIDUCIAL_COPPER_DIA_MM",
    "FIDUCIAL_COUNT",
    "FIDUCIAL_MARGIN_MM",
    "FIDUCIAL_MASK_DIA_MM",
    "SN_LABEL",
    "SN_LABEL_HEIGHT_MM",
    "SN_WRITE_SPACE_CHARS",
    "TITLE_HEIGHT_MM",
    "TITLE_LINE_GAP_MM",
    "TITLE_MARGIN_MM",
    "FiducialResult",
    "SilkPlacement",
    "SilkResult",
    "SnPatchResult",
    "TitleBlockResult",
    "build_fiducials",
    "build_silk",
    "build_sn_patch",
    "build_title_block",
    "fiducial_candidate_sites",
    "obstacle_from_bbox",
    "refdes_label_slot_mm",
    "silk_clearance_mm",
    "soldermask_expansion_mm",
    "via_obstacles",
    "world_courtyard_rings",
]
