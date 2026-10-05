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
  (kJ/mol, tolerance 2): N2 945, H2 436, O2 498, H2O 927, NH3 ~1172.
  OH uses the authoritative pinned NASA-TM-4513/TPIS78 result
  427.8239465937423 kJ/mol with arithmetic tolerance (Reto/delegate ruling).
  NO + 5/2 H2 -> NH3 + H2O: ΔH≈-378.5, ΔG≈-332.0, E≈0.69 V (n=5).
- Pathway fixtures show actual balancing partners: NO + 1/2 H2 -> HNO;
  HNO + 1/2 H2 -> H2NO; H2NO + 1/2 H2 -> NH2OH;
  NH2OH + H2 -> NH3 + H2O. No invented partners for bare impossible arrows.
- Focused canonical scripts/test, scoped container typecheck, Ruff/diff checks;
  source overlays are explicitly development evidence, never release gates.
  Root owns shared rebuild/full suite/deploy/version bump; builder publishes
  exact review candidates under the authorized origin visibility rule.

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

**R13 data (historical):** nineteen unchanged NASA-7 fits from Cantera v3.2.0 conversion of
NASA-TM-4513 (1993): seventeen neutral gas identities, liquid H2O and graphite
as the carbon reference. Full URLs, SHA256 and per-species metadata are in
`src/precis/thermo/nasa7.json`; original notice is `thermo/NOTICE`.
NTRS original report/API explicitly identifies a US-government work with
public use permitted; its standard-state section specifies 1 bar. Cantera
BSD conversion notice is preserved separately, without extending that code
licence to third-party thermochemical data. No Cantera runtime or chemicals
dependency remains. Verified NIST pages establish nine CAS identities;
remaining CAS are explicitly unavailable, rather than invented.

**R13 coverage gap (superseded for the authorized R14 subset below):** actual
Alzueta/Glarborg-2023 YAML has H2NO and HNOH, but
Cantera example-data README explicitly disclaims granting mechanism rights.
The original Burcat-2005 report forbids commercial use of database parts
without written author permission. Their coefficients are not vendored;
dependent pathway totals remain unavailable. HNOH's source note additionally
says trans & equilibrium, requiring an explicit identity/ensemble decision.

**Accepted source criterion (2026-10-05):** Reto/delegate accepted T0 for R13
and superseded the rough newer ATcT OH target of 430±2 with the pinned
NASA-TM-4513/TPIS78 result, 427.8239465937423 kJ/mol. Unchanged coefficients
remain verified against the frozen oracle; the OH regression now uses only
arithmetic tolerance and has no expected-failure marker.
R13 NASA NH2OH TPIS89 ΔHf298=-49.9997151 kJ/mol differs from the inspected
ATcT/A mechanism fit (-43.94975) and the prototype's chemicals table (-43.48);
the older source is labelled and never silently updated.
NO hydrogenation: ΔH=-379.03295, ΔG=-332.56622 kJ/mol, E=0.689361 V (n=5).

**Validation evidence:** frozen fixtures were evaluated directly from original
source files by Cantera 3.2.0 in a bounded scratch uv overlay, without Precis
imports. They are development source-oracle evidence. Canonical focused tests
and actual public FastMCP route use `scripts/test` without UV_WITH; removing
chemicals resolves the prototype's missing-core-dependency problem in the
existing image. Root still owns the locked-image reproducibility/full gate.

Historical focused canonical run before the criterion ruling: 165 passed,
1 strict xfail (now superseded by the accepted OH regression);
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

## Authorized R14 radical subset (Reto, 2026-10-05)

Separate branch `work/nanoreactor/t0-radicals`, based on R13's accepted OH
criterion follow-up. R13 source coefficients and freeze stay untouched.
Use the actual published Glarborg et al. (2018) nitrogen-mechanism NASA-7
records for H2NO/HNOH; include NH2OH/HNO/NH2 only where absent or the
source provides a documented newer fit. Vendor only these needed records,
not a complete mechanism or Burcat database. Pin the original retrieval
URL/content hash, publication DOI, original per-species reference notes,
phase, charge and valid T ranges. HNOH's exact source-defined conformer or
ensemble must appear in its label and notes; no inferred CAS or identity.
The notice must state extraction from a published mechanism, no explicit
licence grant from the authors, academic use and citation. This authorized
academic-use choice supersedes the R13 permission gap only for this subset;
it is not an asserted general redistribution licence.

Verify frozen values independently from the original files. Extend the
partners-explicit NO-to-NH3 ledger end to end through H2NO and HNOH, without
introducing N2 fixation or a bare-arrow invented partner. Correct the
nanoreactor's old Joback/prototype numerical discussion from this ledger.
Retain honest unavailable handling for other identities/phases. No new
runtime dependency or scientific campaign. Focused canonical thermo/rxn
checks, scoped container types, Ruff/diff; no unscheduled full suite/image
rebuild. Push the exact candidate to `origin/work/nanoreactor/t0-radicals`,
record receipts in `inbox/nanoreactor-radicals-ready.md`, and request root's
independent Codex review. P3 extreme-n arithmetic remains deferred.

### R14 source decision and numerical evidence

Actual original CHEMKIN `thermo.dat` from the Glarborg-2018 mechanism mirror,
revision `7c39629e45bda071d9355818169946a30d777017`, SHA256
`46399906982b30923833febeba96ae89720efbc19a7332fcff651179aac8d666`.
Paper: Glarborg/Miller/Ruscic/Klippenstein, *Modeling nitrogen chemistry in
combustion*, PECS 67 (2018) 31–68, DOI `10.1016/j.pecs.2018.01.002`.
The companion original mechanism names the review; its prepublication
header year says 2017, while DTU publication metadata verifies 2018.
Exact record lines, original reference comments, source codes, URL and
full hash are retained in `nasa7.json`; only five source records are vendored.

- H2NO: neutral H2N–O radical, T09/09, Goos/Burcat/Ruscic with ATcT updates,
  accessed July2013; 200/1000/6000 K.
- HNOH: exact active source label "trans & Equ T11/11", same reference;
  200/1000/6000 K. The commented-out cis fit is excluded; no guessed
  pure-trans/cis assignment or equilibrium weights.
- NH2OH: ATcT/A, same original database reference; 200/1000/6000 K.
  Replaces older TPIS89; ΔHf298=-43.9497496 kJ/mol, S298=236.1800801 J/mol/K.
- HNO/NH2: ATcT1.122 JAN17, replacing older NASA fits; HNO200/1000/6000 K,
  NH2**200/1000/3000 K**, respecting the original shorter range.

NOTICE explicitly says values extracted from a published mechanism, no
explicit licence grant from the authors, academic use, cited. This is the
authorized use decision, not a claim that permission was found. The ledger
adopts the same 1 bar convention; the CHEMKIN file does not explicitly
encode reference pressure. No entropy constants or source coefficients
were adjusted. This disclosed reference convention remains a review limit.

Frozen source values are evaluated independently with Cantera3.2.0 from
the upstream YAML species functions, checked coefficient-for-coefficient
against original CHEMKIN records, without Precis imports or a kinetics run.
At298.15 K the H2NO steps have ΔG +32.154671, −23.352317, −93.160149,
−248.208420 kJ/mol, cumulative −332.566215 kJ/mol. The HNOH alternate
second/third steps are +9.651588/−126.164054 kJ/mol; it has an additional
uphill second step. Both sums match unchanged overall NASA endpoints.
NO-to-NH2OH: ΔH=-135.218339, ΔG=-84.357795 kJ/mol; cleavage:
ΔH=-243.814610, ΔG=-248.208420 kJ/mol. The nanoreactor discussion now uses
these source values and excludes the Joback NH2OH estimate (-161.8).

Focused canonical thermo/rxn validation: 183 passed, no xfails; actual
FastMCP/core.get exercises both complete partners-explicit radical paths.
Unknown NOH identity still withholds dependent cumulative totals. Root's
independent review, locked image and full ship gate remain required.
