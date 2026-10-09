"""Sheet materials for the flat-pack generator — a constants module, changed
by commit, not a ``kind='material'`` row.

Each entry carries the nominal and actual thickness, the Young's modulus
where known, and the per-machine ``fit`` / ``kerf`` that seated on a real
cut. **No figure here is measured yet**: ``t_actual`` equals ``t_nominal``
until a caliper reading lands, and every ``fit``/``kerf`` is ``None`` until
Reto's first cut (the 50 mm cube in ``corrugated-3mm``, se-machine-design-5).
A ``None`` figure falls back to 0 mm in the generator, which says so in its
echo (:func:`precis_se.flatpack.generator.resolve_figures`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

#: Machines a material may hold recorded figures for.
MACHINES: Final = ("laser", "cnc", "cricut")


@dataclass(frozen=True)
class Figures:
    """Recorded ``fit`` (gap widening, mm) and ``kerf`` (beam/cutter width,
    mm) that seated on one machine. ``None`` = not yet recorded."""

    fit: float | None = None
    kerf: float | None = None


@dataclass(frozen=True)
class Material:
    slug: str
    t_nominal: float
    t_actual: float
    E_gpa: float | None = None
    figures: dict[str, Figures] = field(default_factory=dict)
    note: str = ""

    def recorded(self, machine: str) -> Figures:
        return self.figures.get(machine, Figures())


_PLYWOOD_NOTE = (
    "t_actual is the nominal until measured; E = 9 GPa is a generic "
    "birch/poplar plywood figure along the face grain."
)

MATERIALS: Final = MappingProxyType(
    {
        "corrugated-3mm": Material(
            slug="corrugated-3mm",
            t_nominal=3.0,
            t_actual=3.0,
            E_gpa=None,
            figures={"laser": Figures()},
            note=(
                "Single-wall corrugated cardboard. No E: the board is markedly "
                "stiffer along its flutes than across them, so one modulus "
                "would mislead. Laser fit/kerf are recorded after the first "
                "50 mm cube seats."
            ),
        ),
        **{
            f"plywood-{t}mm": Material(
                slug=f"plywood-{t}mm",
                t_nominal=float(t),
                t_actual=float(t),
                E_gpa=9.0,
                figures={"cnc": Figures()},
                note=_PLYWOOD_NOTE,
            )
            for t in (6, 9, 12, 18)
        },
    }
)

DEFAULT_MATERIAL: Final = "corrugated-3mm"


def material(slug: str) -> Material:
    try:
        return MATERIALS[slug]
    except KeyError:
        raise KeyError(
            f"unknown material {slug!r}; one of {', '.join(MATERIALS)}"
        ) from None
