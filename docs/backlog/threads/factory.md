# factory

**Status:** ends when the agent execution lanes never silently starve or
halt, spend is bounded, and a crashed run is recoverable — the platform
under every pillar's agent work (`docs/roadmap.md` platform bucket). Today
twenty filed items cover the starvation, halt, budget and crash gaps
(six added by the 2026-10-02 platform pass); the order is silent loss first (starved lanes and rescue passes,
terminal-silent halts), then bounded spend, then the quality-of-life and
container items.
**Last reviewed:** 2026-10-01
**Worktree:** `factory`
**Active:** no — opens at the next session restart if Reto names it.

## Do next

1. **backlog/worker-rotation-starves-rescue-passes.md** — `prio: high`;
   a starved rescue pass silently stops the thing it rescues for hours, and
   the quest loop was only the first victim.
2. **backlog/claude-inproc-lane-starves-old-jobs.md** — the same silent-
   starvation class on the agent lane (hours-old jobs sit unclaimed while
   new ones run); read with 1.
3. **backlog/halt-classes-and-outage-breaker.md** — agent-declared halts
   stop being terminal-silent, so an outage no longer reads as a task verdict.
4. **backlog/budget-guardrails.md** — the SMALL-tier breaker hole and the
   unscheduled hourly runaway check are filed here as open scope; SMALL is
   all-cloud, so a tripped cap does not stop it.
5. **backlog/bare-rung-billing-accounting.md** — bare chain rungs spend
   dollars the meter never counts; the meter half of the gate that
   shipped, and 4 cannot bound what it cannot see.

## Horizon

1. **backlog/coordinator-crash-recovery.md** — the coordinator executor has
   only the wall-clock sweep for crash recovery; later by choice, the
   backstop works.
2. **backlog/infra-failure-tag-classification-gaps.md** — two failure paths
   skip the infra tag and mis-class as content failures.
3. **backlog/agent-container-capability-probe.md** — deploy the probe
   before the container flip; safety net for 4.
4. **backlog/agent-workspace-containers.md** — `prio: high`; persistent
   workspaces and disposable containers for coder agents; behind 3's probe.
5. **backlog/factory-console-and-scheduling.md** — the remaining console
   and registry scope; the scheduling half lives in
   `backlog/cluster-scheduling.md`.
6. **backlog/tick-tool-lists-and-discovery-reflex.md** — per-job tool lists
   in tick prompts; prompt economy, not a failure mode.
7. **backlog/friction-reflection-enable.md** — flip the default-off friction
   footer once a grouping lane exists to absorb its gripes.
8. **backlog/plan-tick-health.md** — plan_tick (the todo planner) spins and
   exhausts silently; zero-output turn-exhausted ticks log as success.
9. **backlog/plan-tick-context-cut.md** — pre-fetch to shrink planner turns,
   exponential re-tick cooldown; overdue since 2026-08-24, needs one healthy
   baseline first (8).
10. **backlog/fix-gripe-oauth-instead-of-api-key.md** — fix_gripe burns
    metered API dollars; an OAuth path exists. Spend bound, so near 4.
    Platform pass 2026-10-02.
11. **backlog/llm-quota-failure-classification.md** — router-owned LLM failure
    classification and deferred retry; the quota half of 3's outage class.
12. **backlog/fixer-salvage-failed-builds.md** — push failed fixer build
    branches instead of discarding them.
13. **backlog/prioritization-auto-scoring.md** — severity x frequency
    auto-score plus a human triage loop.
14. **backlog/backlog-groomer-items-half.md** — groomer for work items;
    blocked on two prereqs.

## Parked

- **backlog/dark-factory-arming.md** — arming the gripe-fix loop dials;
  unparks on Reto's ruling (td461205).

## No action needed

- (none yet)

## Seam

`monitors-that-go-quiet` owns signals that lie; this thread owns the lanes
that stop. `local-compute` consumes the budget and accounting items
(`backlog/llm-cost-accounting.md` stays there).
