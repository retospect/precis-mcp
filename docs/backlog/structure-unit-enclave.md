---
status: ready
title: structure unit-enclave compliance — name the Å, convert at the API, no conversion
prio: high
---

# structure unit-enclave compliance

Supersedes `structure-si-cutover.md` (deleted; findings in its git
history). The 2026-09-12 `/ready` vet falsified that item's premise:
`structure/{ops,relax,preflight,export}.py` and the cathub importer are
woven through ASE (Å/eV-native `Atoms`, EMT calculator, FIRE/BFGS
optimizers, `neighbor_list`, cell filters) — forcing SI-m internals would
add a conversion at every ASE call, multiplying seams instead of deleting
them. Reto ruled 2026-09-12: structure stays Å-native as a **unit
enclave** (glossary), and this item makes it *compliant* with the enclave
rule from `units-policy-cutover.md`: every identifier self-names its
unit, every cross-package API converts to SI.

## In scope

1. **Declare the enclave.** `src/precis/structure/` package docstring +
   each module docstring state the Å/eV convention and the boundary
   sentence: conversion to SI happens only at design-side seams
   (`precis_nm`'s design↔atomistic boundary, owned by the units chain)
   and file serializers.
2. **Self-naming, additive-only.** Public Å-valued functions/constants
   gain `_A`-suffixed names as ALIASES (`covalent_radius_A =
   covalent_radius`; same for other cross-package Å surfaces found by
   audit). Bare names stay importable — `precis_nm` (frozen under the
   units window) imports them; the nm round adopts the `_A` names, after
   which bare aliases can be dropped (leave a one-line note in the units
   spec chain-state). Dataclass fields (`Atom.coords`, `Cell` lattice)
   are NOT renamed — their class docstrings declare Å.
3. **Consumer Å literals → named constants.** `precis_bio/converge.py`
   (`BOX_PADDING = 15.0` Å → `BOX_PADDING_A`) and
   `precis_web/routes/structure.py` (0.37 Å Pauling bond-order decay,
   the ~0.05 eV/Å force threshold) get `_A`/`_eV_per_A`-suffixed named
   constants with a one-line unit comment. No behavior change.
4. **`_m` accessors only where consumed.** Audit for design-side
   consumers converting Å→m by hand today; where found, add a
   `fit_classes.hole_m`-style property/helper at the structure API and
   switch the consumer. If none exist outside frozen `precis_nm`, add
   none — the nm round adds its own at its seams.
5. **Exempt-list handoff.** A short "enclave floor" file list (structure
   modules + atomistic consumer files) written into
   `units-cutover-dossier.md` §2 so the units chain's AST gate test
   exempts them from the no-Å-literal rule.
6. Skill `precis-structure-help`: one enclave paragraph.

## Explicitly NOT in scope

- ANY unit conversion, data migration, or CIF importer.
- `precis_nm` edits (frozen; units chain owns its seams — vet showed the
  coupling is wider than the 4 named sites, but the extra imports
  (vsepr, `probe.angle`, `covalent_radius` in generators) are
  atomistic-INTERNAL and stay Å; seams exist only where design-m meets
  atom coordinates).
- `precis_estimate` / `precis_pathway` (atomistic side of the enclave;
  their `ase.data` usage is already unit-honest).

## Acceptance criteria

- Additive-only: no existing import breaks; `scripts/test --impacted`
  green with zero test-expectation changes.
- Grep: no bare Å-magnitude literal without an `_A`-named home in
  `precis_bio/converge.py` / `precis_web/routes/structure.py`.
- `_A` aliases importable and typed; mypy `src tests` + ruff clean.
- Docstrings: package + every structure module carries the enclave
  declaration; dossier §2 carries the exempt list.

## Target + blast radius

`src/precis/structure/**` (docstrings + aliases in `elements.py` et al.)
· `src/precis_bio/converge.py` · `src/precis_web/routes/structure.py` ·
`docs/backlog/units-cutover-dossier.md` §2 · skill
`precis-structure-help`. No stored-data impact.

## Decisions log

- 2026-09-12 Reto: option (a) enclave-compliance over (b) force-m
  (vet evidence: ASE-woven core; seams would multiply). Enclave rule +
  grants recorded in `units-policy-cutover.md` decisions log and
  `docs/glossary.md`.
