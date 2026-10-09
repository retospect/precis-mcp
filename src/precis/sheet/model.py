"""The sheet-job data model and its validation. Package docstring:
:mod:`precis.sheet`."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

#: Layer operations, in the default machine run order (parts cut free last).
DEFAULT_OP_ORDER: tuple[str, ...] = (
    "engrave_raster",
    "engrave_vector",
    "score",
    "drill",
    "cut",
)
OPS: frozenset[str] = frozenset(DEFAULT_OP_ORDER)

#: Machines a layer may carry settings for. Keys of :attr:`Layer.settings`.
MACHINES: tuple[str, ...] = ("laser", "cricut", "pcb")


class SheetJobError(ValueError):
    """The job is malformed (unknown op, undeclared layer, duplicate name)."""


class SheetExportError(ValueError):
    """An exporter refused the job — always naming the layer it refused."""


@dataclass(frozen=True)
class Sheet:
    width_mm: float
    height_mm: float
    material: str = ""
    thickness_mm: float | None = None


@dataclass(frozen=True)
class Layer:
    """One operation on the sheet. ``settings`` is keyed by machine
    (:data:`MACHINES`); an exporter reads only its own key."""

    name: str
    op: str
    settings: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class Polyline:
    """Open or closed polyline in mm. ``inner`` marks a hole contour of
    ``part`` (cut before the part's outline)."""

    layer: str
    points: tuple[tuple[float, float], ...]
    closed: bool = True
    part: str | None = None
    inner: bool = False


@dataclass(frozen=True)
class Circle:
    layer: str
    cx: float
    cy: float
    r: float
    part: str | None = None
    inner: bool = False


Shape = Polyline | Circle


@dataclass(frozen=True)
class Part:
    """A nested part. ``pose`` is the optional 3-D placement (an emitter's
    own frame dict); ``thickness_mm`` the stock it is cut from."""

    id: str
    name: str
    pose: Mapping[str, Any] | None = None
    thickness_mm: float | None = None


@dataclass(frozen=True)
class Export:
    """An exporter's result: the file text, and the notes it also wrote into
    the file's leading comment (defaults used, figures, refusals avoided)."""

    text: str
    notes: tuple[str, ...]


def order_layers(layers: Iterable[Layer]) -> list[Layer]:
    """Stable sort into :data:`DEFAULT_OP_ORDER`. Unknown ops sort last so
    :meth:`SheetJob.validate` still names them."""
    rank = {op: i for i, op in enumerate(DEFAULT_OP_ORDER)}
    return sorted(layers, key=lambda layer: rank.get(layer.op, len(rank)))


@dataclass
class SheetJob:
    """Sheet + layers (job order = cut order) + shapes + parts + notes."""

    sheet: Sheet
    layers: list[Layer] = field(default_factory=list)
    shapes: list[Shape] = field(default_factory=list)
    parts: list[Part] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def layer(self, name: str) -> Layer:
        for layer in self.layers:
            if layer.name == name:
                return layer
        raise SheetJobError(f"no layer {name!r}")

    def shapes_on(self, layer: str) -> list[Shape]:
        """The layer's shapes in cut order: parts in :attr:`parts` order
        (part-less shapes last, as given), inner contours before outlines."""
        part_rank = {p.id: i for i, p in enumerate(self.parts)}
        own = [s for s in self.shapes if s.layer == layer]
        return sorted(
            own,
            key=lambda s: (
                part_rank.get(s.part or "", len(part_rank)),
                0 if s.inner else 1,
            ),
        )

    def problems(self) -> list[str]:
        """Every validation failure, as a sentence; empty when well-formed."""
        out: list[str] = []
        seen: set[str] = set()
        for layer in self.layers:
            if layer.name in seen:
                out.append(f"layer {layer.name!r} is declared twice")
            seen.add(layer.name)
            if layer.op not in OPS:
                out.append(
                    f"layer {layer.name!r}: unknown op {layer.op!r} "
                    f"(one of {', '.join(DEFAULT_OP_ORDER)})"
                )
            for machine in layer.settings:
                if machine not in MACHINES:
                    out.append(
                        f"layer {layer.name!r}: settings for unknown machine "
                        f"{machine!r} (one of {', '.join(MACHINES)})"
                    )
        part_ids = {p.id for p in self.parts}
        if len(part_ids) != len(self.parts):
            out.append("part ids are not unique")
        for i, shape in enumerate(self.shapes):
            if shape.layer not in seen:
                out.append(f"shape {i}: layer {shape.layer!r} is not declared")
            if shape.part is not None and shape.part not in part_ids:
                out.append(f"shape {i}: part {shape.part!r} is not declared")
            if isinstance(shape, Polyline) and len(shape.points) < 2:
                out.append(f"shape {i}: polyline needs at least two points")
            if isinstance(shape, Circle) and shape.r <= 0:
                out.append(f"shape {i}: circle radius must be positive")
        if self.sheet.width_mm <= 0 or self.sheet.height_mm <= 0:
            out.append("sheet size must be positive")
        return out

    def validate(self) -> SheetJob:
        problems = self.problems()
        if problems:
            raise SheetJobError("; ".join(problems))
        return self


def fmt_mm(value: float, places: int = 4) -> str:
    """Compact fixed-point mm for file output (``10`` not ``10.0000``;
    ``-0`` never)."""
    text = f"{value:.{places}f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def comment_safe(text: str) -> str:
    """Strip the sequence that ends an SVG/XML comment early."""
    return text.replace("--", "-")
