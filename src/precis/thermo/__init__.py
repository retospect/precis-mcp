"""precis.thermo — balanced, source-backed thermochemistry. No DB or network.

Answers "how much energy does this reaction release across steps?" with a
small pinned NASA-TM-4513 NASA-7 subset, converted by Cantera v3.2.0, plus
five Glarborg et al. (2018) nitrogen-mechanism fits for academic use. H(T),
S(T), Cp(T) use the source intervals and a 1 bar convention; the mechanism
does not explicitly encode reference pressure. See NOTICE and nasa7.json
for original IDs, source notes, hashes and the distinct permission status.
Deferred, not built: P3 extreme-n arithmetic (from the shipped
reaction-energetics-ledger backlog item; git history has the spec).

Modules:

* :mod:`~precis.thermo.equation` — parse ``NO + 5/2 H2 -> NH3 + H2O`` into
  species, coefficients and phase hints; auto-balance an exact nullspace.
* :mod:`~precis.thermo.data` — explicit neutral identity → NASA-7 H/S/Cp,
  formation H/G and source labels, or an explicit ``unavailable``.
* :mod:`~precis.thermo.ledger` — reaction ΔH, ΔS, ΔG(T), E°, and the
  per-step plus cumulative pathway ledger with uphill flags.

Why it is shaped this way:

* **Never guess identity or data.** Molecular formulas cannot distinguish
  isomers or ions. Only vetted source IDs resolve: OH is neutral hydroxyl;
  CH3OCH3 and C2H5OH are distinct. Unknown IDs fail closed. HNOH retains the
  mechanism's literal "trans & Equ" label, without claiming pure-trans/cis
  identity or guessed ensemble weights. Cumulative totals become unavailable
  at the first gap. The prototype's xTB/ideal-gas fallback stays deferred: an absolute
  formation reference must be established before mixing a computed method.
* **Dependencies.** Existing lazy SymPy supplies exact nullspace and positive
  feasibility checks. ChemPy 0.10.2 adds solver/ODE dependencies and defaults
  to integers; minimizing an underdetermined balance would conceal ambiguity.
  Balanced scaling is preserved; auto-balanced equations normalize the first
  reactant to one. n applies to that displayed extent. The prototype's
  chemicals formula lookup could select an isomer or ion and its 298 K tables
  cannot provide these temperature functions, so its core dependency is removed.
  Cantera/RMG are unnecessary runtime dependencies for twenty-one small fits.
* **Academic subset, not a licence inference.** R13 withheld H2NO/HNOH because
  a public download or code licence grants no third-party data permission.
  Reto explicitly authorized academic use of five published Glarborg-2018
  records for R14. Their notice says no explicit author licence grant; original
  Goos/Burcat/Ruscic and ATcT notes remain attached. No whole database is
  vendored. NH2OH/HNO/NH2 replace older NASA fits with the mechanism's newer
  thermochemistry; source and changed coverage (NH2 max 3000 K) are explicit.
* **Reference and temperature.** NASA H is anchored to elemental 298 K zeros;
  formation H/G subtract the same-source elemental H/S at the requested T.
  Balanced reaction sums cancel those references. Modern SI R is used, with
  unmodified coefficients. Out-of-range T is rejected, replacing the
  prototype's disclosed constant-H/S approximation. Fits are not measured
  point values. The superseded NH2OH TPIS89 fit and modern mechanism ATcT/A
  record are different source versions; the latter is not a Joback estimate.
* **Phase.** Gas by default; explicit water(l) uses its liquid polynomial.
  Unsupported phases remain unavailable. Fit coverage includes metastable
  liquid extensions; the ledger does not select phase-equilibrium states.

Surface: ``get(kind='rxn', view='energetics', q=<equation(s)>, args={'T': ...,
'n_electrons': ...})`` via ``precis.handlers.rxn`` — stateless, no id. The
data load and ASE/SymPy imports are lazy to keep cold start light. E=-ΔG/(nF)
does not establish an electrochemical reference or infer electron stoichiometry.

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
