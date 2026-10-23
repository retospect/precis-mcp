---
status: in-progress
title: Job slicing — a long compute job yields its claim per slice budget and resumes from the per-step checkpoint
pillar: platform
prio: normal
---

# Job slicing

Reto 2026-10-24 (chat-99): g5-relax-ckpt's per-step checkpoint
(`src/precis_se/hexfold_relax_job.py`, landed e8d10e0a3, gr508493/gr508967)
makes a killed run resumable, so a long relax can also be chopped into
slices: run a slice, let another job have a turn, continue. Build behind a
flag defaulting OFF; enable only after r91 shows a real checkpoint resume
working on prod. Owning thread: [factory](threads/factory.md).

## Motivation / why

A hexfold_relax can hold an `ssh_node` lane for hours; everything queued
behind it (including a human's job, which wins claim rank only at the next
claim) waits the whole run. Slicing turns the run into bounded turns:
past a per-claim wall budget the job checkpoints, releases its claim as
`STATUS:yielded`, and re-queues behind the jobs already waiting.

## Design

### D1. `STATUS:yielded` — a claimable queue member, not a park

`yielded` joins the `STATUS` closed vocab
(`src/precis/store/types.py::_CLOSED_VOCAB`) and the claimable set in
`src/precis/workers/executors/_common.py::claim_executor_jobs` — the
fresh-work EXISTS, the `q.qs` and `top_class` LATERALs read the
`(queued, yielded)` pair. It is NOT in `WAITING` (not a coordinator park,
wake_runner does not own it) and not in `TERMINAL`. A yielded row orders
by its yielded tag's `created_at`, so it re-enters **behind jobs already
waiting** — the yield stamps a fresh queue clock: accumulated queue age
restarts, `refs.prio` and `meta.origin` are untouched (original priority,
same class — human jobs still claim first, job-scheduling-fairness D2/D3
unchanged).

### D2. Slice budget — wall time, code default + DB override

Default 1200 s per claim (a resume pays model load + setup, MACE ~10-30 s —
slices are minutes). Resolution: `app_state` key `claim.slice_s.<executor>`
(the `claim.max_running.<job_type>` pattern at
`_common.py::_MAX_RUNNING_KEY_PREFIX`) wins; else `PRECIS_JOB_SLICE_S`;
else the code default. Enable: `PRECIS_JOB_SLICING` truthy, checked live
so a flip needs no redeploy.

### D3. Yield only when someone is waiting

The yield decision rides the child's per-step `checkpoint` events in
`hexfold_relax_job._dispatch`'s `_event` hook: once past the budget, each
event re-checks `_common._lane_waiters` — EXISTS another claimable job on
this lane (same `meta.executor`, the claim's `target_node` gate,
`lease_until` absent/expired, not self). Nobody waiting → the job runs
on; it yields at the first step boundary where a waiter exists.

### D4. The yield transition — a clean release, not an orphaned lease

`should_stop` returns `"yield"` (cancel/kill win — checked first);
`run_in_child` stops the process group like any stop. The dispatch then:

- flushes the latest checkpoint pointer to `meta.relax_checkpoint`
  (the 60 s throttle in `_event` may lag), so the next claim's
  `_resume_state` continues from it;
- appends `meta.relax_slices[]` `{slice, yielded_at, wall_s, stage,
  fragment, stage_steps, fmax}` and a `job_event` chunk — progress stays
  visible (`fmax` = `curve[-1]`; energy rides the completed-stage record);
- refunds `meta.reserved` (`release_job_reservation`) and clears
  `lease_until` — immediately claimable, nothing held;
- clears `meta.compute_child` (the child is confirmed gone);
- `set_status("yielded")`. No `meta.attempts` bump (a fresh-pass claim
  never bumps — not a retry), no failure, no checkpoint-version write-back
  (`_settle` is terminal-only).

### D5. Every queue-semantics reader learns `yielded`

Readers that mean "waiting to be claimed" read `(queued, yielded)`:

- `claim_executor_jobs` (D1).
- `handlers/job.py` — `_queue_lines` SQL, `_render_one` (queue lines for
  yielded + a `slices:` line off `meta.relax_slices`), `/throughput`
  depth, `_AGENT_TRANSITIONS` (yielded mirrors queued, incl. the
  `cancel_requested` → `cancelled` rewrite — no live child to observe a
  cancel), `mode='retarget'`.
- `workers/sweeper.py` — `_reap_overqueued` (queued age reads the current
  tag's `created_at`, so slices restart the clock and a long-lived job is
  never reaped for total lifetime), `_reap_dead_node_queued`, the
  unplaceable-jobs alert.
- `health_checks.idle_lanes`, `precis_web/routes/status.py` queue list,
  `workers/nursery.py` + `health_digest.py` queue-side counts.

Non-readers stay untouched: `wake_runner` (waiting_* only), the
stale-running reclaim arms (running only), `quest/loop.py`'s
terminal+waiting check (yielded reads as in-flight — true), job_type-
scoped counts (embed_batch, claude_inproc never yield).

## Acceptance criteria

- Flag off (`PRECIS_JOB_SLICING` unset): no code path can produce
  `STATUS:yielded`; behaviour byte-identical.
- A yield-and-resume run reproduces the uninterrupted trajectory
  (`test_reclaim_resumes_same_job` shape, `tests/test_hexfold_relax_job.py`).
- No yield while the lane queue is empty past budget.
- `yielded` counts as neither failure nor retry (`meta.attempts` untouched).
- A human-origin job queued behind a yielding job is claimed first.
- The yielded job's queue age restarts at yield (pinned — D1).
- Resume across N slices reaches `STATUS:succeeded`; `meta.relax_slices`
  records each slice's steps + fmax.

## Explicitly NOT in scope

- Slice fairness beyond "behind jobs already waiting" (no token bucket,
  no per-job min-share) — revisit only if the slice-6 metrics show a
  yielded job starving.
- Other job_types: only hexfold_relax carries the checkpoint file today;
  the `_common` helpers are shared so the next checkpointed type wires in.
- A migration: the override rides `app_state`, the status rides the
  closed-vocab list — no DDL.

## Open questions / decisions log

- Yielded keeps accumulated queue age? **Decided: restarts** — the spec's
  "behind jobs already waiting" is the fresh-tag stamp; pinned in a test.
- Budget keys per executor lane; `claim.slice_s.<executor>` — same
  app_state pattern as `claim.max_running.<job_type>`.
