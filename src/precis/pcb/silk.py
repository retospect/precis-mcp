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
   from ``height_mm`` and centered on the part by default, RELOCATED
   (above/below/left/right the part) or, failing every candidate spot,
   DROPPED when it would overlap a pad — "a fab scrapes silk off pads, so
   text under a pad is silently lost" (task brief). Never emitted blind.
   **Read from one side, at any part rotation.** Glyph orientation is
   pinned to 0 degrees regardless of the part's own rotation — a label
   never goes vertical or upside-down, so nobody ever turns the board to
   read a refdes. Only the label's ANCHOR follows the part's rotation
   (:func:`_place`, same as every other placed silk primitive here) —
   where the label sits relative to its own footprint still tracks the
   footprint, the letters themselves never do. Bottom-side text is still
   mirrored (``mirror=True`` into :func:`precis.pcb.stroke_font.layout_text`)
   because ``B.Silkscreen`` is viewed through the board — "one orientation
   per side" is the rule, not "bottom silk reads from the top".
2. a **courtyard/body outline** — the POLYGON
   :func:`precis.pcb.ir.instance_courtyard_polygon` derives from this
   instance's OWN pad outlines (their convex hull, offset outward), never
   a fixed constant tuned to nothing and, since 2026-08-30, never a
   square either: a square sized by a pin-offset RADIUS over-reserves a
   1x20 edge connector 8-fold while UNDER-reserving a SOIC-8, and no
   single radius fixes both (that function's own docstring carries the
   measured table). See :attr:`precis.pcb.ir.PcbIR.pin_w`'s docstring for
   the neighbouring defect class ("every pad the same 0.4mm disc") the
   pad-size term sidesteps.
   The offset is :func:`silk_clearance_mm`, walked down the fab chain
   (mask expansion -> silk-to-mask clearance -> half the drawn stroke)
   from this board's capability row — so the courtyard cannot overlap its
   own pads by construction, rather than by a margin that happened to be
   big enough.
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
via annuli + the caller's reserved geometry`` and every courtyard/tick/
refdes-label that survives its own check is folded straight back into
that SAME list (via :func:`obstacle_from_bbox`, a conservative
axis-aligned bounding box of what was just drawn — never under-flagging,
same idiom :func:`_segment_box` already uses) before the NEXT check
runs. A part's own courtyard is therefore just the first thing on the
list its own pin-1 tick and label check against, and every part after it
checks against everything committed so far — one growing set, not a
per-instance one plus a separate cross-part one.

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

That reasoning is about the TICK, and it now also suppresses the dot,
which may be too strong: a dot sits outside the courtyard beside pin 1's
own land, so it still points at something real when the outline is gone.
Left as-is rather than widened on a hunch — see
``docs/backlog/pcb-courtyard-polygon.md``.

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

**No per-instance board side in the IR.** :class:`precis.pcb.ir.PcbIR`
carries no top/bottom field at all (confirmed: :func:`precis.pcb.ir.
pin_point` never mirrors, and :func:`precis.pcb.realize.pads_for_ir`
flashes every pad on the single ``PAD_LAYER`` regardless of the store's
own ``pcb_instances.layer`` column) — an existing simplification of the
whole L0-L5 IR path, not something this module can fix without touching
``ir.py``. ``instance_sides`` is therefore a SEPARATE lookup this module
accepts (refdes -> ``'top'``/``'bottom'``, the same string convention
:mod:`precis.pcb.padplace`'s ``_is_bottom`` already uses); an instance
missing from it defaults to ``'top'``.

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

import itertools
import math
import re
from dataclasses import dataclass
from typing import Any

from precis.pcb import stroke_font
from precis.pcb.capabilities import CapabilityRow, design_value
from precis.pcb.geom import (
    convex_polygons_overlap,
    dist_point_to_segment,
    point_in_polygon,
)
from precis.pcb.gerber import DEFAULT_SILK_WIDTH_MM, SOLDERMASK_EXPANSION_MM
from precis.pcb.ir import PcbIR, instance_courtyard_polygon
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
_REFDES_RINGS = 3
_REFDES_DIRECTIONS = 12

#: How far off an axis a direction must lean before the text stops being
#: centred on that axis and starts being pushed to one side of the anchor.
#: A label offset UP wants its baseline on the anchor and its glyphs
#: centred horizontally; one offset up-and-right wants to start at the
#: anchor and run away from the part. At :data:`_REFDES_DIRECTIONS` = 12
#: the components are 0, 0.5, 0.87 and 1.0, so any deadband in (0, 0.5)
#: gives the same answer — the value is a readable middle, not a tuned
#: threshold.
_REFDES_ALIGN_DEADBAND = 0.25


def _refdes_candidates(
    n_rings: int, per_ring: int
) -> tuple[tuple[float, float, str, str, str], ...]:
    """Candidate refdes placements, tried in order, as ``(dx_units,
    dy_units, h_align, v_align, label)`` in the INSTANCE's own local frame
    — so a relocated label moves with the part's own rotation/mirror, not
    with the board's absolute axes.

    ``dx``/``dy`` are scaled by the courtyard's reach IN THAT DIRECTION
    plus a gap before use (:func:`_courtyard_support_mm`), and their
    MAGNITUDE is the ring number, so ring 2 sits twice as far out as ring
    1 — the same meaning the old hand-written ``(0, -2)`` fallback had.

    Order is: centred first (the common case), then each ring outward,
    and within a ring the directions nearest STRAIGHT UP first. Up is
    where a reader expects a refdes, so a label only ends up somewhere
    unusual when everything more conventional was taken. Ties (a
    direction's mirror image about the vertical) break toward the smaller
    angle, so the sweep is deterministic rather than dict-ordered.

    The ladder always walks past candidate 0 for a small part — an 0402's
    courtyard is smaller than any legible label at a normal ``height_mm``,
    so "centred" can never clear its own outline there. Landing outside a
    part's courtyard is still an unambiguous, readable label; more than a
    dropped one is (task brief: "prefer moving the label to shrinking the
    outline"). A candidate is rejected against a pad/via, board-level
    ``reserved`` geometry, or silk ANY part — including this same
    instance's own courtyard and pin-1 mark — has already committed, all
    folded into the SAME growing obstacle list (module docstring's
    "Avoidance is GLOBAL")."""
    out: list[tuple[float, float, str, str, str]] = [
        (0.0, 0.0, "center", "middle", "centred")
    ]
    for ring in range(1, n_rings + 1):
        angles = [2.0 * math.pi * k / per_ring for k in range(per_ring)]
        # Nearest straight up (pi/2) first; the mirror pair breaks toward
        # the smaller angle so the order never depends on float ties.
        angles.sort(key=lambda a: (round(abs(a - math.pi / 2.0), 9), a))
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
    candidate other than the default centered one), or ``"dropped"``
    (never drawn at all — the fact :func:`precis.pcb.drc.
    check_silk_missing` turns into a DRC error).

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
    outcome: str  # "placed" | "relocated" | "dropped"
    reason: str | None = None
    stroke_width_mm: float = 0.0
    height_mm: float | None = None


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
    pin-1 marker with it. On the ESP32-C3 reference board that single
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
    out = []
    for pad in obstacles:
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
    complement to the solid-obstacle checks above: a refdes candidate is
    allowed to sit INSIDE its own courtyard's hollow interior (the default
    centered placement always does), so the courtyard can't be folded into
    ``side_obstacles`` as a filled shape until after this instance's own
    label is placed (see the call site) -- but a glyph stroke still can't
    cross the courtyard's own border line, hollow or not. Reuses the exact
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
#: obstacle list and shoulders this part's own centred refdes label off
#: its default spot (measured on the two-part fixture in
#: ``tests/test_pcb_silk.py``, by 4 micrometres).
_PIN1_TICK_EDGE_FRACTION = 0.15


def _towards(frm: Point, to: Point, dist_mm: float) -> Point:
    dx, dy = to[0] - frm[0], to[1] - frm[1]
    length = math.hypot(dx, dy)
    if length <= 1e-9:
        return frm
    return (frm[0] + dx / length * dist_mm, frm[1] + dy / length * dist_mm)


def _pin1_tick(
    ir: PcbIR, pin1: int, courtyard: list[Point], stroke_width_mm: float
) -> list[Point]:
    """A small tick cutting whichever courtyard VERTEX sits nearest pin
    1's own land-pattern offset — a two-segment ``L`` running back along
    the two edges that meet there.

    Sized off the courtyard's own geometry, never an invented mm constant:
    :data:`_PIN1_TICK_EDGE_FRACTION` of the shorter of the two adjacent
    edges, floored so it stays visible on a tiny part and capped at HALF
    that edge so the two legs can never meet in the middle of a side and
    read as a second outline. On the rectangle a square courtyard always
    produced this is the same corner-cut ``L`` as before; on a real hull
    it follows whatever shape the part's own pads make.

    ``courtyard`` is the CLOSED local-frame polygon
    (:func:`precis.pcb.ir.instance_courtyard_polygon`) — the repeated
    final point is dropped here so a vertex is not considered twice."""
    ring = courtyard[:-1] if len(courtyard) > 1 else list(courtyard)
    if len(ring) < 3:
        return []
    dx, dy = float(ir.pin_dx[pin1]), float(ir.pin_dy[pin1])
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
    ir: PcbIR, pin1: int, stroke_width_mm: float, clearance_mm: float
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
    sweep starts at +x rather than dividing by zero."""
    dia = stroke_width_mm * _PIN1_DOT_STROKES
    dx, dy = float(ir.pin_dx[pin1]), float(ir.pin_dy[pin1])
    hw, hh = float(ir.pin_w[pin1]) / 2.0, float(ir.pin_h[pin1]) / 2.0
    reach = math.hypot(dx, dy)
    base = math.atan2(dy, dx) if reach > 1e-9 else 0.0
    # Clear of pin 1's own pad corner, plus the fab clearance and the
    # dot's own radius — the first ring is the closest the dot may legally
    # sit, and each further ring adds one diameter.
    first = math.hypot(hw, hh) + clearance_mm + dia / 2.0
    out: list[tuple[Point, float]] = []
    for step in range(_PIN1_DOT_DISTANCES):
        radius = first + step * dia
        for k in range(_PIN1_DOT_DIRECTIONS):
            # 0, -1, +1, -2, +2 ... around `base`: the outward direction
            # first, then each symmetric pair. Which member of a pair
            # comes first is arbitrary and does not change which spot is
            # chosen when both are clear — only that both are tried.
            half = (k + 1) // 2
            sign = 1 if k % 2 == 0 else -1
            theta = base + sign * half * 2.0 * math.pi / _PIN1_DOT_DIRECTIONS
            out.append(
                ((dx + math.cos(theta) * radius, dy + math.sin(theta) * radius), dia)
            )
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


def _circle_inside_polygon(
    center: Point, radius: float, poly: list[Point], n: int = 12
) -> bool:
    """Conservative containment for a disc of ``radius`` at ``center``:
    the centre AND ``n`` points around its circumference all inside
    ``poly``. A sampled approximation, same spirit as :func:`_segment_box`'s
    square-cap stroke inflation elsewhere in this module — cheap, and
    never UNDER-flags a disc that pokes outside a concave outline."""
    if not point_in_polygon(center, poly):
        return False
    cx, cy = center
    return all(
        point_in_polygon((cx + radius * math.cos(a), cy + radius * math.sin(a)), poly)
        for a in (2.0 * math.pi * i / n for i in range(n))
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

    ``plane_blockers`` is a SEPARATE list, in the same "pad reshaped as a
    single-layer fake via" ``ctype='via'`` shape
    :func:`precis.pcb.realize._pad_blockers` already uses to feed a real
    component pad into :func:`precis.pcb.planes.plane_pours` as an
    antipad obstacle (see that function's own docstring for why a pad
    has to be a blocker at all — an unlisted pad gets flooded straight
    over). Sized to ``mask_dia_mm``, not the bare copper disc, so the
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


def build_fiducials(
    outline: list[tuple[float, float]] | list[list[float]],
    pads: list[dict[str, Any]],
    *,
    layer: str,
    count: int = FIDUCIAL_COUNT,
    margin_mm: float = FIDUCIAL_MARGIN_MM,
    copper_dia_mm: float = FIDUCIAL_COPPER_DIA_MM,
    mask_dia_mm: float = FIDUCIAL_MASK_DIA_MM,
) -> FiducialResult:
    """``count`` (default 3) optical targets near non-collinear corners of
    ``outline``'s bounding box, on ``layer`` — inset by ``margin_mm``,
    each checked clear of every REAL pad in ``pads`` (a component pad
    flooding a fiducial's mask opening is exactly as fab-fatal as a
    refdes label doing it — see this module's docstring) and fully
    inside ``outline`` (never straddling the board edge).

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
        poly = [(float(p[0]), float(p[1])) for p in outline]
        x0, y0, x1, y1 = _bbox(poly)
        radius = mask_dia_mm / 2.0
        for idx, (sx, sy) in enumerate(_FIDUCIAL_CORNER_SIGNS):
            if len(fids) >= count:
                break
            for m in (margin_mm, margin_mm * 2.0):
                cand = _corner_point(sx, sy, x0, y0, x1, y1, m)
                if not _circle_inside_polygon(cand, radius, poly):
                    continue
                if any(_circle_overlaps_pad(cand, radius, pad) for pad in pads):
                    continue
                if any(
                    math.hypot(cand[0] - fx, cand[1] - fy) < 2 * radius
                    for fx, fy in fids
                ):
                    continue
                fids.append(cand)
                break
            else:
                dropped.append(
                    f"fiducial corner {idx}: no spot clear of the outline edge "
                    "or a pad within 2x margin -- skipped"
                )
    if len(fids) < count:
        dropped.append(
            f"only {len(fids)}/{count} fiducial(s) placed -- the rest had no "
            "corner clear of a pad, another fiducial, or the outline edge"
        )
    fid_pads = [
        {
            "layer": layer,
            "net": "",
            "shape": "circle",
            "x": fx,
            "y": fy,
            "w": copper_dia_mm,
            "role": "fiducial",
        }
        for fx, fy in fids
    ]
    plane_blockers = [
        {
            "ctype": "via",
            "net": "",
            "x": fx,
            "y": fy,
            "dia_mm": mask_dia_mm,
            "layers": [layer],
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
    obstacles = _mask_openings(pads, capability) + list(avoid or ())
    gap = height_mm + TITLE_LINE_GAP_MM
    for corner_name, x_at_max, y_at_max, h_align, v_align, stack_sign in _TITLE_CORNERS:
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
            anchor_x = x1 - m if x_at_max else x0 + m
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
    obstacles = _mask_openings(pads, capability) + list(avoid or ())

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

    for x0, y0 in _candidate_rects(outline_bbox, title_bbox, box_w, box_h, pad_mm):
        corners = [
            (x0, y0),
            (x0 + box_w, y0),
            (x0 + box_w, y0 + box_h),
            (x0, y0 + box_h),
        ]
        if not all(point_in_polygon(c, poly) for c in corners):
            continue
        if any(_box_overlaps_pad(corners, pad) for pad in obstacles):
            continue
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
        return SnPatchResult(draws=draws, bbox=corners, dropped=())

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
) -> SilkResult:
    """Build ``{"top": [...], "bottom": [...]}`` silk draws for every
    PLACED instance in ``ir`` — the one builder :mod:`precis.handlers.pcb`
    calls for every gerber/svg-fab site that needs silk (module
    docstring's "one silk builder, never duplicated" discipline).

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
    reserved`` and every courtyard/tick/refdes-label this function itself
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
    """
    sides = instance_sides or {}
    top: list[dict[str, Any]] = []
    bottom: list[dict[str, Any]] = []
    census: list[SilkPlacement] = []
    reserved_by_side = reserved or {}

    # Both fab-derived, resolved ONCE: `clearance_mm` sets how far every
    # courtyard sits off its own pads, and each pad becomes the mask
    # OPENING every clearance test below actually runs against.
    clearance_mm = silk_clearance_mm(capability, stroke_width_mm=stroke_width_mm)
    pads = _mask_openings(pads, capability)

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

    # The ONE obstacle list per side (module docstring): seeded with real
    # pads, that side's via annuli, and the caller's board-level `reserved`
    # geometry -- then grown in place as each instance's own silk survives,
    # so it is simultaneously "what this instance must avoid" and "what the
    # NEXT instance (and this instance's later draws) must avoid".
    obstacles_by_side: dict[str, list[dict[str, Any]]] = {
        "top": [*pads, *top_via_pads, *reserved_by_side.get("top", [])],
        "bottom": [*pads, *bottom_via_pads, *reserved_by_side.get("bottom", [])],
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
    courtyard_ring: dict[int, tuple[str, list[Point]]] = {}
    for other in placed:
        ring_local = instance_courtyard_polygon(
            ir, other, clearance_mm=clearance_mm, pins=pins_of_inst.get(other, [])
        )
        if not ring_local:
            continue
        other_rot = float(ir.inst_rot[other])
        other_side = _side_for(sides, str(ir.instance_refdes[other]))
        courtyard_ring[other] = (
            other_side,
            _place(
                ring_local,
                cx=float(ir.inst_x[other]),
                cy=float(ir.inst_y[other]),
                rot=0.0 if math.isnan(other_rot) else other_rot,
                mirror=other_side == "bottom",
            ),
        )

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
        # pattern, so no courtyard, rather than an invented size.
        box_local = instance_courtyard_polygon(
            ir, inst, clearance_mm=clearance_mm, pins=pins
        )

        # 1) courtyard/body outline -- checked against everything committed
        # so far (external pads/vias/reserved AND prior parts' own silk),
        # but not yet folded into `side_obstacles` itself: the pin-1 tick
        # below is designed to touch this SAME courtyard's own corner, so
        # that check must still see the pre-courtyard obstacle set.
        courtyard_kept = False
        courtyard_obstacle: dict[str, Any] | None = None
        box_pts: list[Point] = []
        if box_local:
            box_pts = _place(box_local, cx=cx, cy=cy, rot=rot, mirror=mirror)
            hit = _stroke_hits(box_pts, side_obstacles, stroke_width_mm)
            # A whole outline is not lost to one via. Break it around
            # whatever it meets and keep the rest -- what a real silk
            # generator emits, and what the fab would have trimmed to
            # anyway (`_clip_polyline`). Only a courtyard with too little
            # left to read as an outline still drops.
            runs, kept_mm, total_mm = (
                ([box_pts], 1.0, 1.0)
                if hit is None
                else _clip_polyline(
                    box_pts,
                    _near_obstacles(side_obstacles, box_pts, 2.0 * stroke_width_mm),
                    stroke_width_mm,
                )
            )
            if hit is not None and (
                total_mm <= 0 or kept_mm / total_mm < _COURTYARD_MIN_KEPT_FRACTION
            ):
                census.append(
                    SilkPlacement(
                        refdes=refdes,
                        kind="courtyard",
                        side=side_name,
                        outcome="dropped",
                        reason=(
                            "courtyard outline overlaps "
                            f"{_obstacle_label(hit)}, leaving too little to read "
                            f"as an outline ({kept_mm:.2f} of {total_mm:.2f}mm) "
                            "-- dropped"
                        ),
                        stroke_width_mm=stroke_width_mm,
                    )
                )
            else:
                for run in runs:
                    bucket.append(
                        _draw(run, stroke_width_mm, role="outline", refdes=refdes)
                    )
                courtyard_obstacle = obstacle_from_bbox(
                    box_pts, label=f"{refdes} courtyard silk"
                )
                courtyard_kept = True
                census.append(
                    SilkPlacement(
                        refdes=refdes,
                        kind="courtyard",
                        side=side_name,
                        outcome="placed" if hit is None else "relocated",
                        reason=(
                            None
                            if hit is None
                            else (
                                "courtyard outline broken around "
                                f"{_obstacle_label(hit)} and "
                                f"{len(runs)} piece(s) drawn "
                                f"({kept_mm:.2f} of {total_mm:.2f}mm kept)"
                            )
                        ),
                        stroke_width_mm=stroke_width_mm,
                    )
                )

        # 2) pin-1 marker -- only attempted when this instance's OWN
        # courtyard actually survived (module docstring's "A pin-1 tick
        # never survives alone" decision): the tick is a corner-cut of the
        # courtyard outline, meaningless -- and unreadable as stray ink --
        # without it. Checked against the SAME pre-courtyard obstacle
        # snapshot as the courtyard itself (external silk only) -- a tick
        # touching its own courtyard's corner is the intended shape, not a
        # collision.
        tick_obstacle: dict[str, Any] | None = None
        if pins and box_local and not courtyard_kept:
            census.append(
                SilkPlacement(
                    refdes=refdes,
                    kind="pin1",
                    side=side_name,
                    outcome="dropped",
                    reason=(
                        "pin-1 marker skipped -- its courtyard was dropped, "
                        "so a lone tick would read as unanchored ink"
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
                ir, pin1, stroke_width_mm, clearance_mm
            ):
                (dcx, dcy) = _place(
                    [local_centre], cx=cx, cy=cy, rot=rot, mirror=mirror
                )[0]
                # A dot is a zero-length stroke at the dot's own diameter
                # -- the gerber writer's round cap makes it a filled
                # circle, so it needs no new primitive.
                candidate = [(dcx, dcy), (dcx, dcy)]
                if _stroke_overlaps_any_pad(candidate, side_obstacles, dia):
                    continue
                if courtyard_kept and _stroke_crosses_stroke(candidate, box_pts, dia):
                    continue
                # Yield to every OTHER part's body outline on this side,
                # whether or not that part has been processed yet (see
                # `courtyard_ring`).
                if any(
                    other != inst
                    and other_side == side_name
                    and _stroke_crosses_stroke(candidate, ring, dia)
                    for other, (other_side, ring) in courtyard_ring.items()
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
            else:
                # Nowhere clear beside pin 1. The corner tick is a weak
                # mark for the reasons above, but it still says WHICH
                # corner to a reader who knows to look for it, and that
                # beats dropping the marker entirely.
                tick_local = _pin1_tick(ir, pin1, box_local, stroke_width_mm)
                tick_pts = (
                    _place(tick_local, cx=cx, cy=cy, rot=rot, mirror=mirror)
                    if tick_local
                    else []
                )
                tick_hit = (
                    _stroke_hits(tick_pts, side_obstacles, stroke_width_mm)
                    if tick_pts
                    else None
                )
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
        # `side_obstacles` still does NOT include this instance's OWN
        # courtyard here, deliberately: the courtyard is a hollow outline
        # (four border lines), and its bounding box is a solid-rect
        # obstacle once folded in (same "conservative, never under-flag"
        # approximation every other obstacle here uses) -- correct for a
        # FOREIGN part's courtyard (a component's whole footprint is real
        # keep-out for someone else's silk) but wrong for THIS part's own
        # default candidate 0, which is deliberately centered INSIDE its
        # own courtyard. Folded in right after this loop, once this
        # instance's own label search is done, so every later instance
        # still treats it as solid.
        text_rot = 0.0
        gap = height_mm * 0.3
        placed_text = False
        for idx, (du, dv, h_align, v_align, spot) in enumerate(_CANDIDATES):
            # Directional, not one scalar radius: an elongated part reaches
            # much further along its own long axis than across it, and the
            # square courtyard this replaced pushed every label out by the
            # larger of the two (see `_courtyard_support_mm`).
            off = (
                _courtyard_support_mm(box_local, du, dv) + gap
                if (du, dv) != (0.0, 0.0)
                else 0.0
            )
            local_anchor = (du * off, dv * off)
            (ax, ay) = _place([local_anchor], cx=cx, cy=cy, rot=rot, mirror=mirror)[0]
            corners = stroke_font.text_bbox_corners(
                refdes,
                anchor=(ax, ay),
                height_mm=height_mm,
                rotation_deg=text_rot,
                mirror=mirror,
                h_align=h_align,
                v_align=v_align,
            )
            if any(_box_overlaps_pad(corners, pad) for pad in side_obstacles):
                continue
            strokes = stroke_font.layout_text(
                refdes,
                anchor=(ax, ay),
                height_mm=height_mm,
                rotation_deg=text_rot,
                mirror=mirror,
                h_align=h_align,
                v_align=v_align,
            )
            if any(
                _stroke_overlaps_any_pad(pts, side_obstacles, stroke_width_mm)
                for pts in strokes
            ):
                continue  # bbox cleared but a real stroke didn't -- try the next spot
            if courtyard_kept and any(
                _stroke_crosses_stroke(pts, box_pts, stroke_width_mm) for pts in strokes
            ):
                # the courtyard's hollow interior is legal (candidate 0 sits
                # inside it on purpose), but a glyph still can't cross its
                # own courtyard's border line -- see _stroke_crosses_stroke.
                continue
            # Yield to every OTHER part's body outline on this side, placed
            # or not yet (`courtyard_ring`) -- the same precedence the pin-1
            # dot follows, and for the same reason: a label committed early
            # was otherwise free to sit across a later part's outline and
            # take the whole outline down with it. Measured on the 40mm
            # fixture: C14's label alone cost R3 its courtyard AND its
            # pin-1 mark.
            if any(
                other != inst
                and other_side == side_name
                and any(
                    _stroke_crosses_stroke(pts, ring, stroke_width_mm)
                    for pts in strokes
                )
                for other, (other_side, ring) in courtyard_ring.items()
            ):
                continue
            for pts in strokes:
                bucket.append(_draw(pts, stroke_width_mm, role="refdes", refdes=refdes))
            side_obstacles.append(
                obstacle_from_bbox(corners, label=f"{refdes} refdes silk")
            )
            placed_text = True
            if idx > 0:
                census.append(
                    SilkPlacement(
                        refdes=refdes,
                        kind="refdes",
                        side=side_name,
                        outcome="relocated",
                        reason=(
                            "refdes label moved off-center to clear a pad, a via, "
                            f"or silk already committed (drawn at {spot})"
                        ),
                        stroke_width_mm=stroke_width_mm,
                        height_mm=height_mm,
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
                    )
                )
            break
        if not placed_text:
            census.append(
                SilkPlacement(
                    refdes=refdes,
                    kind="refdes",
                    side=side_name,
                    outcome="dropped",
                    reason=(
                        "refdes label dropped -- every candidate placement "
                        "overlaps a pad, a via, or silk already committed"
                    ),
                    stroke_width_mm=stroke_width_mm,
                    height_mm=height_mm,
                )
            )

        # NOW fold this instance's own courtyard in as a solid obstacle
        # (see the comment above the refdes-label loop) -- every instance
        # processed after this one, on this side, must treat the whole
        # footprint as real keep-out.
        if courtyard_obstacle is not None:
            side_obstacles.append(courtyard_obstacle)

    dropped, relocated = _prose_from_census(census)
    return SilkResult(
        draws={"top": top, "bottom": bottom},
        census=tuple(census),
        dropped=dropped,
        relocated=relocated,
    )


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
    "obstacle_from_bbox",
    "silk_clearance_mm",
    "soldermask_expansion_mm",
]
