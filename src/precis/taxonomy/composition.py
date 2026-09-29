"""The composition subject axis: parse a material label into its elements.

`taxonomy-bootstrap.md` §"Generated subject axes". ``ase.formula.Formula``
handles integer stoichiometry and nesting but **raises ``ValueError`` on
every real-world shape a catalysis label actually uses** — verified against
``Ce0.5Zr0.5O2`` (fractional subscript), ``NiFe-LDH`` (class suffix),
``Pd/C`` (support notation) and ``Cu-foam@mesh`` (decoration + support), all
four of which fail ``Formula(label)``. ``Formula`` also does not validate its
symbols — ``Formula("Xy2")`` happily returns ``{"Xy": 2}`` — so every symbol,
on every path, is checked against ``ase.data.chemical_symbols`` before a
result is trusted.

Pipeline: a pre-pass strips support notation (``/`` or ``@``, trailing
segment), a trailing all-caps class suffix, and a trailing lowercase
morphology word; the remainder is tried against ``Formula``; a ``Formula``
failure falls back to a hand-written symbol scanner for fractional
subscripts. Any unvalidated symbol, on any path, makes the whole label
unparseable (:func:`parse_composition` returns ``None``) rather than
returning a partial or wrong result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from ase.data import chemical_symbols
from ase.formula import Formula

from precis.taxonomy.elements import element
from precis.taxonomy.types import AxisEdge

_AXIS = "composition"

#: Real element symbols only — excludes ``chemical_symbols[0] == "X"``, ase's
#: dummy/vacancy placeholder, which must never validate as a real element.
_VALID_SYMBOLS: Final[frozenset[str]] = frozenset(chemical_symbols[1:])

#: All-caps trailing token after a hyphen that names a structural class
#: rather than an element (`taxonomy-bootstrap.md`: "NiFe-LDH", "Cu-MOF").
_CLASS_SUFFIXES: Final[frozenset[str]] = frozenset({"LDH", "MOF", "COF", "SAC", "NC"})

#: Lowercase trailing token after a hyphen that names a morphology, not a
#: chemical entity (`taxonomy-bootstrap.md`: "Cu-foam@mesh").
_MORPHOLOGY_WORDS: Final[frozenset[str]] = frozenset({"foam", "mesh", "nanosheet"})

_TRAILING_HYPHEN_TOKEN = re.compile(r"^(?P<base>.*)-(?P<token>[A-Za-z]+)$")

#: Fallback scanner for a formula ``Formula`` refuses: one element symbol
#: (uppercase, optional lowercase second letter) per match, optionally
#: followed by an integer or decimal stoichiometric count.
_SYMBOL_TOKEN = re.compile(r"[A-Z][a-z]?(?:\d*\.\d+|\d+)?")


@dataclass(frozen=True, slots=True)
class Composition:
    """What a material label parsed into.

    ``elements`` preserves input order (not sorted, not deduplicated by the
    parser) so a caller can tell ``PdCoP`` from ``CoPPd`` if it ever matters.
    """

    elements: tuple[tuple[str, float], ...]
    support: str | None
    class_suffix: str | None
    decoration: str | None
    base_label: str


def parse_composition(label: str) -> Composition | None:
    """Parse a material label; ``None`` for anything that is not a formula.

    See the module docstring for the pipeline. Returning ``None`` rather than
    a best-effort guess is deliberate — an unvalidated scanner would read
    "Mesh" as element fragments, and a caller silently treating that as a
    real composition would be worse than one that got nothing.
    """
    base = label.strip()
    support = None
    sep_idx = _find_support_separator(base)
    if sep_idx is not None:
        base, support = base[:sep_idx], base[sep_idx + 1 :]

    class_suffix: str | None = None
    decoration: str | None = None
    match = _TRAILING_HYPHEN_TOKEN.match(base)
    if match:
        token = match.group("token")
        if token.upper() == token and token in _CLASS_SUFFIXES:
            class_suffix = token
            base = match.group("base")
        elif token.lower() == token and token in _MORPHOLOGY_WORDS:
            decoration = token
            base = match.group("base")

    if not base:
        return None

    elements = _parse_formula(base) or _scan_symbols(base)
    if elements is None:
        return None
    if not all(sym in _VALID_SYMBOLS for sym, _ in elements):
        return None

    return Composition(
        elements=tuple(elements),
        support=support,
        class_suffix=class_suffix,
        decoration=decoration,
        base_label=base,
    )


def _find_support_separator(base: str) -> int | None:
    for idx, ch in enumerate(base):
        if ch in "/@":
            return idx
    return None


def _parse_formula(base: str) -> list[tuple[str, float]] | None:
    try:
        formula = Formula(base)
    except ValueError:
        return None
    return [(sym, float(count)) for sym, count in formula.count().items()]


def _scan_symbols(base: str) -> list[tuple[str, float]] | None:
    """Fallback for fractional subscripts, which ``Formula`` refuses.

    Requires the whole string to be consumed as a contiguous run of
    element-token matches; any leftover character (e.g. the lowercase tail
    of a word ``Formula`` also rejected) fails the scan rather than silently
    dropping it.
    """
    out: list[tuple[str, float]] = []
    pos = 0
    n = len(base)
    while pos < n:
        match = _SYMBOL_TOKEN.match(base, pos)
        if not match or match.start() != pos:
            return None
        sym = match.group(0)
        count_str = ""
        # Split the match back into symbol vs count: the token regex allows
        # 1-2 letters then digits, so re-derive the split explicitly.
        letters = re.match(r"[A-Z][a-z]?", sym)
        assert letters is not None
        sym_only = letters.group(0)
        count_str = sym[len(sym_only) :]
        count = float(count_str) if count_str else 1.0
        out.append((sym_only, count))
        pos = match.end()
    return out if out else None


def composition_edges(label: str) -> tuple[AxisEdge, ...]:
    """Composition-axis parent proposals; empty for an unparseable label.

    Parents are one per distinct element symbol plus a derived
    unary/binary/ternary/quaternary alloy-or-compound class — "alloy" only
    when *every* distinct element is a metal (`elements.element`), otherwise
    "compound". Class suffix, decoration and support are campaign
    ``material-class`` concerns (`subjects.memberships`), not composition.
    """
    comp = parse_composition(label)
    if comp is None:
        return ()
    child = _subject_key(label)
    distinct = list(dict.fromkeys(sym for sym, _ in comp.elements))
    edges = [AxisEdge(child=child, parent=sym.lower(), axis=_AXIS) for sym in distinct]
    edges.append(AxisEdge(child=child, parent=_class_parent(distinct), axis=_AXIS))
    return tuple(edges)


def _class_parent(distinct_symbols: list[str]) -> str:
    prefix = {1: "unary", 2: "binary", 3: "ternary"}.get(
        len(distinct_symbols), "quaternary"
    )
    all_metal = all(_is_metal(sym) for sym in distinct_symbols)
    return f"{prefix}-{'alloy' if all_metal else 'compound'}"


def _is_metal(symbol: str) -> bool:
    el = element(symbol)
    return el is not None and el.is_metal


def _subject_key(label: str) -> str:
    """Lowercase, punctuation to hyphen, collapsed — the node key for a label.

    Same one-line rule as `subjects.subject_key` (the module's public name
    for it), duplicated here rather than imported so this module never
    depends on `subjects`, which imports this module.
    """
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
