# factory

## Resume

- **Pillar:** platform
- **Next:** When activated, make agent-declared halts stop being terminal-silent.
- **Blocked by:** No declared active owner.
- **Unblocks:** Reliable autonomous execution lanes.
- **Acceptance:** Use [halt-classes-and-outage-breaker](../halt-classes-and-outage-breaker.md) and the verification steps in [Do next](#do-next); check current deployment and worktree state before acting.
- **Worktree:** `factory`
- **Builds:** Not estimated here; use the owning item's current slice estimate.
- **Detail:** [Ranked work](#do-next) · [Horizon](#horizon) · [Coordination map](INDEX.md).

## Thread context

Current declared activity: [fleet roster](../../../.claude/fleet/threads.tsv); dated allocation decisions below are historical.

**Status:** ends when the agent execution lanes never silently starve or
halt, spend is bounded, and a crashed run is recoverable — the platform
under every pillar's agent work (`docs/roadmap.md` platform bucket). Today
twenty filed items cover the starvation, halt, budget and crash gaps
(six added by the 2026-10-02 platform pass); the order is silent loss
first (terminal-silent halts; the starved lanes and rescue passes shipped
2026-10-15 with residuals in Horizon), then bounded spend, then the
quality-of-life and container items.
**Last reviewed:** 2026-10-02 (fix lane dropped)
**Worktree:** `factory`
**Allocation decision (historical):** no — opens at the next session restart if Reto names it.

## Do next

1. **backlog/halt-classes-and-outage-breaker.md** — agent-declared halts
   stop being terminal-silent, so an outage no longer reads as a task verdict.
2. **backlog/budget-guardrails.md** — the SMALL-tier breaker hole and the
   unscheduled hourly runaway check are filed here as open scope; SMALL is
   all-cloud, so a tripped cap does not stop it.
3. **backlog/bare-rung-billing-accounting.md** — bare chain rungs spend
   dollars the meter never counts; the meter half of the gate that
   shipped, and 2 cannot bound what it cannot see.

## Horizon

1. **backlog/worker-rotation-starves-rescue-passes.md** — the rescue
   starvation shipped (overdue rescue passes age to the front of the
   rotation); left is the one unbounded job pass still inside the wait.
2. **backlog/claude-inproc-lane-starves-old-jobs.md** — the starvation
   shipped (claim ageing); left are plan_tick's failure rate, quiet claim
   skips and request timeouts on the crossref/enrich passes.
3. **backlog/coordinator-crash-recovery.md** — the coordinator executor has
   only the wall-clock sweep for crash recovery; later by choice, the
   backstop works.
4. **backlog/infra-failure-tag-classification-gaps.md** — two failure paths
   skip the infra tag and mis-class as content failures.
5. **backlog/agent-container-capability-probe.md** — deploy the probe
   before the container flip; safety net for 6.
6. **backlog/agent-workspace-containers.md** — `prio: high`; persistent
   workspaces and disposable containers for coder agents; behind 5's probe.
7. **backlog/factory-console-and-scheduling.md** — the remaining console
   and registry scope; the scheduling half lives in
   `backlog/cluster-scheduling.md`.
8. **backlog/tick-tool-lists-and-discovery-reflex.md** — per-job tool lists
   in tick prompts; prompt economy, not a failure mode.
9. **backlog/friction-reflection-enable.md** — flip the default-off friction
   footer once a grouping lane exists to absorb its gripes.
10. **backlog/plan-tick-health.md** — plan_tick (the todo planner) spins and
   exhausts silently; zero-output turn-exhausted ticks log as success.
11. **backlog/plan-tick-context-cut.md** — pre-fetch to shrink planner turns,
   exponential re-tick cooldown; overdue since 2026-08-24, needs one healthy
   baseline first (10).
12. **backlog/fixer-salvage-failed-builds.md** — push failed fixer build
    branches instead of discarding them.
13. **backlog/prioritization-auto-scoring.md** — severity x frequency
    auto-score plus a human triage loop.
14. **backlog/backlog-groomer-items-half.md** — groomer for work items;
    blocked on two prereqs.

## Parked

- (none)

## No action needed

- **The fix_gripe lane and the gripe-loop arming ladder** — closed by Reto
  2026-10-02 (td460703, "Drop the lane, we run locally session here"). Lane
  OFF: prod `service_config` melchior/`backlog_groom` prio 5 to 0 at
  2026-10-02 22:04Z; re-arm only on Reto's say with
  `precis service prio melchior backlog_groom 5`. Code stays;
  `diagnose_scan` is off too (melchior prio 0, Reto 2026-10-02); owning thread sessions fix gripes. Deleted
  `backlog/fix-gripe-oauth-instead-of-api-key.md` and
  `backlog/dark-factory-arming.md` as moot. `fixer-salvage-failed-builds`,
  `prioritization-auto-scoring` and `backlog-groomer-items-half` presume the
  lane and need a keep-or-drop pass.

## Seam

`monitors-that-go-quiet` owns signals that lie; this thread owns the lanes
that stop. `local-compute` consumes the budget and accounting items
(`backlog/llm-cost-accounting.md` stays there).
