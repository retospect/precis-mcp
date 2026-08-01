---
status: draft
title: Cluster scheduling — one decentralized claim substrate, every scheduling concern a policy on it
model: opus
---

# Cluster scheduling (unified)

## Motivation / why

Scheduling reasoning is scattered across four proposals, one design section, and
two undocumented in-code mechanisms — with no single place that says *how the
cluster decides what runs, where, when, and in what order*:

- **Recurring-clock** — `docs/design/factory-console-and-scheduling.md` §15i
  ("one scheduler, decentralized"), half-built: the dark `scheduler` pass
  (`workers/scheduler.py`) folds two launchd timers; the rest still fire from
  standalone launchd plists.
- **Priority-claim + the spark wedge** — `gpu-priority.md` (human-first `PRIO:`,
  reserve mode, kill backstop, and the per-seed chunking that fixes the
  `autocatpath_explore` GPU wedge, `gr180096`).
- **Topology** — `gpu-cluster-modes.md` (fuse N Sparks for one big model vs
  split for many jobs).
- **Liveness** — `health-watchdog.md` (a freshness digest so scheduled work
  can't silently stop — the melchior-lockout failure mode).
- **Two undocumented in-code cadence mechanisms** — bespoke `app_state`
  `last_run` + advisory-lock throttles (paper_reconcile, llm_reconcile,
  backlog_groom, corpus_reconcile, clusterize), and the standalone launchd
  timers (dream, cron-tick, watch-poll, reconcile, anki-sync).

The fragmentation is the problem: a reader (human or fixer) can't see the whole,
and the same concern (a cadence, a claim order) is re-implemented per producer.
This proposal is the **master frame** — it states the unifying model and the
cross-axis phasing, and defers each axis's mechanical detail to its sub-spec.

**One user changes everything.** There is effectively one human user, so none of
this needs a multi-class fairness scheduler — the hard parts of cluster
scheduling (fair-share, gang scheduling, bin-packing, mid-kernel preemption)
are all out of scope by construction. What remains is small.

## The unifying model

**precis already has exactly one scheduling substrate: the decentralized derived
queue** (ADR 0007/0017, §5 of the scheduling design). Workers *pull* ready work
with `FOR UPDATE SKIP LOCKED`; a claim is a **reserve-at-claim** conditional
advance (`UPDATE … WHERE still_available RETURNING`) — the advance *is* the lock.
No dispatcher, no singleton, no SPOF. Every "scheduler" concern people reach for
is not a second system — it is a **policy layered on that one claim**:

| Concern | Policy on the claim | Sub-spec |
|---|---|---|
| **When** recurring work fires | a conditional-advance lease **on time** (mint a job when `next_fire_at ≤ now()`), run by every worker → exactly-once, no designated node | §15i + this §A |
| **Which** ready job a worker takes next | the **sort** in the claim query (`PRIO:` human-first) | gpu-priority §B |
| **Whether** heavy background may start | a dispatch **gate** (reserve mode stops minting/claiming) | gpu-priority §B |
| **Where** a job runs | **capability-reserved** claim (agentic → agent host, GPU → GPU host) | §5 (built) |
| **How** the GPUs are shaped | a pull-based **hysteretic mode switch** (fuse/split) | gpu-cluster-modes §C |
| **That** it's still running | a liveness **digest** over last-fired/heartbeat state | health-watchdog §D |
| **That** a unit is short + reclaimable | **chunking** (per-seed) + **kill-with-GPU-reclaim** | gpu-priority §B |

The invariant that ties them together: **correctness guarantees (exactly-once,
liveness, GPU reclamation) live in Postgres or in a verified kill — never in a
designated host.** A host being down must never drop a fire, wedge a card, or
silently stall a cadence. That single law is what makes a decentralized,
single-user cluster safe without a scheduler daemon.

## In scope

Five axes, phased so the **urgent correctness fix ships first** and each phase
is independently valuable. §B-1 (the wedge) is the one live production bug; the
rest is consolidation and responsiveness.

### §B-1 — Fix the spark GPU wedge (per-seed chunking) — FIRST, it's a live bug
`autocatpath_explore` runs the whole NO→NH₃ network × 3 seeds × full NEB as one
~90-min in-process MACE blob that overruns its lease and, being an
un-interruptible CUDA call, takes the worker down (SIGTERM-deaf → SIGKILL; 81
starts / 0 completions, `gr180096`). Fan it out per `(model, seed)` →
content-addressed jobs → `aggregate_partials`. Detail + build steps:
`gpu-priority.md` Phase 1 and the code-grounded design of record
`docs/design/autocatpath-integration.md` §3.8. This is a `coder-chain`-sized
build touching `quest/compute.py::dispatch_autocatpath` + the
`autocatpath_explore` plugin. **Operational stop-gap — ACTIVE as of 2026-08-01:**
the NO→NH₃ quest (`quest:164903`) was set `STATUS:dormant` so the allocator stops
minting autocatpath compute and spark stops re-wedging. **Revert** (flip 164903
back to `STATUS:active`) is a required step of shipping §B-1 — a dormant quest
left dormant is silently-stopped research (the §D failure mode). Full trail:
`gr180096`.

### §A — Finish the decentralized recurring-clock (flip §15i on, retire timers)
The `scheduler` pass (`workers/scheduler.py`) already folds `cron_tick` +
`watch_poll` behind a lease-backed conditional advance, dark
(`PRECIS_SCHEDULER_ENABLED` unset). Complete it:
- Fold the remaining foldable **launchd** cadence — `anki_sync` (the
  `precis_anki_sync` plist) — into `CADENCES` (a one-line addition once it
  exposes a store-taking callable, per the module's own note), adding a
  **host-affinity** field to `Cadence` for the host-specific ones.
  (`news_poll` is *not* a launchd timer — it is already an in-loop worker pass
  with its own `app_state` throttle and no `precis_news_poll` plist, so it needs
  no fold under §A; it's a §E consolidation candidate instead, if anything.)
- Flip `PRECIS_SCHEDULER_ENABLED` fleet-wide and **retire the folded launchd
  plists** — the SPOF removal: a fire is now dropped only if the *entire* fleet
  is down, and `catch_up` fires those late-not-lost on recovery.
- `dream` (claude_inproc-pinned to melchior) and `reconcile` (single-host by
  design) **stay standalone** — deliberate capability pins, not SPOFs to fix
  (per §15i and the `scheduler.py` comment).

### §B-2 — Priority-claim + reserve + kill backstop
Human responsiveness on the shared cluster, single-user-simple:
- **Human-first claim — verify + correct the *existing* prio sort, don't add
  one.** The claim path already orders on `refs.prio`:
  `workers/executors/_common.py::claim_executor_jobs` does
  `ORDER BY COALESCE(r.prio, 5) DESC, r.ref_id ASC` (shared by all four
  executors), and `workers/dispatch.py::mint_child_job` already copies the
  parent todo's `prio` onto every dispatched job. So the wiring is live — but
  its **direction looks inverted**: the todo-tree convention (migration
  `0014_refs_prio.sql`, `handlers/todo.py`) is *lower number = more urgent*
  (`prio=1` preempts), while the claim's `DESC` rewards the *highest* number
  first — so as shipped a `PRIO:` urgent (prio=1) GPU job may sort *behind*
  deferrable background (prio=10). §B-2's real work is therefore: (a) confirm
  the intended direction and reconcile the sort with the `0014` convention (a
  sign flip, or an explicit decision that claim-prio is high-wins and the
  convention doc is wrong), and (b) confirm it threads for GPU jobs
  specifically. This resolves `gpu-priority.md`'s open twin ("confirm the todo
  `PRIO:` axis threads to job claim order for GPU jobs"). Not greenfield, and
  not "one comparison" — a correctness reconciliation of a live sort.
- **Reserve mode** — a TTL'd `service_config` flag the dispatch gate reads:
  stop minting/claiming *new heavy background* GPU jobs; the in-flight one
  finishes and the box is the human's. Plus a night `meta.schedule` for heavy
  background.
- **Kill backstop** — for "I want the card *now*": force-kill the holder with
  **verified GPU reclamation** (reuse `struct_relax`'s `kill_container` +
  `reset_gpu`; a bare lease-refund does not reclaim a live wedged CUDA process).
Detail: `gpu-priority.md` Phases 2–4.

### §C — GPU topology modes (fuse vs split) — gated
Fuse N Sparks into one big accelerator vs split for many jobs, as a pull-based
hysteretic switch. **Gated on the one-Spark-quantized test** (may not be needed
at all — interconnect-bound → batch not interactive). Detail + gate:
`gpu-cluster-modes.md` (blocked-by gpu-priority).

### §D — Liveness net (cross-cutting)
A freshness/liveness digest so any scheduled producer that silently stops is
surfaced (the melchior-lockout class of failure: a cadence dark for days with no
monitor). Detail: `health-watchdog.md`. Consumes the same last-fired/heartbeat
state the scheduler and reconcilers already write.

### §E — Consolidate the bespoke `app_state` throttles (DRY, last)
The in-loop `app_state` `last_run` + advisory-lock throttles (paper_reconcile,
llm_reconcile, backlog_groom, corpus_reconcile, clusterize) each re-implement
"run every N, single-flight." Once §A's lease-backed `scheduler` is live and
proven, migrate these onto the same `scheduler_leases` conditional-advance and
declare each producer's cadence in one place (its `ServiceSpec` in
`workers/registry.py`, which already carries profile/gate/cost). Purely a
de-duplication — these already self-throttle *correctly*, so this is lowest
priority and explicitly last.

## Explicitly NOT in scope

- **Multi-class fairness scheduling** — fair-share, gang scheduling, cooperative
  mid-run yield, bounded-hold decay, memory-aware bin-packing: all moot with one
  user. If contention persists after §B-1 chunking, escalate per
  `gpu-priority.md`'s deferred appendix — not before.
- **A dispatcher / singleton scheduler daemon** — the whole point is that the
  claim substrate needs none.
- **Fine-grained per-chunk preemption** — reserve mode + kill backstop cover
  responsiveness coarsely; per-seed chunking is for the *wedge*, not for
  sub-chunk latency.
- **Retiring the `dream` / `reconcile` launchd timers** — deliberate host pins.
- **Rewriting the sub-specs** — `gpu-priority.md`, `gpu-cluster-modes.md`,
  `health-watchdog.md`, and §15i remain the authoritative mechanical detail;
  this doc is the frame + the cross-axis ordering, and does not duplicate them.

## Acceptance criteria

- **§B-1:** `dispatch_autocatpath` mints a seed-per-job + aggregate tree; a
  killed seed loses only that seed and a retry (content-addressed) skips
  completed seeds; the worker stays SIGTERM-responsive and spark stops wedging
  on the monolith; the aggregate yields the same scalar barrier the quest
  harvests today.
- **§A:** with `PRECIS_SCHEDULER_ENABLED` on fleet-wide and the folded launchd
  plists removed, each folded cadence fires **exactly once per interval across
  the fleet** (no double-fire during the flag/timer overlap; no dropped fire
  when the previously-owning host is down); `dream`/`reconcile` unaffected.
- **§B-2:** a human-`PRIO:`-urgent GPU job is claimed ahead of queued
  background — and the claim sort's direction is reconciled with the `0014`
  "lower = more urgent" convention (a test pins the direction so it can't
  silently re-invert); reserve mode stops new heavy background dispatch within
  one claim cycle and auto-expires on TTL; the kill backstop frees and confirms
  the GPU (injected-hang drill).
- **§D:** a deliberately-stopped cadence (e.g. a paused reconcile) shows as
  stale in the liveness digest within its expected interval + margin.
- **§E:** each migrated throttle fires on the same cadence as before, single-
  flight preserved, with its interval now declared in one place; no behaviour
  change observable in `app_state`/logs beyond the source of the tick.

## Target + blast radius

- **§B-1:** `src/precis/quest/compute.py` (`dispatch_autocatpath`,
  `harvest_measures`); the `autocatpath_explore` plugin dispatch
  (`src/precis_pathway/{job,runner}.py`); `auto_check` `child_job_succeeded`
  (reused); engine units `autocatpath.pipeline.run_one_seed` /
  `aggregate_partials` (`/Users/reto/catpath`).
- **§A:** `src/precis/workers/scheduler.py` (`CADENCES`, host-affinity),
  `workers/registry.py` (scheduler service profile), the launchd plists under
  `deploy/roles/precis_{cron_tick,watch_poll,anki_sync}` (retire) + the
  `PRECIS_SCHEDULER_ENABLED` fleet flag.
- **§B-2:** `workers/executors/_common.py::claim_executor_jobs` (the *shared*
  prio sort — the `ORDER BY COALESCE(r.prio,5) DESC` all four executors run;
  reconcile its direction with the `0014` convention) + `workers/dispatch.py::
  mint_child_job` (already copies parent prio); the reserve-mode dispatch gate
  in the dispatch/claim path; reuse of `struct_relax` `kill_container`/
  `reset_gpu` for the backstop. (Not `ssh_node.py` alone — the claim SQL is
  shared, so the change is fleet-wide, not GPU-only.)
- **§C:** per `gpu-cluster-modes.md`.
- **§D:** per `health-watchdog.md`.
- **§E:** the five throttle passes' `_due()`/`_STATE_KEY` blocks in
  `cli/worker.py` + their `ServiceSpec` rows; `Store.claim_scheduler_lease`.

## Open questions / decisions log

- **Split vs single doc (resolved 2026-08-01, Reto):** keep this as the unified
  master; `gpu-priority.md` / `gpu-cluster-modes.md` / `health-watchdog.md`
  remain its detailed sub-specs (they carry their own `blocked-by` chains and
  §B-1 is build-ready — subsuming them would stall the urgent wedge fix). This
  doc adds the frame + cross-axis ordering; the sub-specs add mechanics.
- **§A host-affinity representation** — a per-`Cadence` host field vs a `prio`
  cell on the lease. Pin before building §A.
- **§B-1 plugin location** — does the `autocatpath_explore` dispatch shell to
  the CLI (trivial `--seed`) or import `run()`? Decides whether §B-1 is
  `dispatch_autocatpath`-only or also the plugin (open in `gpu-priority.md`).
- **§B-1 aggregate trigger** — `child_job_succeeded` ×N vs a new "all child jobs
  terminal" evaluator vs an in-process harvest step; the aggregate is cheap/CPU
  so any works (noted in `transfer.md`; `gpu-priority.md` picks
  `child_job_succeeded` ×3).
- **§E timing** — only after §A is proven live for ≥1 week (don't migrate
  working throttles onto an unproven substrate).

### ADR 0048 readiness pass (2026-08-01)

**Resolved into the body (2026-08-01):** the blocker and the `news_poll`
advisory below are now corrected in §B-2 (human-first claim rewritten as a
verify-and-reconcile of the *live, seemingly-inverted* `_common.py` prio sort;
blast radius fixed to the shared claim SQL) and §A (`news_poll` removed from the
launchd-fold list — it's an in-loop pass). The split advisory was already
resolved. The findings are kept below verbatim as the audit trail.

- **blocker — §B-2 "Human-first claim" Target + blast radius is wrong, and
  the acceptance criterion may already hold or already be silently inverted.**
  `workers/executors/_common.py::claim_executor_jobs` already does
  `ORDER BY COALESCE(r.prio, 5) DESC, r.ref_id ASC` on `refs.prio`, and
  `workers/dispatch.py`'s `mint_child_job` already copies `parent_prio`
  straight onto every dispatched job — this is *live* code, not a proposed
  change, and it runs for every executor (`ssh_node.py`, `coordinator.py`,
  `claude_inproc.py`, `claude_docker.py`), not just GPU jobs. Two problems
  follow: (1) "Target + blast radius" names only `workers/executors/
  ssh_node.py`, but the actual claim-order SQL lives in `_common.py`, shared
  by four other executors — a change there is broader than the doc discloses.
  (2) The value's own convention elsewhere (migration `0014_refs_prio.sql`,
  `handlers/todo.py`: "prio=1 preempts strategic rotation... 3..10 ride the
  1/N share" — **lower number = more urgent**) is the opposite of what the
  existing `DESC` claim-order rewards (highest number first) — so as shipped
  today a `PRIO:urgent` (prio=1) job sorts *behind* a lower-priority
  (higher-numbered) one in the GPU claim query, seemingly the opposite of
  "human-first." §B-2 frames this as new, trivial work ("reuse the todo
  `PRIO:` axis in the claim sort... One comparison") without acknowledging
  the mechanism already exists and appears already inverted — a builder
  can't tell from this spec whether §B-2 is a no-op, a sign flip, or
  something else. Note `gpu-priority.md`'s own Open questions log carries
  the unresolved twin of this ("`PRIO:` reuse — confirm the todo `PRIO:`
  axis threads to job claim order for GPU jobs") — answerable by reading the
  code above — while this master doc's "One comparison" phrasing reads as
  already decided, contradicting the sub-spec it declares authoritative on
  mechanics.
- **advisory — §A's "remaining foldable launchd cadences (anki_sync,
  news_poll)" misdescribes `news_poll`.** `deploy/roles/` has
  `precis_anki_sync`, `precis_cron_tick`, `precis_watch_poll` — no
  `precis_news_poll` role/plist exists, and `news_poll`'s `ServiceSpec` in
  `workers/registry.py` carries no `default_profiles` and no `enable_env`,
  so it isn't currently triggered by a standalone launchd timer the way
  `anki_sync` is. There may be nothing to "fold" for `news_poll` under §A as
  framed — worth confirming its actual current trigger (if any) before
  scoping the fold.
- **advisory — split-into-siblings not re-flagged as blocker per caller
  instruction; the doc's own log already records this as resolved
  (2026-08-01, Reto).** Noting for completeness only: §B-1 is the one
  urgent, independently-shippable piece and is already phased first, so the
  unified-doc shape doesn't block it — no action needed.

All other code-grounded claims checked out: the dark `scheduler` pass +
`CADENCES` + `PRECIS_SCHEDULER_ENABLED` (`workers/scheduler.py`,
`workers/registry.py`), `Store.claim_scheduler_lease`
(`store/_scheduler_ops.py`), the `PRIO:` todo axis itself, `struct_relax`'s
`kill_container`/`reset_gpu`, `quest/compute.py::dispatch_autocatpath` /
`harvest_measures`, the `auto_check` evaluator `child_job_succeeded`, all
five named `app_state` throttles (paper_reconcile, llm_reconcile,
backlog_groom, corpus_reconcile, clusterize) in `cli/worker.py`, the §5/§5.2/
§5.3 capability-reserved-claim design section, `autocatpath`'s
`run_one_seed`/`aggregate_partials` in `/Users/reto/catpath`, and all four
referenced sub-specs / design sections (`gpu-priority.md`,
`gpu-cluster-modes.md` with its `blocked-by: gpu-priority`,
`health-watchdog.md`, `factory-console-and-scheduling.md` §15i,
`autocatpath-integration.md` §3.8) exist and match the proposal's
description.

## Relationship to the sub-specs (reconciliation)

This doc supersedes nothing and duplicates nothing; it is the index + ordering.
Precedence when detail conflicts: the sub-spec wins on mechanics, this doc wins
on cross-axis ordering. Pointers kept in sync (both directions):
`gpu-priority.md` §B, `gpu-cluster-modes.md` §C, `health-watchdog.md` §D,
`factory-console-and-scheduling.md` §15i → §A, and `gpu-priority.md` Phase 1 ↔
`autocatpath-integration.md` §3.8 (the build of record for §B-1). Full wedge
trail: `gr180096`.
