# chemistry

**Status:** ends when precis can propose, compute and cite a reaction
pathway and its conditions for a quest, with catalysis (autocatpath, the
catpath engine) as the main line. Catalysis lives under chemistry (Reto,
2026-10-01, Pillar 2 review); code in `src/precis_pathway`, `../catpath`
is the reference engine. Today the items arrived unthreaded and untriaged
in one batch: nobody has checked which still reproduce and which shipped.
Order: triage first, then engine work (seed health, step-level retry, NEB,
desorption) above everything that presents or packages its results.
**Last reviewed:** 2026-10-01 (thread created from the Pillar 2 sweep; quest-catalysis items added)
**Worktree:** `chemistry`
**Active:** yes — Reto, 2026-10-01.

## Do next

**Deadline above the ranking:** the catpath pathway-engine and trust-demo
paper is November 2026's paper of the month (td459589, quest qu459585;
Reto 2026-10-01). Skeleton draft `catpath-methods` exists, with a plan
paragraph per section; next comes importing the missing methods papers. The trust
demo needs the MPI image rebuilt on the node before
`PRECIS_DFT_MPI_RANKS` is set. Rank the items below by what that paper
needs.

1. **Triage the 21 items below** — read each against `src/precis_pathway`
   and `git log`, mark which still reproduce and which shipped, delete the
   shipped, and re-rank this file off the result. Leverage: every rank
   below is a guess until this is done. Start with the `idea` rows
   (seed-health, NEB, desorption, harvest-bookmark, material-off-sample).
2. **qu164903 back to ticking** — it rests after 5 failed ticks
   ("unparseable model output", gr345366, moved here from roadmap-quest);
   plus gr322060, the tracker of 20 relax-sim infra gripes on qu164903.
   Items: `backlog/qu164903-campaign.md`,
   `backlog/catalyst-discovery-quest.md`,
   `backlog/quest-seed-orphan-recovery.md`. Worked now (Reto, 2026-10-01:
   "We work on 2").
3. **qu202467 restart report** — paused 2026-10-01 ($6,211 tote, 0 deeds,
   holding ticks). Restart condition (in its logbook): this thread reports at
   least one named blocker fixed (frontier-table sync wall; literature
   fetch-step bottleneck / six unresolved gold stubs; no tool to check stub
   fetch status without re-searching) AND names the next measurement a tick
   would make; Reto decides on that report.
4. **backlog/autocatpath-seed-health.md** — the seed job family's umbrella
   (59 runs dying without `result.json` is the unresolved core). Engine
   reliability gates every number the quest loop consumes; a diagnosis
   pass that must read the first unbuffered failures before picking a
   remedy.
5. **backlog/pathway-step-level-retry.md** — `ready/medium`; turns
   per-step validity attrition (0.95^20) into additive compute. Unblocks
   usable routes from long pathways without a human or tick deciding each
   re-run.

## Horizon

The first five are engine items moved down from Do next 2026-10-01 to keep
it at five; engine-over-UI still holds.

1. **backlog/neb-barriers-in-the-catpath-pipeline.md** — NEB transition-
   state barriers as a pipeline step (barriers, not just thermodynamics);
   waits on the triage to say what the pipeline already carries.
2. **catalysis-selectivity thread** — owns
   `backlog/catpath-desorption-link-kind.md` since 2026-10-02 (first slice
   of its step-annotation item) plus the NH₃ network, U/pH selectivity
   window and NEB promotion items; Reto ranked it high, so its engine
   edits go ahead of this file's Horizon.
3. **backlog/autocatpath-aggregate-ran-11h-on-a-33s-job.md** — the
   kinetics ceiling shipped (the subprocess timeout, deploy 2026-09-27;
   `precis_pathway/runner.py`, `tests/test_pathway_kinetics_timeout.py`);
   the item is NOT closed: the castor reproduction that settles the
   cause, the stranded `no_to_nh3_pd` aggregate re-dispatch (needs Reto),
   and the broader blocking-dispatch ceiling remain. Trim the item to
   that remainder during Do next 1.
4. **backlog/pathway-conditions-effects-report.md** — `prio: high`,
   written for quest qu164903 (NO→NH₃): the first consumer-visible
   product of the engine. After Do next 4-5 because a report over
   unhealthy seeds is the artefact problem again.
5. **backlog/autocatpath-integration.md** — the remaining slices of the
   native integration (most shipped; present state is in the
   `src/precis_pathway/` docstrings and ADR 0069); read with 4.
6. **backlog/catalyst-physical-realism.md** + **backlog/slab-modelling-knobs.md**
   — defect ensembles, poisoning, slab knobs; make the engine's answer
   physically honest. After the engine reliability block.
7. **backlog/ephemeral-potentials-for-catpath.md** — a throwaway potential
   as a pre-screen, DFT only on survivors; cost lever, waits on Do next 4 so
   the saving is measured on a healthy pipeline.
8. **backlog/quest-redispatch-tier.md** — `redispatch_candidates` re-scores
   at the neb tier regardless of the candidate's own rung; settle before the
   ladder and engine-bump paths interact again.
9. **backlog/quest-data-table-and-formula-discovery.md** — tidy
   `precis quest table` export, the `wrong_site` barrier-trust blocker, then
   the deterministic baseline; the `meta.params` writer and `view='series'`
   shipped.
10. **backlog/quest-artifacts-in-dossier.md** — embed the existing
    pareto/energy-profile renders in the quest's document; the dossier target
    is superseded by `backlog/quest-graph-as-dossier.md`
    (graph-memory-consumers Horizon 1), so this waits on its design.
11. **backlog/material-off-sample-model.md** (material kind's off-sample
   estimate layer, ADR 0070 deferral) +
   **backlog/harvest-bookmark-concurrency.md** — small `idea`s homed here
   by the sweep; rank off 1.
12. **backlog/protomia-gap-eval-and-molecular-properties.md** — eval a
   candidate tool, then close the molecular-property gap it exposes.
13. **backlog/reaction-kind-and-synthesis-cost.md** — sourced reaction-fact
   store and cost over routes; the long arc past one quest's pathways.
14. **backlog/estimate-kind-ms-chemistry-workup.md** — argue before
   simulating; sets up sims, waits on 13 for facts to argue from.
15. **backlog/composable-pipeline-kind.md** +
   **backlog/chem-tools-integration.md** +
   **backlog/structure-import.md** — chaining and import surface for
   chem tools; presentation/packaging layer, after the engine.
16. **backlog/sim-harness.md** — quest-driven automation, writeup draft and
   container drive path (slices 2–3); consumes roadmap-quest's loop.
17. **backlog/pathway-explorer.md** + **backlog/pathway-viewer-ux-batch.md**
   — the UI. Last by the rank rule: it presents what the engine has to
   get right first. Shared presentation logic belongs to plugin-split
   (Seam).
18. **backlog/chem-name-lookup-verb.md** — PubChem-backed
   formula/ID → common name; small, and a dependency of
   reaction-kind-and-synthesis-cost (13). From pillar 4, 2026-10-01.
19. **backlog/catpath-wheel-version-reuse.md** — one catpath version across
   many commits leaves a hand-passed wheel unidentifiable; deploy hygiene for
   the engine this thread owns. Platform pass 2026-10-02.

## Parked

- (none)

## No action needed

- (none yet)

## Seam

- **catalysis-selectivity** changes the ammonia network content and adds
  link fields; this thread keeps the engine contract and the wheel bump.
  `pathway-viewer-ux-batch.md` item 3 moved there.

- **plugin-split** owns `backlog/pathway-presentation-shared-module.md`
  (its Do-next 6: where shared pathway presentation lives, plus the
  catpath version bump and wheel redeploy). Horizon 17 here must not start
  a second copy of that logic.
- **precis-dispatch** (`backlog/precis-dispatch.md`, ranked in local-compute
  Horizon) is the runner this thread's compute jobs (DFT relax) depend on;
  this thread is its first consumer, and its seams extract when a second
  workload lands.
- **roadmap-quest** owns the quest loop that consumes pathways (its
  ticks dispatch the jobs this thread makes healthy); the engine's output
  contract is this thread's, the dispatch and priority flow are theirs.
- `src/precis_pathway/` is a plugin package: the plugin import boundary
  (`tests/test_plugin_import_boundary.py`, plugin-split) applies to every
  change here.
