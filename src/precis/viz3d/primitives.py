"""The scene vocabulary — plain geometric primitives, no behaviour.

Every primitive is a frozen dataclass of raw floats/strings; there is no
"kind" enum bolted on — :mod:`.render` dispatches on ``isinstance``. Colors
are hex strings (``"#rrggbb"``) throughout, chosen by the *caller*
(:mod:`.stickfig` picks CPK) — this module has no color opinions.

A :class:`Stick` carries two colors (``color_a``/``color_b``) rather than
one: the CPK "split bond" convention draws each half of a bond in its own
atom's color, which :mod:`.render` implements geometrically by splitting
every stick at its midpoint (r1+) rather than by gradient-blending a single
line, so occlusion between two crossing bonds sorts correctly half-by-half.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["Ball", "Label", "Polyline", "Primitive", "Scene3", "Stick"]

Point3 = tuple[float, float, float]


@dataclass(frozen=True)
class Ball:
    """A sphere — an atom, a joint, a marker. ``radius`` is in scene units."""

    center: Point3
    radius: float
    color: str


@dataclass(frozen=True)
class Stick:
    """A capsule between two points — a bond, an edge, a strut.

    ``color_a``/``color_b`` are the colors at ``a``/``b`` respectively; equal
    colors render as an ordinary monochrome stick. See the module docstring
    on why this is two colors rather than a gradient.
    """

    a: Point3
    b: Point3
    radius: float
    color_a: str
    color_b: str


@dataclass(frozen=True)
class Polyline:
    """An open chain of segments — a path, a trace, an annotation line."""

    points: tuple[Point3, ...]
    color: str
    width: float = 1.0


@dataclass(frozen=True)
class Label:
    """A text annotation anchored to a 3D point (projected like any other
    primitive, then drawn as SVG ``<text>`` at its projected position)."""

    anchor3d: Point3
    text: str
    color: str = "#000000"


#: The closed set :mod:`.render` dispatches on.
Primitive = Ball | Stick | Polyline | Label


@dataclass
class Scene3:
    """A render-ready bag of primitives plus the unit they're expressed in.

    ``unit_label`` is never consumed for arithmetic — it is display-only,
    surfaced on the scalebar (the one place a unit appears at all; see the
    package docstring's unit contract). ``scale_hint``, if given, is a
    world-unit length :mod:`.render`'s scalebar should treat as a preferred
    round value (e.g. a lattice constant) instead of picking one purely from
    frame coverage; ``None`` leaves the auto-pick alone.
    """

    primitives: list[Primitive] = field(default_factory=list)
    unit_label: str = ""
    scale_hint: float | None = None
