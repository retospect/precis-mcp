---
status: draft
title: Quota-paused jobs — the park/fail lifecycle wedges recurrings and ends agent ticks as opaque failures
pillar: platform
---

# Quota-paused jobs — parked lifecycle wedges recurrings and ends agent ticks as opaque failures

Source gripes: gr536351, gr541128

## Motivation / why

The Claude-subscription quota gate (`precis.budget.quota`,
`breaker._gate_quota`) pauses gated work when a rate-limit window reports
`rejected` or crosses `PRECIS_QUOTA_CEILING_PCT`, auto-clearing when the
window resets. A paused job parks via
`executors/_common.py::park_job_until` (`STATUS:waiting_time` +
`meta.wake_when`; the wake_runner re-queues once the instant passes;
`MAX_PAUSE_PARKS = 6`, then the pause is recorded as a normal failure).
The gate itself works as designed — the damage is downstream: parked and
gate-failed jobs leave their parents (recurring cadences, agent ticks) in
states nothing recovers.

## gr536351 — recurring spawn children stall under the gate

Evidence (prod, 2026-10-29→30): under td6645 "Watches" at least four
recurring spawns are chronically stalled — td161193 (nidra / evening
meditation, seen_count=60, last child open 16h), td161192 (reading /
morning brief, seen_count=32, last child open 9h), td42432 (morning
briefing, seen_count=47, last child open 13h), td42152 (news poll,
seen_count=53, last child open 15h). Every spawned child sits
`STATUS:open` with no claim and no linked job — consistent with *never
dispatched*, not "started but hung". Collision-skip then does its job:
each later tick skips, `seen_count` climbs, and the downstream watchdogs
go stale (`watchdog:reading/cast_audio` 31.6h vs a 26h budget;
`watchdog:ingest/news` and `watchdog:reading/morning_brief_cast` 30.7h).

Forensics (GRIPE_FIX, 2026-10-29): the root cause is the subscription
quota gate, not the recurring or nursery code. jo531123 (meditation,
child of td531122) parked/re-queued roughly every 2h, then failed at
09:16Z ("99% of the seven_day window used, ceiling 85%") leaving
`child-failed:531123` on its parent; jo533533 (reading_brief, under
td533529) sat `waiting_time` on the same gate. `reading/meditation.py`
records the identical failure mode from 2026-08-05 — the remedy that
time routed meditation off the subscription via `llm.op.meditation`, so
the class fix is proven for one op and never generalized.

Already shipped — verify, don't rebuild:
`_has_open_previous_tick_conn` (`workers/schedule/worker.py`) already
excludes `child-failed:*` children from collision-skip and time-bounds a
still-open child to `stuck_hours` (efc894624); the nursery
`stalled-recurring` detector already fires. The open gap is the
*paused/never-claimed* child: young, `STATUS:open`, unclaimed, blocking
every tick — a quota window longer than one tick wedges the cadence by
construction.

Open work:

- Why a spawned child is minted but never claimed — parked in
  `waiting_time` at first dispatch, or no job minted at all (dispatch
  side). Unproven; the reporting surface could not pin the job ids.
- Whether a child whose job sits `waiting_time` should count as
  in-flight for collision-skip; today a young open child blocks by
  design (pile-up guard).
- `child-failed:*` children stay `STATUS:open` forever awaiting an owner
  retry/give-up decision — nothing drives that decision, so the stalled
  set grows. Prod residue: `child-failed:531123` on td531122 needs
  clearing (a Reto prod write; it only wedges the recurring if prod's
  scheduler predates efc894624 — verify before assuming).
- Whether cast ops fall back to a non-subscription rung when
  quota-paused (the `llm.op.meditation` precedent;
  `budget-guardrails.md`'s deliverable ladder already defers "add the
  local rung once the gr479851 degrade ladder lands").

## gr541128 — agent ticks end as opaque failures after park/resume

Evidence (prod, 2026-10-30): `watchdog:autonomy/doctor_report_fresh`
(al540591, 29.5h stale) — doctor_tick jo537676 parked/re-queued
repeatedly on the quota gate, resumed after the ~19:40Z auto-clear, did
real gather/diagnose work (52 tool calls; its findings landed as the
gr536350/gr536351 forensics), then ended `STATUS:failed` with a 230kB
dump instead of a clean report. The prior tick jo536285 rode the same
park/requeue cycle but finished — the gate alone does not always sink a
tick. Transcript calls 49–51, just before the final `put`, repeated an
identical `get(kind='gripe', id=346342)` 3× with identical output —
suggestive of a stuck retry loop burning turns as context/budget ran out
rather than reaching the report step.

The doctor's own report-freshness watchdog is thus a casualty of its own
substrate (self-healing-spine law 6 in the small), not a regression in
nursery/health-digest.

Open work: decide whether the fix is (a) detecting and breaking stuck
retry loops (repeated identical tool calls) near context/budget
exhaustion in agent jobs, or (b) treating a gate-cycled failure as
expected — a bounded, retryable terminal state (auto-retry the tick,
or append the partial report) instead of an opaque `STATUS:failed`
dump.

## In scope

- The collision-skip treatment of a spawned child whose job is
  `waiting_time`-parked (or never minted): bound the stall — the cadence
  must outlive a quota window without a human.
- An owner-decision path for `child-failed:*` recurring children (retry /
  give-up / auto-resolve), so a gate-failed tick does not strand the
  child `STATUS:open` forever.
- The gr541128 (a)/(b) decision, then whichever terminal-behavior change
  it rules.
- Diagnosing why td6645's spawned children never get claimed, as part of
  picking the mechanism.

## Explicitly NOT in scope

- The gate's own thresholds, windows or taper floors — those belong to
  `budget-guardrails.md` (this item consumes the pause, it does not
  re-tune it).
- A second agent-profile host — a separate decision (see
  `cluster-scheduling.md` "De-SPOF the melchior agent worker").
- The prod cleanup writes themselves (clearing `child-failed:531123` on
  td531122; resume-now on `/budget`) — operator actions, not code.
- Per-cast feature work (length calibration, lanes) — `cast-followups.md`.

## Acceptance criteria

- A recurring whose newest spawned child is `STATUS:open` with a
  `waiting_time`-parked (or absent) job does not block ticks longer than
  [TBD — the bound chosen under Open questions]; once the child is
  terminal or past the bound, the next tick mints.
- After a quota window clears, a parked recurring child resumes to a
  terminal state without human intervention (the wake_runner re-queue
  path; pin with a test).
- A `child-failed:*` recurring child reaches an owner decision — retry,
  give-up, or auto-resolve — within [TBD]; a gate-failed tick is never
  silent and never permanent.
- An agent job that ends after `MAX_PAUSE_PARKS` or mid-transcript
  records a diagnosable terminal event naming the cause (pause-exhausted
  vs detected retry loop), not an opaque dump [TBD — shape depends on
  the (a)/(b) ruling].
- Verification target: the td6645 casts (nidra, morning brief, briefing,
  news poll) emit fresh episodes within one cadence of the gate
  clearing, unassisted [TBD — depends on the never-claimed diagnosis].

## Target + blast radius

- `src/precis/workers/schedule/worker.py` — `_has_open_previous_tick_conn`
  collision-skip treatment of parked/unclaimed children.
- `src/precis/workers/executors/_common.py::park_job_until` and
  `workers/wake_runner.py` — the park→wake→requeue lifecycle and its
  park cap.
- `src/precis/workers/dispatch.py` — whether spawned children mint jobs
  at all under a paused gate.
- `src/precis/workers/nursery.py` — `stalled-recurring` and watchdog
  semantics once the lifecycle changes.
- `src/precis/workers/job_types/doctor_tick.py` and the agent tool loop
  (`src/precis/utils/llm/`) — retry-loop detection and/or the
  gate-failure terminal state.
- `src/precis/budget/quota.py`, `breaker.py` — read-side only (the pause
  contract the lifecycle consumes).

## Open questions / decisions log

- **(blocker) Resume on `/budget` or wait for the auto-clear?** Asked of
  Reto on gr536351; until ruled, the spec assumes auto-clear plus the
  stall bounds above.
- **Fallback rung for cast ops when quota-paused?** The
  `llm.op.meditation` precedent says cast work should have a
  non-subscription rung; the deliverable ladder's local rung waits on
  gr479851. Options: per-op `llm.op.*` registry entries vs a generic
  deliverable-class fallback.
- **(a) Retry-loop circuit breaker vs (b) auto-retry on gate-cycled
  failure?** The gr541128 recommendation. (a) detects repeated identical
  tool calls near context/budget exhaustion; (b) makes the terminal
  state a bounded retryable skip. Not mutually exclusive — decide which
  ships first.
- **Why are the spawned children never claimed?** `waiting_time` at
  first dispatch vs a dispatch-side minting gap. Investigation; may
  resolve into a plain code fix.
- **Should a `waiting_time`-parked child count as in-flight for
  collision-skip?** Blocking preserves the pile-up guard but wedges the
  cadence for the length of any gate window; non-blocking piles up
  children while the gate holds.
