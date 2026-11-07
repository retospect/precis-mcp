---
roster: active
pillar: platform
effort: medium
---
# factory

## Resume

- **Next:** Make agent-declared halts stop being terminal-silent (Do next 1).
- **Blocked by:** Nothing; active from 2026-10-17 (Reto: already landing).
- **Unblocks:** Reliable autonomous execution lanes.
- **Acceptance:** Use [halt-classes-and-outage-breaker](../halt-classes-and-outage-breaker.md) and the verification steps in [Do next](#do-next); check current deployment and worktree state before acting.
- **Worktree:** `factory`
- **Builds:** Not estimated here; use the owning item's current slice estimate.
- **Detail:** [Ranked work](#do-next) · [Horizon](#horizon) · [Coordination map](INDEX.md).

## Thread context

Current declared activity: the `roster:` front matter above; dated allocation decisions below are historical.

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
**Allocation decision:** active — Reto 2026-10-17 (already landing).

## Do next

1. **backlog/halt-classes-and-outage-breaker.md** — agent-declared halts
   stop being terminal-silent, so an outage no longer reads as a task verdict.
2. **backlog/budget-guardrails.md** — the SMALL-tier breaker hole and the
   unscheduled hourly runaway check are filed here as open scope; SMALL is
   all-cloud, so a tripped cap does not stop it.
3. **backlog/bare-rung-billing-accounting.md** — bare chain rungs spend
   dollars the meter never counts; the meter half of the gate that
   shipped, and 2 cannot bound what it cannot see.
4. **backlog/sandbox-python-script.md** — `prio: high`; arbitrary Python
   in a no-network container (`sandbox_run mode:script`), from a job or a
   `run:python` todo. Needs no OAuth, so it does not wait on OAuth;
   started 2026-10-18 on Reto's ask. Slice 1 (job-path `mode:script`:
   inline `code`, PyPI wheel lock, `--network none`, harvest on any exit)
   shipped 2026-10-18, slice 2 (`inputs`, `code_ref`, a read-only MCP
   on a unix socket) the same day; next is slice 3 (todo sugar). Not
   live until slice 4 lights castor + pollux, which first needs async
   staging and the package allow-list review.
5. **backlog/relax-slices.md** — `STATUS:yielded` + a per-claim wall
   budget so a checkpointed long relax gives the lane a turn; in
   progress on `worktree-g8-relax-slices`, flag-gated off until r91
   shows a real checkpoint resume on prod.

## Horizon

0. **backlog/dark-factory.md** — the frame for this thread's end state
   (Reto 2026-10-30): precis holds work state, picks the builder lane,
   keeps the build cadence, shows one hierarchy. Landed as a spec
   2026-10-30; its work lives in prod: platform quest qu540835, root
   todo td540836, the six step-1/1b slices under it (work view, gripes
   in the tree, `/work`, fleet reporter, asks list, `precis-work-help`).
   Lanes (`src/precis/plans/lanes.py`): codex on 2026-11-04 (code only,
   no MCP), so the templates class every MCP-bound node by its step
   (triage, brief, review, validate, pcb) and only builds default to
   `code`; `run_slice` refuses a generator on an MCP-less lane, and a
   triage-pinned model narrows the pick to lanes that run it
   (`Lane.accepts_model`; codex declares none, so a claude id never
   lands there). Nodes minted before the classes (plan 543631) carry a
   `params.lane` pin instead. `gripe-fix-direct` (td564019) drops triage
   and brief so a codex build needs no Claude step; the gripe text rides
   the build body. First run (jo565335, gr564261): codex built the fix but
   its workspace-write sandbox keeps `.git` read-only, so `run_slice.poll`
   now commits a clean session's tree with its `scratch/commit-msg.txt`
   instead of widening codex's writable roots. Its escalation then burned
   an attempt on the Claude weekly 429 (gr565359): a quota or rate result
   now parks the slice with no attempt, resubmitting at once on another
   vendor's lane when one fits, else at the parsed reset. Intake:
   `workers/gripe_dispatch.py` (td564019 cut 3, dark until a
   `service_config` row) mints a `gripe-fix-direct` chain per free
   code-lane slot for the hottest agent- or human-filed `fix:code` gripe
   with no chain yet; Reto lights it. Review and validate off Claude
   (td564019 cut 1, Reto 2026-11-04 "route to another devin for now"):
   the reviewer's verdict already rode the final message
   (`handoff: review: …`), so `run_slice` now refuses an MCP-less lane only
   to child-minting generators, inlines the reviewed todo's `landed:` sha
   and brief into a reviewer's brief, the validate body carries the gripe
   report, and the codex and devin lanes fit `review` and `validate`.
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
15. **Factory release loop (td570415)** — `release_cut` → `ci_verdict_poll`
    / `ci_red_digest` → approval → `release_deploy` → `release_confirm`,
    plus `release_poll`'s mint gate and stuck watchdog and
    `scripts/round supersede --partial`, landed across S1-S4 (td573237),
    S5 (td573429), S6 (td573430), S7 (td573431), S8 (td573432), S9
    (td573809), S10 (td574370) and S11 (td574366). It has never run in
    prod and is not armed; the open blockers and arming order sit in
    `docs/runbooks/release-cycle.md` §Factory loop.
16. **Router apply (td568209, 2026-11-06)** — mode `on` now dispatches
    the chooser's pick once with ladder fallback, the
    `llm.decide.kind.<task_kind>` per-kind switch, a tier-floor rule and
    a 30-minute health guard. The first prod flips are the
    coordinator's, per kind.
17. **Kimi interactive builder lane (td576126, 2026-11-06)** — landed:
    `fleet-wave launch NAME --kimi` starts a tier R interactive pane
    through `scripts/kimi-tree --tmux` (cap `FLEET_KIMI_MAX` 2), the
    brief is typed into the pane, deny rules bite through the tree's own
    `kimi-adapt.py` hook (trees cut after the landing only), and
    event-watch gained the handoff/concurrent/quota reflexes. Not
    covered: the STEWARD gate proving Never Ask honours PreToolUse
    exit 2, and the real `kimi login` state — untested until the first
    real pane.

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

## G1 (slice-g1, 2026-11-06Z)

Principal-on-writes and the observe-mode slice scope landed on this
branch (gr575691, td575705 G1): every write stamps a server-set
principal (`slice:<grant>:<role>`, `install`, `user:<abbrev>`,
`personal:<id>`, `worker:<source>`) into `chunks.meta.principal`,
`filed_by.principal` + `revisions.writer.principal`, and
`ref_tags.principal` (migration 0287); a slice grant's writes are gated
by role under `mcp.slice_scope` (DB-only, default `observe`,
would-refuses marked on `tool_calls` and read via
`get(kind='agentlog', id='/scope?…')`). Docs: `precis.principal`
docstring, `docs/conventions/mcp-authoring.md` §Write principals,
`docs/runbooks/dark-factory-arming.md` §Slice write scope,
`precis-work-help` "What may a dispatched slice write". Next per spec:
~24 h of observe, fill `SLICE_TAG_ALLOW` from the readout, then
`enforce`; G3 is the OS-user boundary.
