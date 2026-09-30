# se machine design

**Status:** ends when an se design carries the non-geometric layer
(per-region properties: hydrophobic, charge, field, optical) and a
declarative intent can be realized by pick-and-join, so an LLM reasons over
space, motion, assembly, charge, field and light — a PCB, a cartridge and a
motor as children of one design. North-star: `backlog/se-kind.md` +
`backlog/se-region-property-layer.md`. Today the region-property layer is
unbuilt and blocks three of the six reasoning axes; everything below reads
off that gap until it closes.
**Last reviewed:** 2026-09-30
**Worktree:** `se-machine-design`
**Active:** no — opens at the next session restart if Reto names it.

## Do next

1. **backlog/se-region-property-layer.md** — blocks three of six reasoning
   axes (charge, field, optical); the peer session (unicycle) is already
   the pocket object waiting on it. Ranked 1.
2. **backlog/class-lattice-similarity-spaces-and-laws.md** — owned by
   term-taxonomy; wait, do not duplicate rank here (seam below).
3. **backlog/se-intent-to-realize-loop.md** — blocked-by 1 and 2.
4. **backlog/pcb-se-binding.md** — the mm→m crossing; a producer exists,
   a consumer was never built. Peer session EWOD found this the same day.
5. **backlog/pcb-argue-with-design.md**
6. **backlog/cross-scale-single-assembly.md** — blocked-by 4
   (pcb-se-binding).
7. **backlog/se-bearing-kinematics-check.md** — first consumer is
   `hexfold-t-handle-bearing` (hexfold-toolkit thread).

## Horizon

1. **backlog/se-feasibility-and-cost.md** — owned by nobody today; the far
   end all se threads (se-3d-viewer, se-nucleic-chain, hexfold-toolkit)
   serve. Claimed here.
2. **backlog/se-atomic-round2.md**
3. **backlog/design-workbench-realize.md** — blocked-by 2 (se-atomic-round2).
4. **backlog/se-off-the-shelf-fabrication.md**
5. **backlog/diamondoid-pattern-language.md**
6. **backlog/cad-machine-spec.md**
7. **backlog/cad-dims-and-constraints.md**
8. **backlog/cad-print-in-place.md**
9. **backlog/cad-sdf-rounding-and-field-export.md** — in progress.
10. **backlog/cad-assembly-checklist-seed-items.md**
11. **backlog/cad-diagnose-apply-loop.md**
12. **backlog/printable-atomic-models.md**
13. **backlog/se-fret-round-2.md**
14. **backlog/photoswitch-states-and-spectral-dof.md**
15. **backlog/nm-stick-placement.md**
16. **backlog/nm-face-codes-and-scale.md**
17. **backlog/se-process-skills-as-rewrites.md**

## Parked

- **microfluidic cartridge modelling** — unparked by Reto 2026-09-30 to
  the Horizon only, and lives inside `backlog/cross-scale-single-assembly.md`
  (Do-next 6) rather than as its own entry.
- **gr451270** — se atomic tpms/schwarzite generator: family aliases
  rejected by the error message that names them, serial required-param
  discovery, inconsistent defaults. Unparks alongside gr451269 (same
  generator).
- **gr451269** — se atomic tpms/schwarzite generator emits topologically
  correct nets whose bond lengths are not carbon and nothing checks it;
  companion to gr451270, same generator, same dogfood.

## No action needed

- (none yet)

## Seam

`se-3d-viewer` owns rendering of whatever the model carries (its Horizon 1
`se-pick-hierarchy` is the same surface this thread's model produces) —
this thread owns the model, that one owns the picture. pcb platform items
live in `pcb-platform.md`; `pcb-se-binding`/`pcb-argue-with-design` are
this thread's, not pcb-platform's or ewod-pcb's.
