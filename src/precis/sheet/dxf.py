"""ASCII DXF R12 exporter for a :class:`~precis.sheet.model.SheetJob`.

Hand-written: ``POLYLINE``/``VERTEX``/``SEQEND`` for polylines, ``CIRCLE``
for circles (so a ``drill`` layer's holes come back as circles), one DXF
layer per vector layer of the job, same names. R12 has no units header; mm
is stated in the leading group-999 comments. Refuses ``engrave_raster`` by
layer name. Design notes: the package docstring (:mod:`precis.sheet`).
"""

from __future__ import annotations

from precis.sheet.model import (
    Circle,
    Export,
    Polyline,
    SheetExportError,
    SheetJob,
    fmt_mm,
)

#: Vector ops the DXF carries. Raster has no DXF representation.
SUPPORTED_OPS: frozenset[str] = frozenset({"cut", "score", "engrave_vector", "drill"})

#: ACI colour per layer index, so a viewer separates the layers too.
_ACI: tuple[int, ...] = (7, 5, 1, 3, 2, 30, 4, 6)


def _pairs(*items: tuple[int, str]) -> list[str]:
    out: list[str] = []
    for code, value in items:
        out.append(str(code))
        out.append(value)
    return out


def export_dxf(job: SheetJob) -> Export:
    """The job as DXF R12 text. Coordinates are the job's (mm, y up)."""
    job.validate()
    for layer in job.layers:
        if layer.op not in SUPPORTED_OPS:
            raise SheetExportError(
                f"layer {layer.name!r}: op {layer.op!r} is not supported by the "
                f"DXF exporter (supported: {', '.join(sorted(SUPPORTED_OPS))})"
            )
    notes = [
        "units: mm (DXF R12 carries no units header; mm by convention)",
        f"sheet: {fmt_mm(job.sheet.width_mm)} x {fmt_mm(job.sheet.height_mm)} mm"
        + (f", {job.sheet.material}" if job.sheet.material else "")
        + (
            f" {fmt_mm(job.sheet.thickness_mm)} mm"
            if job.sheet.thickness_mm is not None
            else ""
        ),
        f"parts: {len(job.parts)}"
        + (": " + ", ".join(p.id for p in job.parts) if job.parts else ""),
        "layers (cut order): "
        + ", ".join(f"{layer.name} op={layer.op}" for layer in job.layers),
        *job.notes,
    ]
    lines: list[str] = []
    for note in notes:
        lines += _pairs((999, note.replace("\n", " ")))
    lines += _pairs(
        (0, "SECTION"),
        (2, "HEADER"),
        (9, "$ACADVER"),
        (1, "AC1009"),
        (0, "ENDSEC"),
        (0, "SECTION"),
        (2, "TABLES"),
        (0, "TABLE"),
        (2, "LTYPE"),
        (70, "1"),
        (0, "LTYPE"),
        (2, "CONTINUOUS"),
        (70, "0"),
        (3, "Solid line"),
        (72, "65"),
        (73, "0"),
        (40, "0.0"),
        (0, "ENDTAB"),
        (0, "TABLE"),
        (2, "LAYER"),
        (70, str(len(job.layers))),
    )
    for i, layer in enumerate(job.layers):
        lines += _pairs(
            (0, "LAYER"),
            (2, layer.name),
            (70, "0"),
            (62, str(_ACI[i % len(_ACI)])),
            (6, "CONTINUOUS"),
        )
    lines += _pairs((0, "ENDTAB"), (0, "ENDSEC"), (0, "SECTION"), (2, "ENTITIES"))
    for layer in job.layers:
        for shape in job.shapes_on(layer.name):
            lines += _entity(shape, layer.name)
    lines += _pairs((0, "ENDSEC"), (0, "EOF"))
    return Export("\n".join(lines) + "\n", tuple(notes))


def _entity(shape: Polyline | Circle, layer: str) -> list[str]:
    if isinstance(shape, Circle):
        return _pairs(
            (0, "CIRCLE"),
            (8, layer),
            (10, fmt_mm(shape.cx, 6)),
            (20, fmt_mm(shape.cy, 6)),
            (30, "0"),
            (40, fmt_mm(shape.r, 6)),
        )
    out = _pairs(
        (0, "POLYLINE"),
        (8, layer),
        (66, "1"),
        (70, "1" if shape.closed else "0"),
        (10, "0"),
        (20, "0"),
        (30, "0"),
    )
    for x, y in shape.points:
        out += _pairs(
            (0, "VERTEX"),
            (8, layer),
            (10, fmt_mm(x, 6)),
            (20, fmt_mm(y, 6)),
            (30, "0"),
        )
    out += _pairs((0, "SEQEND"), (8, layer))
    return out
