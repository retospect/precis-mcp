# se nucleic chain

**Status:** ends when a nucleic-acid design goes from sequence to
orderable strands with an honest atom model (physics-checked, findings that
name design objects) and round-trips caDNAno, reported in the se + hexfold
paper (td344088). Today the chain domain (helix/strand/domain → layout →
relax → realize → export) is landed and dogfooded on prod, and the walker
item is built and deleted: slice A is live on the fleet (7acc5e40) and
dogfooded on prod (`dogfood-walker-a`, 2026-09-30); slices B and C (ratchet
guard, spectral channel budget, make_steps, view='stations') are on main
awaiting the next gated deploy. Dogfood B+C on the fleet, make the loop
atoms a prod dogfood found unchained honest, then the layout follow-ons.
**Last reviewed:** 2026-09-30
**Worktree:** `se-nucleic-chain`

## Do next

1. **td458169** — the walker B+C prod dogfood (guard, budget, crosstalk,
   make_steps, stations on `dogfood-walker-a`); none of the four has run
   on prod, and the paper's protocol figure (td344088) and the ewod zone
   compiler (Horizon 2) both consume make_steps' output. Waits on the
   fleet carrying se migration 0017.
2. **gr457928** — realize_chain's loop nucleotides are rigid template copies,
   so every loop O3'–P bond is 5–10 Å and envelope_fit warns on every loop
   forever (a standing false positive that hides real drift). Fix designed
   (geo relax over loop residues, duplex pinned; persist residue rows;
   envelope_fit skips loops); awaiting Reto's ruling on default-on vs
   opt-in.
3. **gr458145** — view='block' with args.state prints a stored station
   pose as "(authored …)"; cosmetic, but it is the authored-vs-derived
   distinction the walker ruled on, and a reader of the dogfood above will
   hit it first. Cheap; ride along with 2.
4. **backlog/se-fold-layout-coaxial-and-tails.md** — ready, independent of
   1–3; fold_layout cannot place coaxial stacks or single-stranded tails, so
   every ViennaRNA-derived design with a tail lays out wrong.
5. **backlog/se-pick-hierarchy.md** — shared with se-3d-viewer (its Horizon
   1); the chain-design instance (its 2026-09-30 section: residue +
   base-pair rows under a segment block, atom pick) turns "aO44" into
   "O3' of DA 8, hp.h0@3".

## Horizon

1. **atom findings name design objects** (unfiled; lands with gr457928) —
   once residue rows persist, structure-level findings on a bound segment
   read as helix@offset + residue, so one view='validate' covers both
   scales; the LLM gets a findings loop it can act on.
2. **backlog/ewod-synthesis-protocol.md** (ewod-pcb thread's) — the zone
   compiler that consumes make_steps' make tree; waits on Do-next 1; the
   first end-to-end design → dispense protocol.
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
8. **td344088** (se + hexfold paper; td345823 next) — reports this arc; waits
   on Do-next 1 for its figure.

## Parked

- **caDNAno handedness settle** — unparks when a real caDNAno file is
  available; file it then (Horizon 5).

## No action needed

- **gr457929**, **gr457930** — fixed b3afcff9, tagged done.
- **backlog/se-walker-light-protocol.md** — landed in three slices
  (6e3fb3b1 and the B+C ship of 2026-09-30) and deleted, delete-on-ship;
  its decisions log is restated in `src/precis_se/chain/__init__.py`
  "Walker states" and the `precis-se-walker-help` skill.
