---
status: in-progress
title: a balanced reaction returns its per-step ΔH, ΔG and E° from tabulated thermochemistry, with an xTB fallback for missing species
pillar: 3d-design
prio: normal
---

# Reaction energetics ledger (T0 of the nanoreactor chain)

Slice 1 (shipped: tabulated ΔH/ΔG/E° and the pathway ledger via `get(kind='rxn',
view='energetics')`, in `precis.thermo`; the dependency `chemicals`) is
described by that package's docstring and `precis-rxn-help`. What remains:

## Motivation / why

A step that is uphill at the thermodynamic level needs a drive, whatever its
barrier ([nanoreactor-no-nh3](nanoreactor-no-nh3.md)). The tabulated data has
holes exactly where the nanoreactor path lives: NH2OH has no S°, and the
radical intermediates H2NO and HNOH are absent. The ledger reports those as
`unavailable` and withholds ΔG; this item fills them without guessing.

## In scope

- Fallback for a species with missing or partial data: GFN2-xTB via `tblite`
  (already a dependency) and ASE `IdealGasThermo` (already a dependency),
  from a SMILES or xyz input. It is labelled `method=xtb-idealgas` and never
  mixed silently with tabulated values; the reaction total states which method
  each term came from. Decide how ΔHf for radicals is obtained (atomisation
  against tabulated atoms, or an isodesmic reference) before building.
- Acceptance: `NO + 3/2 H2 -> NH2OH` returns a ΔG, with NH2OH's entropy marked
  `xtb-idealgas`; the full pathway NO → HNO → H2NO → NH2OH → NH3 + H2O returns
  the cumulative ledger with no `unavailable` term.
- An entity read can store results as `rxn` property rows
  (`property='dG_rxn'`, `method='tabulated'|'xtb-idealgas'`), which needs the
  `_METHODS` vocabulary extended (`tabulated`, `xtb-idealgas`).

## Explicitly NOT in scope

- Transition states and barriers: catpath owns those (T1).
- Solution-phase or electrode-referenced free energies beyond E° from ΔG.
- DFT-level thermochemistry.
- Heat-capacity correction away from 298.15 K (the shipped slice reuses the
  298 K values and says so).

## Target + blast radius

`precis.thermo` gains a computed-data module; `handlers/rxn.py` gains optional
store-backed rows and the method values. No new dependency.
