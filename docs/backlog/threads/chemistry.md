# chemistry

**Status:** ends when precis can propose, compute and cite a reaction
pathway and its conditions for a quest, with catalysis (autocatpath, the
catpath engine) as the main line. Catalysis lives under chemistry (Reto,
2026-10-01, Pillar 2 review); code in `src/precis_pathway`, `../catpath`
is the reference engine. Today the 2026-10-02 triage is done: no item
shipped whole, most are PARTIAL with a named remainder (recorded per
entry below); engine reliability ranks above everything that presents or
packages its results.
**Last reviewed:** 2026-10-02 (every item triaged against the code and commit history)
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

1. **gr345366 cause B + gr322060** — qu164903 ticks again (last tick
   succeeded 2026-10-01 21:14Z, failure count 0). The 2026-10-01 rest was
   quota notices again, not parse failures: `result_from_agent`'s guard
   missed the CLI's new `terminal_reason='completed'` stamp; fixed
   2026-10-02 (`router.py::result_from_agent`), protects every quest only
   once deployed. Left: cause B (stray `]` after `dossier_text`, needs a
   decision) and gr322060, the relax-infra tracker.
   `backlog/qu164903-campaign.md` residuals are ops/Reto (st164913
   un-rule-out, kinetics cutover prod write, presentation items);
   `backlog/quest-seed-orphan-recovery.md` is down to its audit half.
2. **qu202467 restart report** — paused 2026-10-01 ($6,211 tote, 0 deeds,
   holding ticks). Restart condition (in its logbook): this thread reports at
   least one named blocker fixed (frontier-table sync wall; literature
   fetch-step bottleneck / six unresolved gold stubs; no tool to check stub
   fetch status without re-searching) AND names the next measurement a tick
   would make; Reto decides on that report.
3. **backlog/autocatpath-seed-health.md** — PARTIAL (child-killed fix
   a772a52aa and the 0.11 tier overlays shipped). Left: read the first
   unbuffered prod failures before picking a remedy, the re-lease churn
   evidence, the dev-loop decisions. Engine reliability gates every
   number the quest loop consumes.
4. **backlog/quest-redispatch-tier.md** — confirmed open:
   `quest/compute.py::redispatch_candidates` calls `dispatch_autocatpath`
   with no `tier=`, so every re-dispatch runs at neb. Moved up from
   Horizon: each re-dispatch at the wrong rung spends neb compute and
   records a rung the candidate never earned; code-only and small.
5. **backlog/pathway-step-level-retry.md** — PARTIAL (the ladder half,
   `promote_tiers` off-frontier promotion, shipped 2026-09-16). Left: the
   per-step re-queue with a fresh seed, which turns 0.95^20 attrition into
   additive compute.

## Horizon

The first five are engine items moved down from Do next 2026-10-01 to keep
it at five; engine-over-UI still holds.

1. **backlog/neb-barriers-in-the-catpath-pipeline.md** — NEB transition-
   state barriers as a pipeline step (barriers, not just thermodynamics);
   triage 2026-10-02: no code yet, the item is a design call (endpoint
   pairing, cost gate, convergence handling).
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
   and the broader blocking-dispatch ceiling remain (triage 2026-10-02
   confirmed exactly that remainder).
4. **backlog/pathway-conditions-effects-report.md** — `prio: high`,
   written for quest qu164903 (NO→NH₃): the first consumer-visible
   product of the engine. Phases 0-1 shipped (12b0a5e68); Phase 2 (report
   step) and Phase 3 (coverage job, cross-repo) are left. After Do next
   3-5 because a report over unhealthy seeds is the artefact problem again.
5. **backlog/autocatpath-integration.md** — the remaining slices of the
   native integration (most shipped; present state is in the
   `src/precis_pathway/` docstrings and ADR 0069); read with 4.
6. **backlog/catalyst-physical-realism.md** + **backlog/slab-modelling-knobs.md**
   — defect ensembles, poisoning, slab knobs; make the engine's answer
   physically honest. After the engine reliability block. Slab knobs:
   `remove_atom` op and variable-cell relax (precis-dft container side)
   open, `n_slab` provenance half-threaded.
7. **backlog/ephemeral-potentials-for-catpath.md** — a throwaway potential
   as a pre-screen, DFT only on survivors; cost lever, waits on Do next 3 so
   the saving is measured on a healthy pipeline.
8. **backlog/quest-data-table-and-formula-discovery.md** — the
   `precis quest table` export verb does not exist yet, then the
   deterministic baseline; `meta.params`, `view='series'` and the
   `wrong_site` distrust gate (575e24f38) shipped.
9. **backlog/quest-artifacts-in-dossier.md** — embed the existing
    pareto/energy-profile renders in the quest's document; the dossier target
    is superseded by `backlog/quest-graph-as-dossier.md`
    (graph-memory-consumers Horizon 1), so this waits on its design.
10. **backlog/material-off-sample-model.md** (material kind's off-sample
   estimate layer, ADR 0070 deferral) +
   **backlog/harvest-bookmark-concurrency.md** — small `idea`s homed here
   by the sweep; harvest-bookmark has no live repro (one job per
   candidate today).
11. **backlog/protomia-gap-eval-and-molecular-properties.md** — eval a
   candidate tool, then close the molecular-property gap it exposes.
12. **backlog/reaction-kind-and-synthesis-cost.md** — sourced reaction-fact
   store and cost over routes; rung 1 (the `rxn` kind, e2f420ab4) shipped,
   rungs 2-5 left.
13. **backlog/estimate-kind-ms-chemistry-workup.md** — argue before
   simulating; sets up sims, waits on 12 for facts to argue from.
14. **backlog/composable-pipeline-kind.md** +
   **backlog/chem-tools-integration.md** +
   **backlog/structure-import.md** — chaining and import surface for
   chem tools; presentation/packaging layer, after the engine.
15. **backlog/sim-harness.md** — quest-driven automation, writeup draft and
   container drive path (slices 2–3); consumes roadmap-quest's loop.
16. **backlog/pathway-explorer.md** + **backlog/pathway-viewer-ux-batch.md**
   — the UI. Last by the rank rule: it presents what the engine has to
   get right first. Shared presentation logic belongs to plugin-split
   (Seam).
17. **backlog/chem-name-lookup-verb.md** — PubChem-backed
   formula/ID → common name; small, and a dependency of
   reaction-kind-and-synthesis-cost (12). From pillar 4, 2026-10-01.
18. **backlog/catpath-wheel-version-reuse.md** — one catpath version across
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
