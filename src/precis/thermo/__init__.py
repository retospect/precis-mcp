"""precis.thermo — tabulated reaction thermochemistry. Pure: no DB, no network.

Answers "how much energy does this balanced reaction release, and how is it
split across steps?" from tabulated standard-state data (the ``chemicals``
package: ΔHf° and S° at 298.15 K, ATcT/TRC/others; its data ships in the
wheel, so tests and the cluster need no network).

Modules:

* :mod:`~precis.thermo.equation` — parse ``NO + 5/2 H2 -> NH3 + H2O`` into
  species, coefficients and phase hints; check element balance.
* :mod:`~precis.thermo.data` — one species → ΔHf°, S°, ΔGf° with a source
  label (``tabulated``) per quantity, or an explicit ``unavailable``.
* :mod:`~precis.thermo.ledger` — reaction ΔH, ΔS, ΔG(T), E°, and the
  per-step plus cumulative pathway ledger with uphill flags.

Why it is shaped this way:

* **Never guess.** A missing ΔHf° or S° (NH2OH has no S°; H2NO and HNOH are
  absent) makes that term ``unavailable`` and the dependent totals
  ``unavailable``, naming the species and quantity. Nothing is estimated and
  nothing silently mixes methods. A computed fallback (xTB + ideal gas) is a
  later slice and will be labelled by its own method, never ``tabulated``.
* **Temperature.** ΔHf° and S° are 298.15 K values. At another T the ledger
  still uses them (ΔG = ΔH − TΔS) and says so; no Cp correction is applied.
* **Phase.** Gas by default; ``(l)`` selects the liquid-phase tables. A
  phase the tables lack is ``unavailable``, not silently swapped for gas.

Surface: ``get(kind='rxn', view='energetics', q=<equation(s)>, args={'T': ...,
'n_electrons': ...})`` via ``precis.handlers.rxn`` — stateless, no id. The
``chemicals`` import is lazy (inside functions) to keep cold start light.

See ``precis-rxn-help``.
"""

from __future__ import annotations

from precis.thermo.equation import Equation, Term, parse_equation
from precis.thermo.ledger import (
    F_CONST,
    PathwayLedger,
    ReactionResult,
    SpeciesResult,
    pathway_ledger,
    reaction_energetics,
)

__all__ = [
    "F_CONST",
    "Equation",
    "PathwayLedger",
    "ReactionResult",
    "SpeciesResult",
    "Term",
    "parse_equation",
    "pathway_ledger",
    "reaction_energetics",
]
