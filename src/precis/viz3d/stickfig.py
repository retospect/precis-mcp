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

__all__ = ["Style", "cpk_color", "stick_scene"]

#: CPK colours (hex), following the Jmol CPK convention exactly. Own table —
#: see the module docstring on why this isn't imported from ``precis_web``.
#: The complete Jmol assignment for Z=1-109 (Jmol itself leaves the
#: superheavies unassigned); values match ASE's ``ase.data.colors.jmol_colors``
#: — element→colour assignments are factual data, transcribed rather than
#: runtime-imported so this module's chain stays numpy-only. Symbols outside
#: the table (typos, placeholders, Z≥110) fall back to a loud pink so an
#: uncolourable element reads as obviously wrong, never a plausible-looking
#: guess — the same pink CPK itself uses for "whatever's left".
_CPK: dict[str, str] = {
    "H": "#ffffff", "He": "#d9ffff",
    "Li": "#cc80ff", "Be": "#c2ff00", "B": "#ffb5b5", "C": "#909090",
    "N": "#3050f8", "O": "#ff0d0d", "F": "#90e050", "Ne": "#b3e3f5",
    "Na": "#ab5cf2", "Mg": "#8aff00", "Al": "#bfa6a6", "Si": "#f0c8a0",
    "P": "#ff8000", "S": "#ffff30", "Cl": "#1ff01f", "Ar": "#80d1e3",
    "K": "#8f40d4", "Ca": "#3dff00", "Sc": "#e6e6e6", "Ti": "#bfc2c7",
    "V": "#a6a6ab", "Cr": "#8a99c7", "Mn": "#9c7ac7", "Fe": "#e06633",
    "Co": "#f090a0", "Ni": "#50d050", "Cu": "#c88033", "Zn": "#7d80b0",
    "Ga": "#c28f8f", "Ge": "#668f8f", "As": "#bd80e3", "Se": "#ffa100",
    "Br": "#a62929", "Kr": "#5cb8d1",
    "Rb": "#702eb0", "Sr": "#00ff00", "Y": "#94ffff", "Zr": "#94e0e0",
    "Nb": "#73c2c9", "Mo": "#54b5b5", "Tc": "#3b9e9e", "Ru": "#248f8f",
    "Rh": "#0a7d8c", "Pd": "#006985", "Ag": "#c0c0c0", "Cd": "#ffd98f",
    "In": "#a67573", "Sn": "#668080", "Sb": "#9e63b5", "Te": "#d47a00",
    "I": "#940094", "Xe": "#429eb0",
    "Cs": "#57178f", "Ba": "#00c900", "La": "#70d4ff", "Ce": "#ffffc7",
    "Pr": "#d9ffc7", "Nd": "#c7ffc7", "Pm": "#a3ffc7", "Sm": "#8fffc7",
    "Eu": "#61ffc7", "Gd": "#45ffc7", "Tb": "#30ffc7", "Dy": "#1fffc7",
    "Ho": "#00ff9c", "Er": "#00e675", "Tm": "#00d452", "Yb": "#00bf38",
    "Lu": "#00ab24", "Hf": "#4dc2ff", "Ta": "#4da6ff", "W": "#2194d6",
    "Re": "#267dab", "Os": "#266696", "Ir": "#175487", "Pt": "#d0d0e0",
    "Au": "#ffd123", "Hg": "#b8b8d0", "Tl": "#a6544d", "Pb": "#575961",
    "Bi": "#9e4fb5", "Po": "#ab5c00", "At": "#754f45", "Rn": "#428296",
    "Fr": "#420066", "Ra": "#007d00", "Ac": "#70abfa", "Th": "#00baff",
    "Pa": "#00a1ff", "U": "#008fff", "Np": "#0080ff", "Pu": "#006bff",
    "Am": "#545cf2", "Cm": "#785ce3", "Bk": "#8a4fe3", "Cf": "#a136d4",
    "Es": "#b31fd4", "Fm": "#b31fba", "Md": "#b30da6", "No": "#bd0d87",
    "Lr": "#c70066", "Rf": "#cc0059", "Db": "#d1004f", "Sg": "#d90045",
    "Bh": "#e00038", "Hs": "#e6002e", "Mt": "#eb0026",
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


def cpk_color(element: str) -> str:
    """The CPK colour a ``cpk`` stick figure draws ``element`` in — for a
    legend that must match the picture."""
    return _CPK.get(element, _CPK_DEFAULT)


def _element_color(element: str, style: Style) -> str:
    if style.color == "mono":
        return _MONO_COLOR
    return cpk_color(element)


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
