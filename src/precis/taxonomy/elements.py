"""The periodic axis's static table — group, period, block, series per element.

``taxonomy-bootstrap.md`` §"Generated subject axes": the periodic axis is a
static 118-row table shipped in-tree, deliberately **not** ``mendeleev`` or
``pymatgen`` — both live in the ``[estimate]`` extra that production does not
install (auto-memory ``deploy-extras-gap``), and a periodic table does not
change often enough to need a live dependency. Symbol, atomic number, name
and crystal symmetry are not duplicated here — they come from ``ase.data``
(a core dep) at import time via :func:`_from_ase`. Only the four curated
columns (group, period, block, series) are hand-maintained data, one row per
element in atomic-number order, index 0 == hydrogen.

``series`` follows the common-chemistry-classroom classification, not a
single canonical IUPAC source (there isn't one for the metalloid/halogen
boundary or the group-12/group-3 transition-metal edge): alkali metals and
alkaline earths are groups 1-2 excluding hydrogen; lanthanides/actinides are
the two 15-element f-block rows with ``group=None``; the six classic
metalloids (B, Si, Ge, As, Sb, Te) are joined by Po and At, which modern
tables increasingly classify as metalloid rather than post-transition-metal
or halogen; every other d-block element (groups 3-12, including the zinc
group) is ``transition-metal``; the remaining p-block metals are
``post-transition-metal``; noble gases are group 18; everything left
(H, C, N, O, F, P, S, Cl, Se, Br, I, Ts) is ``reactive-nonmetal``. Superheavy
elements past Og's neighbours (Nh-Ts) have no measured chemistry; the series
assigned is the predicted classification used by most public periodic
tables, not an experimental fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from ase.data import atomic_names, chemical_symbols, reference_states

Block = Literal["s", "p", "d", "f"]
Series = Literal[
    "alkali-metal",
    "alkaline-earth-metal",
    "lanthanide",
    "actinide",
    "transition-metal",
    "post-transition-metal",
    "metalloid",
    "reactive-nonmetal",
    "noble-gas",
]

#: Platinum-group metals (`taxonomy-bootstrap.md` periodic axis spec).
_PLATINUM_GROUP: Final[frozenset[str]] = frozenset({"Ru", "Rh", "Pd", "Os", "Ir", "Pt"})

# Curated columns only: (symbol, atomic number, group, period, block, series).
# ``group`` is ``None`` for the two f-block rows (lanthanides, actinides),
# per the class docstring above. Deliberately static data, not a
# mendeleev/pymatgen call: see the module docstring for why.
_TABLE: Final[tuple[tuple[str, int, int | None, int, Block, Series], ...]] = (
    ("H", 1, 1, 1, "s", "reactive-nonmetal"),
    ("He", 2, 18, 1, "s", "noble-gas"),
    ("Li", 3, 1, 2, "s", "alkali-metal"),
    ("Be", 4, 2, 2, "s", "alkaline-earth-metal"),
    ("B", 5, 13, 2, "p", "metalloid"),
    ("C", 6, 14, 2, "p", "reactive-nonmetal"),
    ("N", 7, 15, 2, "p", "reactive-nonmetal"),
    ("O", 8, 16, 2, "p", "reactive-nonmetal"),
    ("F", 9, 17, 2, "p", "reactive-nonmetal"),
    ("Ne", 10, 18, 2, "p", "noble-gas"),
    ("Na", 11, 1, 3, "s", "alkali-metal"),
    ("Mg", 12, 2, 3, "s", "alkaline-earth-metal"),
    ("Al", 13, 13, 3, "p", "post-transition-metal"),
    ("Si", 14, 14, 3, "p", "metalloid"),
    ("P", 15, 15, 3, "p", "reactive-nonmetal"),
    ("S", 16, 16, 3, "p", "reactive-nonmetal"),
    ("Cl", 17, 17, 3, "p", "reactive-nonmetal"),
    ("Ar", 18, 18, 3, "p", "noble-gas"),
    ("K", 19, 1, 4, "s", "alkali-metal"),
    ("Ca", 20, 2, 4, "s", "alkaline-earth-metal"),
    ("Sc", 21, 3, 4, "d", "transition-metal"),
    ("Ti", 22, 4, 4, "d", "transition-metal"),
    ("V", 23, 5, 4, "d", "transition-metal"),
    ("Cr", 24, 6, 4, "d", "transition-metal"),
    ("Mn", 25, 7, 4, "d", "transition-metal"),
    ("Fe", 26, 8, 4, "d", "transition-metal"),
    ("Co", 27, 9, 4, "d", "transition-metal"),
    ("Ni", 28, 10, 4, "d", "transition-metal"),
    ("Cu", 29, 11, 4, "d", "transition-metal"),
    ("Zn", 30, 12, 4, "d", "transition-metal"),
    ("Ga", 31, 13, 4, "p", "post-transition-metal"),
    ("Ge", 32, 14, 4, "p", "metalloid"),
    ("As", 33, 15, 4, "p", "metalloid"),
    ("Se", 34, 16, 4, "p", "reactive-nonmetal"),
    ("Br", 35, 17, 4, "p", "reactive-nonmetal"),
    ("Kr", 36, 18, 4, "p", "noble-gas"),
    ("Rb", 37, 1, 5, "s", "alkali-metal"),
    ("Sr", 38, 2, 5, "s", "alkaline-earth-metal"),
    ("Y", 39, 3, 5, "d", "transition-metal"),
    ("Zr", 40, 4, 5, "d", "transition-metal"),
    ("Nb", 41, 5, 5, "d", "transition-metal"),
    ("Mo", 42, 6, 5, "d", "transition-metal"),
    ("Tc", 43, 7, 5, "d", "transition-metal"),
    ("Ru", 44, 8, 5, "d", "transition-metal"),
    ("Rh", 45, 9, 5, "d", "transition-metal"),
    ("Pd", 46, 10, 5, "d", "transition-metal"),
    ("Ag", 47, 11, 5, "d", "transition-metal"),
    ("Cd", 48, 12, 5, "d", "transition-metal"),
    ("In", 49, 13, 5, "p", "post-transition-metal"),
    ("Sn", 50, 14, 5, "p", "post-transition-metal"),
    ("Sb", 51, 15, 5, "p", "metalloid"),
    ("Te", 52, 16, 5, "p", "metalloid"),
    ("I", 53, 17, 5, "p", "reactive-nonmetal"),
    ("Xe", 54, 18, 5, "p", "noble-gas"),
    ("Cs", 55, 1, 6, "s", "alkali-metal"),
    ("Ba", 56, 2, 6, "s", "alkaline-earth-metal"),
    ("La", 57, None, 6, "f", "lanthanide"),
    ("Ce", 58, None, 6, "f", "lanthanide"),
    ("Pr", 59, None, 6, "f", "lanthanide"),
    ("Nd", 60, None, 6, "f", "lanthanide"),
    ("Pm", 61, None, 6, "f", "lanthanide"),
    ("Sm", 62, None, 6, "f", "lanthanide"),
    ("Eu", 63, None, 6, "f", "lanthanide"),
    ("Gd", 64, None, 6, "f", "lanthanide"),
    ("Tb", 65, None, 6, "f", "lanthanide"),
    ("Dy", 66, None, 6, "f", "lanthanide"),
    ("Ho", 67, None, 6, "f", "lanthanide"),
    ("Er", 68, None, 6, "f", "lanthanide"),
    ("Tm", 69, None, 6, "f", "lanthanide"),
    ("Yb", 70, None, 6, "f", "lanthanide"),
    ("Lu", 71, None, 6, "f", "lanthanide"),
    ("Hf", 72, 4, 6, "d", "transition-metal"),
    ("Ta", 73, 5, 6, "d", "transition-metal"),
    ("W", 74, 6, 6, "d", "transition-metal"),
    ("Re", 75, 7, 6, "d", "transition-metal"),
    ("Os", 76, 8, 6, "d", "transition-metal"),
    ("Ir", 77, 9, 6, "d", "transition-metal"),
    ("Pt", 78, 10, 6, "d", "transition-metal"),
    ("Au", 79, 11, 6, "d", "transition-metal"),
    ("Hg", 80, 12, 6, "d", "transition-metal"),
    ("Tl", 81, 13, 6, "p", "post-transition-metal"),
    ("Pb", 82, 14, 6, "p", "post-transition-metal"),
    ("Bi", 83, 15, 6, "p", "post-transition-metal"),
    ("Po", 84, 16, 6, "p", "metalloid"),
    ("At", 85, 17, 6, "p", "metalloid"),
    ("Rn", 86, 18, 6, "p", "noble-gas"),
    ("Fr", 87, 1, 7, "s", "alkali-metal"),
    ("Ra", 88, 2, 7, "s", "alkaline-earth-metal"),
    ("Ac", 89, None, 7, "f", "actinide"),
    ("Th", 90, None, 7, "f", "actinide"),
    ("Pa", 91, None, 7, "f", "actinide"),
    ("U", 92, None, 7, "f", "actinide"),
    ("Np", 93, None, 7, "f", "actinide"),
    ("Pu", 94, None, 7, "f", "actinide"),
    ("Am", 95, None, 7, "f", "actinide"),
    ("Cm", 96, None, 7, "f", "actinide"),
    ("Bk", 97, None, 7, "f", "actinide"),
    ("Cf", 98, None, 7, "f", "actinide"),
    ("Es", 99, None, 7, "f", "actinide"),
    ("Fm", 100, None, 7, "f", "actinide"),
    ("Md", 101, None, 7, "f", "actinide"),
    ("No", 102, None, 7, "f", "actinide"),
    ("Lr", 103, None, 7, "f", "actinide"),
    ("Rf", 104, 4, 7, "d", "transition-metal"),
    ("Db", 105, 5, 7, "d", "transition-metal"),
    ("Sg", 106, 6, 7, "d", "transition-metal"),
    ("Bh", 107, 7, 7, "d", "transition-metal"),
    ("Hs", 108, 8, 7, "d", "transition-metal"),
    ("Mt", 109, 9, 7, "d", "transition-metal"),
    ("Ds", 110, 10, 7, "d", "transition-metal"),
    ("Rg", 111, 11, 7, "d", "transition-metal"),
    ("Cn", 112, 12, 7, "d", "transition-metal"),
    ("Nh", 113, 13, 7, "p", "post-transition-metal"),
    ("Fl", 114, 14, 7, "p", "post-transition-metal"),
    ("Mc", 115, 15, 7, "p", "post-transition-metal"),
    ("Lv", 116, 16, 7, "p", "post-transition-metal"),
    ("Ts", 117, 17, 7, "p", "reactive-nonmetal"),
    ("Og", 118, 18, 7, "p", "noble-gas"),
)


@dataclass(frozen=True, slots=True)
class Element:
    """One periodic-table row: static curated columns plus ``ase.data``."""

    symbol: str
    number: int
    name: str
    group: int | None
    """``None`` for the f-block lanthanide/actinide rows."""
    period: int
    block: Block
    series: Series
    crystal_symmetry: str | None
    """Reference-state crystal symmetry from ``ase.data.reference_states``
    (e.g. ``"fcc"``, ``"bcc"``, ``"hcp"``, ``"diatom"``); ``None`` when ase
    has no reference state for the element (radioactive/superheavy)."""

    @property
    def is_metal(self) -> bool:
        return self.series not in ("reactive-nonmetal", "noble-gas", "metalloid")

    @property
    def is_transition_metal(self) -> bool:
        return self.series == "transition-metal"

    @property
    def is_platinum_group(self) -> bool:
        return self.symbol in _PLATINUM_GROUP


def _crystal_symmetry(number: int) -> str | None:
    state = reference_states[number]
    if state is None:
        return None
    symmetry = state.get("symmetry")
    return str(symmetry) if symmetry is not None else None


ELEMENTS: Final[tuple[Element, ...]] = tuple(
    Element(
        symbol=symbol,
        number=number,
        name=atomic_names[number],
        group=group,
        period=period,
        block=block,
        series=series,
        crystal_symmetry=_crystal_symmetry(number),
    )
    for symbol, number, group, period, block, series in _TABLE
)

assert len(ELEMENTS) == 118
assert [e.symbol for e in ELEMENTS] == chemical_symbols[1:]

_BY_SYMBOL: Final[dict[str, Element]] = {e.symbol: e for e in ELEMENTS}


def element(symbol: str) -> Element | None:
    """Case-sensitive symbol lookup; ``None`` for anything not a real element."""
    return _BY_SYMBOL.get(symbol)
