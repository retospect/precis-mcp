---
status: idea
title: Rescue-pass wait still includes one unbounded job pass
pillar: platform
prio: normal
model: opus
---

# Rescue-pass wait still includes one unbounded job pass

> The starvation itself shipped: `run_loop` ages an overdue rescue pass
> (`RESCUE_SERVICES` in `workers/runner.py`, budget
> `PRECIS_WORKER_RESCUE_BUDGET_S`, default 300 s) to the front of the
> rotation, so its wait is the budget plus one in-flight pass instead of a
> whole rotation (melchior 2026-08-14: `quest_loop_reconcile` silent ~7h).
> Repro: `tests/workers/test_rescue_ageing.py`.

## What is left

The in-flight-pass term is bounded for ref-passes (`deadline_s`,
gr486823) but not for job executors, which stay unbounded on purpose
(abandoning one strands a claimed job's lease). One `job_claude_inproc`
pass runs up to `min(batch_size, 4)` multi-minute jobs back to back, so a
rescue pass can still wait 30–60 min behind it.

Options, cheapest first:

1. Let a job executor check between jobs whether a rescue pass is overdue
   and return early (keeps the rest of its claim queued, not leased).
2. Cap `job_claude_inproc` to one job per pass on a shared `--profile all`
   worker. Costs lane throughput: the rest of the rotation then runs
   between every job.
3. The durable target from the 2026-09-21 architecture review: separately
   supervised health/rescue, job/planner and background lanes, with
   priority bands only inside a lane.

## Verify

```
scripts/prod-psql --ro "SELECT payload->>'handler', max(ts) FROM worker_logs
  WHERE payload->>'handler' IN ('sweeper','nursery','quest_loop_reconcile')
  AND ts > now() - interval '1 day' GROUP BY 1"
```

`worker: <pass> overdue (...); running ahead of its rotation slot` lines in
a worker log mean the interleave is doing the work the rotation could not.
