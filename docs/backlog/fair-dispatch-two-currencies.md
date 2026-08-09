---
status: draft
title: Fair dispatch — one candidate-picker, two cost currencies (cloud $ vs local slots), user-first
model: opus
---

# Fair dispatch — one candidate-picker, two cost currencies, user-first

## Motivation / why

Review findings (2026-08-08), plus the operator's stated allocation policy.

**Fairness defects in the dispatch lane** (`src/precis/workers/dispatch.py`):

1. `_candidate_parent_ids` orders `ORDER BY r.ref_id LIMIT 50` — the
   head-of-line starvation trap already fixed twice elsewhere
   (`auto_check.py` → `ORDER BY random()`, with the rationale in its
   docstring; the doable view → least-served rotation). Latent today
   (~6 effective candidates in prod, 242 raw auto-run todos), bites the
   moment the eligible population exceeds the page — which the taproot
   dogfood already produced once (~100 active `llm_tier` todos,
   OPEN-ITEMS §plan_tick backlog).
2. `prio` is honored at job-*claim* time
   (`executors/_common.py::claim_executor_jobs`, `COALESCE(prio,5), ref_id`)
   but ignored at *candidate* time — an urgent parent past position 50
   never mints, so the slice-6a prio plumbing is undermined one stage
   upstream.
3. The daily-ceiling cadence exemption (`_cadence_parent_ids`) filters
   *within* the ref_id-ascending page. Cadence ticks are freshly-minted
   children → highest ref_ids → tail of the page: under a ≥50 backlog
   the exemption that exists to protect the morning brief (2026-08-07,
   six hours unminted) cannot see it. A tripped ceiling is now the
   routine daily state (state-map §guardrails), so this path is
   load-bearing.
4. Budget consumption is first-come-first-served: within the daily
   envelope, whichever tree enumerates earliest spends; ~5 trees at the
   $10 tree cap drain a $50 ceiling before others tick once. The human
   lane already has least-served rotation
   (`_todo_views._fetch_doable`: `ORDER BY prio, (picks_7d+1)/(1+w)`);
   the robot lane has none.
5. Maintenance hazard feeding all of the above: ~100 lines of
   eligibility SQL duplicated by hand between `_candidate_parent_ids`
   and `_claim_and_dispatch`, symmetric only by discipline.

**The two-currency problem.** The guardrails already learned half of it
(migration 0112: `placement='local'` rows are excluded from the $ caps —
cluster GPUs are sunk cost). The scheduling layer never learned the other
half: local capacity is a *resource-allocation* problem, not a spend
problem, with its own policy —

- **Work-conserving**: the cluster should always be busy; a tripped
  cloud-$ ceiling must never idle local slots.
- **Good mix**: local slots shared fairly across roots, not drained by
  whichever tree enumerates first.
- **User-first, no preemption**: when the operator is actively working,
  their jobs claim ahead of background work — running jobs finish, new
  claims prefer the user. (Direction already pinned for GPU work:
  `gpu-priority.md` human-first claim + reserve mode, shipped.)

## In scope

1. **Shared eligibility-SQL builder.** One function renders the
   dispatch-eligibility predicate, parameterized enumerate vs lock
   (`FOR UPDATE OF r SKIP LOCKED`); `_candidate_parent_ids` and
   `_claim_and_dispatch` both call it. Deletes the hand-kept symmetry.
2. **One candidate-picker policy, shared across sweeping passes.**
   Ordering: `COALESCE(prio,5) ASC, <least-served root> ASC, random()`.
   The fairness term is per-strategic-root service over a trailing
   window, reusing the doable-view rotation shape (picks or recorded
   spend per root — open question 1). Applied to: dispatch candidate
   enumeration, `schedule/worker.py::_candidate_recurring_ids` (drop its
   `ORDER BY ref_id`), and offered to future sweeps as the default.
   `auto_check` keeps its random sample (already fair; no prio concept).
3. **Cadence candidates enumerated separately.** A dedicated query for
   ticks-under-a-`meta.schedule`-watch, unioned ahead of discretionary
   candidates — the exemption stops depending on page position. Fixes
   finding 3 structurally.
4. **Two-lane budget gating.** Classify each candidate's next job as
   local-bound or cloud-bound at dispatch time (open question 2). The
   global daily ceiling (`planner_guardrails.daily_budget`) gates
   **cloud-bound discretionary** candidates only; local-bound candidates
   dispatch whenever slots are advertised (`resource_slots` /
   `llm_serving.py`) — work-conserving. The per-todo/per-tree $ caps
   keep their existing 0112 semantics (cloud rows only). Local fairness
   comes from the picker's least-served term, not from $ math.
5. **User-first claim window.** A user-activity signal (open question 3)
   sets a short-TTL "interactive" flag; while set, the job claim in
   `claim_executor_jobs` and the dispatch picker strictly prefer
   `prio<=2` (user/chat/cadence) work and throttle background minting
   (skip discretionary dispatch when local slots are ≥N-1 busy). No
   kill, no preemption — running jobs finish; this is claim-order only.
   Coarse manual override stays: reserve mode (`gpu-priority.md`).
6. **Auto-run signal consolidation.** Retire the dead
   `executor:<runner>` tag branch in `_claim_and_dispatch`
   ("Reserved; v1 has no registered executor:* values") and its arm of
   the three-way OR in every eligibility query. Prod-scan for
   `meta.executor`-only writers; keep `meta.executor` reading as
   back-compat but the eligibility predicate collapses to
   `meta ? 'llm_tier' OR meta ? 'executor'` (single builder site after
   item 1, so this is one edit).
7. **Schedule/scheduler disambiguation.** `run_schedule_pass` gets one
   declared trigger: keep the scheduler's `cron_tick` lease (exactly-once
   across the fleet), drop the copy in the default worker rotation.
   Rename `workers/scheduler.py` → `workers/cadence.py` (or
   `workers/schedule/` → `workers/recurring/` — pick one at build time)
   so the two subsystems stop colliding on grep.
8. **Unified blocked-state module.** One registry of block reasons —
   each entry = reason id + SQL fragment + human label — covering the
   four mechanisms that today live apart: STATUS gating, the
   `_DOABLE_EXCLUSION_TAGS` open-tag registry, child-liveness
   (`_parked_child_still_blocks_sql`, incl. the parked-bypass/hard-block
   split), and job-status blocking (`_job_blocks_dispatch_sql`). The
   item-1 eligibility builder composes its predicate from this registry
   (NOT the other way round — the registry is the single source); the
   doable view, nursery stuck-doable check, and attention view render
   their "why is this blocked" strings from the same entries. Extends
   the pattern `_DOABLE_EXCLUSION_TAGS` already proved ("adding a new
   exclusion form means appending to the registry, no SQL edits").

## Explicitly NOT in scope

- Preemption / killing running jobs (reserve-mode kill backstop already
  exists for the GPU case; unchanged).
- Multi-class fairness scheduling, weights config, or per-user
  accounting — one human user; the picker's single least-served term is
  the whole mix policy.
- Changing the guardrail caps themselves (values, 0112 placement
  semantics, `daily_budget` single-source contract with the scheduler).
- The melchior single-`claude_inproc`-worker SPOF (OPEN-ITEMS item;
  orthogonal — this proposal fixes what gets *minted/claimed first*,
  not throughput).
- Dropping `meta.executor` *writes/reads* entirely (item 6 keeps the
  key as back-compat; a full migration of legacy writers is a follow-on).
- The nursery digest's own `ORDER BY ref_id LIMIT 50` pagination
  (visibility, not execution — item 8 gives nursery the shared reason
  vocabulary, not new pagination) — OPEN-ITEMS "Dispatch-review
  residuals" entry.

## Acceptance criteria

- One eligibility-SQL builder; `dispatch.py` contains no duplicated
  predicate blocks (grep: `_parked_child_still_blocks_sql` referenced
  from the builder only).
- Test: with >limit eligible candidates, a prio=1 candidate beyond the
  old page position mints in the first pass.
- Test: with >limit eligible discretionary candidates and a tripped
  ceiling, a cadence tick (highest ref_id) still mints.
- Test: two roots, one with heavy recent service — the starved root's
  candidate mints first at equal prio.
- Test: tripped daily ceiling + advertised local slots → a local-bound
  candidate mints; a cloud-bound discretionary candidate does not.
- Test: interactive flag set → a prio=5 background job is not claimed
  while a prio=1 job is queued; flag expiry restores normal order.
- Schedule pass: >limit recurrings → every recurring is inspected
  within k passes (no deterministic tail starvation).
- Eligibility SQL has a single auto-run predicate; grep for
  `executor:%` in `dispatch.py` returns nothing.
- Exactly one caller of `run_schedule_pass` outside tests; no module
  named both `schedule*` and `scheduler*` under `workers/`.
- Block-reason registry is the only definition site: grep finds
  `_parked_child_still_blocks_sql` / `_job_blocks_dispatch_sql` logic
  only inside the registry module; nursery stuck-doable and the
  attention view render reason labels from registry entries (test:
  adding a registry entry surfaces in dispatch SQL, doable exclusion,
  and nursery reason string without further edits).

## Target + blast radius

- `src/precis/workers/dispatch.py` (builder, picker, cadence union,
  lane gate) — highest risk; gr192606-class runaways guard against
  regression via existing tests.
- `src/precis/workers/executors/_common.py::claim_executor_jobs`
  (interactive-window preference).
- `src/precis/workers/schedule/worker.py` (picker ordering; item 7
  rename + single-trigger touches `workers/registry.py` and
  `workers/scheduler.py`).
- `src/precis/workers/planner_guardrails.py` (ceiling verdict gains
  lane awareness; `daily_budget` contract unchanged).
- New: user-activity signal write path (MCP/asa touchpoint or manual
  toggle) + small helper module for the picker.
- New: block-reason registry module (item 8) —
  `handlers/_todo_views.py` (doable exclusion + attention view),
  `workers/nursery.py` (stuck-doable reasons) become consumers.
- Docs: `state-map.md` todo-tree/guardrails sections;
  `cluster-scheduling.md` cross-reference (this is its law-3/law-1
  refinement for the dispatch lane).

## Open questions / decisions log

1. **Fairness term: picks or spend?** Per-root `status:done` events 7d
   (doable-view shape, cheap, already indexed) vs per-root
   `llm_call_log` spend (truer for cost, splits naturally by placement
   for the two lanes). Leaning: picks for the local lane (slot-time ≈
   job count), cloud-$ for the cloud lane.
2. **Local-vs-cloud classification at dispatch time.** The router
   resolves placement per rung dynamically (`router._placement_of`,
   chains can spill). Candidate classification must be a cheap static
   approximation — proposal: classify by the todo's tier/operation
   default chain head (local-served model advertised in
   `resource_slots` ⇒ local-bound), accept that a spill-to-cloud after
   dispatch bills the envelope retroactively (caps still bound it).
3. **User-activity signal.** Options: (a) manual toggle only (reserve
   mode generalized, zero false positives), (b) TTL heartbeat on
   MCP-session verb traffic (automatic, but session MCP hits prod —
   needs a write-cheap path, e.g. `host_heartbeat`-style row), (c) both
   — toggle authoritative, heartbeat advisory. Leaning (c).
4. **Does the interactive window throttle cadence work too?** Leaning
   no — cadences are the user's own deliverables (the 2026-08-07 lesson).
