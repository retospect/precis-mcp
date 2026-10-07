# se machine design

## Resume

- **Pillar:** 3d-design
- **Next:** Build [print-file-scale](../print-file-scale.md) (½ build), then O2–O4 or O5 once Reto answers item -6.
- **Blocked by:** Print-file-scale is ready; subsequent organic work waits on item -6. Load-test numbers, PCB rough-box decision and first Print files click remain owed by Reto.
- **Unblocks:** Intent-based machine design with charge, field and optical properties.
- **Acceptance:** Use [print-file-scale](../print-file-scale.md): writer scale, scale metadata/filename, route bounds and printed-size checks; preserve the already dogfooded region slice A.
- **Worktree:** `se-machine-design`
- **Builds:** Not estimated here; use the owning item's current slice estimate.
- **Detail:** [Ranked work](#do-next) · [Horizon](#horizon) · [Coordination map](INDEX.md).

## Thread context

Current declared activity: [fleet roster](../../../.claude/fleet/threads.tsv); dated allocation decisions below are historical.

**Status:** ends when an se design carries the non-geometric layer
(per-region properties: hydrophobic, charge, field, optical) and a
declarative intent can be realized by pick-and-join, so an LLM reasons over
space, motion, assembly, charge, field and light — a PCB, a cartridge and a
motor as children of one design. North-star: `backlog/se-kind.md` +
`backlog/se-region-property-layer.md`. As the se owner it ranks the design-model items the other se threads
(se-3d-viewer, se-nucleic-chain, hexfold-toolkit) consume. Today the region-property layer is
unbuilt and blocks three of the six reasoning axes; everything below reads
off that gap until it closes.
**Last reviewed:** 2026-10-04 (95% ship review: all work on main)
**Worktree:** `se-machine-design`
**Allocation decision (historical):** yes — Reto, 2026-10-01 (Pillar 2 review).

## Detailed handoff state (2026-10-04)

- **Branch state at the 95% review (2026-10-04T06:48Z):** nothing is
  only on this branch; everything is on main. Nothing is half-done.
  Next step: print-file-scale (½ build), then O2–O4 or O5 once Reto
  answers item -6. Reto owes: the load-test numbers (-6), the PCB
  rough-box decision (-8), and the first "Print files" click on
  organic-bracket-1's 3-D page.
- **Round 3 is deployed** (929107f32, 2026-10-03T20:35Z). It was
  dogfooded on prod data with main's code: `view='print'`, the 3MF export
  and `view='drc'` render. The joint sweep was not exercised: no prod
  design declares a joint `params.range`.
- **Region slice A** (Do next 1) is live on prod (4181421ce, 2026-10-04T00:15Z, migrations core 0182 and se 0018). Dogfooded there through a throwaway design, now retired:
  - every documented op works as precis-se-regions-help says: measures with measurands, patch and ring selectors, add/set/remove pocket, `set_measure measurand=`, `unit_mismatch` in drc, and `view='ops'` round-trip;
  - every documented refusal fires.
  Five polish issues: reason-column noise in `view='measures'`, unit drift in a taxon's text, unclear "declaration on frame" wording, `atoms:` accepted on an unbound block, and taxon `under=` leaking. Grammar and review verdicts are in `~/.claude/projects/-Users-reto-precis-mcp/reviews/se-machine-design.md` §3–§5.
- **Organic print** (Do next 2): print 1, se `organic-bracket-1` as
  file v3, is printing in PLA (Reto, 2026-10-03T21:18Z). Bambu Studio
  02.08.02.61 showed only "invalid config", a Studio bug for every
  non-Bambu 3MF that 02.08.03.66 fixes. It reported no floating region and
  no cantilever.
  - Reto's verdict on the look (21:20Z): "cool but not quite organic.
    Smoother would be goooder." So print 2 wants O2–O4.
  - The load test is still owed, in item se-machine-design-6. The solver
    predicts about 0.04 mm tip drop at 1 kg and 0.08 mm at 2 kg.
  - **The print check and the download button are live on prod** (round 4, 727728cc9, 2026-10-04T04:40Z), with the fieldops version stamp (2c440745c).
    - `precis/cad/mesh_check.py` checks the shipped mesh for Bambu's floating-region rule and Studio 02.08.02.61's cantilever rule, and the 3MF package against the core spec. It also does a guarded export-time tail lift, which Reto allowed at 19:32Z with nothing stored.
    - `fieldops.flat_bed` cuts a flat bed face in the field.
    - `GET /se/{slug}/print/{block}.3mf|.stl` serves the file, with a "Print files" section on the 3-D page.
    Dogfooded on prod at 04:50Z:
    - `view='print' args={'block':'bracket'}` on `organic-bracket-1` runs on the shipped mesh: flat bed contact applied, cleanup dropped 8740 degenerate triangles, 109 slivers left, 47 vertices lifted at tol 0.5 mm, no findings. The 3MF export writes 3.3 MB.
    - Two message gaps were fixed in 3285cbdc4 (round 6, not yet deployed). The summary view said "no findings" though it skips the mesh checks, and the block view gave no count line when the counts were 0.
    - The web route could not be checked by an agent, because the web UI needs a login. Reto's first click on "Print files" on the bracket's 3-D page is the check.
    - The old bracket field has no `fieldops` provenance key, as expected: it was minted before the stamp.
    gr464493: print-group and manufacture 3MFs still skip the check.
  - **Scaled downloads (se-3d-viewer-7, Reto answered A1/B1/C1/D1).**
    Mine:
    - the writer scale, ½ build: `scale=` on `write_mesh`/`_write_3mf`,
      `precis:scale_factor` metadata, an `-x<factor>` filename,
      `?scale=&model=` on the route (422 with `min_scale`), and
      `print/{block}.json`;
    - the print check at printed size;
    - the atom models in `backlog/printable-atomic-models.md`.
    The dialog is se-3d-viewer's. The writer scale is
    `backlog/print-file-scale.md` (ready).
  - **Open question to Reto** (item -6): declared flat faces with O5 (load
    ports with a solid contact boss), about 1 build, ahead of O2–O4?
- **Joint sweep** shipped c5a2e8624 and was dogfooded on prod 2026-10-03
  (round 2): a throwaway design gave `joint_sweep_interference` at 90° as
  specified, and was then retired (note §12a). Residual: rigidly
  connected, unparented blocks stay still during the sweep. Its first
  consumer is `hexfold-t-handle-bearing` (hexfold-toolkit).
- **drc cost** (note §12a): `view='drc'` on unicycle-c1 takes about 17 s. 92% of
  that is `geometry_plausibility._pair_clearance` (about 1 s per connect);
  the fastener insertion pass is about 0.3 s per screw. The profile is in
  finding 5. No finding for se-3d-viewer. When finding 5 is
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
   (measurands, selectors, pockets) live on prod and dogfooded (Resume
   state); B waits on
   measures-substrate, C on the class lattice (both knowledge-mesh).
2. **Organic print: backlog/structural-solution-space.md §Slice 4
   bridge** (Reto 2026-10-03). The next build item. Note §14 lists it as
   O2–O5, none with a migration (O1 shipped 264f412c0):
   - O5 + declared flat faces: about 1 build. Its rank against O2–O4
     waits on Reto (item -6).
   - O2: `realize(min_member=)` mapped to the filter radius, a pitch guard
     at min_member/3, and a `min_member` capability field.
   - O3: default rounding at min_member/3.
   - O4: optimiser convergence.
   - `backlog/print-file-scale.md`: the writer scale for scaled downloads,
     ½ build (se-3d-viewer-7).
   Print 1 is the bracket `organic-bracket-1` (job 464356). Printer: Bambu
   Lab X1 Carbon with tree supports; the goal is support-free.
3. **backlog/flatpack-furniture-generator.md** — `ready`, 3 builds
   (Reto 2026-10-03). Reto's answers:
   - laser first, 3 mm corrugated cardboard, a 50 mm cube, with the fit
     recorded on the first cut;
   - finger joints by default, with a straight edge as an option;
   - se-machine-design-7, option 1: a shared sheet job under flat-pack
     from build 1. PCB writes into it only through ewod-pcb's adapter.
   Builds: (1) core + laser SVG + DXF; (2) panels, joints, nesting; (3)
   checks and the 50 mm cardboard cut, whose physical fit check is Reto's.
4. **backlog/class-lattice-similarity-spaces-and-laws.md** — owned by
   term-taxonomy; wait, do not duplicate rank here (seam below).
5. **backlog/se-intent-to-realize-loop.md** — blocked-by 1 and 4.
6. **backlog/pcb-se-binding.md** — the mm→m crossing; a producer exists,
   a consumer was never built. Peer session EWOD found this the same day.
   Vet round 3 folded 2026-10-02; two passes (pcb side first), both
   carry migrations. Reto accepted the five v1 calls 2026-10-02.
   - **Rough box first** (Reto 2026-10-03T20:30Z), proposed in item
     se-machine-design-8 and awaiting his decision. It is one build, no
     migration:
     - an se block bound to a board slug, resolved at read time;
     - a box of the board outline × (tallest bottom part + 1.6 mm,
       flagged + tallest top part);
     - mounting holes as through-cylinders with fastener ports.
     No part has a stored height today, so heights come from a table of
     package-class maxima. On heater-base-test, the six largest classes
     cover 113 of 140 parts. Every part whose height is not its own is
     named in a finding. Stacks are v2.
     Before building, tell ewod-pcb and pcb-easyeda-round-trip.
7. **pcb-se-binding v2** — section "Follow-up v2" in
   `backlog/pcb-se-binding.md` (Reto 2026-10-02): per-part envelopes
   with real heights and subtracted mounting holes. Blocked-by 6.
8. **backlog/pcb-argue-with-design.md**
9. **backlog/cross-scale-single-assembly.md** — blocked-by 6
   (pcb-se-binding).

## Flat-pack open questions (argued 2026-10-03, for Do next 3)

- **Answered 2026-10-03** (se-machine-design-4): laser, 3 mm corrugated
  cardboard, finger joints default plus a straight-edge option. The
  arguments below stand as the reasoning.
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
22. **backlog/flatpack-living-hinges.md** — kerf-cut bend zones (Reto
    2026-10-03); blocked-by the flat-pack generator (Do next 3). Its
    reference is stored as web ref
    `rs-online-com-designspark-laser-cut-living-hinges-for-neater`.
23. **backlog/precis-se-help-exceeds-the-skill-size-cap.md** — small skill
    hygiene: the skill is over the 32 KB hard cap and allowlisted; split
    the FRET/optical and discrete-states domains out. Cheap, any time; do
    it before the next domain section is added to that skill.

## Parked

- **microfluidic cartridge modelling** — unparked by Reto 2026-09-30 to
  the Horizon only, and lives inside `backlog/cross-scale-single-assembly.md`
  (Do-next 9) rather than as its own entry.
- **gr451269** — se atomic tpms/schwarzite generator emits topologically
  correct nets whose bond lengths are not carbon and nothing checks it;
  same generator and dogfood as the family-alias and default fixes.
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
