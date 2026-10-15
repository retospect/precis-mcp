"""Cricut SVG exporter for a :class:`~precis.sheet.model.SheetJob`.

Design notes are in the package docstring (:mod:`precis.sheet`). A blade
plotter does only ``cut`` and ``score``, so every other op
(``engrave_vector``, ``engrave_raster``, ``drill``) is refused by layer
name — same refusal shape as the laser writer's.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from precis.sheet.model import (
    Export,
    SheetExportError,
    SheetJob,
    comment_safe,
    fmt_mm,
)
from precis.sheet.svg_laser import HAIRLINE_MM, PALETTE, _attr, _shape

MACHINE = "cricut"

#: Per-op fallback when a layer carries no ``cricut`` settings. Starting
#: points for 3 mm corrugated cardboard on a Maker-class machine — the
#: knife blade for the through-cut, the scoring stylus for a fold line —
#: not a material record; the notes say when they were used.
CRICUT_DEFAULTS: Mapping[str, Mapping[str, Any]] = {
    "cut": {"pressure": 350, "blade": "knife"},
    "score": {"pressure": 40, "blade": "scoring-stylus"},
}

SUPPORTED_OPS: frozenset[str] = frozenset(CRICUT_DEFAULTS)


def layer_settings(job: SheetJob) -> list[tuple[str, str, Mapping[str, Any], bool]]:
    """``(layer name, colour, settings, used_defaults)`` per layer, in job
    order. Raises :class:`SheetExportError` on an op the Cricut cannot do."""
    out: list[tuple[str, str, Mapping[str, Any], bool]] = []
    for i, layer in enumerate(job.layers):
        if layer.op not in SUPPORTED_OPS:
            raise SheetExportError(
                f"layer {layer.name!r}: op {layer.op!r} is not supported by the "
                f"cricut SVG exporter (supported: {', '.join(sorted(SUPPORTED_OPS))})"
            )
        own = layer.settings.get(MACHINE)
        settings = dict(CRICUT_DEFAULTS[layer.op])
        if own:
            settings.update(own)
        out.append((layer.name, PALETTE[i % len(PALETTE)], settings, not own))
    return out


def export_cricut_svg(job: SheetJob) -> Export:
    """The job as a Design Space SVG (mm, y flipped to SVG's y-down)."""
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
        "colour -> settings (set the operation on each imported layer in "
        "Design Space; the file carries them only as data- attributes):",
    ]
    for name, colour, settings, defaults in table:
        op = job.layer(name).op
        line = (
            f"  {colour} {name} op={op} pressure {settings['pressure']} "
            f"blade {settings['blade']}"
        )
        if defaults:
            line += " (cricut defaults: the layer carries no cricut settings)"
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
            f'data-pressure="{settings["pressure"]}" '
            f'data-blade="{_attr(str(settings["blade"]))}">'
        )
        for shape in job.shapes_on(name):
            lines.append(_shape(shape, h))
        lines.append("</g>")
    lines.append("</svg>")
    return Export("\n".join(lines) + "\n", tuple(notes))
