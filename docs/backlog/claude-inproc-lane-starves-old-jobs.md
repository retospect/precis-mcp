---
status: draft
title: claude_inproc lane serves new jobs while hours-old ones sit unclaimed
prio: normal
---

# claude_inproc lane serves new jobs while hours-old ones sit unclaimed

## Motivation / why

Observed 2026-08-23. The lane is live — a `news_poll` completed 42 min before
the sample, new dispatches kept arriving — yet the queue held four jobs older
than two hours that were not being picked:

```
news_poll        244691   28 sec
diagnose_gripe   244689   3 min
plan_tick        244484   2h 14m
taproot_backfill 244455   2h 29m    <- eventually ran, ~2.5h after dispatch
draft_refresh    244452   2h 32m
plan_tick        244327   3h 12m
plan_tick        244114   3h 38m
plan_tick        244060   3h 42m
```

Fresh arrivals completing while older entries wait is not FIFO drain. Either
the claim orders by something that lets new arrivals cut, or those older jobs
repeatedly fail to claim and silently re-queue. The historical counts hint at
the second: `plan_tick` has 37 `failed` against 105 `succeeded`, by far the
worst ratio on the lane.

Cost of not fixing: an interactive job dispatched from a session waits hours
with no signal, which is what made the lane look dead during this session's
triage (it isn't — see also `agent-worker-despof`).

Context that confounded the diagnosis: `com.precis.worker-agent` has been
disabled since 2026-07-23 (plist renamed `.bak-20260723`, deliberately — the
agent profile arms `news_poll` / `plan_tick` / `briefing` / `meditation` /
`dream_agent` cadences and was turned off for token cost). The lane is served
instead by the `--profile all` worker, which registers `job_claude_inproc`
among everything else. So lane latency is a *scheduling* question, not an
outage.

## In scope

- Determine why an older queued `claude_inproc` job is passed over: read the
  claim query in `precis/workers/executors/claude_inproc.py::run_claude_inproc_pass`
  and the shared claim/lease path in `precis/workers/base.py`.
- If claims are failing and re-queueing, surface it — a job that fails to
  claim N times should say so rather than look identical to "waiting".
- Confirm whether `plan_tick`'s 37 failures share a cause with the stalling.

## Explicitly NOT in scope

- Re-enabling `com.precis.worker-agent`. That was a deliberate cost decision;
  reviving it re-arms every cadence and is not needed to serve the lane.
- Reworking priority tiers generally.

## Acceptance criteria

- A queued `claude_inproc` job's wait time is explainable from its own state —
  either it is genuinely behind work, or its claim failures are visible.
- No job older than the next-oldest is skipped without a recorded reason.

## Target + blast radius

`precis/workers/executors/claude_inproc.py`, `precis/workers/base.py`
(`_claim_fresh` / lease path). Read-mostly diagnosis first; a fix may touch
claim ordering, which affects every pass that shares the base claim.

## Open questions / decisions log

- Is the skip an ordering choice or repeated claim failure? Undetermined —
  this was diagnosed from queue snapshots, not from a claim-path trace.
- Separate hygiene item, noted not chased: the disabled
  `/Library/LaunchDaemons/com.precis.worker-agent.plist.bak-20260723` contains
  a **plaintext prod DB password**, and `deploy/roles/precis_worker_agent/` is
  still wired into `deploy/site.yml` — so a deploy may resurrect the unit the
  `.bak` rename disabled, since that rename is out-of-band and recorded
  nowhere in the repo.
