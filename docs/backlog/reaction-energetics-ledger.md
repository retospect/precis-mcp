---
status: draft
title: a balanced reaction returns its per-step ΔH, ΔG and E° from tabulated thermochemistry, with an xTB fallback for missing species
pillar: 3d-design
prio: normal
---

# Reaction energetics ledger (T0 of the reaction-tunnel chain)

## Motivation / why

precis has no thermochemistry. Asking "how much energy does
2 NO + 5 H2 → 2 NH3 + 2 H2O release, and how is it split across steps?"
needs a scratch script today. That question is the first gate of
[no-nh3-reaction-tunnel](no-nh3-reaction-tunnel.md) and of every catalysis
quest: a step that is uphill at the thermodynamic level needs a drive,
whatever its barrier. The numbers in the tunnel item came from a scratch
run of the `chemicals` package (CalebBell, MIT licence). It covers NO, H2,
NH3, H2O, HNO and H, but has no S° for NH2OH and nothing for H2NO or HNOH.

## In scope

- Dependency: `chemicals` (tabulated ΔHf, S°, Cp: ATcT, TRC, others).
- A pure function in a new `precis.thermo` module: equation string, T,
  phase hints → per-species ΔHf/ΔGf with source, and reaction ΔH, ΔG, and
  E° = −ΔG/(nF) when `n_electrons` is given. It also takes a list of
  equations (a pathway) and returns a cumulative ledger, marking the
  uphill steps.
- Fallback for a species with missing or partial data: GFN2-xTB via
  `tblite` (already a dependency) and ASE `IdealGasThermo` (already a
  dependency), from a SMILES or xyz input. It is labelled
  `method=xtb-idealgas` and never mixed silently with tabulated values.
  The reaction total states which method each term came from.
- MCP surface: `get(kind='rxn', view='energetics', q='<equation>',
  args={'T': 298.15, 'n_electrons': 5})`, which is stateless and needs no
  entity. An entity read can also store results as `rxn` property rows
  (`property='dG_rxn'`, `method='tabulated'|'xtb-idealgas'`).
- Skill text in `precis-rxn-help`.

## Explicitly NOT in scope

- Transition states and barriers: catpath owns those (T1).
- Solution-phase or electrode-referenced free energies beyond E° from ΔG.
- DFT-level thermochemistry.

## Acceptance criteria

- `NO + 5/2 H2 -> NH3 + H2O` at 298.15 K returns ΔH −378.5 ± 1 and ΔG
  −332.0 ± 1 kJ/mol, with the source per species. With `n_electrons=5`
  it returns E° ≈ 0.69 V.
- `NO + 3/2 H2 -> NH2OH` returns a ΔG, with NH2OH's entropy marked
  `xtb-idealgas`.
- A pathway input (NO → HNO → H2NO → NH2OH → NH3 + H2O, H2 as the H
  source) returns the cumulative ledger and flags NO → HNO as uphill.
- An unbalanced equation is `BadInput`, naming the element that does not
  balance.
- Tests run without network access (the `chemicals` data ships in the
  wheel).

## Target + blast radius

New `src/precis/thermo/`. `handlers/rxn.py` gains a view. Skill
`precis-rxn-help`. Adds one dependency, `chemicals`.
