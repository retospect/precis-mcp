# chemistry

**Status:** ends when precis can propose, compute and cite a reaction
pathway and its conditions for a quest, with catalysis (autocatpath, the
catpath engine) as the main line. Catalysis lives under chemistry (Reto,
2026-10-01, Pillar 2 review); code in `src/precis_pathway`, `../catpath`
is the reference engine. Today the items arrived unthreaded and untriaged
in one batch: nobody has checked which still reproduce and which shipped.
Order: triage first, then engine work (seed health, step-level retry, NEB,
desorption) above everything that presents or packages its results.
**Last reviewed:** 2026-10-01 (thread created from the Pillar 2 sweep)
**Worktree:** `chemistry`
**Active:** yes — Reto, 2026-10-01.

## Do next

1. **Triage the 21 items below** — read each against `src/precis_pathway`
   and `git log`, mark which still reproduce and which shipped, delete the
   shipped, and re-rank this file off the result. Leverage: every rank
   below is a guess until this is done. Start with the `idea` rows
   (seed-health, NEB, desorption, harvest-bookmark, material-off-sample).
2. **backlog/autocatpath-seed-health.md** — the seed job family's umbrella
   (59 runs dying without `result.json` is the unresolved core). Engine
   reliability gates every number the quest loop consumes; a diagnosis
   pass that must read the first unbuffered failures before picking a
   remedy.
3. **backlog/pathway-step-level-retry.md** — `ready/medium`; turns
   per-step validity attrition (0.95^20) into additive compute. Unblocks
   usable routes from long pathways without a human or tick deciding each
   re-run.
4. **backlog/neb-barriers-in-the-catpath-pipeline.md** — NEB transition-
   state barriers as a pipeline step (barriers, not just thermodynamics);
   waits on the triage to say what the pipeline already carries.
5. **backlog/catpath-desorption-link-kind.md** — desorption edges are
   bookkept as ΔE = 0 like H-supply edges, so CHE math cannot tell a real
   cost from bookkeeping; the fix is a typed link kind on the catpath
   side, not in precis. Small; same engine-extension family as 4.
6. **backlog/autocatpath-aggregate-ran-11h-on-a-33s-job.md** — the
   kinetics ceiling shipped (the subprocess timeout, deploy 2026-09-27;
   `precis_pathway/runner.py`, `tests/test_pathway_kinetics_timeout.py`);
   the item is NOT closed: the castor reproduction that settles the
   cause, the stranded `no_to_nh3_pd` aggregate re-dispatch (needs Reto),
   and the broader blocking-dispatch ceiling remain. Trim the item to
   that remainder during 1.
7. **backlog/pathway-conditions-effects-report.md** — `prio: high`,
   written for quest qu164903 (NO→NH₃): the first consumer-visible
   product of the engine. After 2–3 because a report over unhealthy seeds
   is the artefact problem again.
8. **backlog/autocatpath-integration.md** — the remaining slices of the
   native integration (most shipped; present state is in the
   `src/precis_pathway/` docstrings and ADR 0069); read with 2.

## Horizon

1. **backlog/catalyst-physical-realism.md** + **backlog/slab-modelling-knobs.md**
   — defect ensembles, poisoning, slab knobs; make the engine's answer
   physically honest. After the engine reliability block.
2. **backlog/ephemeral-potentials-for-catpath.md** — a throwaway potential
   as a pre-screen, DFT only on survivors; cost lever, waits on 2 so the
   saving is measured on a healthy pipeline.
3. **backlog/material-off-sample-model.md** (material kind's off-sample
   estimate layer, ADR 0070 deferral) +
   **backlog/harvest-bookmark-concurrency.md** — small `idea`s homed here
   by the sweep; rank off 1.
4. **backlog/protomia-gap-eval-and-molecular-properties.md** — eval a
   candidate tool, then close the molecular-property gap it exposes.
5. **backlog/reaction-kind-and-synthesis-cost.md** — sourced reaction-fact
   store and cost over routes; the long arc past one quest's pathways.
6. **backlog/estimate-kind-ms-chemistry-workup.md** — argue before
   simulating; sets up sims, waits on 5 for facts to argue from.
7. **backlog/composable-pipeline-kind.md** +
   **backlog/chem-tools-integration.md** +
   **backlog/structure-import.md** — chaining and import surface for
   chem tools; presentation/packaging layer, after the engine.
8. **backlog/sim-harness.md** — quest-driven automation, writeup draft and
   container drive path (slices 2–3); consumes roadmap-quest's loop.
9. **backlog/pathway-explorer.md** + **backlog/pathway-viewer-ux-batch.md**
   — the UI. Last by the rank rule: it presents what the engine has to
   get right first. Shared presentation logic belongs to plugin-split
   (Seam).

## Parked

- (none)

## No action needed

- (none yet)

## Seam

- **plugin-split** owns `backlog/pathway-presentation-shared-module.md`
  (its Do-next 6: where shared pathway presentation lives, plus the
  catpath version bump and wheel redeploy). Horizon 9 here must not start
  a second copy of that logic.
- **roadmap-quest** owns the quest loop that consumes pathways (its
  ticks dispatch the jobs this thread makes healthy); the engine's output
  contract is this thread's, the dispatch and priority flow are theirs.
- `src/precis_pathway/` is a plugin package: the plugin import boundary
  (`tests/test_plugin_import_boundary.py`, plugin-split) applies to every
  change here.
