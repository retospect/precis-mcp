---
status: draft
title: hexfold integration — the sp² defect-notation library as se's `hexfold_spec` generator
prio: high
model: opus
blocked-by: se-nanobud-graph
---

# hexfold integration

Design session 2026-09-15 (Reto + agent). `hexfold` is a **separate,
independent package** (`../hexfold`, numpy-only, MIT-candidate, meant to
become an interchange format) — a SMILES-like notation for curved sp²
carbon: defect placement on a hexagonal lattice rather than atom lists.
Its spec is `../hexfold/SPEC.md`; this file covers only the precis side.
Sibling: `se-nanobud-graph.md` (owns embed/`geo`/register in
`precis/structure/georelax.py`, which this item consumes, never
duplicates).

## Seam — one dataclass, one adapter

hexfold owns **topology** (`Net`: atoms as path IDs, bonds, ring ports,
regions, `Report`); precis owns **geometry and storage**. Dependency is
one-way (`precis_se → hexfold`); hexfold never imports precis. No new
kind, verb, or migration for v1.

- `pyproject.toml`: `[tool.uv.sources] hexfold = { path = "../hexfold",
  editable = true }` for dev; git/PyPI pin for deploy (catpath precedent).
- `precis_se/atomic/generators/hexfold_spec.py`: `params={"spec": "<.hx
  text>", "dry_run"?: bool}` → `hexfold.build(strict=True)` → `Net` →
  `GeneratedBlock`. Ordinals from the path↔ordinal map become
  `atom_index`; `topology` carries the spec text, the canonical JSON,
  regions, ring ports, `report.to_dict()`; `desc` = provenance.
  `BuildError` → `BadInput(report.render())`. `dry_run` runs
  `check(geometry=True)` and returns the rendered report as the echo,
  minting nothing — the agent's edit/check loop.
- Geometry: hexfold `stick` coordinates are the **seed**; precis's ladder
  (`geo` → `emt` → `ml`) is the physics. If a seed is present, skip
  `embed_from_graph`. Loud error above the dense-`eigh` atom bound.
- **One-scene rule**: everything joined by `fuse` is one connected net →
  exactly one `structure` design bound to one se block; module
  decomposition survives as `topology` metadata, never as separate
  scenes (a Scene cannot hold a bond whose endpoints live in two
  designs). Disconnected nets → one block each. `bond`-attached foreign
  fragments *may* stay separate blocks joined by `kind='bond'` connects;
  realization choice recorded in provenance.
- Ring ports: `GeneratedPort` gains `atoms: list[int]`; `atom_index`
  stays the ring's atom 0 for back-compat. Whether `se_ports` needs a
  column is decided here, not in hexfold.
- Skill `precis-hexfold-help`: notation primer, menu table with citations
  (Baowan/Cox/Hill 2010 ref 679; Wang & Li 2009 ref 543 — note the
  survey's "Cases A–D" attribution in `se-nanobud-graph.md` is wrong; the
  primaries name `9-6`/`8-7`), what each `check` code means.

## Acceptance

- `hexfold_spec` builds a `(5,5)` tube + C60 `[2+2]` nanobud end-to-end
  into a `structure` design on the dev DB; byte-deterministic; `dry_run`
  returns a report without minting.
- Adapter test round-trips `Net → GeneratedBlock → Scene` checking atom
  count, bond count, path↔ordinal map — nothing about lattice semantics
  (that is hexfold's suite).
- Follow-on (not this item): `cnt`/`fullerene`/`cone` generators collapse
  into hexfold specs so there is one lattice implementation.

## Decisions log

- Decided: separate package (interchange posture); counting is a
  diagnostic not a gate; lattice-path atom IDs with sublattice; `bond`/
  `fuse` as the two attachment verbs, menus as macros; sextant integer
  orientations; canonicalization by lex-min over ≤6·D frames, mirrors
  excluded; author-declared origin (no auto-select in v1).
- Decided: neck = short `(5,0)`/`(6,0)` zigzag tube fused at both ends
  (Baowan 2010), not a cone; no separate neck builder.
- Open: `ψ` (join angle) as a solvable target — deferred; reported only.
