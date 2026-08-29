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
        {"layer": "F.Cu", "net": "3V3", "shape": "circle"|"rect"|"obround",
         "x": .., "y": .., "w": .., "h": ..},   # h ignored for "circle"
      ],
      "silkscreen": {"top": [<draw>, ...], "bottom": [<draw>, ...]},  # <draw>
      # is the same {"width_mm", "segments"} shape as a track, layer-less.
    }

Everything here is a pure function of that dict → ``{filename: content}`` —
zero I/O, trivially unit-testable. :func:`zip_fab` is the one thin
convenience that touches bytes (an in-memory zip), still no disk/network.
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

# RS-274X format spec: 4 integer digits, 6 decimal digits, leading zeros
# omitted, absolute coordinates — %FSLAX46Y46*%. Every coordinate is an
# integer count of this unit (mm / 10**_MM_DECIMALS).
_MM_DECIMALS = 6
_UNITS_PER_MM = 10**_MM_DECIMALS
_FS = "%FSLAX46Y46*%"

# JLC-typical soldermask swell per side over the pad outline; not vendor-
# tunable per order in this writer, just a sane default (see capabilities.py
# for the numbers that ARE meant to be looked up/verified).
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


# ─────────────────────────────────────────────────────────────────────
# draws: strokes (tracks/silk) and flashes (pads/via barrels)
# ─────────────────────────────────────────────────────────────────────
def _emit_flash(
    pad: dict[str, Any], apertures: _ApertureTable, body: list[str]
) -> None:
    aid = _aperture_for_pad(pad, apertures)
    body.append(f"D{aid}*")
    body.append(f"{_coord(float(pad['x']), float(pad['y']))}D03*")


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


def _emit_region(pour: dict[str, Any], body: list[str]) -> None:
    """``G36``/``G37`` polygon region for a copper pour/plane — filled by the
    boundary trace, no aperture needed (a region's fill isn't a stroke).

    Holes are emitted as clear-polarity (``%LPC*%``) regions after the
    solid one, which is the standard RS-274X idiom for a plane's antipads.
    A pour that carries ``holes`` and images them as solid copper would
    short every foreign via passing through the plane — so the hole list is
    not optional decoration, it is the difference between a plane and a
    short.
    """
    poly = [(float(p[0]), float(p[1])) for p in pour["polygon"]]
    if not poly:
        return
    _emit_region_ring(poly, body)
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
        if ctype == "track" and item.get("layer") == layer:
            _emit_stroke(item, apertures, body)
        elif ctype == "pour" and item.get("layer") == layer:
            _emit_region(item, body)
        elif ctype == "via" and layer in _via_layers(item, layers):
            pad = {
                "shape": "circle",
                "x": item["x"],
                "y": item["y"],
                "w": item["dia_mm"],
            }
            _emit_flash(pad, apertures, body)
    for pad in model.get("pads", []):
        if pad.get("layer") == layer:
            _emit_flash(pad, apertures, body)
    return _assemble(f"Copper,L{index + 1},{pos}", apertures, body)


def soldermask_gerber(model: dict[str, Any], side: str) -> str:
    """Top/bottom soldermask openings — pads on the corresponding outer
    copper layer, each expanded by :data:`SOLDERMASK_EXPANSION_MM`. Vias are
    assumed tented (no opening) — the common default; a tented-via override
    is future work, not needed for slice 4."""
    layers: list[str] = model["layers"]
    layer = layers[0] if side == "top" else layers[-1]
    apertures = _ApertureTable()
    body: list[str] = []
    for pad in model.get("pads", []):
        if pad.get("layer") == layer:
            _emit_flash(_expand_pad(pad, SOLDERMASK_EXPANSION_MM), apertures, body)
    return _assemble(f"Soldermask,{'Top' if side == 'top' else 'Bot'}", apertures, body)


def silkscreen_gerber(model: dict[str, Any], side: str) -> str:
    apertures = _ApertureTable()
    body: list[str] = []
    for draw in model.get("silkscreen", {}).get(side, []):
        _emit_stroke(draw, apertures, body)
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


def export_gerbers(model: dict[str, Any], *, name: str = "design") -> dict[str, str]:
    """One file per copper layer + top/bottom soldermask + top/bottom
    silkscreen + the board outline. See the module docstring for the model
    shape."""
    layers: list[str] = model["layers"]
    files = {
        f"{name}-{_layer_token(layer)}.gbr": copper_gerber(model, layer)
        for layer in layers
    }
    files[f"{name}-F_Mask.gbr"] = soldermask_gerber(model, "top")
    files[f"{name}-B_Mask.gbr"] = soldermask_gerber(model, "bottom")
    files[f"{name}-F_Silkscreen.gbr"] = silkscreen_gerber(model, "top")
    files[f"{name}-B_Silkscreen.gbr"] = silkscreen_gerber(model, "bottom")
    files[f"{name}-Edge_Cuts.gbr"] = outline_gerber(model)
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
                "be fabricated. Cache real footprints, or pass "
                "allow_synthesized=True for a preview-only export."
            )
    out = export_gerbers(model, name=name)
    out.update(excellon_files(model, name=name))
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
    "EDGE_CUT_WIDTH_MM",
    "SOLDERMASK_EXPANSION_MM",
    "SynthesizedPadError",
    "copper_gerber",
    "excellon_files",
    "export_fab",
    "export_gerbers",
    "outline_gerber",
    "silkscreen_gerber",
    "soldermask_gerber",
    "zip_fab",
]
