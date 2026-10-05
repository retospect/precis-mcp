"""Species → tabulated standard-state data. The only module that touches the
``chemicals`` data tables; imported lazily inside functions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Source/method label for every number this module returns.
TABULATED = "tabulated"

#: ``chemicals`` method names that are group-contribution *estimates*, not
#: tabulated data. Never used: the default lookup silently falls back to them
#: (JOBACK gave NH2OH ΔHf° = -161.8 kJ/mol where ATcT says -43.5).
_ESTIMATORS = frozenset({"JOBACK"})

#: Elements in their gas-phase standard state: ΔHf° = 0 by definition (the
#: ``chemicals`` tables return None for H2/N2, so supply the zero explicitly).
_STANDARD_STATE_GASES = frozenset(
    {"H2", "N2", "O2", "F2", "Cl2", "He", "Ne", "Ar", "Kr", "Xe"}
)

#: S° (J/mol/K, 298.15 K, CODATA) of the standard-state element per atom, used
#: only to turn S° into ΔGf° = ΔHf° − T(S° − Σ S°_elements). An element
#: missing here leaves ΔGf° unavailable; reaction ΔG never needs it.
_ELEMENT_S_PER_ATOM: dict[str, float] = {
    "H": 130.680 / 2,
    "N": 191.609 / 2,
    "O": 205.152 / 2,
    "F": 202.791 / 2,
    "Cl": 223.081 / 2,
    "C": 5.740,  # graphite
}


@dataclass(frozen=True)
class SpeciesData:
    formula: str
    phase: str
    cas: str | None
    #: J/mol; None = unavailable
    dHf: float | None
    #: J/mol/K; None = unavailable
    S: float | None
    #: J/mol at T; None = unavailable
    dGf: float | None
    source: str = TABULATED
    #: table each value came from, e.g. ``"ΔHf° ATCT_G, S° CRC"``
    tables: str = ""
    #: quantities not available, e.g. ``["S°"]``
    missing: list[str] = field(default_factory=list)
    note: str = ""


def lookup_species(
    formula: str, phase: str, atoms: dict[str, int], T: float
) -> SpeciesData:
    """Tabulated ΔHf°/S° for ``formula`` in ``phase`` ('g' or 'l').

    A valid formula that is absent from the tables (radicals such as H2NO)
    comes back with every quantity unavailable — never a guess.
    """
    from chemicals import (
        Hfg,
        Hfg_methods,
        Hfl,
        Hfl_methods,
        S0g,
        S0g_methods,
        S0l,
        S0l_methods,
        nested_formula_parser,
    )
    from chemicals.identifiers import search_chemical

    cas: str | None = None
    try:
        chem = search_chemical(formula)
        if dict(nested_formula_parser(chem.formula)) == atoms:
            cas = chem.CASs
    except ValueError:
        cas = None
    if cas is None:
        return SpeciesData(
            formula,
            phase,
            None,
            None,
            None,
            None,
            missing=["ΔHf°", "S°"],
            note="not in the tabulated data",
        )

    if phase == "g":
        h, h_tab = _pick(Hfg, Hfg_methods, cas)
        s, s_tab = _pick(S0g, S0g_methods, cas)
        if h is None and formula in _STANDARD_STATE_GASES:
            h, h_tab = 0.0, "by definition"
    else:
        h, h_tab = _pick(Hfl, Hfl_methods, cas)
        s, s_tab = _pick(S0l, S0l_methods, cas)
    missing = [name for name, v in (("ΔHf°", h), ("S°", s)) if v is None]
    return SpeciesData(
        formula,
        phase,
        cas,
        None if h is None else float(h),
        None if s is None else float(s),
        _dgf(h, s, atoms, T),
        tables=", ".join(
            f"{n} {t}"
            for n, t, v in (("ΔHf°", h_tab, h), ("S°", s_tab, s))
            if v is not None
        ),
        missing=missing,
        note=(f"no {phase}-phase value for " + ", ".join(missing)) if missing else "",
    )


def _pick(value_fn: Any, methods_fn: Any, cas: str) -> tuple[float | None, str]:
    """First tabulated (non-estimator) value in the table's own method order."""
    for m in methods_fn(cas):
        if m not in _ESTIMATORS:
            v = value_fn(cas, method=m)
            if v is not None:
                return float(v), str(m)
    return None, ""


def _dgf(
    h: float | None, s: float | None, atoms: dict[str, int], T: float
) -> float | None:
    if h is None or s is None:
        return None
    try:
        s_elem = sum(_ELEMENT_S_PER_ATOM[el] * n for el, n in atoms.items())
    except KeyError:
        return None
    return float(h - T * (s - s_elem))
