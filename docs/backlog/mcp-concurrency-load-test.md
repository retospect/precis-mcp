---
status: measured
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

## MEASURED 2026-09-29 — neither predicted wall is the binding constraint

Built (`scripts/mcp-loadtest`, `scripts/mcp_loadtest/harness.py`) and run.
Ramp N=1..64, 15-20 s per step, dev DB, four-verb mix. **The answer is a
third wall that both hypotheses above missed, and it arrives long before
either of them.**

| N | canary p95 ms | pg conns | server CPU | harness CPU | calls/s |
|---|---|---|---|---|---|
| 1 | 16 | 2 | 0.67 | 0.04 | 24.7 |
| 8 | 360 | 4 | 1.17 | 0.05 | 27.9 |
| 32 | 1289 | 4 | 1.17 | 0.05 | 28.5 |
| 64 | 2481 | 4 | 1.17 | 0.05 | 29.9 |

**Aggregate throughput is flat at ~28 calls/s from N=1 to N=64.**
Concurrency buys nothing. Latency is simply `N / 28` seconds — the curve is
pure queueing, and the server does the same total work whether one caller or
sixty-four are waiting.

**The wall is the GIL, not the thread pool.** Server CPU pins at ~1.17 of 12
available cores and will not climb. The anyio pool is not starved of threads;
its threads cannot run at once, because the work is CPU-bound Python. This
inverts the remedy the item assumed: **raising the pool size changes
nothing**, and the open question "should the thread limiter become a tunable"
is answered *no* — it is not the control.

Four things that make the reading hard to argue with:

* **The Postgres wall was never approached.** 4 connections of
  `max_connections` 100, at every step. Hypothesis 2 is not wrong, it is
  unreachable — something else binds two orders of magnitude earlier.
* **The canary degrades identically to the DB verbs.** At N=64, canary p50
  2085 ms against `get:paper` 2098 ms and semantic search 2376 ms. A
  file-backed read that touches neither DB nor embedder is just as slow as
  everything else, so there is one queue, not per-resource contention.
* **The harness is excluded by measurement, not assertion.** It sits at 0.05
  of 12 cores while the server holds 1.17. A saturated load generator
  produces this same rising-latency curve, so the harness samples its own CPU
  and the verdict function refuses to name a wall when the generator is the
  busier of the two.
* **Not an artefact of the BLAS thread cap.** A control run with
  `PRECIS_LOADTEST_NO_THREAD_CAP=1` gives 1.18 cores and the same throughput.

**Zero errors at every step, N=1 through 64.** It never fails, it only slows.
That is worse than failing for the serving plan: an admission controller
watching for errors would see a green system at any concurrency.

### What this means for ~24 sessions

One `precis serve` process serves ~28 calls/s total. At 24 concurrent
sessions every call takes ~24x its solo latency — canary p50 measured 737 ms
at N=24 against 4 ms at N=1. Nothing breaks; everything crawls.

So the ceiling is a **latency** budget, not a breakage point. Holding canary
p95 under ~500 ms means roughly **N=8-12 per server process**, which puts the
24-session target at **2-3 `precis serve` processes behind a balancer** —
process-level parallelism, since that is the only thing that defeats a GIL
ceiling. This is now a serving-topology question, and it lands on
`vllm-per-node-serving.md`'s admission controller rather than on a thread-pool
tunable.

### Still owed

* **The harness has no tests of its own.** `scripts/mcp_loadtest/` sits
  outside mypy's `src tests` scope and no gate exercises it, so it passed
  ruff and nothing else. It is inert on the cluster — a standalone script,
  imported by nothing in `src/`, run only when invoked — so the exposure is
  bit-rot, not risk: `pct()`, `tool_error()`, `CpuMeter.read()` and
  `verdict()` are pure functions that a handful of unit tests would pin
  cheaply, and the verdict thresholds in particular are the part most likely
  to drift into being wrong without anyone noticing.

* **Which work holds the GIL.** The measurement says ~1.17 cores of Python
  bytecode; it does not say whether that is JSON serialisation, search
  scoring, embedding, or the FastMCP layer. A `py-spy` profile at N=32 names
  it, and that decides whether "move the hot work out of Python" is a small
  fix or a large one.
* **Confirm on prod-shaped data.** This ran against a migrated but nearly
  empty `precis_loadtest`. Row counts change the DB verbs' cost; they do not
  obviously change a GIL ceiling, but the claim should be re-measured against
  a restored corpus before the number is quoted as prod's.
* **The multi-process arm.** Two or three `serve` processes behind a balancer,
  re-ramped, to confirm throughput actually scales with processes.

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

* ~~Should the anyio thread limiter become a tunable?~~ **Answered no,
  2026-09-29.** Measurement shows the pool is not the constraint — the GIL
  is. A bigger pool cannot help work that cannot run in parallel.
* pgbouncer sizing for the prod path — out of scope here, but if the dev-DB
  run shows connections binding before threads do, prod pool sizing becomes a
  real question rather than a hypothetical.
* Whether the harness drives real `claude -p` agents (realistic, expensive,
  noisy) or synthetic callers issuing a fixed verb mix (cheap, repeatable,
  less faithful). Probably both: synthetic for the ramp, real agents for one
  confirmation run.
