# Quest backpressure was blind to the autocatpath_seed fan-out

**Root cause (fixed, branch `feat/quest-backpressure-simtypes`).** The discovery
loop's per-quest backpressure ("no new batch while the previous is in flight")
and the cross-quest starvation gate both counted non-terminal jobs in
`quest_tick._SIM_JOB_TYPES = ("autocatpath_explore", "struct_relax")`. The
barrier lane was refactored to the `autocatpath_seed` / `autocatpath_aggregate`
fan-out (47332ad3), retiring `autocatpath_explore` — but the wait set was never
updated, and `_pending_sim_ids` joined only a *single* parent hop while a seed
job sits three levels below its candidate (seed_job → seed_todo → agg_todo →
candidate → serves → quest). So both gates saw only the fast `struct_relax`
lane, went empty the moment a relax finished, and let the loop propose a fresh
batch every slice regardless of a deep seed queue. One catalyst quest (164903)
piled up **238 seeds**. Fix: add the seed/aggregate types to `_SIM_JOB_TYPES` +
rewrite `_pending_sim_ids` as a subtree walk; regression test in
`tests/test_quest_tick_job.py::TestSimWaitSetReachesBarrierFanout` (the existing
phase-machine tests stub these helpers, which is why it slipped through).

**Operational residual — quest 164903 (do after the catpath rework deploys).**
To stop the bleed, 164903 was set `STATUS:dormant` and its ~117 queued
`autocatpath_seed` jobs `cancelled` (seed *todos* kept as the dedup guard); the
one running job killed. Re-activate + re-dispatch once catpath is redeployed.
The queued seeds were frozen at their dispatch-time config, so re-dispatch (not
un-cancel) is what picks up the new tool.

**pose_count is a prod-DB config value, not code.** 164903's
`meta.reaction_config.search.pose_count = 6` was hand-stamped in prod (code
default is `autocatpath.config.SearchConfig.pose_count = 4`; precis's seed-time
`REACTION_CONFIG` sets it nowhere). Cutting the per-seed cost is a DB edit on the
quest config (`jsonb_set … '{reaction_config,search,pose_count}' '4'`), forward
only — frozen queued jobs keep their snapshot.

**Open design question (see `quest-job-sequencing`).** The gate now *works*, but
the right in-flight target is unsettled: `quest-job-sequencing` frames "many
pending jobs per quest" as the natural state, while the operator's intent here is
"a couple in flight, look, decide next." Reconnecting the sensor restores
one-batch-at-a-time; whether the WIP cap should be tighter/looser (and priced
against the one-GPU-at-a-time, hours-per-seed drain) is a tuning call, not a bug.
