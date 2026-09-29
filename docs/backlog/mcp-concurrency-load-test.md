---
status: draft
title: MCP concurrency load test — find the wall before the first overnight run
prio: high
---

# MCP concurrency load test

Design session 2026-09-29 (Reto + agent, big-model-manage worktree).
Companion to `eval-run-spine.md` (which ships first) and
`vllm-per-node-serving.md` (which this de-risks).

## Motivation / why

The plan is ~24 concurrent agent sessions against the precis MCP, each
oversubscribed against a local model's slots. The standing guidance is a
**2–3 MCP-heavy-agent ceiling** (memory `mcp-fleet-concurrency-limit`). That
is an 8–10× jump past a documented limit, taken on faith, with an overnight
run as the first test. It should be measured first, and cheaply.

There are **two independent walls with near-identical symptoms**, which is
the reason this needs instrumenting rather than just trying:

1. **The anyio worker pool.** The verbs register as plain sync callables, so
   FastMCP runs each on a worker thread. Before `372238ce` nothing set
   `current_default_thread_limiter` and all 40 were shared, so enough
   concurrent slow embeds exhausted the pool and *every* call queued for a
   thread — including a static `get(kind='skill')` touching neither DB nor
   embedder. The fix (an interactive budget in `src/precis/config.py` plus
   `src/precis/embedder.py`'s `BoundedConcurrencyEmbedder`, 4 in-flight,
   shedding rather than queueing) raised the ceiling; it did not remove it.
2. **Postgres connections.** `gate-concurrency.md` records the test DB
   hitting its 100-connection maximum under the full suite at `-n6`, with
   RST'd connections. Prod fronts Postgres with pgbouncer, so the prod
   picture differs — but the harness runs against the dev DB, where it does
   not.

Both present as "calls hang". The diagnostic triad for the pool case — calls
hang, `serve` at 0% CPU in state S, `pg_stat_activity` showing no active
query but your own — is unambiguous once you know it, and the harness should
just record the discriminating facts rather than make anyone re-derive them.

## In scope

* A harness that fans N concurrent callers at a live `precis serve`, **dev DB
  only** (`scripts/dev`). `scripts/exercise-mcp/run.sh` is the existing
  single-agent precedent for wiring `claude -p` at the MCP.
* Ramp N; report per-verb p50/p95/p99 and error rate at each step.
* Sample the two discriminators alongside: worker-thread occupancy and open
  Postgres connection count. The output must say *which* wall was hit.
* Name the first verb to fall over. The suspects are the ones that can park a
  thread: embeds, `safe_fetch`, perplexity, RAG search.
* A documented safe ceiling, with the number the load gate and the run
  driver should use.
* **Re-prefill measurement** (slice 2, once something is serving): how long an
  MCP stall has to be before vLLM evicts the session's KV blocks, and what
  the resulting re-prefill costs. That number sets the admission
  controller's cap, so this harness has to produce it — it is a deliverable,
  not an observation.
* **Admission-controller prototype** (slice 2): a loop that tops sessions up
  against vLLM's `/metrics` — add while `num_requests_waiting` is ~0 and
  `gpu_cache_usage_perc` is under threshold, stop when waiting climbs. It
  scrapes the endpoint directly and never reads Postgres; sub-second
  response is the whole point.

## Explicitly NOT in scope

* Fixing what it finds. Each wall becomes its own item with a measurement
  attached.
* Load-testing prod. The session `precis` MCP targets PROD with a
  write-capable role; this runs on the dev DB.
* The serving stack itself — `vllm-per-node-serving.md`.
* Raising the anyio pool size as a reflex. Whether it should become
  configurable is a finding, not a premise.

## Acceptance criteria

* A table of verb × concurrency → p50/p95/p99 latency and error rate, over a
  ramp that reaches the point where something degrades.
* The binding constraint is named, with the evidence that distinguishes
  thread-pool exhaustion from connection exhaustion.
* A recommended concurrency ceiling, and the stall duration at which KV
  eviction starts (slice 2).
* The harness is re-runnable by one command, so the number can be re-measured
  after any fix rather than quoted forever from this run.

## Target + blast radius

New harness under `scripts/` · `src/precis/config.py` (interactive budget) ·
`src/precis/embedder.py` (`BoundedConcurrencyEmbedder`) · read-only against
everything else. No schema, no handlers.

## Open questions / decisions log

* Should the anyio thread limiter become a tunable, or is the per-call budget
  the right control? Answer after measuring.
* pgbouncer sizing for the prod path — out of scope here, but if the dev-DB
  run shows connections binding before threads do, prod pool sizing becomes a
  real question rather than a hypothetical.
* Whether the harness drives real `claude -p` agents (realistic, expensive,
  noisy) or synthetic callers issuing a fixed verb mix (cheap, repeatable,
  less faithful). Probably both: synthetic for the ramp, real agents for one
  confirmation run.
