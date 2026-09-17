"""Instance-placed pad geometry — the missing link between a footprint's
own local pads (:mod:`precis.pcb.easyeda`'s ``pads``, cached in
``part_footprints``) and :mod:`precis.pcb.gerber`'s board-coordinate
``model["pads"]`` shape. See ``docs/backlog/pcb-fab-output-unwired.md``:
**nothing in this codebase transformed a footprint pad by its instance's
position/rotation/side before this module existed** — the gap this closes.

**Coordinate convention.** A footprint's pads are authored in the
footprint's own local frame, +X right / +Y up, origin at the footprint's
own reference point (:mod:`precis.pcb.easyeda` already flips EasyEDA's
native +Y-down into this convention on parse). Placing one pad on the
board is, in order:

1. **Mirror** (bottom-side instances only) — negate the local X coordinate
   (reflect across the footprint's own local Y axis). This is the
   "flip the part over like a card, top edge stays on top" convention:
   a pad that sat to the local *east* of the footprint's origin ends up to
   the local *west* once the whole footprint is flipped for bottom-side
   mounting. Y is untouched.
2. **Rotate** by the instance's ``rot`` degrees, in :mod:`precis.pcb.export`
   ``jlc_rotation``'s own documented frame — **our internal rotation is
   CW-from-north** (0° = the footprint's natural/authored orientation,
   increasing = clockwise as seen from above). **:func:`_rotate_cw` is NOT
   the only place that matrix lives** — this docstring claimed it was until
   2026-08-29, which was both wrong and actively obstructive: a reader who
   believed it stopped looking. :func:`precis.pcb.landpattern.rotate_offset`
   spells out the identical ``[[cos, sin], [-sin, cos]]``, and is what
   ``silk.py`` and ``stroke_font.py`` call — so TEXT and SILK rotate through
   the footprint module while footprint pads rotate through this private
   copy. The two agree today; the mirror-before-rotate policy is likewise
   implemented twice and pinned by two separate test suites asserting the
   same fact. Folding both into one affine transform is a tracked item
   (``docs/backlog/pcb-engine-plan.md``, "one affine-transform path").
   Every other module's rotation reasoning
   (``export.jlc_rotation``) is a re-expression of the SAME 0°-natural,
   CW-positive convention applied to this same transform, not a second
   invented one.
3. **Translate** by the instance's ``(x, y)``.

Mirroring happens BEFORE rotation: a bottom-side part is flipped first
(as it physically is, placed face-down), then the whole flipped footprint
is rotated to its authored placement angle — reversing the order would
rotate in the wrong handedness for any ``rot != 0`` bottom part. This
ordering, and the CW-vs-mirror interaction, is asserted directly in
``tests/test_pcb_padplace.py`` — "a silently-wrong rotation produces a
board that looks plausible and is unbuildable" (task brief, verbatim), so
the numbers are pinned, not just smoke-tested.

**Copper layer.** A pad's own ``layer`` (``F.Cu``/``B.Cu``, EasyEDA-parsed)
is the footprint's AUTHORED side. A bottom-side instance flips it (the
whole footprint is on the other side of the board now); a top-side
instance leaves it as authored. A through-hole pad (``drill`` set) instead
gets ONE flash per board copper layer — a real annular ring runs the full
stack, not just the two outer layers — plus one Excellon hole in
``model["drills"]`` (``plated=True``: a soldered component lead, the
same "plated unless marked otherwise" default :mod:`precis.pcb.export`'s
Excellon writer already documents).

**Shape/size.** :mod:`precis.pcb.gerber`'s apertures carry NO rotation
angle (``_ApertureTable`` is shape+size only) — a genuinely oblique
(non-multiple-of-90°) rotation cannot be represented exactly for a
rectangular/obround pad by this writer. :func:`_swap_wh` handles the one
case that CAN be represented exactly — an effective rotation that lands on
90°/270° swaps width and height so the flashed aperture still matches the
pad's true footprint; any other angle keeps the authored w/h (an honest,
documented approximation, not a silent wrong answer — circle pads are
unaffected either way since they have no orientation).
"""

from __future__ import annotations

import math
from typing import Any

#: EasyEDA's raw ``PAD~shape~...`` token (or an authored local footprint's
#: own lowercase shape word, upper-cased the same way) -> the shape
#: vocabulary :mod:`precis.pcb.gerber` accepts. ``POLYGON`` used to
#: down-approximate to ``rect`` (a free-form pad shape has no aperture in
#: that writer) — now carried through as its own ``"polygon"`` shape,
#: which :func:`precis.pcb.gerber._emit_pad` renders as a region fill
#: (G36/G37) off the pad's ``poly`` vertex ring instead of an aperture
#: flash (pcb-ewod-multitile Slice 1). ``CIRCLE``/``OBROUND`` are the
#: authoring-side spellings of ``ELLIPSE``/``OVAL`` — a local footprint
#: never speaks EasyEDA's token dialect, so both alphabets map onto the
#: same three-or-four-shape output vocabulary here.
_SHAPE_MAP = {
    "ELLIPSE": "circle",
    "CIRCLE": "circle",
    "OVAL": "obround",
    "OBROUND": "obround",
    "RECT": "rect",
    "POLYGON": "polygon",
}


def _rotate_cw(x: float, y: float, deg: float) -> tuple[float, float]:
    """Rotate ``(x, y)`` by ``deg`` degrees CLOCKWISE — our internal
    0°-natural, CW-positive rotation frame (see module docstring and
    :func:`precis.pcb.export.jlc_rotation`)."""
    theta = math.radians(deg)
    c, s = math.cos(theta), math.sin(theta)
    return x * c + y * s, -x * s + y * c


def is_bottom_instance(inst: dict[str, Any]) -> bool:
    """Same string convention :mod:`precis.pcb.export` already uses
    (``cpl_csv``/``specctra_dsn``) — never a second parallel parse.

    Public (gr341516) — :func:`precis.pcb.ir.from_graph` reads this SAME
    predicate to populate :attr:`~precis.pcb.ir.PcbIR.inst_bottom`, and
    :mod:`precis.handlers.pcb` reads it again to build a courtyard's
    board-side tag for :func:`precis.pcb.drc.check_courtyard_overlap`. One
    parse of ``pcb_instances.layer``'s ``"top"``/``"bottom"`` string, not
    three — the exact "one rule, two call sites, drifted" defect this
    module's other docstrings already name for pad geometry and rotation."""
    return str(inst.get("layer") or "top").lower() in ("bottom", "bot", "b")


def _swap_wh(rot_deg: float, *, tol_deg: float = 0.05) -> bool:
    """True when ``rot_deg`` (mod 180) is within ``tol_deg`` of 90° — the
    only rotation this aperture-less writer can represent exactly for a
    non-circular pad by swapping width/height (module docstring)."""
    r = rot_deg % 180.0
    return abs(r - 90.0) <= tol_deg


def _effective_layer(pad_layer: str, *, bottom: bool) -> str:
    if not bottom:
        return pad_layer
    if pad_layer == "F.Cu":
        return "B.Cu"
    if pad_layer == "B.Cu":
        return "F.Cu"
    return pad_layer  # already a named inner/other layer -- leave alone


def _transform_local_point(
    lx: float, ly: float, inst: dict[str, Any]
) -> tuple[float, float]:
    """Mirror -> rotate (module docstring's exact order), LEAVING OFF the
    final translate — the shared half of :func:`place_pad_point` a polygon
    pad's vertex ring also needs: mirroring and rotation are both linear
    (no translate term), so applying this to a vertex expressed relative
    to its own pad's center and adding that center's OWN transformed
    position afterwards gives the same answer as transforming the
    absolute vertex directly — one transform, reused per-point instead of
    re-derived for the polygon case."""
    if is_bottom_instance(inst):
        lx = -lx
    return _rotate_cw(lx, ly, float(inst.get("rot") or 0.0))


def place_pad_point(pad: dict[str, Any], inst: dict[str, Any]) -> tuple[float, float]:
    """The one pad-center coordinate transform (mirror -> rotate ->
    translate, module docstring's exact order). Exposed standalone because
    it is the load-bearing piece the round-trip/rotation tests pin
    directly, independent of shape/layer bookkeeping."""
    lx, ly = float(pad["x"]), float(pad["y"])
    rx, ry = _transform_local_point(lx, ly, inst)
    return float(inst["x"]) + rx, float(inst["y"]) + ry


def place_footprint_pads(
    pads: list[dict[str, Any]],
    inst: dict[str, Any],
    *,
    layers: list[str],
    pin_map: dict[str, Any] | None = None,
    pin_to_net: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One instance's footprint pads, transformed into board coordinates.

    Returns ``(pads, drills)`` — ``pads`` in :mod:`precis.pcb.gerber`'s
    ``model["pads"]`` shape (one entry per copper layer for a through-hole
    pad, one for SMD), ``drills`` in :mod:`precis.pcb.export`'s
    ``model["drills"]`` shape (``{"x","y","dia_mm","plated"}``) for every
    through-hole pad's Excellon hole.

    ``inst`` needs ``x``/``y`` (already placed — an unplaced instance has
    nothing to transform, the caller's job to skip) and optionally
    ``rot``/``layer``. ``pin_map``/``pin_to_net`` are both optional — a
    pad's ``net`` field is decorative only (:mod:`precis.pcb.gerber` never
    reads it, see its module docstring's model shape comment); missing
    net-name data degrades to an empty string, never a skipped pad.

    ``role``/``mask``/``paste`` (pcb-ewod-multitile Slice 1's authoring
    surface, ``docs/backlog/pcb-component-model.md`` §Features' minimum
    viable slice) ride straight through onto the placed pad dict when the
    source pad carries them — an EasyEDA-parsed catalog footprint never
    does, so those pads are silently untouched (``.get`` with the same
    default :mod:`precis.pcb.gerber` already assumed). A ``shape:
    "polygon"`` pad's ``poly`` vertex ring is transformed per-vertex by
    the SAME mirror/rotate :func:`place_pad_point` uses for the center
    (:func:`_transform_local_point`) — the center is still emitted too
    (as the ring's own bbox center) so a consumer that only reads
    ``x``/``y``/``w``/``h`` (the courtyard/DRC bbox fallback) keeps
    working.
    """
    bottom = is_bottom_instance(inst)
    inst_rot = float(inst.get("rot") or 0.0)
    pin_map = pin_map or {}
    pin_to_net = pin_to_net or {}

    out_pads: list[dict[str, Any]] = []
    out_drills: list[dict[str, Any]] = []
    for pad in pads:
        bx, by = place_pad_point(pad, inst)
        raw_shape = str(pad.get("shape") or "").upper()
        shape = _SHAPE_MAP.get(raw_shape, "rect")
        w = float(pad["w"])
        h = float(pad.get("h", pad["w"]))
        total_rot = inst_rot + float(pad.get("rot") or 0.0)
        if shape in ("rect", "obround") and _swap_wh(total_rot):
            w, h = h, w

        entry_dict = pin_map.get(str(pad.get("number")))
        pin_name = (
            str(entry_dict.get("name"))
            if isinstance(entry_dict, dict) and entry_dict.get("name") is not None
            else str(pad.get("number") or "")
        )
        net = pin_to_net.get(pin_name, "")

        drill = pad.get("drill")
        base: dict[str, Any] = {
            "net": net,
            "shape": shape,
            "x": round(bx, 4),
            "y": round(by, 4),
            "w": round(w, 4),
        }
        if shape != "circle":
            base["h"] = round(h, 4)
        if shape == "polygon" and pad.get("poly"):
            base["poly"] = [
                [
                    round(float(inst["x"]) + rx, 4),
                    round(float(inst["y"]) + ry, 4),
                ]
                for rx, ry in (
                    _transform_local_point(float(vx), float(vy), inst)
                    for vx, vy in pad["poly"]
                )
            ]
        if pad.get("role") is not None:
            base["role"] = str(pad["role"])
        if pad.get("mask") is not None:
            base["mask"] = str(pad["mask"])
        if pad.get("paste") is not None:
            base["paste"] = str(pad["paste"])
        if drill:
            # Carry the drill ON the pad, not only in ``out_drills``. The
            # pad rows already encode the CONSEQUENCE of being through-hole
            # (they land on every copper layer, just below) while discarding
            # the CAUSE, so nothing downstream could ask a pad whether it is
            # plated-through. Solder paste is the consumer that needs it —
            # paste over a plated hole falls through, so the stencil must
            # skip THT pads — and inferring it back by matching a pad's
            # coordinate against ``model["drills"]`` would be re-deriving a
            # fact we chose to throw away, at rounding-tolerance risk.
            base["drill"] = float(drill)

        target_layers = (
            list(layers)
            if drill
            else [_effective_layer(str(pad.get("layer") or "F.Cu"), bottom=bottom)]
        )
        for layer in target_layers:
            out_pads.append({"layer": layer, **base})

        if drill:
            out_drills.append(
                {
                    "x": round(bx, 4),
                    "y": round(by, 4),
                    "dia_mm": float(drill),
                    "plated": True,
                }
            )
    return out_pads, out_drills


def board_pads(
    instances: list[dict[str, Any]],
    footprints: dict[str, dict[str, Any]],
    *,
    layers: list[str],
    pin_to_net: dict[tuple[str, str], str] | None = None,
    local_footprints: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Every placed instance's pads, transformed into board coordinates —
    the whole design's ``(model["pads"], model["drills"])``.

    An instance with no ``x``/``y`` yet (unplaced) or whose ``part_lcsc``
    has no cached ``part_footprints`` row (never fetched via
    :mod:`precis.pcb.easyeda`, or a part with no linked footprint at all)
    contributes nothing — an honest gap, not invented geometry (task
    brief: "if the pad data ... turns out insufficient ... say so
    precisely rather than inventing geometry"). ``pin_to_net`` is keyed
    ``(refdes, pin_name) -> net_name`` (:meth:`precis.store.pcb_graph`'s
    own membership shape, flattened by the caller).

    ``local_footprints`` (pcb-ewod-multitile Slice 1, optional) is the
    SAME shape as ``footprints`` but keyed by name instead of C-number —
    :meth:`~precis.store.Store.pcb_local_footprints_for`'s cache for an
    instance authored with no LCSC part at all
    (``pcb_components.footprint`` names it). Checked only when the
    instance has no ``part_lcsc`` match, never as a second source for a
    catalog part — a component picks exactly one footprint origin."""
    pin_to_net = pin_to_net or {}
    local_footprints = local_footprints or {}
    all_pads: list[dict[str, Any]] = []
    all_drills: list[dict[str, Any]] = []
    for inst in instances:
        if inst.get("x") is None or inst.get("y") is None:
            continue
        lcsc = str(inst.get("part_lcsc") or "")
        fp = footprints.get(lcsc) if lcsc else None
        if fp is None:
            name = str(inst.get("footprint") or "")
            fp = local_footprints.get(name) if name else None
        if not fp or not fp.get("pads"):
            continue
        refdes = str(inst.get("refdes") or "")
        local_pin_to_net = {
            pin: net for (r, pin), net in pin_to_net.items() if r == refdes
        }
        pads, drills = place_footprint_pads(
            fp["pads"],
            inst,
            layers=layers,
            pin_map=fp.get("pin_map"),
            pin_to_net=local_pin_to_net,
        )
        all_pads.extend(pads)
        all_drills.extend(drills)
    return all_pads, all_drills


__all__ = [
    "board_pads",
    "is_bottom_instance",
    "place_footprint_pads",
    "place_pad_point",
]
