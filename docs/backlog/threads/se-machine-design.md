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

## Resume state (2026-10-02)

- **Region slice A** (Do next 1): built and CI-green, handed to the
  orchestrator to squash-land after 0181 (round 2). Branch
  `worktree-agent-a48fe82af9f90530b`, tip 47677a6b7; migrations core
  `0182_se_measurand_seed.sql` + se `0018_se_regions.sql`. Grammar, review
  verdicts and three open calls (taxon-id identity, pin scope,
  unbound-block exemption):
  `~/.claude/projects/-Users-reto-precis-mcp/reviews/se-machine-design.md`
  §3–§5. After it lands, dogfood on prod when the round deploys.
- **Joint sweep** shipped c5a2e8624 (`params.range` + required
  `params.moves`). Residual gr462067: rigidly connected, unparented blocks
  stay still during the sweep. Its first consumer is
  `hexfold-t-handle-bearing` (hexfold-toolkit).
- **Inferred insertion (Reto 2026-10-02):** `moves` stays required.
  Reto asked whether tool travel and bolt insertion can be inferred; the
  proposal (an insertion-corridor check beside `toolaccess`, no
  migration) is in
  `~/.claude/projects/-Users-reto-precis-mcp/review-queue/answered/se-machine-design-1.md`.
  If Reto agrees, file it as a backlog item and rank it here.

## Do next

1. **backlog/se-region-property-layer.md** — blocks three of six reasoning
   axes (charge, field, optical); the peer session (unicycle) is already
   the pocket object waiting on it. Ranked 1. Sliced 2026-10-02: slice A
   (measurands, selectors, pockets) built, landing in round 2 (Resume state); B waits on
   measures-substrate, C on the class lattice (both knowledge-mesh).
2. **backlog/class-lattice-similarity-spaces-and-laws.md** — owned by
   term-taxonomy; wait, do not duplicate rank here (seam below).
3. **backlog/se-intent-to-realize-loop.md** — blocked-by 1 and 2.
4. **backlog/pcb-se-binding.md** — the mm→m crossing; a producer exists,
   a consumer was never built. Peer session EWOD found this the same day.
   Vet round 3 folded 2026-10-02; two passes (pcb side first), both
   carry migrations. Reto accepted the five v1 calls 2026-10-02.
5. **pcb-se-binding v2** — section "Follow-up v2" in
   `backlog/pcb-se-binding.md` (Reto 2026-10-02): per-part envelopes
   with real heights and subtracted mounting holes. Blocked-by 4.
6. **backlog/pcb-argue-with-design.md**
7. **backlog/cross-scale-single-assembly.md** — blocked-by 4
   (pcb-se-binding).

## Horizon

1. **backlog/se-feasibility-and-cost.md** — owned by nobody today; the far
   end all se threads (se-3d-viewer, se-nucleic-chain, hexfold-toolkit)
   serve. Claimed here.
2. **backlog/ts-stabilizing-pocket.md** — transition-state pocket
   (Reto 2026-10-01): derive an se pocket spec from a reaction's
   transition state and realise it as a backboned scaffold. Blocked
   on Do next 1 and 3; the chemistry thread consumes it and measures
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
  (Do-next 6) rather than as its own entry.
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

## No action needed

- (none yet)

## Seam

`se-3d-viewer` owns rendering of whatever the model carries (its Horizon 1
`se-pick-hierarchy` is the same surface this thread's model produces) —
this thread owns the model, that one owns the picture. pcb platform items
live in `pcb-platform.md`; `pcb-se-binding`/`pcb-argue-with-design` are
this thread's, not pcb-platform's or ewod-pcb's.
