"""Parse and balance-check a reaction equation string. Pure; no chemicals import
beyond the formula parser, imported lazily."""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction

from precis.errors import BadInput

_SEP_RE = re.compile(r"\s*(?:->|→|=)\s*")
_COEF_RE = re.compile(r"^(?P<c>\d+\s*/\s*\d+|\d+\.\d+|\.\d+|\d+)?\s*(?P<sp>.+)$")
_PHASE_RE = re.compile(r"^(?P<f>.+?)\s*\((?P<p>g|l)\)$")
_FORMULA_RE = re.compile(r"^[A-Z][A-Za-z0-9()]*$")

_NEXT = "get(kind='rxn', view='energetics', q='NO + 5/2 H2 -> NH3 + H2O')"


@dataclass(frozen=True)
class Term:
    """One species on one side: ``coef`` > 0 is its magnitude; ``phase`` is
    ``'g'`` or ``'l'``; ``atoms`` its element counts."""

    formula: str
    coef: Fraction
    phase: str
    atoms: dict[str, int]

    @property
    def label(self) -> str:
        return f"{self.formula}({self.phase})"


@dataclass(frozen=True)
class Equation:
    text: str
    reactants: tuple[Term, ...]
    products: tuple[Term, ...]


def formula_atoms(formula: str) -> dict[str, int]:
    """Element counts of a plain formula; ``BadInput`` when it is not one."""
    from ase.data import chemical_symbols
    from chemicals import nested_formula_parser

    if not _FORMULA_RE.match(formula):
        raise BadInput(
            f"{formula!r} is not a plain chemical formula",
            next=_NEXT,
        )
    try:
        atoms = nested_formula_parser(formula)
    except Exception as exc:  # chemicals raises bare ValueError/KeyError
        raise BadInput(
            f"could not parse species formula {formula!r}: {exc}",
            next=_NEXT,
        ) from exc
    unknown = sorted(el for el in atoms if el not in chemical_symbols[1:])
    if not atoms or unknown:
        raise BadInput(
            f"species {formula!r} is not a chemical formula"
            + (f" (not elements: {', '.join(unknown)})" if unknown else ""),
            next=_NEXT,
        )
    return {k: int(v) for k, v in atoms.items()}


def _parse_side(side: str, *, eq: str) -> tuple[Term, ...]:
    terms: list[Term] = []
    for raw in side.split("+"):
        tok = raw.strip()
        if not tok:
            raise BadInput(f"empty species in equation {eq!r}", next=_NEXT)
        m = _COEF_RE.match(tok)
        if m is None:  # pragma: no cover - regex matches any non-empty token
            raise BadInput(f"cannot read term {tok!r}", next=_NEXT)
        coef_s, species = m.group("c"), m.group("sp").strip()
        try:
            coef = Fraction(coef_s.replace(" ", "")) if coef_s else Fraction(1)
        except (ValueError, ZeroDivisionError) as exc:
            raise BadInput(
                f"bad coefficient {coef_s!r} in {tok!r}", next=_NEXT
            ) from exc
        if coef <= 0:
            raise BadInput(f"coefficient must be positive in {tok!r}", next=_NEXT)
        pm = _PHASE_RE.match(species)
        phase = "g"
        if pm:
            species, phase = pm.group("f"), pm.group("p")
        terms.append(Term(species, coef, phase, formula_atoms(species)))
    return tuple(terms)


def _imbalance(
    reactants: tuple[Term, ...], products: tuple[Term, ...]
) -> dict[str, Fraction]:
    net: dict[str, Fraction] = {}
    for sign, side in ((-1, reactants), (1, products)):
        for t in side:
            for el, n in t.atoms.items():
                net[el] = net.get(el, Fraction(0)) + sign * t.coef * n
    return {el: v for el, v in net.items() if v != 0}


def parse_equation(text: str) -> Equation:
    """Parse ``'NO + 5/2 H2 -> NH3 + H2O'``; ``BadInput`` if unparseable or
    unbalanced (naming the elements that do not balance)."""
    eq = text.strip()
    if not eq:
        raise BadInput("empty equation", next=_NEXT)
    parts = _SEP_RE.split(eq)
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise BadInput(
            f"equation {eq!r} needs exactly one '->' (or '=') with species on both sides",
            next=_NEXT,
        )
    reactants = _parse_side(parts[0], eq=eq)
    products = _parse_side(parts[1], eq=eq)
    bad = _imbalance(reactants, products)
    if bad:
        detail = ", ".join(
            f"{el} (product side has {abs(v)} "
            f"{'more' if v > 0 else 'fewer'} than the reactant side)"
            for el, v in sorted(bad.items())
        )
        raise BadInput(
            f"equation {eq!r} is not balanced: {detail}",
            next=_NEXT,
        )
    return Equation(eq, reactants, products)
