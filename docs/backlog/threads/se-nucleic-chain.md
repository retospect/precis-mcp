# se nucleic chain

**Status:** ends when a nucleic-acid design goes from sequence to
orderable strands with an honest atom model (physics-checked, findings that
name design objects) and round-trips caDNAno, reported in the se + hexfold
paper (td344088). Today the chain domain (helix/strand/domain → layout →
relax → realize → export) is landed and dogfooded on prod, and the walker
item is built, deleted, deployed (5ca0cfff on the fleet 2026-09-30 12:23Z)
and dogfooded end to end on prod (`dogfood-walker-a`: guard, channel
budget, crosstalk, make_steps, view='stations' all as specified;
td458169); fold_layout lays out every pseudoknot-free fold (bulges and
coaxial stacks end to end, tails as single-occupancy stubs); loop atoms
chain behind `relax_loops`, envelope_fit skips them, residue rows persist.
Flip the loop-relax default on Reto's word, then the pick hierarchy.
**Last reviewed:** 2026-09-30
**Worktree:** `se-nucleic-chain`

## Do next

1. **gr457928, the default flip** — the slice is in: `realize_chain
   relax_loops=true` chains the loop backbone (geometric relax, duplex
   pinned, worst O3'–P step reported before/after), `envelope_fit` skips
   loop nucleotides and names a protruding atom as `O3' of DA 8 (stem@3)`,
   and `chain_atoms.residues` persists one row per residue. Left: Reto's
   ruling on default-on — flipping `RELAX_LOOPS_DEFAULT` in
   `atomic/generate.py` plus the skill's "default false" sentence is the
   whole change; then tag the gripe done.
2. **backlog/se-pick-hierarchy.md** — shared with se-3d-viewer (its Horizon
   1); the chain-design instance (its 2026-09-30 section: residue +
   base-pair rows under a segment block, atom pick) turns "aO44" into
   "O3' of DA 8, hp.h0@3".

## Horizon

1. **atom findings name design objects** — `envelope_fit` now names its
   atom by residue and helix offset (gr457928's slice); the other
   structure-level findings on a bound segment (bond geometry, clashes)
   still say `aO44` — extend them the same way through
   `atomic/validate.py::chain_atom_name` when one bites in a dogfood.
2. **backlog/ewod-synthesis-protocol.md** (ewod-pcb thread's) — the zone
   compiler that consumes make_steps' make tree (live on prod since
   5ca0cfff); the first end-to-end design → dispense protocol.
3. **backlog/se-chain-insertions-deletions.md** — per-offset chain edits;
   its 2026-09-30 section carries the unpair/nick/mismatch op (the action
   a base-pair pick offers); waits on Reto's ruling on the op shape.
4. **backlog/nanostructure-check-tiers.md** §"Chains: the physics tier is
   oxDNA" — oxDNA as a rented relax rung against relax_chain's block-scale
   settle; waits on 1–3 and the oxDNA binary in the image; the only
   honesty check relax_chain has.
5. **caDNAno round trip** (unfiled) — settle the handedness reflection
   against a real file; waits on a file to compare.
6. **backlog/se-chain-staple-sequences.md** — staple assignment beyond the
   scaffold; waits on 3; orderable strand lists.
7. **backlog/se-protein-chain-import.md** — proteins on the same block tree;
   waits on a protein-bearing design being wanted.
8. **td344088** (se + hexfold paper; td345823 next) — reports this arc; the
   walker dogfood (td458169) is its protocol figure's source.

## Parked

- **caDNAno handedness settle** — unparks when a real caDNAno file is
  available; file it then (Horizon 5).

## No action needed

- **gr457929**, **gr457930** — fixed b3afcff9, tagged done.
- **gr458145** — fixed in the commit that re-ranked this file (block view
  names the state whose stored pose it shows); close on ship.
- **td458169** — done 2026-09-30; results in the todo's text.
- **gr458316** — layout_chain's 5p/3p ports carry the backbone-exit pose and
  realize lets the atom replace it; dogfooded on prod 2026-09-30
  (view='stations' target='f2.s0.3p' on dogfood-walker-a), tagged done.
- **gr458472** — filed 2026-09-30 from that re-dogfood: a `put(kind='se',
  ops=[…])` call lost its ops (put's schema has no `ops=`) and wiped the
  design; recovered by replaying design_revisions by hand. The refusal half
  is fixed in the commit that added this line (put with no ops refuses
  while the design has blocks). The rest — a restore_revision op, a
  revisions view, `ops=` on put's schema — is se persist work, not this
  thread's; it stays on the gripe.
- **backlog/se-fold-layout-coaxial-and-tails.md** — shipped 2026-09-30 and
  deleted, delete-on-ship: a helix reached through zero unpaired
  nucleotides is placed end to end on the one it stacks on (phase0 tuned so
  the backbone exits meet, one extra rise per bulged nucleotide), and an
  unpaired tail is a single-occupancy stub helix `<strand>.t5`/`.t3` — the
  representation `build_domain`'s own refusal of `loop_before_nt` on a
  first domain already named. Remaining refusals: no pairs, pseudoknot,
  > 10 000 nt, an already-routed strand.
- **backlog/se-walker-light-protocol.md** — landed in three slices
  (6e3fb3b1 and the B+C ship of 2026-09-30) and deleted, delete-on-ship;
  its decisions log is restated in `src/precis_se/chain/__init__.py`
  "Walker states" and the `precis-se-walker-help` skill.
