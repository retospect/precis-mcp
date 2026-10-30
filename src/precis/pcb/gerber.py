"""Gerber X2 (RS-274X) + Excellon writer — the copper sibling of
:mod:`precis.pcb.export`.

Slice 4 of ``docs/backlog/pcb-guided-place-route.md`` **replaces the
``.kicad_pcb`` writer as the critical path** (user decision 2026-08-27):
routing through a version-brittle s-expression board format so an external
binary (``kicad-cli``) can flatten it into gerbers is backwards. RS-274X is
already flat — aperture definitions + draws — so we emit it directly off a
canonical copper model, the same "pure function, no binary" discipline
:mod:`precis.pcb.export` already uses for BOM/CPL/DSN. No ``.kicad_pcb``
writer and no ``kicad-cli`` dependency belong on this path; the spec demotes
both to an optional human-viewing convenience, out of scope here.

**Input model.** Slice 4 ships independent of the IR (slice 3) and the
realizer (slices 5-7), so there is no ``pcb_copper`` table to read yet. The
model dict below is deliberately shaped to match the *eventual*
``pcb_copper`` row (``ctype: track|via|pour``, ``geom``) recorded in the
schema section of the backlog, so the realizer can hand this module its
output largely as-is once it exists::

    {
      "layers": ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],   # ordered, top→bottom
      "outline": [[x, y], ...],                          # closed polygon, mm
      "copper": [
        {"ctype": "track", "layer": "F.Cu", "net": "GND", "width_mm": 0.25,
         "segments": [
             {"shape": "line", "start": [x, y], "end": [x, y]},
             {"shape": "arc", "start": [x, y], "end": [x, y],
              "center": [x, y], "cw": True},
         ]},
        {"ctype": "via", "net": "GND", "x": .., "y": .., "dia_mm": 0.6,
         "drill_mm": 0.3, "span": ["F.Cu", "B.Cu"]},   # span omitted = through
        {"ctype": "pour", "layer": "In1.Cu", "net": "GND",
         "polygon": [[x, y], ...]},
      ],
      "pads": [
        {"layer": "F.Cu", "net": "3V3",
         "shape": "circle"|"rect"|"obround"|"polygon",
         "x": .., "y": .., "w": .., "h": ..,   # h ignored for "circle";
         # w/h are an informational bbox only for "polygon" (the real
         # shape is "poly" below) -- kept so a courtyard/DRC bbox
         # fallback still has something to read.
         "poly": [[x, y], ...],   # REQUIRED when shape == "polygon" (a
         # closed-or-open vertex ring, board mm) -- rendered as a
         # G36/G37 region fill, never an aperture flash
         # (pcb-ewod-multitile Slice 1: `gerber._emit_pad`).
         "role": "solderable"|"electrode"|"probe",   # optional, default
         # "solderable" when omitted -- carried straight through from an
         # authored local footprint (precis.pcb.padplace), absent on an
         # EasyEDA-parsed catalog pad.
         "mask": "open"|"covered", "paste": "none"|"full",   # optional
         # per-pad intent (same Slice 1); a "covered" pad gets NO
         # per-pad soldermask opening (see `mask_open_regions` below),
         # "none" paste gets no stencil aperture.
         "refdes": "U1", "pin": "3"},   # OPTIONAL — see the X2 identity
         # note below; pads built outside realize.pads_for_ir may omit
         # these two, so component identity is written only when a caller
         # supplies it: refdes+pin -> %TO.C+%TO.P, refdes alone -> %TO.C.
      ],
      "mask_open_regions": [   # optional (Slice 1) -- one soldermask
        {"side": "top"|"bottom", "polygon": [[x, y], ...]},   # opening
        # PER REGION rather than per pad: an EWOD electrode field is
        # opened as one region covering pads AND the gaps between them
        # (a ~100um gap can't hold a mask dam, and mask edges are
        # topography the droplet pins on) -- pads under it are normally
        # authored "mask": "covered" so they get no competing per-pad
        # opening. `soldermask_gerber` emits this as its own region fill.
      ],
      "silkscreen": {"top": [<draw>, ...], "bottom": [<draw>, ...]},  # <draw>
      # is normally the same {"width_mm", "segments"} shape as a track,
      # layer-less, optionally carrying "polarity": "clear" to erase
      # rather than add ink, and a "refdes" naming the part it belongs to
      # (-> %TO.C; silk.py's per-part courtyard/pin-1/refdes draws carry
      # theirs, board-level marks carry refdes=""); OR
      # {"shape": "region", "polygon": [...]} for a solid fill — see
      # silkscreen_gerber's own docstring.
    }

Everything here is a pure function of that dict → ``{filename: content}`` —
zero I/O, trivially unit-testable. :func:`zip_fab` is the one thin
convenience that touches bytes (an in-memory zip), still no disk/network.

**X2 object attributes** (``%TO.N,<net>*%``, ``%TO.P,<refdes>,<pin>*%``
and ``%TO.C,<refdes>*%``, cleared with ``%TD*%``) are written alongside
the draw/flash they describe — the same mechanism the file-level
``%TF...*%`` attributes already use, one level down, onto individual
objects instead of the whole file. This is what lets
:mod:`precis.pcb.gerber_view` label a hovered trace/via/pad with its net
(and, where the model supplies it, its component pin) *from the gerber
itself* rather than by cross-referencing the in-memory model — see that
module's docstring for why the second option is the wrong one. Every
``%TO...*%`` this writer emits is matched by a ``%TD*%`` immediately
after the object it describes, so an attribute can never carry over onto
the next draw/flash by omission.

``%TO.C`` is emitted on every object the model attributes to a
component: copper pads (alongside ``%TO.P`` when the pin is known) and
the silkscreen's per-part draws (courtyard, pin-1 mark, refdes label).
Reto ruled this in 2026-10-21 (overriding 5d36040ce): the fab process
doesn't read component attributes, but fab and assembly people open the
same export in their own Gerber viewers and need the labels — and it is
what lets ``gerber_view._part_body_els`` box a pad-less part (a mounting
hole, fiducial or logo) from its labelled outlines.
"""

from __future__ import annotations

import io
import math
import zipfile
from typing import Any

from shapely.geometry import Polygon as _ShapelyPolygon

# RS-274X format spec: 4 integer digits, 6 decimal digits, leading zeros
# omitted, absolute coordinates — %FSLAX46Y46*%. Every coordinate is an
# integer count of this unit (mm / 10**_MM_DECIMALS).
_MM_DECIMALS = 6
_UNITS_PER_MM = 10**_MM_DECIMALS
_FS = "%FSLAX46Y46*%"

# JLC-typical soldermask swell per side over the pad outline — the
# FALLBACK only: `soldermask_gerber` prefers this board's own
# `model["soldermask_expansion_mm"]`, resolved off the fab-capability row
# (`capabilities.FIELDS`), and uses this when the caller supplied none.
SOLDERMASK_EXPANSION_MM = 0.05
EDGE_CUT_WIDTH_MM = 0.1
DEFAULT_SILK_WIDTH_MM = 0.15


# ─────────────────────────────────────────────────────────────────────
# low-level coordinate + aperture plumbing
# ─────────────────────────────────────────────────────────────────────
def _u(mm: float) -> int:
    return round(mm * _UNITS_PER_MM)


def _coord(x_mm: float, y_mm: float) -> str:
    return f"X{_u(x_mm)}Y{_u(y_mm)}"


class _ApertureTable:
    """Dedups aperture definitions by (shape, size) within one file — the
    same size used by two draws gets one ``%ADD..*%``, not two. IDs start at
    10 (0-9 are reserved by the spec) and are assigned in first-use order."""

    def __init__(self) -> None:
        self._ids: dict[tuple[str, tuple[float, ...]], int] = {}
        self._next = 10

    def id_for(self, shape: str, *sizes: float) -> int:
        key = (shape, tuple(round(s, 4) for s in sizes))
        aid = self._ids.get(key)
        if aid is None:
            aid = self._next
            self._ids[key] = aid
            self._next += 1
        return aid

    def defs(self) -> list[str]:
        out = []
        for (shape, sizes), aid in sorted(self._ids.items(), key=lambda kv: kv[1]):
            if shape == "C":
                out.append(f"%ADD{aid}C,{sizes[0]:.4f}*%")
            else:  # "R" rectangle, "O" obround — same WxH modifier shape
                out.append(f"%ADD{aid}{shape},{sizes[0]:.4f}X{sizes[1]:.4f}*%")
        return out


def _aperture_for_pad(pad: dict[str, Any], apertures: _ApertureTable) -> int:
    shape = pad.get("shape", "circle")
    if shape == "circle":
        return apertures.id_for("C", float(pad["w"]))
    if shape in ("rect", "obround"):
        code = "R" if shape == "rect" else "O"
        return apertures.id_for(code, float(pad["w"]), float(pad.get("h", pad["w"])))
    raise ValueError(f"unknown pad shape {shape!r}")


def _is_polygon_pad(pad: dict[str, Any]) -> bool:
    return pad.get("shape") == "polygon" and bool(pad.get("poly"))


# ─────────────────────────────────────────────────────────────────────
# X2 object attributes — %TO.N (net) / %TO.P (component pin) /
# %TO.C (component refdes), %TD (clear)
# ─────────────────────────────────────────────────────────────────────
def _emit_object_attrs(
    body: list[str],
    *,
    net: str | None = None,
    refdes: str | None = None,
    pin: str | None = None,
) -> bool:
    """Emit the identity attributes for the object about to be drawn.
    Returns whether anything was emitted, so the caller knows whether it
    owes a matching ``%TD*%`` afterwards — an object with no identity gets
    no attribute lines at all, not an empty ``%TO.N,*%``.

    ``%TO.C,<refdes>*%`` goes on every object named to a component — a pad
    (with or without a known pin) and a per-part silk draw alike — so a
    viewer can collect the part's objects, including a part that has no
    pads at all (module docstring). Order follows KiCad's: net, pin,
    component."""
    emitted = False
    if net:
        body.append(f"%TO.N,{net}*%")
        emitted = True
    if refdes and pin:
        body.append(f"%TO.P,{refdes},{pin}*%")
        emitted = True
    if refdes:
        body.append(f"%TO.C,{refdes}*%")
        emitted = True
    return emitted


def _emit_with_attrs(
    body: list[str],
    draw: Any,
    *,
    net: str | None = None,
    refdes: str | None = None,
    pin: str | None = None,
) -> None:
    """Wrap one draw/flash call in its ``%TO...*%``/``%TD*%`` pair (if it
    has any identity to carry) — the single place that guarantees an
    attribute never leaks onto the object emitted after it."""
    has_attrs = _emit_object_attrs(body, net=net, refdes=refdes, pin=pin)
    draw()
    if has_attrs:
        body.append("%TD*%")


# ─────────────────────────────────────────────────────────────────────
# draws: strokes (tracks/silk) and flashes (pads/via barrels)
# ─────────────────────────────────────────────────────────────────────
def _emit_flash(
    pad: dict[str, Any], apertures: _ApertureTable, body: list[str]
) -> None:
    aid = _aperture_for_pad(pad, apertures)
    body.append(f"D{aid}*")
    body.append(f"{_coord(float(pad['x']), float(pad['y']))}D03*")


def _emit_pad(pad: dict[str, Any], apertures: _ApertureTable, body: list[str]) -> None:
    """One pad, either an aperture flash (circle/rect/obround,
    :func:`_emit_flash`) or, for a ``shape: "polygon"`` pad (authored
    copper — pcb-ewod-multitile Slice 1), a ``G36``/``G37`` region fill
    off its ``poly`` vertex ring — region fill over an aperture macro
    per the backlog's own open-questions call (simplest, and the writer
    already owns this primitive for pours/silk regions)."""
    if _is_polygon_pad(pad):
        _emit_region_ring([(float(p[0]), float(p[1])) for p in pad["poly"]], body)
        return
    _emit_flash(pad, apertures, body)


def _emit_stroke(
    draw: dict[str, Any], apertures: _ApertureTable, body: list[str]
) -> None:
    """A polyline/arc-chain draw at ``draw['width_mm']`` — used for both
    copper tracks and silkscreen lines (they differ only in which layer/file
    they land in, decided by the caller)."""
    width = float(draw.get("width_mm", DEFAULT_SILK_WIDTH_MM))
    aid = apertures.id_for("C", width)
    body.append(f"D{aid}*")
    cur: tuple[float, float] | None = None
    mode: str | None = None
    for seg in draw.get("segments") or []:
        start = (float(seg["start"][0]), float(seg["start"][1]))
        end = (float(seg["end"][0]), float(seg["end"][1]))
        if cur != start:
            body.append(f"{_coord(*start)}D02*")
        if seg.get("shape") == "arc":
            cx, cy = float(seg["center"][0]), float(seg["center"][1])
            gcode = "G02" if seg.get("cw", True) else "G03"
            if mode != gcode:
                body.append(f"{gcode}*")
                mode = gcode
            i, j = _u(cx - start[0]), _u(cy - start[1])
            body.append(f"{_coord(*end)}I{i}J{j}D01*")
        else:
            if mode != "G01":
                body.append("G01*")
                mode = "G01"
            body.append(f"{_coord(*end)}D01*")
        cur = end


def _emit_region(
    pour: dict[str, Any], body: list[str], *, net: str | None = None
) -> None:
    """``G36``/``G37`` polygon region for a copper pour/plane — filled by the
    boundary trace, no aperture needed (a region's fill isn't a stroke).

    Holes are emitted as clear-polarity (``%LPC*%``) regions after the
    solid one, which is the standard RS-274X idiom for a plane's antipads.
    A pour that carries ``holes`` and images them as solid copper would
    short every foreign via passing through the plane — so the hole list is
    not optional decoration, it is the difference between a plane and a
    short.

    The ``%TO.N*%``/``%TD*%`` pair (if the pour has a net) wraps only the
    solid ring, never the holes: a hole is where this net's copper ISN'T —
    tagging it with the pour's own net would mislabel the one place on this
    object that is deliberately not that net.
    """
    poly = [(float(p[0]), float(p[1])) for p in pour["polygon"]]
    if not poly:
        return
    _emit_with_attrs(body, lambda: _emit_region_ring(poly, body), net=net)
    holes = pour.get("holes") or []
    if not holes:
        return
    body.append("%LPC*%")
    for hole in holes:
        ring = [(float(p[0]), float(p[1])) for p in hole]
        if ring:
            _emit_region_ring(ring, body)
    body.append("%LPD*%")


def _emit_region_ring(poly: list[tuple[float, float]], body: list[str]) -> None:
    body.append("G36*")
    body.append("G01*")
    body.append(f"{_coord(*poly[0])}D02*")
    for pt in [*poly[1:], poly[0]]:
        body.append(f"{_coord(*pt)}D01*")
    body.append("G37*")


def _polyline_draw(points: list[Any], *, width_mm: float) -> dict[str, Any]:
    pts = [(float(p[0]), float(p[1])) for p in points]
    segments = [
        {"shape": "line", "start": pts[i], "end": pts[i + 1]}
        for i in range(len(pts) - 1)
    ]
    return {"width_mm": width_mm, "segments": segments}


def _outline_draw(outline: Any) -> dict[str, Any]:
    if isinstance(outline, dict):  # pre-built {"segments": [...]} (arcs allowed)
        return {"width_mm": EDGE_CUT_WIDTH_MM, "segments": outline.get("segments", [])}
    pts = list(outline)
    if pts and tuple(pts[0]) != tuple(pts[-1]):
        pts = [*pts, pts[0]]  # close the polygon
    return _polyline_draw(pts, width_mm=EDGE_CUT_WIDTH_MM)


def _expand_pad(pad: dict[str, Any], margin_mm: float) -> dict[str, Any]:
    out = dict(pad)
    if _is_polygon_pad(pad):
        # Grow the ring itself (shapely buffer, mitred like every other
        # outward offset this package computes — `ir.py`'s courtyard
        # offset is the same join style) rather than the w/h it has no
        # meaningful use for.
        ring = _ShapelyPolygon([(float(p[0]), float(p[1])) for p in pad["poly"]])
        grown = ring.buffer(margin_mm, join_style=2)
        out["poly"] = [[float(x), float(y)] for x, y in grown.exterior.coords]
        return out
    out["w"] = float(pad["w"]) + 2 * margin_mm
    if pad.get("shape", "circle") != "circle":
        out["h"] = float(pad.get("h", pad["w"])) + 2 * margin_mm
    return out


def _via_layers(item: dict[str, Any], all_layers: list[str]) -> list[str]:
    """Copper layers a via flashes on: an explicit override, a blind/buried
    ``span``, or — the default — every layer (a through via)."""
    if item.get("layers"):
        return list(item["layers"])
    span = item.get("span")
    if span:
        i0, i1 = all_layers.index(span[0]), all_layers.index(span[1])
        lo, hi = min(i0, i1), max(i0, i1)
        return all_layers[lo : hi + 1]
    return list(all_layers)


# ─────────────────────────────────────────────────────────────────────
# file assembly
# ─────────────────────────────────────────────────────────────────────
def _assemble(function_attr: str, apertures: _ApertureTable, body: list[str]) -> str:
    lines = [
        "G04 precis pcb gerber export*",
        _FS,
        "%MOMM*%",
        f"%TF.FileFunction,{function_attr}*%",
        "%TF.FilePolarity,Positive*%",
        "%TF.GenerationSoftware,precis,pcb-gerber,1.0*%",
        "%TF.Part,Single*%",
        *apertures.defs(),
        "G01*",
        "G75*",  # multi-quadrant arc mode — required before any G02/G03 use
        *body,
        "M02*",
    ]
    return "\n".join(lines) + "\n"


def _layer_token(layer: str) -> str:
    """KiCad/JLC-style filename token: ``F.Cu`` → ``F_Cu``."""
    return layer.replace(".", "_")


# ─────────────────────────────────────────────────────────────────────
# per-file writers
# ─────────────────────────────────────────────────────────────────────
def copper_gerber(model: dict[str, Any], layer: str) -> str:
    """One RS-274X file for a single copper layer: tracks, pours (as
    ``G36``/``G37`` regions), via barrel flashes that span this layer, and
    any pad landing on it."""
    layers: list[str] = model["layers"]
    index = layers.index(layer)
    pos = "Top" if index == 0 else "Bot" if index == len(layers) - 1 else "Inr"
    apertures = _ApertureTable()
    body: list[str] = []
    for item in model.get("copper", []):
        ctype = item.get("ctype")
        net = item.get("net")
        if ctype == "track" and item.get("layer") == layer:
            _emit_with_attrs(
                body, lambda item=item: _emit_stroke(item, apertures, body), net=net
            )
        elif ctype == "pour" and item.get("layer") == layer:
            _emit_region(item, body, net=net)
        elif ctype == "via" and layer in _via_layers(item, layers):
            pad = {
                "shape": "circle",
                "x": item["x"],
                "y": item["y"],
                "w": item["dia_mm"],
            }
            _emit_with_attrs(
                body, lambda pad=pad: _emit_flash(pad, apertures, body), net=net
            )
    for pad in model.get("pads", []):
        if pad.get("layer") == layer:
            _emit_with_attrs(
                body,
                lambda pad=pad: _emit_pad(pad, apertures, body),
                net=pad.get("net"),
                refdes=pad.get("refdes"),
                pin=pad.get("pin"),
            )
    return _assemble(f"Copper,L{index + 1},{pos}", apertures, body)


def soldermask_gerber(model: dict[str, Any], side: str) -> str:
    """Top/bottom soldermask openings — pads on the corresponding outer
    copper layer, each expanded by the board's own soldermask expansion.
    Vias are assumed tented (no opening) — the common default; a
    tented-via override is future work, not needed for slice 4.

    The expansion comes from ``model["soldermask_expansion_mm"]`` when the
    caller resolved one off this board's fab-capability row, falling back
    to :data:`SOLDERMASK_EXPANSION_MM`. **That key is not decoration.**
    :func:`precis.pcb.silk.silk_clearance_mm` derives every courtyard's
    size by starting from this same swell, so a mask film drawn with one
    expansion while silk was placed against another is two numbers for one
    physical edge — the drift this key exists to prevent.

    A pad carrying ``mask: "covered"`` (pcb-ewod-multitile Slice 1) gets
    NO per-pad opening here — soldermask stays over it, either because it
    is genuinely meant to stay covered, or because a field-wide
    ``model["mask_open_regions"]`` entry opens it instead (an electrode
    field: "one region covering pads AND gaps", not per-pad swollen
    openings with a mask dam between every neighbour — see that key's own
    handling below)."""
    layers: list[str] = model["layers"]
    layer = layers[0] if side == "top" else layers[-1]
    expansion = float(model.get("soldermask_expansion_mm", SOLDERMASK_EXPANSION_MM))
    apertures = _ApertureTable()
    body: list[str] = []
    for pad in model.get("pads", []):
        if pad.get("layer") == layer and pad.get("mask") != "covered":
            _emit_pad(_expand_pad(pad, expansion), apertures, body)
    for region in model.get("mask_open_regions") or []:
        if region.get("side") != side:
            continue
        poly = [(float(p[0]), float(p[1])) for p in region.get("polygon") or []]
        if len(poly) >= 3:
            _emit_region_ring(poly, body)
    return _assemble(f"Soldermask,{'Top' if side == 'top' else 'Bot'}", apertures, body)


def declares_parylene_coat(model: dict[str, Any]) -> bool:
    """The board takes the parylene conformal coat: it carries an EWOD
    array (a ``role: 'electrode'`` pad or a field-wide ``mask_open``
    region). The one predicate both the fab-notes parylene process order
    (:func:`fab_notes_txt`) and the intra-footprint clearance relaxation
    in :func:`precis.pcb.drc.check_clearance` read, so the warn applies
    exactly where the fab is told to coat."""
    pads = model.get("pads") or []
    return any(p.get("role") == "electrode" for p in pads) or bool(
        model.get("mask_open_regions")
    )


def fab_notes_txt(model: dict[str, Any]) -> str | None:
    """The ``<name>-fab-notes.txt`` rider in the fab bundle — the assembly
    notes a gerber cannot carry (pcb-ewod-multitile.md's "fab-notes text
    file in the gerber zip"; ewod-controller-and-hv-supply.md Slice 3).

    Returns ``None`` when the board has nothing to say, so a plain design's
    bundle gains no stray file. Everything here is DERIVED from the model —
    a note is emitted because the geometry that needs it is present, never
    remembered from an authoring-time string.

    - an EWOD array (electrode pads or a field-wide ``mask_open`` region)
      gets the parylene process order — the coating step coats whatever it
      sees, so anything that must stay uncoated is masked first;
    - a ``role: 'tape_land'`` pad (the ITO top plate's copper-tape
      landing) gets its own masking callout: parylene is conformal and a
      coated land is an open circuit, so the strip is taped off during
      deposition.
    """
    pads = model.get("pads") or []
    ewod = declares_parylene_coat(model)
    tape_lands = sorted(
        {str(p.get("refdes") or "?") for p in pads if p.get("role") == "tape_land"}
    )
    if not (ewod or tape_lands):
        return None
    lines = ["Fabrication/assembly notes"]
    if ewod:
        lines.append(
            "- parylene process order: assemble the bottom side -> mask the "
            "bottom (fixturing/tape) -> parylene deposit the top. The coating "
            "is conformal over everything it can see; the order is what "
            "keeps solderable surfaces uncoated."
        )
    if tape_lands:
        lines.append(
            f"- mask the copper-tape landing ({', '.join(tape_lands)}) during "
            "parylene deposition: parylene is conformal, and a coated tape "
            "land is an open circuit to the ITO top plate."
        )
    return "\n".join(lines) + "\n"


def solderpaste_gerber(model: dict[str, Any], side: str) -> str:
    """Top/bottom solder paste — the **stencil**, not the board.

    Two things make this different from the soldermask, and both are the
    kind of detail that produces a plausible-looking file that assembles
    wrong:

    - **Through-hole pads get no aperture.** Paste printed over a plated
      hole falls through it; THT parts are hand-soldered, wave-soldered or
      paste-in-hole-dispensed, never stencil-printed from this layer. A pad
      carrying a ``drill`` is skipped.
    - **Vias get no aperture either** — same reason, and they are tented
      anyway (see :func:`soldermask_gerber`).
    - **A pad carrying ``paste: "none"``** (pcb-ewod-multitile Slice 1) is
      likewise skipped — an EWOD electrode is bare ENIG under a
      parylene/oil dielectric, never stencil-printed; no per-shape special
      case, the same explicit intent as ``mask``.

    Apertures are 1:1 with the pad. Real stencil houses shrink fine-pitch
    apertures (typically 5-15% area reduction) to control solder volume,
    but the correct reduction is a function of pad pitch, stencil
    thickness and aperture aspect ratio — none of which this model
    carries. A single global shrink constant would be a number tuned to
    nothing, which is the defect this subsystem keeps producing; 1:1 is
    the honest default and the reduction belongs with the stencil order.
    """
    layers: list[str] = model["layers"]
    layer = layers[0] if side == "top" else layers[-1]
    apertures = _ApertureTable()
    body: list[str] = []
    for pad in model.get("pads", []):
        if pad.get("layer") != layer:
            continue
        if pad.get("drill"):
            continue
        if pad.get("paste") == "none":
            continue
        _emit_pad(pad, apertures, body)
    return _assemble(
        f"SolderPaste,{'Top' if side == 'top' else 'Bot'}", apertures, body
    )


def silkscreen_gerber(model: dict[str, Any], side: str) -> str:
    """A silk draw is normally a stroke (see the module docstring's
    ``<draw>`` shape). Two extra shapes are recognised here, both reused
    verbatim from the copper-pour machinery above rather than inventing a
    silk-specific one:

    - ``{"shape": "region", "polygon": [...]}`` — a solid ``G36``/``G37``
      fill (:func:`_emit_region`, the same writer a copper pour uses;
      regions are legal on any layer, not just copper). Used for a filled
      silk patch, e.g. :func:`precis.pcb.silk.build_sn_patch`'s S/N label
      box.
    - a normal stroke draw carrying ``"polarity": "clear"`` — wrapped in
      ``%LPC*%``/``%LPD*%`` so it ERASES whatever solid silk came before
      it instead of adding to it, the exact idiom :func:`_emit_region`
      already uses for a pour's antipad holes (this is that same
      dark-then-clear-then-back-to-dark sequence, just spelled with a
      stroke instead of a hole ring — a knocked-out letter is a hole
      shaped like a glyph, not a filled polygon). A draw with no
      ``"polarity"`` key (or any value other than ``"clear"``) draws dark,
      unchanged from before this shape existed.

    A draw carrying a ``refdes`` is tagged ``%TO.C,<refdes>*%`` (cleared
    by the usual matching ``%TD*%``) — a part's courtyard ring, pin-1
    mark and refdes label are all its objects, and this is the only
    labelling a pad-less part (mounting hole, fiducial, logo) gets.
    """
    apertures = _ApertureTable()
    body: list[str] = []
    for draw in model.get("silkscreen", {}).get(side, []):
        refdes = str(draw.get("refdes") or "") or None

        def _emit(draw: dict[str, Any] = draw) -> None:
            if draw.get("shape") == "region":
                _emit_region({"polygon": draw["polygon"]}, body)
            elif draw.get("polarity") == "clear":
                body.append("%LPC*%")
                _emit_stroke(draw, apertures, body)
                body.append("%LPD*%")
            else:
                _emit_stroke(draw, apertures, body)

        _emit_with_attrs(body, _emit, refdes=refdes)
    return _assemble(f"Legend,{'Top' if side == 'top' else 'Bot'}", apertures, body)


def outline_gerber(model: dict[str, Any]) -> str:
    """Board edge — ``Profile,NP`` (non-plated), the X2 attribute a fab reads
    to find the outline without filename guessing."""
    apertures = _ApertureTable()
    body: list[str] = []
    outline = model.get("outline")
    if outline:
        _emit_stroke(_outline_draw(outline), apertures, body)
    return _assemble("Profile,NP", apertures, body)


# ─────────────────────────────────────────────────────────────────────
# non-copper fab layers (gr520329): coverlay, stiffener, adhesive,
# tape-mask, doc notes, bend zones
# ─────────────────────────────────────────────────────────────────────
#
# The model keys :func:`precis.pcb.session.fab_layers_from_features`
# fills from the board's ``features[]`` — each is a list of flat dicts::
#
#   coverlay_openings: [{"side": "top"|"bottom", "polygon": [[x,y],…]}]
#   stiffeners:        [{"side", "material", "thickness_mm", "polygon"}]
#   adhesives:         [{"side", "polygon"}]
#   tape_masks:        [{"kind", "polygon"}]      (Dwgs.User regions)
#   doc_notes:         [{"text", "x", "y", "layer"}]  (stroked text)
#   bend_zones:        [{"line": [[x,y],[x,y]], "radius_mm", "angle_deg"}]
#
# Filename convention is KiCad/JLC-style (``-F_Coverlay.gbr`` …); the X2
# ``FileFunction`` is the spec's own vocabulary where it has one
# (``Glue`` for the adhesive film), ``Other,<layer>`` on the files a
# spec value does not name — coverlay and stiffener included, the
# vocabulary has neither.


def coverlay_gerber(model: dict[str, Any], side: str) -> str:
    """One coverlay file: each authored opening drawn as a ``G36``/``G37``
    region — the film cut the fab makes, imaged directly."""
    apertures = _ApertureTable()
    body: list[str] = []
    for opening in model.get("coverlay_openings") or []:
        if opening.get("side") != side:
            continue
        poly = [(float(p[0]), float(p[1])) for p in opening.get("polygon") or []]
        if len(poly) >= 3:
            _emit_region_ring(poly, body)
    return _assemble(
        f"Other,Coverlay_{'Top' if side == 'top' else 'Bot'}", apertures, body
    )


def stiffener_gerber(model: dict[str, Any], side: str) -> str:
    """One stiffener file: the bonded stiffener's footprint as a region,
    its material/thickness carried as a ``G04`` line (the attribute
    vocabulary has no material field — a human-readable comment is the
    honest carrier)."""
    apertures = _ApertureTable()
    body: list[str] = []
    for st in model.get("stiffeners") or []:
        if st.get("side") != side:
            continue
        poly = [(float(p[0]), float(p[1])) for p in st.get("polygon") or []]
        if len(poly) < 3:
            continue
        body.append(
            f"G04 stiffener {st.get('material') or '?'} "
            f"{float(st.get('thickness_mm') or 0.0):g}mm*"
        )
        _emit_region_ring(poly, body)
    return _assemble(
        f"Other,Stiffener_{'Top' if side == 'top' else 'Bot'}", apertures, body
    )


def adhesive_gerber(model: dict[str, Any], side: str) -> str:
    """One adhesive-film file — the bondply/PSA sheet's outline as a
    region. ``Glue`` is the X2 file function for an adhesive layer."""
    apertures = _ApertureTable()
    body: list[str] = []
    for ad in model.get("adhesives") or []:
        if ad.get("side") != side:
            continue
        poly = [(float(p[0]), float(p[1])) for p in ad.get("polygon") or []]
        if len(poly) >= 3:
            _emit_region_ring(poly, body)
    return _assemble(f"Glue,{'Top' if side == 'top' else 'Bot'}", apertures, body)


#: Cap height of drawings-layer text — a doc note or the bend-zone label.
DWGS_TEXT_HEIGHT_MM = 1.5


def _text_draw(text: str, x: float, y: float) -> dict[str, Any] | None:
    """``text`` stroked at ``(x, y)`` (baseline-left, 0°) as one
    ``{"width_mm", "segments"}`` draw — :mod:`precis.pcb.stroke_font`
    glyph strokes, the same writer silk labels use."""
    from precis.pcb import stroke_font

    strokes = stroke_font.layout_text(
        text, anchor=(x, y), height_mm=DWGS_TEXT_HEIGHT_MM
    )
    segments: list[dict[str, Any]] = []
    for pts in strokes:
        segments += [
            {"shape": "line", "start": list(pts[i]), "end": list(pts[i + 1])}
            for i in range(len(pts) - 1)
        ]
    if not segments:
        return None
    return {"width_mm": 0.15, "segments": segments}


def _bend_zone_draws(zone: dict[str, Any]) -> list[dict[str, Any]]:
    """A bend zone's Dwgs.User drawing: the fold line stroked, plus a
    ``R=<r> A=<deg>`` note sat just past the zone's top edge at the
    fold's midpoint."""
    try:
        (x1, y1), (x2, y2) = [
            (float(p[0]), float(p[1])) for p in (zone.get("line") or ())
        ]
        radius = float(zone["radius_mm"])
        angle = float(zone["angle_deg"])
    except (TypeError, ValueError, KeyError):
        return []
    if math.hypot(x2 - x1, y2 - y1) <= 0:
        return []
    draws = [
        {
            "width_mm": 0.15,
            "segments": [{"shape": "line", "start": [x1, y1], "end": [x2, y2]}],
        }
    ]
    half = radius * math.radians(abs(angle)) / 2.0
    mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    nx, ny = -(y2 - y1), (x2 - x1)  # the fold's normal
    n = math.hypot(nx, ny)
    note = _text_draw(
        f"BEND R={radius:g}MM A={angle:g}DEG",
        mx + nx / n * (half + 0.5),
        my + ny / n * (half + 0.5),
    )
    if note is not None:
        draws.append(note)
    return draws


def drawings_gerber(model: dict[str, Any], layer: str) -> str:
    """One user-drawings file (``Dwgs.User`` or another ``layer`` name):
    ``doc_note`` text stroked at its anchor, plus — on ``Dwgs.User``
    itself — tape-mask regions and bend-zone fold lines with their
    radius/angle note."""
    apertures = _ApertureTable()
    body: list[str] = []
    for note in model.get("doc_notes") or []:
        if str(note.get("layer") or "Dwgs.User") != layer:
            continue
        draw = _text_draw(
            str(note.get("text") or ""),
            float(note.get("x") or 0.0),
            float(note.get("y") or 0.0),
        )
        if draw is not None:
            _emit_stroke(draw, apertures, body)
    if layer == "Dwgs.User":
        for mask in model.get("tape_masks") or []:
            poly = [(float(p[0]), float(p[1])) for p in mask.get("polygon") or []]
            if len(poly) < 3:
                continue
            body.append(f"G04 tape mask ({mask.get('kind') or 'parylene'})*")
            _emit_region_ring(poly, body)
        for zone in model.get("bend_zones") or []:
            for draw in _bend_zone_draws(zone):
                _emit_stroke(draw, apertures, body)
    return _assemble(f"Other,{layer}", apertures, body)


def _drawings_layer_names(model: dict[str, Any]) -> list[str]:
    """The drawing layers that carry content — ``Dwgs.User`` plus any
    layer a ``doc_note`` names, in first-use order."""
    names: list[str] = []
    for note in model.get("doc_notes") or []:
        layer = str(note.get("layer") or "Dwgs.User")
        if layer not in names:
            names.append(layer)
    if (model.get("tape_masks") or model.get("bend_zones")) and (
        "Dwgs.User" not in names
    ):
        names.insert(0, "Dwgs.User")
    return names


def export_gerbers(model: dict[str, Any], *, name: str = "design") -> dict[str, str]:
    """One file per copper layer + top/bottom soldermask + top/bottom solder
    paste + top/bottom silkscreen + the board outline + each authored
    non-copper fab layer (coverlay openings, stiffeners, adhesives, the
    user drawings). See the module docstring for the model shape."""
    layers: list[str] = model["layers"]
    files = {
        f"{name}-{_layer_token(layer)}.gbr": copper_gerber(model, layer)
        for layer in layers
    }
    files[f"{name}-F_Mask.gbr"] = soldermask_gerber(model, "top")
    files[f"{name}-B_Mask.gbr"] = soldermask_gerber(model, "bottom")
    files[f"{name}-F_Paste.gbr"] = solderpaste_gerber(model, "top")
    files[f"{name}-B_Paste.gbr"] = solderpaste_gerber(model, "bottom")
    files[f"{name}-F_Silkscreen.gbr"] = silkscreen_gerber(model, "top")
    files[f"{name}-B_Silkscreen.gbr"] = silkscreen_gerber(model, "bottom")
    files[f"{name}-Edge_Cuts.gbr"] = outline_gerber(model)
    # Each fab-layer file exists only when the model authors content for
    # it — a rigid board's bundle gains no stray files.
    for side in ("top", "bottom"):
        tok = "F" if side == "top" else "B"
        if any(e.get("side") == side for e in model.get("coverlay_openings") or []):
            files[f"{name}-{tok}_Coverlay.gbr"] = coverlay_gerber(model, side)
        if any(e.get("side") == side for e in model.get("stiffeners") or []):
            files[f"{name}-{tok}_Stiffener.gbr"] = stiffener_gerber(model, side)
        if any(e.get("side") == side for e in model.get("adhesives") or []):
            files[f"{name}-{tok}_Adhesive.gbr"] = adhesive_gerber(model, side)
    for layer in _drawings_layer_names(model):
        files[f"{name}-{_layer_token(layer)}.gbr"] = drawings_gerber(model, layer)
    return files


# ─────────────────────────────────────────────────────────────────────
# Excellon drill
# ─────────────────────────────────────────────────────────────────────
def _excellon_file(holes: list[dict[str, Any]]) -> str:
    """Tool table (one ``Tnn`` per distinct diameter, ascending) + a
    ``METRIC`` coordinate body — decimal-point coordinates, so no LZ/TZ
    zero-suppression ambiguity to get wrong."""
    sizes = sorted({round(float(h["dia_mm"]), 4) for h in holes})
    tool_of = {s: i + 1 for i, s in enumerate(sizes)}
    lines = ["M48", "METRIC"]
    lines += [f"T{tool_of[s]:02d}C{s:.4f}" for s in sizes]
    lines += ["%", "G90", "G05"]
    cur_tool: int | None = None
    for h in holes:
        t = tool_of[round(float(h["dia_mm"]), 4)]
        if t != cur_tool:
            lines.append(f"T{t:02d}")
            cur_tool = t
        if h.get("slot"):
            # Routed slot: the standard Excellon canned slot, tool = width.
            (x1, y1), (x2, y2) = h["slot"]
            lines.append(
                f"X{float(x1):.4f}Y{float(y1):.4f}G85X{float(x2):.4f}Y{float(y2):.4f}"
            )
            continue
        lines.append(f"X{float(h['x']):.4f}Y{float(h['y']):.4f}")
    lines.append("M30")
    return "\n".join(lines) + "\n"


def excellon_files(model: dict[str, Any], *, name: str = "design") -> dict[str, str]:
    """Plated (``-PTH.drl``) and non-plated (``-NPTH.drl``) drill files, kept
    separate per fab convention — a fab drills+plates them on different
    passes. Vias are always plated; ``model['drills']`` entries default
    ``plated=True`` (a THT component pad) unless marked otherwise (a
    mechanical/mounting hole)."""
    plated = [
        {"x": item["x"], "y": item["y"], "dia_mm": item["drill_mm"]}
        for item in model.get("copper", [])
        if item.get("ctype") == "via"
    ]
    non_plated: list[dict[str, Any]] = []
    for d in model.get("drills", []):
        (plated if d.get("plated", True) else non_plated).append(d)
    out = {}
    if plated:
        out[f"{name}-PTH.drl"] = _excellon_file(plated)
    if non_plated:
        out[f"{name}-NPTH.drl"] = _excellon_file(non_plated)
    return out


# ─────────────────────────────────────────────────────────────────────
# bundle
# ─────────────────────────────────────────────────────────────────────
class SynthesizedPadError(RuntimeError):
    """A pad in the model is a synthesized BOUND, not a real footprint."""


def synthesized_pad_reasons(model: dict[str, Any]) -> list[str]:
    """One line per part carrying a synthesized pad — which FOOTPRINT
    explains it and which fix closes it (gr536876).

    The refusal used to name only the affected nets, which read as a
    board defect; the real defect is on the footprint side. A catalog
    pad carries ``part_lcsc`` (its ``part_footprints`` key): absent a
    ``pin_unmatched`` flag the row is simply uncached — ``op='footprint'``
    pulls or authors it. ``pin_unmatched`` means the row IS cached but
    its ``pin_map`` names none of the design's declared pins for it (a
    row authored off a pin_map-less export stores ``pin_map=NULL`` —
    the taz-leaf-f failure): re-authoring the row *with* ``pin_map`` is
    the fix, not a re-fetch and not a board change. A pad with neither
    is design-local copper the landpattern sized by construction.
    Shared by :func:`export_fab` and
    :func:`precis.pcb.epro_write.epro_files` so both refusals name the
    same cause."""
    by_refdes: dict[str, dict[str, Any]] = {}
    for pad in model.get("pads") or []:
        if not pad.get("synthesized"):
            continue
        refdes = str(pad.get("refdes") or "?")
        info = by_refdes.setdefault(refdes, {"lcsc": "", "unmatched": set()})
        if pad.get("part_lcsc"):
            info["lcsc"] = str(pad["part_lcsc"])
        if pad.get("pin_unmatched"):
            info["unmatched"].add(str(pad.get("pin") or "?"))
    lines: list[str] = []
    for refdes in sorted(by_refdes):
        info = by_refdes[refdes]
        lcsc = info["lcsc"]
        head = f"{lcsc} ({refdes})" if lcsc else refdes
        if info["unmatched"]:
            names = sorted(info["unmatched"])
            shown = ", ".join(names[:5]) + (", …" if len(names) > 5 else "")
            lines.append(
                f"{head}: footprint cached but its pin_map joins none of "
                f"{len(names)} declared pin(s) ({shown}) — re-author the "
                "row with pin_map via op='footprint' args.footprint"
            )
        elif lcsc:
            lines.append(
                f"{head}: no cached footprint — op='footprint' pulls or authors it"
            )
        else:
            lines.append(f"{refdes}: design-local pad — synthesized by construction")
    return lines


#: The closing line of either synthesized-pad refusal: the op that fixes
#: the rows :func:`synthesized_pad_reasons` named, then the preview
#: escape.
SYNTHESIZED_PAD_FIX = (
    "Fix: put(kind='pcb', args={'op':'footprint','part':'<lcsc>'}) pulls or "
    "authors the real footprint (args.footprint takes a {pads, pin_map} "
    "row); or pass allow_synthesized=True for a preview-only export."
)


def export_fab(
    model: dict[str, Any], *, name: str = "design", allow_synthesized: bool = False
) -> dict[str, str]:
    """Gerbers + Excellon in one dict — the full JLCPCB-uploadable fab set.

    **Refuses a model containing synthesized pads.**
    :mod:`precis.pcb.landpattern` synthesizes plausible pad offsets when no
    real footprint is cached, and says in its own docstring that those are
    bounds which "must never be exported to fabrication". They are in the
    model because DRC and connectivity need pad geometry to mean anything;
    this is the boundary where that stops being enough. A board built from
    a dimensionally-plausible guess at a part solders to nothing, and the
    failure is discovered by a human holding the assembled board — so it
    fails here instead, loudly. ``allow_synthesized=True`` for a caller
    that genuinely wants the preview (a viewer, a test) rather than a
    board.
    """
    if not allow_synthesized:
        bad = sorted(
            {
                str(p.get("net", "?"))
                for p in model.get("pads", [])
                if p.get("synthesized")
            }
        )
        if bad:
            raise SynthesizedPadError(
                f"{len(bad)} net(s) carry synthesized pad geometry "
                f"({', '.join(bad[:5])}{'...' if len(bad) > 5 else ''}) — these "
                "are land-pattern BOUNDS, not the real footprint, and must not "
                "be fabricated:"
                + "".join(f"\n  {line}" for line in synthesized_pad_reasons(model))
                + f"\n{SYNTHESIZED_PAD_FIX}"
            )
    out = export_gerbers(model, name=name)
    out.update(excellon_files(model, name=name))
    notes = fab_notes_txt(model)
    if notes is not None:
        out[f"{name}-fab-notes.txt"] = notes
    return out


def zip_fab(files: dict[str, str]) -> bytes:
    """Zip a ``{filename: content}`` fab set in memory — the one place this
    module touches bytes; still no disk/network I/O."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname, content in sorted(files.items()):
            zf.writestr(fname, content)
    return buf.getvalue()


__all__ = [
    "DEFAULT_SILK_WIDTH_MM",
    "DWGS_TEXT_HEIGHT_MM",
    "EDGE_CUT_WIDTH_MM",
    "SOLDERMASK_EXPANSION_MM",
    "SYNTHESIZED_PAD_FIX",
    "SynthesizedPadError",
    "adhesive_gerber",
    "copper_gerber",
    "coverlay_gerber",
    "drawings_gerber",
    "excellon_files",
    "export_fab",
    "export_gerbers",
    "fab_notes_txt",
    "outline_gerber",
    "silkscreen_gerber",
    "soldermask_gerber",
    "solderpaste_gerber",
    "stiffener_gerber",
    "synthesized_pad_reasons",
    "zip_fab",
]
