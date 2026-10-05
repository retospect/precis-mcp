"""Parse neutral formulas and balance with an exact, lazy SymPy nullspace."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
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
    auto_balanced: bool = False


def formula_atoms(formula: str) -> dict[str, int]:
    """Element counts of a plain formula; ``BadInput`` when it is not one."""
    from ase.data import chemical_symbols
    from ase.formula import Formula

    if not _FORMULA_RE.match(formula):
        raise BadInput(
            f"{formula!r} is not a plain chemical formula",
            next=_NEXT,
        )
    try:
        atoms = Formula(formula).count()
    except (ValueError, AssertionError) as exc:
        raise BadInput(
            f"could not parse species formula {formula!r}: {exc}",
            next=_NEXT,
        ) from exc
    unknown = sorted(el for el in atoms if el not in chemical_symbols[1:])
    if not atoms or unknown or any(n <= 0 for n in atoms.values()):
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
    """Preserve a balanced equation's extent; otherwise balance uniquely and
    normalize the first reactant to one. Never introduce missing partners."""
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
        reactants, products = _balance(reactants, products)
        eq = _side_text(reactants) + " -> " + _side_text(products)
        return Equation(eq, reactants, products, auto_balanced=True)
    return Equation(eq, reactants, products)


def _side_text(terms: tuple[Term, ...]) -> str:
    return " + ".join(
        (f"{t.coef} " if t.coef != 1 else "")
        + t.formula
        + (f"({t.phase})" if t.phase != "g" else "")
        for t in terms
    )


def _balance(
    reactants: tuple[Term, ...], products: tuple[Term, ...]
) -> tuple[tuple[Term, ...], tuple[Term, ...]]:
    from sympy import Eq, Matrix, symbols
    from sympy.solvers.simplex import InfeasibleLPError, lpmin

    terms = reactants + products
    elements = sorted({el for t in terms for el in t.atoms})
    rows = [
        [
            t.atoms.get(el, 0) * (-1 if i < len(reactants) else 1)
            for i, t in enumerate(terms)
        ]
        for el in elements
    ]
    basis = Matrix(rows).nullspace()
    if not basis:
        raise BadInput(
            "no valid balance using these species; supply actual partners", next=_NEXT
        )
    if len(basis) > 1:
        # A strictly positive solution, if one exists, can be scaled so all
        # coefficients >= 1. Exact simplex distinguishes infeasibility from
        # an infinite family; a minimum-integer answer would hide ambiguity.
        x = symbols(f"x:{len(terms)}")
        try:
            lpmin(
                0,
                [Eq(sum(v * z for v, z in zip(row, x, strict=True)), 0) for row in rows]
                + [z >= 1 for z in x],
            )
        except InfeasibleLPError as exc:
            raise BadInput(
                "no valid positive balance using these species", next=_NEXT
            ) from exc
        raise BadInput(
            "nonunique/underdetermined balance; supply balanced coefficients",
            next=_NEXT,
        )
    vector = basis[0]
    if vector[0] == 0 or any(v / vector[0] <= 0 for v in vector):
        raise BadInput("no valid positive balance using all these species", next=_NEXT)
    balanced = tuple(
        replace(t, coef=Fraction(int(v.p), int(v.q)))
        for t, v in zip(terms, vector / vector[0], strict=True)
    )
    return balanced[: len(reactants)], balanced[len(reactants) :]
