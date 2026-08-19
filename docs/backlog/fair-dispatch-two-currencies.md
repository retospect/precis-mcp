---
status: draft
title: Fair dispatch — one candidate-picker, two cost currencies (cloud $ vs local slots), user-first
model: opus
---

# Fair dispatch — one candidate-picker, two cost currencies, user-first

## Motivation / why

Review findings (2026-08-08), plus the operator's stated allocation policy.
Evidence: gr191337 (taproot_backfill monopolizes the claude_inproc lane for
hours), gr191125 (band-5 starves behind re-minted band-2 cron), gr200375
(fetch_oa monopolizes the serial melchior loop).

**Fairness defects in the dispatch lane** (`src/precis/workers/dispatch.py`):

1. `_candidate_parent_ids` orders `ORDER BY r.ref_id LIMIT 50` — the
   head-of-line starvation trap already fixed twice elsewhere
   (`auto_check.py` → `ORDER BY random()`, with the rationale in its
   docstring; the doable view → least-served rotation). ~~Latent today
   (~6 effective candidates in prod, 242 raw auto-run todos)~~ — **NO
   LONGER LATENT, confirmed live 2026-08-19.** The predicate returns **86**
   candidates; melchior runs `--batch-size 32`, so the page is 32, not 50.
   Two freshly-minted `taproot_backfill` todos (218295 / 218296, root
   todos, fully eligible — no blocking child, no exclusion tag, no
   schedule) sit at queue positions **85 and 86** and are therefore
   unreachable on every pass, indefinitely. The user-visible symptom is
   "I queued work and nothing ever happened", with no attention tag and
   no failed job to explain it — the todo just stays `STATUS:open`
   forever, which is the worst possible failure signature.

   Compounding it: **all 86 candidates currently have zero live child
   jobs** — the head of the queue is not churning, so the same oldest 32
   re-occupy the page every pass and nothing behind them ever advances.
   Whatever causes the zero-mint is a separate defect (under
   investigation), but it converts this ordering flaw from "slow" into
   "permanently stuck", which is the argument for fixing the picker even
   before the mint bug is understood. Aging or `random()` would have let
   the tail through regardless of the head's state.
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

## Live incident 2026-08-19 — cadence-exempt spend pins the ceiling permanently

Root cause of the zero-mint noted in finding 1 above, now identified.
**Discretionary dispatch has been paused continuously since 2026-08-16
06:04 UTC** (~3.5 days) by the global daily cost ceiling:

```
dispatch: daily ceiling ($59.85 >= $50.00) — discretionary dispatch paused
```

The pass itself is healthy and runs every ~10-15 min (`dispatch claimed=0
ok=0 failed=0`, last 2026-08-19 07:33:57 UTC); 53 ceiling-hit logs between
08-16 06:04 and 08-17 19:15. Cadence + zero-LLM candidates stay exempt, so
recurring work (news_poll, briefing, card_forge) kept succeeding the whole
time — which is exactly why this reads as "everything is fine" from the
outside while every non-recurring executor todo silently never runs.

**The window is trailing-24h, not calendar-day, so it does not self-clear
— and the exempt work is what holds it open.** `dispatch.py`'s own comment
already names the pathology from a prior occurrence: "Cadence-exempt quest
ticks kept the trailing-24h window over the ceiling permanently, which
starved every `autocatpath_aggregate` mint for 29h (2026-08-16/17)". That
29h incident has now recurred as a 3.5-day one and should be treated as
chronic, not incidental: exempt spend alone exceeds the envelope, so the
gate that is supposed to throttle discretionary work has become a
permanent off-switch for it.

This is a **third** fairness currency the design above doesn't yet cover:
not cloud-$ vs local-slots, but *exempt vs discretionary claim on the same
$ envelope*. Options, in rough order of structural merit:

1. Reserve a discretionary floor — cadence/zero-LLM exempt work may not
   consume more than X% of the envelope, so discretionary always retains a
   slice. (Directly kills the self-sustaining lockout.)
2. Charge exempt work to a separate envelope, so cadence spend cannot move
   the discretionary gate at all.
3. Age-based override — a candidate starved beyond N hours mints regardless
   (bounded, one job), so nothing is *indefinitely* invisible.
4. Raise `PRECIS_DAILY_COST_CEILING`. Treats the symptom, and the trailing
   window means it will re-pin at whatever the new value is.

Whatever is picked, the **observability gap is the urgent half**: a
ceiling-paused todo shows `STATUS:open`, no attention tag, no failed job,
no user-visible signal of any kind. It is indistinguishable from work that
simply has not been reached yet. At minimum, a todo skipped by the ceiling
should carry a visible reason tag, and `/status` (or the nursery digest)
should surface "discretionary dispatch paused, N candidates waiting, oldest
Nh" as a first-class state.
