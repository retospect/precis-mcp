"""Laser SVG exporter for a :class:`~precis.sheet.model.SheetJob`.

Design notes are in the package docstring (:mod:`precis.sheet`). Refuses
``engrave_raster`` by layer name (reserved until the grayscale build).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from precis.sheet.model import (
    Circle,
    Export,
    Polyline,
    SheetExportError,
    SheetJob,
    comment_safe,
    fmt_mm,
)

MACHINE = "laser"

#: LightBurn's cut palette, layers 00…15, in order. Layer *i* of the job takes
#: colour *i*; LightBurn maps colour → its own layer on import.
PALETTE: tuple[str, ...] = (
    "#000000",
    "#0000FF",
    "#FF0000",
    "#00E000",
    "#D0D000",
    "#FF8000",
    "#00E0E0",
    "#FF00FF",
    "#B4B4B4",
    "#0000A0",
    "#A00000",
    "#00A000",
    "#A0A000",
    "#C08000",
    "#00A0FF",
    "#A000A0",
)

#: Per-op fallback when a layer carries no ``laser`` settings. Starting
#: points for 3 mm corrugated cardboard on a diode laser, not a material
#: record — the notes say when they were used.
LASER_DEFAULTS: Mapping[str, Mapping[str, Any]] = {
    "cut": {"power_pct": 100, "speed_mm_s": 10, "passes": 1},
    "drill": {"power_pct": 100, "speed_mm_s": 10, "passes": 1},
    "score": {"power_pct": 20, "speed_mm_s": 100, "passes": 1},
    "engrave_vector": {"power_pct": 15, "speed_mm_s": 200, "passes": 1},
}

SUPPORTED_OPS: frozenset[str] = frozenset(LASER_DEFAULTS)

#: Hairline in mm; LightBurn ignores stroke width, viewers need a non-zero one.
HAIRLINE_MM = 0.01


def layer_settings(job: SheetJob) -> list[tuple[str, str, Mapping[str, Any], bool]]:
    """``(layer name, colour, settings, used_defaults)`` per layer, in job
    order. Raises :class:`SheetExportError` on an op the laser cannot do."""
    out: list[tuple[str, str, Mapping[str, Any], bool]] = []
    for i, layer in enumerate(job.layers):
        if layer.op not in SUPPORTED_OPS:
            raise SheetExportError(
                f"layer {layer.name!r}: op {layer.op!r} is not supported by the "
                f"laser SVG exporter (supported: {', '.join(sorted(SUPPORTED_OPS))})"
            )
        own = layer.settings.get(MACHINE)
        settings = dict(LASER_DEFAULTS[layer.op])
        if own:
            settings.update(own)
        out.append((layer.name, PALETTE[i % len(PALETTE)], settings, not own))
    return out


def export_laser_svg(job: SheetJob) -> Export:
    """The job as a LightBurn-ready SVG (mm, y flipped to SVG's y-down)."""
    job.validate()
    table = layer_settings(job)
    w, h = job.sheet.width_mm, job.sheet.height_mm
    notes = [
        "units: mm; x right, y down (sheet frame flipped)",
        f"sheet: {fmt_mm(w)} x {fmt_mm(h)} mm"
        + (f", {job.sheet.material}" if job.sheet.material else "")
        + (
            f" {fmt_mm(job.sheet.thickness_mm)} mm"
            if job.sheet.thickness_mm is not None
            else ""
        ),
        f"parts: {len(job.parts)}"
        + (": " + ", ".join(p.id for p in job.parts) if job.parts else ""),
        "colour -> settings (set these up in LightBurn; the file carries them "
        "only as data- attributes):",
    ]
    for name, colour, settings, defaults in table:
        op = job.layer(name).op
        line = (
            f"  {colour} {name} op={op} power {settings['power_pct']} % "
            f"speed {settings['speed_mm_s']} mm/s passes {settings['passes']}"
        )
        if defaults:
            line += " (laser defaults: the layer carries no laser settings)"
        notes.append(line)
    notes.extend(job.notes)

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<!--",
        *(" " + comment_safe(n) for n in notes),
        "-->",
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{fmt_mm(w)}mm" '
        f'height="{fmt_mm(h)}mm" viewBox="0 0 {fmt_mm(w)} {fmt_mm(h)}">',
    ]
    for name, colour, settings, _defaults in table:
        op = job.layer(name).op
        lines.append(
            f'<g id="{_attr(name)}" data-op="{op}" stroke="{colour}" fill="none" '
            f'stroke-width="{fmt_mm(HAIRLINE_MM)}" '
            f'data-power-pct="{settings["power_pct"]}" '
            f'data-speed-mm-s="{settings["speed_mm_s"]}" '
            f'data-passes="{settings["passes"]}">'
        )
        for shape in job.shapes_on(name):
            lines.append(_shape(shape, h))
        lines.append("</g>")
    lines.append("</svg>")
    return Export("\n".join(lines) + "\n", tuple(notes))


def _attr(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _part_attrs(shape: Polyline | Circle) -> str:
    if shape.part is None:
        return ""
    role = "inner" if shape.inner else "outer"
    return f' data-part="{_attr(shape.part)}" data-role="{role}"'


def _shape(shape: Polyline | Circle, sheet_h: float) -> str:
    if isinstance(shape, Circle):
        return (
            f'<circle cx="{fmt_mm(shape.cx)}" cy="{fmt_mm(sheet_h - shape.cy)}" '
            f'r="{fmt_mm(shape.r)}"{_part_attrs(shape)}/>'
        )
    cmds = []
    for i, (x, y) in enumerate(shape.points):
        cmds.append(f"{'M' if i == 0 else 'L'} {fmt_mm(x)} {fmt_mm(sheet_h - y)}")
    if shape.closed:
        cmds.append("Z")
    return f'<path d="{" ".join(cmds)}"{_part_attrs(shape)}/>'
