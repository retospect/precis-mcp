"""Reaction ΔH / ΔS / ΔG(T) / E° and the cumulative pathway ledger.

Energies are J/mol internally (``None`` = unavailable); the renderer in
``precis.handlers._rxn_energetics`` converts to kJ/mol.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from precis.errors import BadInput
from precis.thermo.data import SpeciesData, lookup_species
from precis.thermo.equation import Equation, Term, parse_equation

#: Faraday constant, C/mol.
F_CONST = 96485.33212

T_REF = 298.15

_NOTE_T = "H(T), S(T), Cp(T): NASA-7 polynomial fits, standard pressure 1 bar; not measured point values"


@dataclass(frozen=True)
class SpeciesResult:
    side: str  # 'reactant' | 'product'
    coef: float
    data: SpeciesData


@dataclass
class ReactionResult:
    equation: str
    T: float
    species: list[SpeciesResult]
    dH: float | None  # J/mol
    dS: float | None  # J/mol/K
    dG: float | None  # J/mol at T
    n_electrons: float | None = None
    E: float | None = None  # V
    #: why a total is unavailable, e.g. ``"ΔG: NH2OH(g) S° unavailable"``
    unavailable: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def uphill(self) -> tuple[bool, str] | None:
        """``(is_uphill, basis)`` with basis ``'ΔG'`` or ``'ΔH'`` (ΔG
        unavailable); ``None`` when neither is available."""
        if self.dG is not None:
            return self.dG > 0, "ΔG"
        if self.dH is not None:
            return self.dH > 0, "ΔH"
        return None


@dataclass
class PathwayLedger:
    steps: list[ReactionResult]
    #: cumulative ΔH / ΔG after each step; None once any step so far lacks it
    cum_dH: list[float | None]
    cum_dG: list[float | None]
    T: float


def _species(term: Term, side: str, T: float) -> SpeciesResult:
    return SpeciesResult(
        side, float(term.coef), lookup_species(term.formula, term.phase, term.atoms, T)
    )


def _check_T(T: float) -> float:
    try:
        t = float(T)
    except (TypeError, ValueError, OverflowError) as exc:
        raise BadInput(
            f"T={T!r} must be a number (kelvin)", next="args={'T': 298.15}"
        ) from exc
    if not math.isfinite(t) or not t > 0:
        raise BadInput(f"T={T!r} must be finite and > 0 K", next="args={'T': 298.15}")
    return t


def _total(
    species: list[SpeciesResult], attr: str, quantity: str
) -> tuple[float | None, list[str]]:
    """Σ ν·attr products minus reactants, or ``(None, reasons)``."""
    total = 0.0
    reasons: list[str] = []
    for sp in species:
        v = getattr(sp.data, attr)
        if v is None:
            why = "unavailable: " + sp.data.note
            reasons.append(f"{sp.data.formula}({sp.data.phase}) {quantity} {why}")
            continue
        total += (sp.coef if sp.side == "product" else -sp.coef) * v
    return (None, reasons) if reasons else (total, [])


def _energetics(eq: Equation, T: float, n_electrons: float | None) -> ReactionResult:
    species = [_species(t, "reactant", T) for t in eq.reactants] + [
        _species(t, "product", T) for t in eq.products
    ]
    dH, why_h = _total(species, "H", "H(T)")
    dS, why_s = _total(species, "S", "S°")
    dG = None if dH is None or dS is None else dH - T * dS
    unavailable: list[str] = []
    if why_h:
        unavailable.append("ΔH and ΔG unavailable: " + "; ".join(why_h + why_s))
    elif why_s:
        unavailable.append("ΔG unavailable: " + "; ".join(why_s))
    notes: list[str] = [
        _NOTE_T,
        "energies and n refer to one displayed reaction extent; E=-ΔG/(nF) does not define an electrode reference",
    ]
    if eq.auto_balanced:
        notes.append(
            "auto-balanced: normalized first reactant coefficient to 1; supplied n_electrons applies to this equation"
        )
    E: float | None = None
    if n_electrons is not None:
        if dG is not None:
            E = -dG / (n_electrons * F_CONST)
    return ReactionResult(
        eq.text, T, species, dH, dS, dG, n_electrons, E, unavailable, notes
    )


def reaction_energetics(
    equation: str, *, T: float = T_REF, n_electrons: float | None = None
) -> ReactionResult:
    """Energetics of one supplied or uniquely auto-balanced equation.
    ``BadInput`` for invalid inputs, impossible or nonunique balancing."""
    t = _check_T(T)
    if n_electrons is not None:
        try:
            n_electrons = float(n_electrons)
        except (TypeError, ValueError, OverflowError) as exc:
            raise BadInput(
                "n_electrons must be a finite positive number",
                next="args={'n_electrons': 5}",
            ) from exc
        if not math.isfinite(n_electrons) or n_electrons <= 0:
            raise BadInput(
                "n_electrons must be finite and > 0", next="args={'n_electrons': 5}"
            )
    return _energetics(parse_equation(equation), t, n_electrons)


def pathway_ledger(equations: list[str], *, T: float = T_REF) -> PathwayLedger:
    """Per-step results plus cumulative ΔH/ΔG, each ``None`` from the first
    step that lacks it onward (a partial sum would read as a total)."""
    t = _check_T(T)
    if not equations:
        raise BadInput(
            "no equations given",
            next="get(kind='rxn', view='energetics', q='NO + 1/2 H2 -> HNO; HNO + 2 H2 -> NH3 + H2O')",
        )
    steps = [_energetics(parse_equation(e), t, None) for e in equations]
    cum_h: list[float | None] = []
    cum_g: list[float | None] = []
    run_h: float | None = 0.0
    run_g: float | None = 0.0
    for s in steps:
        run_h = None if run_h is None or s.dH is None else run_h + s.dH
        run_g = None if run_g is None or s.dG is None else run_g + s.dG
        cum_h.append(run_h)
        cum_g.append(run_g)
    return PathwayLedger(steps, cum_h, cum_g, t)
