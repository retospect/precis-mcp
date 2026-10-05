---
status: in-progress
title: T0 balances neutral reactions and returns source-backed temperature-dependent energetics
pillar: 3d-design
prio: normal
---

# Reaction energetics ledger (T0 of the nanoreactor chain)

Slice 1 is an unmerged prototype at `7a909900917967a8eb38d1613cfc291c77d21a0a`.
Builder branch: `work/nanoreactor/t0-energetics`. Independent review and root's
serialized image/full gate/version/integration remain required before shipping.
This revision supersedes the prototype's xTB fallback and constant-H/S scope.

## Authorized T0 revision (2026-10-05)

- Auto-balance with an exact nullspace; require a unique, strictly positive
  coefficient vector. Preserve already-balanced scaling. Otherwise normalize
  the first reactant to one, explicitly displaying fractions and the equation;
  supplied electron count applies to that displayed reaction extent.
- Compare current ChemPy source/dependencies with the existing SymPy core
  dependency and a small Fraction implementation before selecting a solver.
- Explicit vetted neutral identities and aliases only. Formula atom counts
  cannot select isomers or charge states. Print resolved name/CAS or explicitly
  unavailable identity on every species row, including pathways.
- Inspect actual pinned NASA-7/Burcat/ATcT and nitrogen-mechanism records and
  their own redistribution terms. Vendor only a small approved subset with
  URL, commit/version, SHA256, species IDs, source notes, method, phase, charge,
  temperature intervals and licence notice. Public download and a repository's
  code licence do not by themselves license third-party thermochemical data.
- Evaluate H(T), S(T), Cp(T) in repo; no Cantera/RMG runtime, Joback estimate,
  silent extrapolation or invented missing data. Report uncovered species and
  data permissions concretely. Decide whether chemicals is still necessary.
- Validate finite positive T/n, reject unsupported pathway electron-count args,
  and retain E=-ΔG/(nF) with reaction normalization and no inferred electrode.
- Public `tools.core.get`/FastMCP schema regression, neutral OH and ether/ethanol
  identity regressions; independent frozen-source checks and atomization targets
  (kJ/mol, tolerance 2): N2 945, H2 436, O2 498, H2O 927, OH ~430, NH3 ~1172.
  NO + 5/2 H2 -> NH3 + H2O: ΔH≈-378.5, ΔG≈-332.0, E≈0.69 V (n=5).
- Pathway fixtures show actual balancing partners: NO + 1/2 H2 -> HNO;
  HNO + 1/2 H2 -> H2NO; H2NO + 1/2 H2 -> NH2OH;
  NH2OH + H2 -> NH3 + H2O. No invented partners for bare impossible arrows.
- Focused canonical scripts/test, scoped container typecheck, Ruff/diff checks;
  source overlays are explicitly development evidence, never release gates.
  No shared rebuild/full suite/deploy/push/version bump by this builder.

### Decisions and coverage evidence

**Solver:** existing core SymPy 1.14.0, lazy nullspace and exact positive
feasibility (simplex). Current upstream release ChemPy 0.10.2 was verified
via GitHub releases API and actual pinned setup/source files; master commit
`749e426f67e7c37793b9a6feb7f7de6b27666d4e` is dated 2026-09-19.
Its pinned setup adds pyodesys, pyneqsys, PuLP, quantities and other
dependencies. Actual comparison returns integer 2/5/2/2 for the NO example,
no solution for H2->O2 and rejects C+O2->CO+CO2 with underdetermined=False.
SymPy's exact vector `(1, 5/2, 1, 1)` is normalized without losing rational output;
positive feasibility distinguishes impossible from nonunique systems.

**Data:** nineteen unchanged NASA-7 fits from Cantera v3.2.0 conversion of
NASA-TM-4513 (1993): seventeen neutral gas identities, liquid H2O and graphite
as the carbon reference. Full URLs, SHA256 and per-species metadata are in
`src/precis/thermo/nasa7.json`; original notice is `thermo/NOTICE`.
NTRS original report/API explicitly identifies a US-government work with
public use permitted; its standard-state section specifies 1 bar. Cantera
BSD conversion notice is preserved separately, without extending that code
licence to third-party thermochemical data. No Cantera runtime or chemicals
dependency remains. Verified NIST pages establish nine CAS identities;
remaining CAS are explicitly unavailable, rather than invented.

**Coverage gap:** actual Alzueta/Glarborg-2023 YAML has H2NO and HNOH, but
Cantera example-data README explicitly disclaims granting mechanism rights.
The original Burcat-2005 report forbids commercial use of database parts
without written author permission. Their coefficients are not vendored;
dependent pathway totals remain unavailable. HNOH's source note additionally
says trans & equilibrium, requiring an explicit identity/ensemble decision.

**Source disagreements:** independent Cantera 3.2.0 evaluation of the pinned
files gives OH->O+H ΔH=427.8239466 kJ/mol, 0.1760534 beyond the requested
430±2 tolerance. Its unchanged TPIS78 coefficients are verified against the
frozen oracle; the requested acceptance is a strict expected failure, not a
PASS. A licensed newer OH fit is still needed to meet that numeric target.
NASA NH2OH TPIS89 ΔHf298=-49.9997151 kJ/mol differs from the inspected
ATcT/A mechanism fit (-43.94975) and the prototype's chemicals table (-43.48);
the older source is labelled and never silently updated.
NO hydrogenation: ΔH=-379.03295, ΔG=-332.56622 kJ/mol, E=0.689361 V (n=5).

**Validation evidence:** frozen fixtures were evaluated directly from original
source files by Cantera 3.2.0 in a bounded scratch uv overlay, without Precis
imports. They are development source-oracle evidence. Canonical focused tests
and actual public FastMCP route use `scripts/test` without UV_WITH; removing
chemicals resolves the prototype's missing-core-dependency problem in the
existing image. Root still owns the locked-image reproducibility/full gate.

Focused canonical run: 165 passed, 1 strict xfail (OH acceptance above);
thermo/renderer statement coverage 96%. Scoped container typecheck and
whole-tree Ruff check/format pass. The actual registered FastMCP call reaches
tools.core.get, extras forwarding, runtime and handler; tests also reject
public T=0/nonfinite n/T and pathway n rather than discarding it.

## Superseded prototype follow-up (retained rationale)

A thermodynamically uphill step needs a drive, whatever its barrier
([nanoreactor-no-nh3](nanoreactor-no-nh3.md)). The prototype's chemicals
tables lacked NH2OH entropy and H2NO/HNOH data. A proposed GFN2-xTB/tblite
plus ASE IdealGasThermo fallback would require a separately established
absolute formation reference (atomization or isodesmic), with its computed
method labelled rather than mixed silently. That alternative remains
deferred, as do stored rxn property rows and new method vocabulary.
The prototype's constant-298-K approximation is replaced by licensed fits.
Transition states/barriers (T1), solution/electrode references and DFT are
still outside T0. This revision changes no DB schema or stored properties.
