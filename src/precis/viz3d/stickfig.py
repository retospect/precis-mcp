"""The CPK stick-figure adapter — the one domain-touching renderer in this
slice, and the model for every later ``viz3d`` view (envelope, cad): a
small pure function from atom data to a :class:`~precis.viz3d.primitives.Scene3`,
with no knowledge of the ``se``/``structure`` store layer above it.

Reuses :func:`precis.structure.elements.covalent_radius_A` for ball sizing
(the "one table, not a third copy" instruction) — CPK *colors* are new data
here, since :mod:`precis.structure` has no color opinion (it's a text/graph
IR) and :mod:`precis_web.routes.structure`'s ``_CPK`` table is a *web* UI
concern this package must not import (the package docstring's "no store, no
web" boundary) — a small, deliberately overlapping palette, not a shared
import.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from precis.structure.elements import covalent_radius_A
from precis.viz3d.primitives import Ball, Point3, Primitive, Scene3, Stick
from precis.viz3d.render import ScalebarSpec

__all__ = ["Style", "stick_scene"]

#: CPK colours (hex), following the Jmol CPK convention exactly. Own small
#: table — see the module docstring on why this isn't imported from
#: ``precis_web``. Covers the ``structure`` atomistic IR's curated element
#: palette (``precis.structure.elements``); unknown elements fall back to a
#: loud pink so a typo/uncurated element reads as obviously wrong, never a
#: plausible-looking guess.
_CPK: dict[str, str] = {
    "H": "#ffffff", "He": "#d9ffff", "B": "#ffb5b5", "C": "#909090",
    "N": "#3050f8", "O": "#ff0d0d", "F": "#90e050", "Si": "#f0c8a0",
    "P": "#ff8000", "S": "#ffff30", "Cl": "#1ff01f", "Br": "#a62929",
    "I": "#940094", "Ni": "#50d050", "Cu": "#c88033", "Pd": "#006985",
    "Pt": "#d0d0e0", "Au": "#ffd123",
}  # fmt: skip
_CPK_DEFAULT = "#ff2fa0"

#: ``style.color == "mono"`` — every atom this single neutral grey.
_MONO_COLOR = "#606060"

#: ``color_by``'s diverging colormap anchors: low -> mid -> high.
_COLORMAP_STOPS: tuple[tuple[float, tuple[int, int, int]], ...] = (
    (0.0, (0x30, 0x50, 0xF8)),  # blue
    (0.5, (0xFF, 0xFF, 0xFF)),  # white
    (1.0, (0xFF, 0x0D, 0x0D)),  # red
)


@dataclass(frozen=True)
class Style:
    """Stick-figure construction knobs.

    ``stick_radius``/``ball_scale`` and ``color``/``color_by`` are consumed
    HERE — they shape the geometry :func:`stick_scene` builds. ``halo``/
    ``fog``/``scalebar`` mirror :class:`precis.viz3d.render.Style`'s fields
    of the same name 1:1 (matching the ``se-view-figures`` recipe's flat
    JSON ``style`` block) but are inert in this module: :class:`.Scene3`
    carries no rendering knobs, so a caller wiring this up to
    :func:`precis.viz3d.render.render_svg` forwards them verbatim into that
    module's own ``Style`` — the (later) view-figure wiring slice, not
    built here.
    """

    stick_radius: float = 0.12
    ball_scale: float = 0.25
    color: Literal["cpk", "mono"] = "cpk"
    halo: bool = True
    fog: bool = False
    scalebar: ScalebarSpec = "auto"
    color_by: dict[int, float] | None = None


def _colormap(t: float) -> str:
    """Diverging blue -> white -> red colormap, ``t`` clamped to ``[0, 1]``."""
    t = max(0.0, min(1.0, t))
    for (t0, c0), (t1, c1) in pairwise(_COLORMAP_STOPS):
        if t0 <= t <= t1:
            span = t1 - t0
            f = (t - t0) / span if span > 0.0 else 0.0
            r = round(c0[0] + (c1[0] - c0[0]) * f)
            g = round(c0[1] + (c1[1] - c0[1]) * f)
            b = round(c0[2] + (c1[2] - c0[2]) * f)
            return f"#{r:02x}{g:02x}{b:02x}"
    return "#ffffff"  # unreachable given clamped t and stops spanning [0, 1]


def _element_color(element: str, style: Style) -> str:
    if style.color == "mono":
        return _MONO_COLOR
    return _CPK.get(element, _CPK_DEFAULT)


def _atom_colors(elements: list[str], style: Style) -> list[str]:
    colors = [_element_color(el, style) for el in elements]
    if not style.color_by:
        return colors
    values = list(style.color_by.values())
    lo, hi = min(values), max(values)
    span = hi - lo
    for idx, value in style.color_by.items():
        if 0 <= idx < len(colors):
            t = 0.5 if span < 1e-12 else (value - lo) / span
            colors[idx] = _colormap(t)
    return colors


def stick_scene(
    elements: list[str],
    coords: NDArray[np.floating],
    bonds: list[tuple[int, int]],
    *,
    style: Style | None = None,
) -> Scene3:
    """Atoms + bond graph -> a CPK stick-figure :class:`Scene3`, in Å.

    ``coords`` is ``(len(elements), 3)``. ``bonds`` is a list of atom-index
    pairs; each becomes one :class:`~precis.viz3d.primitives.Stick` with a
    split color at each end (the CPK convention — see the ``primitives``
    module docstring), rendered as two half-sticks starting at ``refine=1``.
    Ball radius is ``covalent_radius_A(element) * style.ball_scale`` — the
    SAME curated table :mod:`precis.structure` uses for bond detection,
    never a second/inconsistent radius table.
    """
    if style is None:
        style = Style()
    n = len(elements)
    xyz = np.asarray(coords, dtype=np.float64)
    if xyz.shape != (n, 3):
        raise ValueError(
            f"coords must be shape ({n}, 3) to match elements, got {xyz.shape}"
        )

    colors = _atom_colors(elements, style)
    primitives: list[Primitive] = []
    for element, pos, color in zip(elements, xyz, colors):
        center: Point3 = (float(pos[0]), float(pos[1]), float(pos[2]))
        radius = covalent_radius_A(element) * style.ball_scale
        primitives.append(Ball(center=center, radius=radius, color=color))

    for i, j in bonds:
        a: Point3 = (float(xyz[i, 0]), float(xyz[i, 1]), float(xyz[i, 2]))
        b: Point3 = (float(xyz[j, 0]), float(xyz[j, 1]), float(xyz[j, 2]))
        primitives.append(
            Stick(
                a=a,
                b=b,
                radius=style.stick_radius,
                color_a=colors[i],
                color_b=colors[j],
            )
        )

    return Scene3(primitives=primitives, unit_label="Å")
