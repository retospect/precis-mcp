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
paragraph per section; next comes importing the missing methods papers.
**No GPAW relax has ever completed in prod** (the two DFT-rung jobs ever
dispatched both failed; the 09-18 serial run measured 10–18 min per ionic
step, gr346449). So the timed MPI run is also the first completion test: if
it fails for any reason other than speed, chemistry reports and stops.
Ruled 2026-10-02 (chemistry-6, option 1): the orchestrator rebuilds the MPI
image on pollux in the round-2 window; pollux stays the `dft` host
(local-compute). The pollux image (2026-06-21) has no MPI. Branch
`chemistry-dft-mpi-env` (324559cda, with the orchestrator to gate) renders
`PRECIS_DFT_MPI_RANKS`, `PRECIS_DFT_OMP_THREADS`, `PRECIS_DFT_CPUSET` and
`PRECIS_DFT_PAW_DIR` from the dft role; all default to today's behaviour.
Setting values on pollux (8 ranks, 1 thread, the fast cores, node-local
scratch while `/mnt/cluster` is hung) is a prod config change the
orchestrator takes with the round-2 deploy and lists for Reto. Before the
build: the new Dockerfile bakes no PAW datasets and nothing mounts them, so
copy them out of the June image first and point `dft_paw_dir` at them.
Measure: wall per SCF iteration; one ionic step at 1 rank on the new image
first, then 8 ranks (review note §5). Rank the items below by what that
paper needs.

1. **gr322060** (relax-infra tracker). gr345366 closed 2026-10-03:
   no parse failures on 63301c5c across three quests' ticks.
   `backlog/qu164903-campaign.md` residuals are ops/Reto
   (st164913 un-rule-out, kinetics cutover prod write, presentation items);
   `backlog/quest-seed-orphan-recovery.md` is down to its audit half.
2. **qu202467 (NO from exhaust → fertilizer N) restart** — RULED
   2026-10-02 (chemistry-7, option 1): restart after round 2 deploys,
   capped at 3 ticks. No tick-cap mechanism exists, so chemistry enforces
   it by hand: set STATUS:active after the deploy, record `meta.tick_count`
   at that moment, and when it reaches +3, re-pause to dormant unless one of
   those ticks cited [pa220629], [pa215329], [pa198891], [pa417969] or
   [pa202897] or changed the ledger from them. Report the outcome either way.
   **Outcome 2026-10-03:** restarted 13:51Z; the 17:11Z tick read all five
   papers, closed both gold-bridge fronts as off-target, credited
   pc2727852. Per the ruling it stays active (it cited them);
   recommendation to re-pause is review-queue `chemistry-9`; Reto 20:05Z
   (via knowledge-mesh-12): qu202467 is the knowledge-mesh measures pilot;
   held DORMANT (set 20:06Z, no ticks) until that pilot lands (~4 builds,
   knowledge-mesh thread). Restart is knowledge-mesh's call, not ours. tick_count
   did not advance on that tick (stuck at 314; gr464538), so count
   ticks from the quest's chunks, not meta. The "six unresolved gold stubs" blocker was
   a visibility defect: five had bodies since August/September, but the
   tick's literature section shows about 12 of 484 served papers, and the
   cite instruction read "unlisted" as "stub". Fixed in abf642971
   (`tick.py::_served_papers_detail`: held/stub split on the cut line, plus
   a status line for each cut paper the logbook names); general to every
   quest with more than about 12 served papers. Recommended: restart after
   round 2 deploys, capped at 3 ticks, re-pause if it doesn't cite the five.
   Not fixed: the frontier-table sync wall (undiagnosed) — the next
   blocker if the quest holds again.
3. **backlog/autocatpath-seed-health.md** — PARTIAL (child-killed fix
   a772a52aa and the 0.11 tier overlays shipped). Prod read 2026-10-03:
   the August "child exited without result.json" population and the lease
   churn are gone in the last 30 days; all 24 seed failures are 5–7.5 h
   wall kills from the weeks of 09-14/09-21, and the week of 09-28 has 68
   succeeded, 0 failed. Wall kills now stamp `failure_class='timeout'`
   (2026-10-03). Cause found 2026-10-03: all 24 are verify-tier seeds
   (24 of 27 failed; neb 54/54, screening 61/61), which get the same
   90 min hint / 2.5 h lease as every tier, so **no verify pathway has
   ever completed in prod** — the paper's authoritative pass. Shipped
   2026-10-03: `PRECIS_AUTOCATPATH_VERIFY_WALL_SECONDS` (unset = general
   wall) and the harvest holds a timed-out seed instead of re-running it at
   the same wall. **Measured 2026-10-03:** verify seed job 464221
   (pathway 449732 seed 0, pollux, unpinned) ran 13:57:55Z–19:03:04Z =
   **5 h 05 m**, 50 states; the child grows to ~76 GB GPU memory, so
   budget one whole GPU per seed. Wall value 28800 (2× = 36618 s, cap
   binds; margin 1.57×) sent via `scripts/round eta`. **Waiting on Reto:**
   review-queue `chemistry-8` — 3 seeds (~137 GPU-h) / 2 seeds (~92) /
   best_first kept (~16) for the 9 qu164903 (NO→NH3 on Pd(111))
   candidates. Nothing re-dispatches before the answer; the run then
   sets `params.resources.cpuset` (shipped 2026-10-03).
4. **backlog/pathway-step-level-retry.md** — PARTIAL (the ladder half,
   `promote_tiers` off-frontier promotion, shipped 2026-09-16). Left: the
   per-step re-queue with a fresh seed, which turns 0.95^20 attrition into
   additive compute.

## Horizon

Items 1-4 and 6 are engine items moved down from Do next 2026-10-01 to keep
it at five; engine-over-UI still holds.

1. **backlog/neb-barriers-in-the-catpath-pipeline.md** — NEB transition-
   state barriers as a pipeline step (barriers, not just thermodynamics);
   triage 2026-10-02: no code yet, the item is a design call (endpoint
   pairing, cost gate, convergence handling). Endpoint pairing now has a
   candidate method, filed in the catpath repo as
   `docs/backlog/global-endpoint-sampling-nebscape.md` (Reto, 2026-10-02;
   Jung et al. 2026, `pa329700`). It proposes global-optimised IS/FS plus
   reaction atom mapping in place of recipe pose + rattle, aimed at
   qu164903's endpoint-agreement trust failures, with precis's AGOX
   `struct_search` as an optional endpoint generator. Measure first; no
   build without a ruling.
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
5. **backlog/chem-database-tie-ins.md** — MOF and catalyst database
   tie-ins (Reto, 2026-10-02): structure seeds, reference energies,
   screening. Decided 2026-10-02: MOF library + ODAC23 references,
   Catalysis-Hub first (Reto requests the SUNCAT credentials), on-demand
   import, show-and-flag only. Build order in the item; step 1 (CIF import
   + CoRE MOF/QMOF adapter) needs no credentials. Ranked here because the
   November trust-demo paper needs reference energies UMA was not trained
   on.
6. **backlog/autocatpath-integration.md** — the remaining slices of the
   native integration (most shipped; present state is in the
   `src/precis_pathway/` docstrings and ADR 0069); read with 4.
7. **backlog/catalyst-physical-realism.md** + **backlog/slab-modelling-knobs.md**
   — defect ensembles, poisoning, slab knobs; make the engine's answer
   physically honest. After the engine reliability block. Slab knobs:
   `remove_atom` op and variable-cell relax (precis-dft container side)
   open, `n_slab` provenance half-threaded.
8. **backlog/ephemeral-potentials-for-catpath.md** — a throwaway potential
   as a pre-screen, DFT only on survivors; cost lever, waits on Do next 3 so
   the saving is measured on a healthy pipeline.
9. **backlog/quest-data-table-and-formula-discovery.md** — the
   `precis quest table` export verb does not exist yet, then the
   deterministic baseline; `meta.params`, `view='series'` and the
   `wrong_site` distrust gate (575e24f38) shipped.
10. **backlog/quest-artifacts-in-dossier.md** — embed the existing
    pareto/energy-profile renders in the quest's document; the dossier target
    is superseded by `backlog/quest-graph-as-dossier.md`
    (graph-memory-consumers Horizon 1), so this waits on its design.
11. **backlog/material-off-sample-model.md** (material kind's off-sample
   estimate layer, ADR 0070 deferral) +
   **backlog/harvest-bookmark-concurrency.md** — small `idea`s homed here
   by the sweep; harvest-bookmark has no live repro (one job per
   candidate today).
12. **backlog/protomia-gap-eval-and-molecular-properties.md** — eval a
   candidate tool, then close the molecular-property gap it exposes.
13. **backlog/reaction-kind-and-synthesis-cost.md** — sourced reaction-fact
   store and cost over routes; rung 1 (the `rxn` kind, e2f420ab4) shipped,
   rungs 2-5 left.
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
