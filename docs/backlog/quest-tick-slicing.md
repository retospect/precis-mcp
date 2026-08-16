---
status: draft
title: Slice the quest tick into resumable coordinator stages
prio: high
model: opus
---

# Slice the quest tick into resumable coordinator stages

## Motivation / why

One `quest_tick` is a single monolithic in-process call
(`run_quest_tick`, `src/precis/quest/tick.py` — anchor: the phase table
in `workers/job_types/quest_tick.py::_phase_tick`'s caller): prompt
assembly → primary LLM call (900 s/rung × failover chain) → apply/ledger
→ inline lit-search (S2 + per-DOI acquire) → compute dispatch → commit
re-prompt ladder (up to 2 more chain-walked LLM calls, **no
`timeout_s`**) → narrative-compress call → dossier regen. Up to 4 LLM
calls per tick, hours of wall-clock. `coordinator_state` checkpoints
only loop phase (`tick`/`await`), never intra-tick progress — a slice
killed at phase 9 redoes the prompt and the primary LLM from scratch.
Measured cost of the monolith: quest 164903 burned 53% of 24 h of tick
time re-running one doomed cloud call (comment at
`workers/job_types/quest_tick.py` §"timeout-kind pause"); during the
2026-08-10→16 deploy-bounce storm the tick could never finish inside the
inter-restart window at all — a livelock (gripe 210417). Drain-before-
bounce (shipped separately) removes the common killer; this item makes
the tick cheap to kill so the loop is robust to the killers that remain
(node reboot, OOM, wall-clock).

## In scope

- Split `run_quest_tick` at its natural phase boundaries into coordinator
  **stages** driven by the existing Yield state machine
  (`coordinator_state.phase` grows sub-states, e.g. `tick:llm` →
  `tick:apply` → `tick:search` → `tick:compute` → `tick:ladder` →
  `await`). One stage ≈ one slice; a kill costs only the current stage.
- Persist the primary LLM payload at the `tick:llm`→`tick:apply`
  boundary (reference an agentlog/chunk by id from `coordinator_state`,
  don't inline a 50 KB payload into quest meta) so apply/ledger/compute
  resume without re-prompting.
- Give the ladder + compress LLM calls explicit `timeout_s` (today: none
  — transport default; the 2026-08-13 timeout-pause budget fix covers
  only the primary call).
- Idempotence audit of each stage on re-run (ledger add, deed mint,
  search acquire are already content-addressed/idempotent — verify, and
  note per stage).

## Explicitly NOT in scope

- Requeue-from-checkpoint for a crashed `STATUS:running` slice (the
  claim SQL requires `queued`; flagged future work at
  `workers/executors/coordinator.py` §_persist_dispatch_result). The
  reap/re-mint path stays; it just gets cheap.
- Changing the failover chain / rung policy (llm router).
- Splitting phases into separate *jobs* (todos) — stages stay slices of
  the one coordinator job so the idem-key/backpressure model is
  untouched.

## Acceptance criteria

- A tick killed after the primary LLM call resumes at `tick:apply` on
  the next slice without a second LLM call (test: kill between stages,
  assert one router invocation total).
- Every tick LLM call carries an explicit `timeout_s`.
- A fresh quest with no `coordinator_state` runs the full ladder
  unchanged (behavioral no-op when never killed): existing
  `tests/test_quest_tick*` stay green.
- Max slice wall-clock ≈ one LLM call (no stage bundles two).

## Target + blast radius

`src/precis/quest/tick.py` (run_quest_tick split),
`src/precis/workers/job_types/quest_tick.py` (state machine),
`src/precis/workers/executors/coordinator.py` (read-only — state shape
already generic). Quest loop reconcile/backpressure untouched.

## Open questions / decisions log

- Where the LLM payload checkpoint lives: agentlog ref vs a `ord < 0`
  chunk on the quest — pick whichever the transcript persist
  (`tick.py::_persist_job_transcript`) already writes, and reference it.
- Stage granularity of the ladder: one slice per rung (preferred — each
  rung is its own LLM call) vs one slice for the whole ladder.
