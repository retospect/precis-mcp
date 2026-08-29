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
   increasing = clockwise as seen from above). :func:`_rotate_cw` is the
   one place that matrix lives; every other module's rotation reasoning
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

#: EasyEDA's raw ``PAD~shape~...`` token -> the shape vocabulary
#: :func:`precis.pcb.gerber._aperture_for_pad` accepts. ``POLYGON`` (a
#: free-form pad shape) has no equivalent aperture in that writer — it is
#: approximated as its own authored w/h rectangle, a deliberate
#: approximation (not attempted freeform tracing), same spirit as the
#: oblique-rotation approximation above.
_SHAPE_MAP = {
    "ELLIPSE": "circle",
    "OVAL": "obround",
    "RECT": "rect",
    "POLYGON": "rect",
}


def _rotate_cw(x: float, y: float, deg: float) -> tuple[float, float]:
    """Rotate ``(x, y)`` by ``deg`` degrees CLOCKWISE — our internal
    0°-natural, CW-positive rotation frame (see module docstring and
    :func:`precis.pcb.export.jlc_rotation`)."""
    theta = math.radians(deg)
    c, s = math.cos(theta), math.sin(theta)
    return x * c + y * s, -x * s + y * c


def _is_bottom(inst: dict[str, Any]) -> bool:
    """Same string convention :mod:`precis.pcb.export` already uses
    (``cpl_csv``/``specctra_dsn``) — never a second parallel parse."""
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


def place_pad_point(pad: dict[str, Any], inst: dict[str, Any]) -> tuple[float, float]:
    """The one pad-center coordinate transform (mirror -> rotate ->
    translate, module docstring's exact order). Exposed standalone because
    it is the load-bearing piece the round-trip/rotation tests pin
    directly, independent of shape/layer bookkeeping."""
    lx, ly = float(pad["x"]), float(pad["y"])
    if _is_bottom(inst):
        lx = -lx
    rx, ry = _rotate_cw(lx, ly, float(inst.get("rot") or 0.0))
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
    """
    bottom = _is_bottom(inst)
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
        if shape != "circle" and _swap_wh(total_rot):
            w, h = h, w

        entry_dict = pin_map.get(str(pad.get("number")))
        pin_name = (
            str(entry_dict.get("name"))
            if isinstance(entry_dict, dict) and entry_dict.get("name") is not None
            else str(pad.get("number") or "")
        )
        net = pin_to_net.get(pin_name, "")

        drill = pad.get("drill")
        base = {
            "net": net,
            "shape": shape,
            "x": round(bx, 4),
            "y": round(by, 4),
            "w": round(w, 4),
        }
        if shape != "circle":
            base["h"] = round(h, 4)
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
    own membership shape, flattened by the caller)."""
    pin_to_net = pin_to_net or {}
    all_pads: list[dict[str, Any]] = []
    all_drills: list[dict[str, Any]] = []
    for inst in instances:
        if inst.get("x") is None or inst.get("y") is None:
            continue
        lcsc = str(inst.get("part_lcsc") or "")
        fp = footprints.get(lcsc) if lcsc else None
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
    "place_footprint_pads",
    "place_pad_point",
]
