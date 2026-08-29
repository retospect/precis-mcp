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
2. a **courtyard/body outline** — a square sized from this instance's OWN
   land-pattern reach: :func:`precis.pcb.ir.instance_pad_radius` (the pin
   offsets) plus that instance's own widest resolved pad half-size
   (``ir.pin_w``/``ir.pin_h`` — real per-pin pad SIZE, not the universal
   0.2mm ``maze.PAD_RADIUS_MM`` keep-out every pin used to share
   regardless of package before 2026-08-29, since deleted). Never a fixed
   constant tuned to nothing —
   see :attr:`precis.pcb.ir.PcbIR.pin_w`'s own docstring for exactly the
   defect class ("every pad the same 0.4mm disc") this sidesteps.
3. a **pin-1 marker** — a small corner tick cut at whichever courtyard
   corner sits nearest pin 1's own land-pattern offset (or the first
   declared pin, when no pin is literally named ``"1"``).

**Suppression, not silent loss.** Every drawn stroke — text, outline,
tick alike — is checked against the passed-in ``pads`` (real flashed pad
geometry, e.g. :func:`precis.pcb.padplace.board_pads`'s output, or the
synthesized-bound fallback :func:`precis.pcb.realize.pads_for_ir`) before
it survives into the result. An overlapping outline/tick segment is
dropped outright (there is no sensible "relocate a courtyard box"); an
overlapping refdes tries the candidate list first. :class:`SilkResult`
carries both ``dropped`` and ``relocated`` as human-readable messages —
never swallowed (task brief, verbatim).

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
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Any

from precis.pcb import stroke_font
from precis.pcb.gerber import DEFAULT_SILK_WIDTH_MM
from precis.pcb.ir import PcbIR, instance_pad_radius
from precis.pcb.landpattern import rotate_offset

Point = tuple[float, float]

#: Typical fab-default refdes silk height (mm) — the same status as
#: :data:`precis.pcb.gerber.DEFAULT_SILK_WIDTH_MM`: a documented default,
#: not a courtyard-class "tuned to nothing" constant (this sizes TEXT, a
#: cosmetic/typographic choice every silk generator makes, not a part's
#: physical footprint).
DEFAULT_REFDES_HEIGHT_MM = 1.0

#: Candidate refdes placements, tried in order, expressed as
#: ``(dx_units, dy_units, h_align, v_align)`` in the INSTANCE's own local
#: frame (dx/dy scaled by the courtyard reach + a gap before use) — so a
#: relocated label moves with the part's own rotation/mirror, not with
#: the board's absolute axes. The first candidate (centered on the part)
#: is the common case; the rest walk around the courtyard's four sides.
_CANDIDATES: tuple[tuple[float, float, str, str], ...] = (
    (0.0, 0.0, "center", "middle"),
    (0.0, 1.0, "center", "baseline"),
    (0.0, -1.0, "center", "top"),
    (1.0, 0.0, "left", "middle"),
    (-1.0, 0.0, "right", "middle"),
)


@dataclass(frozen=True, slots=True)
class SilkResult:
    """``draws`` is exactly :mod:`precis.pcb.gerber`'s
    ``model["silkscreen"]`` shape (``{"top": [<draw>, ...], "bottom": [...]}``,
    see that module's docstring for ``<draw>``). ``dropped``/``relocated``
    are human-readable — never silently swallowed."""

    draws: dict[str, list[dict[str, Any]]]
    dropped: tuple[str, ...] = ()
    relocated: tuple[str, ...] = ()


# ─────────────────────────────────────────────────────────────────────
# pure 2D overlap primitives — no shapely; a rotated text/outline box
# against an axis-aligned circle/rect pad is exactly SAT + point-to-
# polygon distance, both a few lines, and :mod:`precis.pcb.geom`'s own
# "pure, no dependencies" discipline for this subsystem's cheap eyes.
# ─────────────────────────────────────────────────────────────────────
def _point_in_polygon(p: Point, poly: list[Point]) -> bool:
    x, y = p
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            x_at_y = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < x_at_y:
                inside = not inside
    return inside


def _dist_point_to_segment(p: Point, a: Point, b: Point) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 < 1e-12:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _polygon_overlaps_circle(poly: list[Point], center: Point, radius: float) -> bool:
    if radius <= 0:
        return _point_in_polygon(center, poly)
    if _point_in_polygon(center, poly):
        return True
    n = len(poly)
    return any(
        _dist_point_to_segment(center, poly[i], poly[(i + 1) % n]) <= radius
        for i in range(n)
    )


def _polygons_overlap(poly_a: list[Point], poly_b: list[Point]) -> bool:
    """Separating Axis Theorem for two convex polygons — exact (both a
    text/outline box and an axis-aligned pad rect are convex)."""
    for poly in (poly_a, poly_b):
        n = len(poly)
        for i in range(n):
            x1, y1 = poly[i]
            x2, y2 = poly[(i + 1) % n]
            nx, ny = -(y2 - y1), (x2 - x1)
            a_vals = [nx * px + ny * py for px, py in poly_a]
            b_vals = [nx * px + ny * py for px, py in poly_b]
            if max(a_vals) < min(b_vals) or max(b_vals) < min(a_vals):
                return False
    return True


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


def _box_overlaps_pad(box: list[Point], pad: dict[str, Any]) -> bool:
    if str(pad.get("shape") or "circle") == "circle":
        cx, cy = float(pad["x"]), float(pad["y"])
        return _polygon_overlaps_circle(box, (cx, cy), float(pad["w"]) / 2.0)
    return _polygons_overlap(box, _pad_rect_polygon(pad))


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


def _stroke_overlaps_any_pad(
    points: list[Point], pads: list[dict[str, Any]], stroke_width_mm: float
) -> bool:
    half = stroke_width_mm / 2.0
    for a, b in itertools.pairwise(points):
        box = _segment_box(a, b, half)
        if any(_box_overlaps_pad(box, pad) for pad in pads):
            return True
    return False


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


def _courtyard_box(ir: PcbIR, inst: int, reach_mm: float) -> list[Point]:
    """The instance's own local-frame courtyard square, closed (first
    point repeated last) — the caller rotates/mirrors/translates it."""
    return [
        (-reach_mm, -reach_mm),
        (reach_mm, -reach_mm),
        (reach_mm, reach_mm),
        (-reach_mm, reach_mm),
        (-reach_mm, -reach_mm),
    ]


def _pin1_id(ir: PcbIR, inst: int, pins: list[int]) -> int:
    for pid in pins:
        if str(ir.pin_label[pid]) == "1":
            return pid
    return min(pins)


def _pin1_tick(
    ir: PcbIR, pin1: int, reach_mm: float, stroke_width_mm: float
) -> list[Point]:
    """A small corner tick at whichever courtyard corner sits nearest
    pin 1's own land-pattern offset — a two-segment ``L`` cutting that
    corner, sized off the courtyard's own reach (never an invented mm
    constant): proportional to it, floored so it stays visible on a
    tiny part."""
    dx, dy = float(ir.pin_dx[pin1]), float(ir.pin_dy[pin1])
    sx = -1.0 if dx < 0 else 1.0
    sy = -1.0 if dy < 0 else 1.0
    corner = (sx * reach_mm, sy * reach_mm)
    tick_len = max(reach_mm * 0.3, stroke_width_mm * 4.0) if reach_mm > 0 else 0.0
    if tick_len <= 0:
        return []
    a = (corner[0] - sx * tick_len, corner[1])
    c = (corner[0], corner[1] - sy * tick_len)
    return [a, corner, c]


def _courtyard_reach_mm(ir: PcbIR, inst: int, pins: list[int]) -> float:
    """This instance's courtyard half-extent, in mm — the real per-pin
    reach (:func:`precis.pcb.ir.instance_pad_radius`, pin-CENTRE offset)
    plus the instance's own widest resolved pad half-size
    (``ir.pin_w``/``ir.pin_h``), never a fixed constant. ``0.0`` for a
    pinless instance (mounting hole, fiducial) — there is no land-pattern
    geometry to derive a box from, so no courtyard is drawn for one,
    rather than inventing a size."""
    if not pins:
        return 0.0
    half_pad = max(max(float(ir.pin_w[p]), float(ir.pin_h[p])) for p in pins) / 2.0
    return float(instance_pad_radius(ir)[inst]) + half_pad


def _place(
    points: list[Point], *, cx: float, cy: float, rot: float, mirror: bool
) -> list[Point]:
    out = []
    for lx, ly in points:
        rx, ry = rotate_offset(lx, ly, rot, mirrored=mirror)
        out.append((cx + rx, cy + ry))
    return out


def readable_text_rotation(rot_deg: float) -> float:
    """Fold a part rotation into the range silk text may legally be drawn at.

    A reference designator exists to be READ, by a human holding the board
    and by a pick-and-place operator checking it. A part rotated 180 gets
    upside-down text and a part at 270 gets text running bottom-to-top —
    both are legible only by turning the board over in your hands, which is
    exactly what a refdes is supposed to save you from. Every EDA tool
    normalizes this; KiCad's rule is the one used here: reduce to
    ``(-90, 90]``, so text reads either left-to-right or bottom-to-top and
    never upside-down.

    Only the GLYPH orientation is folded. The label's anchor still rotates
    with the part (see :func:`_place`), because where the label sits
    relative to its own footprint must follow the footprint.
    """
    r = math.fmod(rot_deg, 360.0)
    if r <= -180.0:
        r += 360.0
    elif r > 180.0:
        r -= 360.0
    if r > 90.0:
        r -= 180.0
    elif r <= -90.0:
        r += 180.0
    return r


def _side_for(inst_sides: dict[str, str], refdes: str) -> str:
    raw = str(inst_sides.get(refdes) or "top").lower()
    return "bottom" if raw in ("bottom", "bot", "b") else "top"


def build_silk(
    ir: PcbIR,
    pads: list[dict[str, Any]],
    *,
    instance_sides: dict[str, str] | None = None,
    height_mm: float = DEFAULT_REFDES_HEIGHT_MM,
    stroke_width_mm: float = DEFAULT_SILK_WIDTH_MM,
) -> SilkResult:
    """Build ``{"top": [...], "bottom": [...]}`` silk draws for every
    PLACED instance in ``ir`` — the one builder :mod:`precis.handlers.pcb`
    calls for every gerber/svg-fab site that needs silk (module
    docstring's "one silk builder, never duplicated" discipline).

    ``pads`` is the board's real flashed pad geometry, in
    :mod:`precis.pcb.gerber`'s ``model["pads"]`` shape — used ONLY to
    decide what silk would be scraped off, never to place anything (pad
    positions come from ``pads``, everything else comes from ``ir``).
    """
    sides = instance_sides or {}
    top: list[dict[str, Any]] = []
    bottom: list[dict[str, Any]] = []
    dropped: list[str] = []
    relocated: list[str] = []

    pins_of_inst: dict[int, list[int]] = {}
    for pid in range(ir.n_pins):
        pins_of_inst.setdefault(int(ir.pin_instance[pid]), []).append(pid)

    for inst in range(ir.n_instances):
        cx, cy = float(ir.inst_x[inst]), float(ir.inst_y[inst])
        if math.isnan(cx) or math.isnan(cy):
            continue  # unplaced -- nothing to draw silk for yet
        refdes = str(ir.instance_refdes[inst])
        rot = float(ir.inst_rot[inst])
        rot = 0.0 if math.isnan(rot) else rot
        mirror = _side_for(sides, refdes) == "bottom"
        bucket = bottom if mirror else top

        pins = pins_of_inst.get(inst, [])
        courtyard_reach = _courtyard_reach_mm(ir, inst, pins)

        # 1) courtyard/body outline
        if courtyard_reach > 0:
            box_local = _courtyard_box(ir, inst, courtyard_reach)
            box_pts = _place(box_local, cx=cx, cy=cy, rot=rot, mirror=mirror)
            if _stroke_overlaps_any_pad(box_pts, pads, stroke_width_mm):
                dropped.append(f"{refdes}: courtyard outline overlaps a pad -- dropped")
            else:
                bucket.append(
                    _draw(box_pts, stroke_width_mm, role="outline", refdes=refdes)
                )

        # 2) pin-1 marker
        if pins:
            pin1 = _pin1_id(ir, inst, pins)
            tick_local = _pin1_tick(ir, pin1, courtyard_reach, stroke_width_mm)
            if tick_local:
                tick_pts = _place(tick_local, cx=cx, cy=cy, rot=rot, mirror=mirror)
                if _stroke_overlaps_any_pad(tick_pts, pads, stroke_width_mm):
                    dropped.append(f"{refdes}: pin-1 marker overlaps a pad -- dropped")
                else:
                    bucket.append(
                        _draw(tick_pts, stroke_width_mm, role="pin1", refdes=refdes)
                    )

        unsupported = sorted({c for c in refdes if not stroke_font.supported(c)})
        if unsupported:
            # Not fatal (an unsupported character just draws nothing and
            # still advances the cursor -- see stroke_font's own docstring)
            # but a caller should know the refdes silk prints blank there,
            # same "don't silently swallow" discipline as a dropped label.
            dropped.append(
                f"{refdes}: unsupported character(s) {''.join(unsupported)!r} "
                "in refdes silk -- drawn as a gap, not a glyph"
            )

        # 3) refdes text -- try each candidate placement, drop if none is clear
        text_rot = readable_text_rotation(rot)
        gap = height_mm * 0.3
        placed_text = False
        for idx, (du, dv, h_align, v_align) in enumerate(_CANDIDATES):
            off = courtyard_reach + gap if (du, dv) != (0.0, 0.0) else 0.0
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
            if any(_box_overlaps_pad(corners, pad) for pad in pads):
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
                _stroke_overlaps_any_pad(pts, pads, stroke_width_mm) for pts in strokes
            ):
                continue  # bbox cleared but a real stroke didn't -- try the next spot
            for pts in strokes:
                bucket.append(_draw(pts, stroke_width_mm, role="refdes", refdes=refdes))
            placed_text = True
            if idx > 0:
                relocated.append(
                    f"{refdes}: refdes label moved off-center to clear a pad "
                    f"(candidate {idx})"
                )
            break
        if not placed_text:
            dropped.append(
                f"{refdes}: refdes label dropped -- every candidate placement "
                "overlaps a pad"
            )

    return SilkResult(
        draws={"top": top, "bottom": bottom},
        dropped=tuple(dropped),
        relocated=tuple(relocated),
    )


__all__ = [
    "DEFAULT_REFDES_HEIGHT_MM",
    "SilkResult",
    "build_silk",
    "readable_text_rotation",
]
