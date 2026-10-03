# se machine design

**Status:** ends when an se design carries the non-geometric layer
(per-region properties: hydrophobic, charge, field, optical) and a
declarative intent can be realized by pick-and-join, so an LLM reasons over
space, motion, assembly, charge, field and light — a PCB, a cartridge and a
motor as children of one design. North-star: `backlog/se-kind.md` +
`backlog/se-region-property-layer.md`. As the se owner it ranks the design-model items the other se threads
(se-3d-viewer, se-nucleic-chain, hexfold-toolkit) consume. Today the region-property layer is
unbuilt and blocks three of the six reasoning axes; everything below reads
off that gap until it closes.
**Last reviewed:** 2026-10-01 (Pillar 2 review: now the se OWNER; three unthreaded se items adopted)
**Worktree:** `se-machine-design`
**Active:** yes — Reto, 2026-10-01 (Pillar 2 review).

## Resume state (2026-10-03)

- **Region slice A** (Do next 1): built and CI-green, and NOT in round 2
  (prod 63301c5c has neither core 0182 nor se 0018). The orchestrator took
  it for round 3: it squashed tip f330ca43e as f497c89f5 and is landing it
  through the remote gate with migrations core
  `0182_se_measurand_seed.sql` and se `0018_se_regions.sql` (verdict §13).
  When it shows on main, mark it here; `round in` for that sha is the
  orchestrator's. Dogfood it on prod after the round 3 deploy. Grammar and
  review verdicts are in
  `~/.claude/projects/-Users-reto-precis-mcp/reviews/se-machine-design.md`
  §3–§5.
- **Organic print** (Do next 2; Reto 2026-10-03: "I'd like that to
  progress"): the Slice 4 bridge ran on prod for the first time
  (note §14). `realize(strategy='simp')` gives a watertight 3MF in mm,
  but the shape is a 1 mm voxel staircase, and the smoothing ops are wrong
  in two ways: gr464340 (open/close half-pitch bias) and gr464343
  (`open=` erased the load point and still bound the result). The
  throwaway design `se-simp-dogfood-1003` and its two cad designs stay on
  prod as the gr464343 repro; retire them when it closes. The proposed
  first test piece is review-queue item se-machine-design-3.
- **Joint sweep** shipped c5a2e8624 and was dogfooded on prod 2026-10-03
  (round 2): a throwaway design gave `joint_sweep_interference` at 90° as
  specified, and was then retired (note §12a). Residual gr462067: rigidly
  connected, unparented blocks stay still during the sweep. Its first
  consumer is `hexfold-t-handle-bearing` (hexfold-toolkit).
- **drc cost** (note §12a): `view='drc'` on unicycle-c1 takes about 17 s. 92% of
  that is `geometry_plausibility._pair_clearance` (about 1 s per connect);
  the fastener insertion pass is about 0.3 s per screw. The profile is on
  gr450524 (finding 5). No finding for se-3d-viewer. When finding 5 is
  taken up, the number to beat is 1.9 s per connect under cProfile (about
  1 s without it). It dominates any design with more than a handful of
  connects.
- **Inferred insertion (Reto 2026-10-02):** `moves` stays required.
  Reto asked whether tool travel and bolt insertion can be inferred; the
  proposal is in
  `~/.claude/projects/-Users-reto-precis-mcp/review-queue/answered/se-machine-design-1.md`.
  Its bolt-insertion part has since shipped from se-3d-viewer
  (fe1e6840c: `toolaccess.insertion_path`, rule `fastener_insertion_path`,
  run by `fasten(reach=True)` in `view='drc'`), so do not file it. Still
  unbuilt: inferring the moving end for `press`/`bearing` joints where
  exactly one end is bought. File that only if Reto asks for it.

## Do next

1. **backlog/se-region-property-layer.md** — blocks three of six reasoning
   axes (charge, field, optical); the peer session (unicycle) is already
   the pocket object waiting on it. Ranked 1. Sliced 2026-10-02: slice A
   (measurands, selectors, pockets) built, landing in round 3 (Resume state); B waits on
   measures-substrate, C on the class lattice (both knowledge-mesh).
2. **Organic print: backlog/structural-solution-space.md §Slice 4
   bridge** (Reto 2026-10-03). The next build item. Note §14 lists it as
   O1–O4, none with a migration:
   - O1: gr464340 and gr464343.
   - O2: `realize(min_member=)` mapped to the filter radius, a pitch guard
     at min_member/3, and a `min_member` capability field.
   - O3: default rounding at min_member/3.
   - O4: optimiser convergence.
   Then the first test piece, once Reto answers review-queue item
   se-machine-design-3.
3. **backlog/flatpack-furniture-generator.md** (Reto 2026-10-03; on the
   orchestrator's branch until its next ship). Stays `draft` until Reto
   answers review-queue item se-machine-design-4 on machine and plywood.
   The open questions are argued below under "Flat-pack open questions".
4. **backlog/class-lattice-similarity-spaces-and-laws.md** — owned by
   term-taxonomy; wait, do not duplicate rank here (seam below).
5. **backlog/se-intent-to-realize-loop.md** — blocked-by 1 and 4.
6. **backlog/pcb-se-binding.md** — the mm→m crossing; a producer exists,
   a consumer was never built. Peer session EWOD found this the same day.
   Vet round 3 folded 2026-10-02; two passes (pcb side first), both
   carry migrations. Reto accepted the five v1 calls 2026-10-02.
7. **pcb-se-binding v2** — section "Follow-up v2" in
   `backlog/pcb-se-binding.md` (Reto 2026-10-02): per-part envelopes
   with real heights and subtracted mounting holes. Blocked-by 6.
8. **backlog/pcb-argue-with-design.md**
9. **backlog/cross-scale-single-assembly.md** — blocked-by 6
   (pcb-se-binding).

## Flat-pack open questions (argued 2026-10-03, for Do next 3)

- **Joinery default:** finger joints on the carcass corners, and through-tabs
  (tab-and-slot) for the shelves, on both machines. A laser cannot cut a
  dado (a pocket), and one joinery family keeps the cut file to a single
  through-cut layer for both machines. Dadoes become a Maslow-only option
  later.
- **Which machine first:** Reto decides. I recommend the laser at toy
  scale (W 200 mm, 3 mm ply), because it makes the item's own physical fit
  check cheap. Because `kerf`, `fit`, `cutter_d` and the sheet are
  parameters, the Maslow then needs only its own fit test.
- **se-first or 2-D-first:** neither on its own. The source of truth is a
  2-D panel model: each panel's rectilinear outline with its tabs, slots
  and dog-bones, plus its 3-D placement. Two things derive from it:
  - the SVG/DXF;
  - the se design: one block per panel, bound to a cad design built from
    a box, `add` tabs, `cut` slots and `cut` dog-bone cylinders, all
    primitives that already exist.
  Deriving cut outlines from a 3-D solid would need a section-to-polygon
  extractor that cad does not have. With this design, se's existing
  interference check validates every tab-in-slot fit in 3-D for free, and
  the solid still composes with machine design (the item's reason for
  se-first).
- **DXF:** the tree has no writer. Write a minimal ASCII DXF (LINE and
  POLYLINE entities, units mm) by hand, without adding a dependency.

## Horizon

1. **backlog/se-feasibility-and-cost.md** — owned by nobody today; the far
   end all se threads (se-3d-viewer, se-nucleic-chain, hexfold-toolkit)
   serve. Claimed here.
2. **backlog/ts-stabilizing-pocket.md** — transition-state pocket
   (Reto 2026-10-01): derive an se pocket spec from a reaction's
   transition state and realise it as a backboned scaffold. Blocked
   on Do next 1 and 5; the chemistry thread consumes it and measures
   the barrier. Unparks when the realize loop lands.
3. **backlog/se-atomic-round2.md**
4. **backlog/design-workbench-realize.md** — blocked-by 3 (se-atomic-round2).
5. **backlog/se-off-the-shelf-fabrication.md**
6. **backlog/tslot-profile-library.md** — T-slot profiles as real parts
   (Reto 2026-10-01); makes the off-the-shelf captive-T-slot joint
   buildable.
7. **backlog/rack-19in-helper.md** — 19-inch rack helper (Reto
   2026-10-01); first consumer of the T-slot library.
8. **backlog/diamondoid-pattern-language.md**
9. **backlog/cad-machine-spec.md**
10. **backlog/cad-dims-and-constraints.md**
11. **backlog/cad-print-in-place.md**
12. **backlog/cad-assembly-checklist-seed-items.md**
13. **backlog/cad-diagnose-apply-loop.md**
14. **backlog/printable-atomic-models.md**
15. **backlog/se-fret-round-2.md**
16. **backlog/photoswitch-states-and-spectral-dof.md**
17. **backlog/nm-stick-placement.md**
18. **backlog/nm-face-codes-and-scale.md**
19. **backlog/se-process-skills-as-rewrites.md**
20. **backlog/boxel-exercise-tooling-gaps.md** — what the boxel exercise
    taught about the cad/structure surface; read it before starting
    the cad items (9–11, 13).
21. **backlog/situation-rule-tables.md** — `blocked-by` design-state-core
    (multiscale-design-core Do-next 1, not ranked here). Three-verdict pair
    checks over swept volumes in se drc; arguably multiscale-design-core's
    (it is a constraint-catalogue piece), kept here because its only
    consumer today is se drc — seam, move it if that thread opens first.
22. **backlog/precis-se-help-exceeds-the-skill-size-cap.md** — small skill
    hygiene: the skill is over the 32 KB hard cap and allowlisted; split
    the FRET/optical and discrete-states domains out. Cheap, any time; do
    it before the next domain section is added to that skill.

## Parked

- **microfluidic cartridge modelling** — unparked by Reto 2026-09-30 to
  the Horizon only, and lives inside `backlog/cross-scale-single-assembly.md`
  (Do-next 9) rather than as its own entry.
- **gr451270** — se atomic tpms/schwarzite generator: family aliases
  rejected by the error message that names them, serial required-param
  discovery, inconsistent defaults. Unparks alongside gr451269 (same
  generator).
- **gr451269** — se atomic tpms/schwarzite generator emits topologically
  correct nets whose bond lengths are not carbon and nothing checks it;
  companion to gr451270, same generator, same dogfood.
- **backlog/method-transfer-stellar-signal-separation.md** — Graz notes;
  maybe useful for object manipulation (Reto, 2026-10-01). Unparks if an
  object-manipulation thread opens.
- **One dormant quest owned by this thread** (Reto 2026-10-03, td460284):
  qu161907 (A self-assembling, atomically-precise compute substrate, a
  computational seed you can grow). It is tagged `thread:se-machine-design`
  and carries a logbook decision entry recording the ownership; keep it
  `STATUS:dormant`. Its draft dr42995 (nano-computer) still gets
  `draft_refresh` passes; this thread schedules nothing against either.

## No action needed

- (none yet)

## Seam

`se-3d-viewer` owns rendering of whatever the model carries (its Horizon 1
`se-pick-hierarchy` is the same surface this thread's model produces) —
this thread owns the model, that one owns the picture. pcb platform items
live in `pcb-platform.md`; `pcb-se-binding`/`pcb-argue-with-design` are
this thread's, not pcb-platform's or ewod-pcb's.
