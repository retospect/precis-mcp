---
status: draft
title: se nucleic acids — helix/strand/domain model, loops, relax, register + clash DRC, ViennaRNA, dna realization, scadnano/oxDNA/PDB export
prio: high
model: opus
blocked-by: precis-chain-kernel
---

# `se` nucleic acids — DNA/RNA design in the block tree

## Motivation / why

The repo has zero nucleic-acid modelling (grep `nucleic|origami|Hoogsteen`
over `src/` → nothing; `protein` in `src/precis_bio/` is sequence→AlphaFold
only). The light-driven-walker research line and any origami/aptamer work
need DNA/RNA at four altitudes — tube with bend/twist limits → helix/loop/
strand layout → sequence, fold, pairing geometry → atoms per region — and
they must be usable **without** a walker. `se` already has the ladder: six
levels (`src/precis_se/__init__.py`), atomic mode realizing a block as
chemistry and binding a `structure` (`src/precis_se/atomic/generate.py`,
`atomic/bind.py`), per-block L2 jsonb columns (`dof`, `chromophore`), pair
relations in `se_topology` (`threading`), and the `formfind` precedent for
solver poses written back as `origin='proposed'`. This binds the
`precis_chain` kernel to that ladder.

Decomposition (scadnano's, chosen 2026-09-27): **helix carries geometry**
(path, motif, per-bp frames, tube); **strand routes** (sequence + ordered
domains `(helix, forward, start, end)`); **loop** = the ssDNA gap between two
domains, pinned at two backbone exit points, nucleotide count free; pairing is
**derived** from co-occupancy of a helix offset, never declared. Crossover =
0/1-nt loop between adjacent helices; foothold/toehold = single-occupied
domain; ssDNA scaffold region = single occupancy; hairpin = one strand, two
antiparallel domains on one helix + a loop.

## In scope

**Storage** — migration `src/precis_se/migrations/0015_se_chain.sql`:
`se_blocks.chain jsonb` (helix: `{role:'helix', motif, path:{waypoints_m |
lattice:{kind,row,col}}, n_units, phase0, register:{lattice, insertions,
deletions}, min_bend_radius_m?, min_gap_m?}`; strand: `{role:'strand',
sequence, nucleic:'DNA'|'RNA'}`); `se_topology.kind` CHECK →
`('threading','domain')` + a partial unique index `WHERE kind='domain'` on
`(ref_id, subject_name, (meta->>'ord'))`; domain row `subject_name`=strand,
`object_name`=helix, `meta={ord, forward, start, end, geometry?, overrides?,
loop_before_nt?}`. `SeBlock.chain`, `SeTree.domains`; `persist.py` gains the
column, a `_DOMAIN_COLS` (with `meta` — `_THREADING_COLS` has none) and a
kind-filtered load alongside threading.

**Ops, pure** (`precis_se/ops.py::_OPS`; vetting `precis_se/chain/vocab.py`):
`declare_helix`, `declare_strand`, `add_domain`, `remove_domain`
(destructive by the `remove_` prefix rule), `clear_chain`, `layout_chain`
(materialises child segments `<helix>.s<k>`: `cyl:r<..>nmh<..>nm` envelopes
from `precis_chain.envelope.capsule_pose`, ports `5p`/`3p`; re-run retires
and regenerates, `origins['envelope']='proposed'`; default `max_seg_len` =
one lattice repeat, so a 7 kb design yields a few hundred children;
straight lattice helices may use `array_block` and `pattern-groups.md` is the
collapse mechanism). Lengths parsed at the boundary through the existing
units path; stored metres/radians.

**Ops, handler-level** (`precis_se/atomic/apply.py::HANDLER_LEVEL_OPS`,
human-Apply gated because they spend compute, read the store, or need an
optional dep): `relax_chain` (segment rigid bodies + hinges + loops +
crossover pins → `precis_chain.relax.relax_bundle`, Lp from `material` rows
via the `compose.py::LP_KEY` path; poses back as `origin='proposed'`,
user-origin poses move only under `move=`; one revision per call),
`fold_layout` (ViennaRNA dot-bracket → helix/strand/domain ops; lazy import,
`Unsupported` when absent; scaffold-length input allowed here only),
`realize_chain` (see Atomic realization). None of these run in
`design_turn`'s pure dry-run, so no double execution.

**Derived pairing** — `precis_se/chain/pairing.py::derive_pairing(tree)`: per
helix per offset the occupying (strand, domain, forward); exactly two
antiparallel occupants → pair; one → single-stranded; two parallel or three
or more → `chain.occupancy` error (triplex is out of scope for this cut).
O(total domain length), not O(n²).

**Constants** — `precis_se/chain/nucleic.py`, sources in the docstring:
B-DNA 0.334 nm / 34.3° (10.5 bp/turn) / r 1.0 nm; A-RNA 0.28 nm / 32.7°
(11 bp/turn) / r 1.15 nm; ssDNA contour 0.63 nm/nt; honeycomb 7 bp,
21 bp/2 turns; square 8 bp, 32 bp/3 turns; inter-helix centre spacing
2.5 nm default for both lattices (scadnano's `helix spacing`; measured
2.4–2.6 nm, Douglas 2009 / Ke 2009 — cite in docstring); oxDNA unit
0.8518 nm; Leontis–Westhof 12 families = 6 unordered edge pairs of
`{W,H,S}` × `{cis,trans}`, encoded `"W-W-cis"`, `"W-H-trans"`, … with
aliases `WC`, `wobble`, `reverse-Hoogsteen`, `mismatch` and
`ALLOWED_PAIRS[geometry]`. Persistence lengths (dsDNA 50 nm, dsRNA 60 nm,
ssDNA 2 nm) are `material` rows with conditions; **pure seams use the coded
default only**, store-read values reach `relax_chain` and the handler-side
findings. Default dsDNA min bend radius Lp/5 = 10 nm at **warn**; an
authored `min_bend_radius` promotes to error.

**Findings, pure** — `precis_se/chain/drc.py::findings(tree)`, one call in
`precis_se/drc.py::drc`: `chain.bend` error · `chain.twist_register` warn ·
`chain.clash` warn (kernel capsule pass over all segments; tol = `min_gap`;
skips consecutive same-helix segments and pairs sharing a crossover) ·
`chain.loop_short` error (`|exit_a − exit_b| > (n+1)·c + tol`; a 0-nt
crossover therefore requires exits within one bond — this IS the register
check) · `chain.loop_slack` info · `chain.floppy` info (single-stranded span
> coded Lp default) · `chain.dangling_domain` error · `chain.occupancy` error
· `chain.pairing_geometry` error.
**Findings, handler-side** — appended in `handler.py::_render_drc` after the
pure pass, like `se_precedent.findings(store, tree, ref_id)`: when a
`material` Lp row exists, the pure `chain.floppy` rows are dropped and
re-emitted against the row's value (one row per span, never two);
`chain.fold_disagree` warn and `chain.offtarget` warn from
`precis_se/chain/fold.py` — fold checks bounded to strands ≤ 200 nt
(`RNA.fold` is O(n³)); off-target via a 6-mer hash index over all strands,
O(total length); `chain.fold_unavailable` info when `RNA` is absent.
**Exclusion** — `precis_se/validate.py::envelope_overlaps` skips every
segment↔segment pair (chain.clash owns them); the SDF budget goes to
chain-vs-non-chain pairs.

**Views** — `view='chain'` (helices: motif, n, turns, segments, occupancy;
strands: length, domains, loops; pairing table), `view='export'`
(`args={format}`), `view='topology'` gains domain rows; both new views
registered in `handler.py::_VIEW_ARGS`.

**Atomic realization** — generators are pure `params → GeneratedBlock`
(`generators/__init__.py::Generator`) and `prepare_generate` always mints a
NEW block, so a region of an existing helix cannot go through
`GENERATORS`. New handler-level op `realize_chain(block, start, end,
fidelity:'backbone'|'allatom', sites:[…])`: builds coords from
`precis_chain.fibre.unit_frames` × per-base Arnott fibre templates
(`precis_se/chain/atoms.py`), bonds intra-nucleotide + O3'–P, no H-bonds as
bonds; follows the `prepare_generate`/`finish_generate` split
(`PendingRealizeChain`: coords + ports prepared inline, `store.structure_save`
+ `atomic/bind.py::bind_structure` only after the caller's `save_tree`, so a
failed tree save never strands a minted structure). Ports payload — `5p`/`3p`
with `axis_atom`/`phase_atom`, attachment ports `n<i>.c5m|maj|min` for listed
`sites` only. The structure binds to the **segment child** whose range covers
the region (one region per segment in this cut), so `envelope_fit` checks
the segment's own envelope against its atoms; the helix parent carries none.
Loops realized only after `relax_chain`. A standalone
`GENERATORS["dna"]` (sequence in, straight duplex out, no tree) is kept for
the hairpin/junction cases. `precis/structure/export.py::to_pdb(scene)`
adapter over `precis_chain.pdb.write_pdb`; `structure` `view='pdb'`.

**Export** — `precis_se/chain/export.py`: scadnano JSON (helix→helix,
domain→domain), caDNAno legacy (lattice designs only, else `Unsupported`
naming the gap), oxDNA `.top`+`.conf`, PDB per realized region.

**Skill + docs** — `src/precis/data/skills/precis-se-chain-help.md`; op rows
in `precis-se-help.md`; `precis-overview.md` se row appends the skill;
glossary entries (done).

## Explicitly NOT in scope

- Walkers, stations, occupancy states, light transitions, spectral budget,
  make-tree protocol (→ `se-walker-light-protocol`).
- Protein import (→ `se-protein-chain-import`).
- Triplexes and parallel duplexes: reported as `chain.occupancy`, not
  modelled (needs a third backbone azimuth and a geometry on the third
  domain).
- A new binding kind. Tube geometry is derived from the block's own `chain`
  declaration; L3 stays a bound `structure`. Reopen only if oxDNA-relaxed
  conformers must be stored.
- Running oxDNA/CanDo; whole-origami atoms (≈470k atoms for 7 kb) — atoms per
  region only.
- Thermodynamics, melting, salt dependence, knot/threading detection —
  `relax_chain` is a mechanical settle; stated in the skill.
- Fold checks on strands > 200 nt outside `fold_layout`.
- Constraint-composed paths (solver choosing unit counts); insertion/deletion
  global twist check (register hook reserved); backbone-ribbon rendering in
  `precis.viz3d`.

## Acceptance criteria

- Hairpin `GGGGAAAACCCC`: `fold_layout` → 1 helix (4 bp), 1 strand, 2
  domains, 4-nt loop; `derive_pairing` → 4 `W-W-cis`; `GENERATORS["dna"]`
  realizes it; `to_pdb` text parses back via `precis_chain.pdb.read_trace`.
- 4-way junction (4 helices, 4 strands, 8 domains, 4 zero-nt crossovers at
  register-correct offsets) authored in one `put`; `view='drc'` has no
  `chain.*` error; shifting one crossover by 1 bp fires `chain.loop_short`.
- Rectangle origami built procedurally in the test (24 helices × 256 bp
  square lattice, scaffold + ~100 staples, ≈192 segments): `layout_chain`
  < 2 s; `relax_chain` < 30 s; `view='drc'` < 5 s with zero
  `overlap_budget_exceeded` and zero calls to
  `precis_se.validate.cad_relate.clearance` (monkeypatched) where both names
  match `<helix>.s<k>`; scadnano export parses, every domain appears exactly once;
  oxDNA `.top` neighbour lists form one path per strand, `.conf` line count =
  nucleotides.
- 21 bp honeycomb range passes, 22 bp fails `chain.twist_register`; parallel
  helices at 2.0 nm centre spacing → `chain.clash`, at 2.5 nm → clean.
- Loop of 3 nt across 3 nm → `chain.loop_short`; 20 nt across 3 nm →
  `chain.loop_slack`.
- Relax: two helices joined by a 2-nt loop from 6 nm settle to exit-to-exit
  ≤ 1.9 nm; poses read back `origin='proposed'`; a `user` pose is unchanged
  without `move=`; `relax_chain` appears in `HANDLER_LEVEL_OPS` and is a
  proposal in the web turn.
- `realize_chain(helix, 0, 21, 'allatom')` binds a `structure` to the
  segment child covering 0–21, deferred until after `save_tree` (a forced
  save failure leaves no `structure` row); theorems from coords: P–P 6.6–7.2 Å along a strand;
  rise 3.34 ± 0.02 Å; C1'–C1' 10.4–10.8 Å; minor/major groove 12/22 ± 1 Å
  from P positions; 21 bp regains phase (frame x dot ≥ 0.98);
  `validate_atomic` reports no `bond_length_sanity`; `envelope_fit` clean;
  `5p` port has a measured `rot`.
- `RNA` unimportable → `view='drc'` still renders with one
  `chain.fold_unavailable`; installed → hairpin MFE `((((....))))` and a
  planted 8-nt staple–staple complement → `chain.offtarget`; a 6 kb scaffold
  is skipped by the fold check with its length named.
- Three strands on one offset → `chain.occupancy`.
- Declared `geometry:'W-H-trans'` on a domain whose letters cannot pair
  that way → `chain.pairing_geometry`.
- All new lengths refuse bare numbers (`UnitRequiredError`) and store metres.

## Target + blast radius

`se` handler (`src/precis_se/handler.py` views drc/topology/chain/export,
`_VIEW_ARGS`), `precis_se/ops.py`, `persist.py`, `validate.py`, `drc.py`,
`atomic/apply.py::HANDLER_LEVEL_OPS` + `atomic/generate.py` (new
`realize_chain`), new `precis_se/chain/`, se-plugin migration 0015,
`GENERATORS`, `precis/structure/export.py` + `structure` handler view,
`pyproject.toml` (`[chain]` extra = ViennaRNA), skills `precis-se-chain-help`
/ `precis-se-help` / `precis-overview`. No worker, no web route. Deploy: the
`[chain]` extra must be added to the MCP serve role and the UV_WITH bridge
(`docs/backlog/mcps-venv-deploy-gaps.md` class) or fold checks silently
report `fold_unavailable` in prod.

## Open questions / decisions log

- 2026-09-27 helix-carries-geometry over strand-carries-geometry: a scaffold
  snakes across every helix, so a "scaffold tube" is meaningless; scadnano
  export becomes near-identity. Decided.
- 2026-09-27 relax A + B in the first cut: bend/clash on authored (straight)
  origami geometry is vacuous; settled geometry is what the checks must see.
  Decided.
- 2026-09-27 ViennaRNA as `[chain]` extra + lazy import + `Unsupported`;
  promote to core when the wheel resolves on every CI/deploy platform.
  Decided, revisit at ship.
- 2026-09-27 rectangle fixture procedural in the test. Decided.
- 2026-09-27 (review) loop contour (n+1)·c and the register check IS
  `chain.loop_short` at n=0; `relax_chain`/`fold_layout`/`realize_chain` are
  handler-level; region realization needs `realize_chain` because generators
  are tree-blind; triplex out; segment↔segment pairs excluded from the SDF
  scan wholesale (connects do not save the broad phase). Decided.
- OPEN: per-position geometry `overrides` vs a 1-bp domain — overrides
  (proposed; keeps domain count = scadnano domain count).
- OPEN: inter-helix spacing one constant vs per-lattice — one (2.5 nm)
  proposed, per-design `min_gap` overrides.
