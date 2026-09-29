---
status: ready
title: se nucleic acids — atoms per region (realize_chain) + scadnano/caDNAno/oxDNA/PDB export
prio: high
model: opus
blocked-by: se-nucleic-acid
---

# `se` nucleic acids — atomic realization + interop export

## Motivation / why

`se-nucleic-acid` gives the block tree helices, strands, domains, derived
pairing, relax and the `chain_*` DRC. It stops at the tube/segment level:
no atoms, no file a wet-lab or simulation tool can open. This item is the
third slice its `ready` vet named (2026-09-28): atoms for a chosen region of
a helix, and export of the whole design to the formats the field uses
(scadnano for editing, caDNAno legacy, oxDNA for simulation, PDB per
realized region). It was split off so the first nucleic build lands the
data model and checks without also carrying fibre-atom templates and four
file formats in one unattended pass. Nothing in `se-nucleic-acid` depends
on this item; `se-walker-light-protocol` does (its cursor geometry reads a
`sites` port that only `realize_chain` mints).

## In scope

**Atomic realization** — generators are pure `params → GeneratedBlock`
(`generators/__init__.py::Generator`, reached as `builder(params)` from
`prepare_generate`, which always mints a NEW block), so a region of an
existing helix does **not** go through `GENERATORS` and this item adds no
`GENERATORS` entry. The code path is: new handler-level op
`realize_chain(block, start, end, fidelity:'backbone'|'allatom', sites:[…])`
in `precis_se/atomic/apply.py::HANDLER_LEVEL_OPS`, whose handler-side
implementation (`atomic/generate.py::prepare_realize_chain`) does every
tree read itself — finds the segment child covering `[start, end]` by the
range in its `chain` meta, takes occupancy from `derive_pairing(tree)`,
reads placed loop curves from the domain rows' `meta.loop_curve` — and then
calls one **pure** geometry helper
`precis_se/chain/atoms.py::build_region(units, frames, fidelity, sites)`
(per-unit frames from `precis_chain.fibre.unit_frames`, occupancy list,
optional loop curves in; coords, elements, bonds, ports out). Coords are
per-base Arnott fibre templates placed in those frames, bonds
intra-nucleotide + O3'–P, no H-bonds as bonds. The op follows the
`prepare_generate`/`finish_generate` split
(`PendingRealizeChain`: coords + ports prepared inline, `store.structure_save`
+ `atomic/bind.py::bind_structure` only after the caller's `save_tree`, so a
failed tree save never strands a minted structure). Ports payload — `5p`/`3p`
with `axis_atom`/`phase_atom`, attachment ports `n<i>.c5m|maj|min` for listed
`sites` only. The structure binds to the **segment child** whose range covers
the region (one region per segment in this cut), so `envelope_fit` checks
the segment's own envelope against its atoms; the helix parent carries none.
Loops realized only after `relax_chain`: a range containing a loop whose
domain row has no `meta.loop_curve` yet raises `Unsupported` naming the
loop, never a guessed geometry. So for the hairpin the 4-bp stem realizes
on its own; the 4-nt loop realizes only after `relax_chain`. Earlier drafts
called this `GENERATORS["dna"]`; that name is retired — a sequence-in,
tree-blind duplex generator is not built here (it would duplicate
`realize_chain` on a one-helix design).
`precis/structure/export.py::to_pdb(scene)` adapter over
`precis_chain.pdb.write_pdb`; `structure` `view='pdb'` dispatched in
`src/precis/handlers/structure.py` next to the `poscar` branch.

**Export** — `precis_se/chain/export.py`, surfaced as `view='export'`
(`args={format}`, registered in `handler.py::_VIEW_ARGS`): scadnano JSON
(helix→helix, domain→domain), caDNAno legacy (lattice designs only, else
`Unsupported` naming the gap), oxDNA `.top`+`.conf` (from
`fibre.unit_frames`, `.conf` in oxDNA units 0.8518 nm), PDB per realized
region.

**Skill + docs** — `realize_chain` op row and `view='export'` row appended
to `precis-se-chain-help.md` (minted by `se-nucleic-acid`); `precis-se-help`
op table gains `realize_chain`.

## Explicitly NOT in scope

- Everything `se-nucleic-acid` ships: storage, pure ops, `layout_chain`,
  `relax_chain`, `fold_layout`, derived pairing, constants, `chain_*` DRC,
  `view='chain'`/`view='topology'`.
- Running oxDNA/CanDo — export only. Whole-origami atoms (≈470k atoms for
  7 kb) — atoms per region only, one region per segment.
- Import of scadnano/caDNAno/oxDNA files into the tree (export is one-way
  in this cut; import is a follow-up if the walker work needs round trips).
- H-bonds as bonds; base-pair geometry beyond the Arnott fibre templates
  (no per-step propeller/roll from a fitted model).
- Walker cursor geometry (`view='stations'`) — that item consumes the
  `sites` ports minted here.

## Acceptance criteria

- Hairpin `GGGGAAAACCCC` (authored via `se-nucleic-acid`'s `fold_layout`):
  `realize_chain` realizes the 4-bp stem; a region spanning the loop
  before `relax_chain` → `Unsupported` naming the loop; after it, stem +
  loop realize; `to_pdb` text parses back via
  `precis_chain.pdb.read_trace`.
- `realize_chain(helix, 0, 21, 'allatom')` binds a `structure` to the
  segment child covering 0–21, deferred until after `save_tree` (a forced
  save failure leaves no `structure` row); theorems from coords: P–P 6.6–7.2 Å
  along a strand; rise 3.34 ± 0.02 Å; C1'–C1' 10.4–10.8 Å; minor/major
  groove **as shortest inter-strand P···P distances, 11.5/17.5 ± 1 Å**
  (Saenger's 5.7/11.7 Å widths plus the 5.8 Å phosphate allowance — the
  convention the earlier "12/22" left unnamed, see the 2026-09-29 log entry);
  21 bp regains phase (frame x dot ≥
  0.98); `validate_atomic` reports no `bond_length_sanity`; `envelope_fit`
  clean; `5p` port has a measured `rot`; a listed `sites: [n5]` yields ports
  `n5.c5m`, `n5.maj`, `n5.min` and an unlisted index yields none.
- `fidelity:'backbone'` emits P, C4', C1' only, same P–P and rise theorems.
- Rectangle origami (the procedural 24 × 256 bp fixture from
  `se-nucleic-acid`'s tests, imported not duplicated): scadnano export
  parses as JSON, every domain appears exactly once, helix count 24; oxDNA
  `.top` neighbour lists form one path per strand and `.conf` line count =
  nucleotides + header; caDNAno export succeeds on the square-lattice
  fixture and raises `Unsupported` naming the helix on a second fixture: one
  `declare_helix` whose `chain.path` is `waypoints_m` (three points, gentle
  arc) instead of `lattice`.
- `realize_chain` appears in `HANDLER_LEVEL_OPS` and is a proposal, not an
  execution, in the web `design_turn` dry-run.
- `structure` `view='pdb'` on the bound structure returns text whose ATOM
  count equals the realized atom count.

## Target + blast radius

`precis_se/atomic/apply.py::HANDLER_LEVEL_OPS` + `atomic/generate.py`
(`prepare_realize_chain`, `PendingRealizeChain`; `GENERATORS` untouched), new
`precis_se/chain/atoms.py` + `precis_se/chain/export.py`, `se` handler
(`view='export'`, `_VIEW_ARGS`), `precis/structure/export.py` (adapter),
`src/precis/handlers/structure.py` (`view='pdb'` dispatch), skills
`precis-se-chain-help` / `precis-se-help`. No migration, no worker, no web
route, no new extra.

## Open questions / decisions log

- 2026-09-28 split out of `se-nucleic-acid` on its `ready` vet's signal
  (three separable slices; realization + export need the data model but the
  data model, relax and DRC do not need them). Reto ruled "split" the same
  day. `se-walker-light-protocol` re-pointed to block on this item because
  its cursor geometry reads `realize_chain` `sites` ports. Decided.
- 2026-09-28 (ready gate) blocker: the parent's "tree-aware
  `GENERATORS["dna"]`" wording contradicted `Generator =
  Callable[[dict], GeneratedBlock]` reached as `builder(params)` — no tree
  access exists on that path. Resolved: no `GENERATORS` entry; the
  handler-level `realize_chain` does the tree reads and calls the pure
  `chain/atoms.py::build_region`. `Unsupported` on an unrelaxed loop kept.
  Advisory (caDNAno `Unsupported` fixture unnamed) → waypoint-path helix
  named in the AC. Decided.
- 2026-09-29 PRECONDITION MET, and the criterion needs restating. The
  parent's slice 2 pass A replaced the antipodal pair with a groove-asymmetric
  one: `precis_se/chain/nucleic.py::MINOR_GROOVE_SPAN_RAD` — **δ = 144°**
  for B-DNA (220.5° for A-RNA), the backbones symmetric about the frame
  normal at `∓δ/2`, cited to Kornyshev & Leikin 2000 and Allahyarov et
  al. 2003. The
  space plan now has two channels of different width with the minor one
  named, so an asymmetric atom template is no longer forbidden by the exits
  it must hang off; **the 12/22 Å numbers are this item's job**, not the
  space plan's.
  Restate the criterion before building against it: "12/22 ± 1 Å" does not
  name a convention, and the two candidate conventions differ by
  `1/cos α = 1.15`. 22 Å cannot be a shortest inter-strand P···P distance at
  all (the phosphate cylinder is only 17.8 Å across), so 12/22 must be an
  **axial-span** reading (12 + 22 = 34 Å is one pitch) — likely, but an
  inference, not a convention any source states. At δ = 144° with
  phosphates at `nucleic.P_RADIUS_M` the model gives shortest P···P distances
  of 11.8 Å and 17.5 Å (against the tabulated 11.5/17.5), i.e. axial
  spans of ≈13.6 Å and ≈20.6 Å. Pick the P···P convention and the pair
  becomes 11.5/17.5 ± 1; keep the axial one and it is 13.6/20.6 ± 1. Either
  is buildable; "12/22" in an unnamed convention is not.
- OPEN (advisory, not a build blocker): whether `view='export'` PDB should
  concatenate every realized region into one file with distinct chain ids
  or emit one file per region. Default for the build: one file, chain id
  per segment child, `TER` between them.
