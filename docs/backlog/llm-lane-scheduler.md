---
status: draft
title: Class lanes on each local LLM endpoint — slot count from the server's load, floors, caps, fillers, user work first
pillar: local-compute
prio: high
---

# Class lanes on each local LLM endpoint

Reto, 2026-10-23: "can we say castor always gardens, but at least 2
lanes. If classify, at least two lanes, … if user up to 4 lanes
available? That sort of 'use all the cycle for forever op but bump if
others exist'". Earlier ruling: "we want to schedule evenly, with human
jobs taking precedence over background." Reto, 2026-10-02 (review item
local-compute-4, ruling 3): target ~32 parallel sequences per serving box
with best-practice serving, plus a feedback (PID-style) controller that
keeps the boxes busy.

**Scope.** Owns the `llm-local` class of `outbound-policy.md` end to end:
how many slots a local endpoint advertises (the capacity controller,
folded from `llm-dispatch-feedback-controller.md` 2026-10-23) and which
call gets one (classes, floors, maxes, fillers). Its only state is the
shipped slot-hold primitive — `resource_slots` / `resource_slot_holds`
(`store/_resource_slots_ops.py`), reserved by `local_serving.acquire` —
extended with `class`, floors, max and filler; no second semaphore, no
gateway daemon. Uses the rate bucket for nothing (local endpoints have no
rate). Does not set rates, timeouts or `on_refuse` (`outbound-policy.md`),
does not choose the model or server (`vllm-per-node-serving.md`), and does
not pick which job a worker claims (below).

**Two scheduling levels, both legitimate.** Level 1, the job queue: which
job a worker claims and on which host (`cluster-scheduling.md`,
`job-scheduling-fairness.md`; `claim_executor_jobs`, `prio`,
reserve-at-claim for hardware slots). Level 2, lanes: which LLM call gets
a slot on an endpoint (this spec). The levels do not reach into each
other: a job that makes LLM calls waits in `acquire` like today, and no
job is ever minted only to wait for a lane — fillers are pull generators,
so the lane level never grows a queue of jobs that exist to soak idle
slots (gr506771's pile-up: recurring ticks minted gardener jobs that
queued for hours).

## Motivation / why

`local_serving.acquire` is one semaphore per model: 32 slots on castor
SGLang, first come first served, a bounded wait
(`PRECIS_LOCAL_SLOT_WAIT_S`) then fall to cloud. A background flood
(classify, llm_summarize at ~32.7k calls/day, ~7.5 concurrent on average)
takes every slot, so an interactive request waits behind it or pays the
cloud rung, and a "forever" job (graph_garden) has no way to soak idle
slots without starving everyone when they return.

The slot count itself is a fixed number in three places that drift
apart: the `llm` card's `max_parallel`, the `resource_slots` `llm:*`
capacity, and the server's own limit (`--max-running-requests` /
`--max-num-seqs`). The 6-vs-4 mismatch in the shipped local summarise
chain (`workers/job_types/derived_drain.py`) was that drift. A fixed cap
is wrong in both directions: long prompts exhaust KV cache below it,
short ones leave the box idle above it.

## In scope

### Capacity: slots per endpoint, one number in three places

- **One number.** The endpoint's `resource_slots` row capacity is what
  `local_serving.acquire` reserves against; card `max_parallel`
  (`reconcile_llm_served_slots`) and the server's hard limit are set
  from it, the server's limit being the ceiling the controller moves
  below. A test fails if a second writer appears.
- **Setpoint on the box's live pressure**, scraped each heartbeat beat on
  the serving host. Signals: the server's metrics (SGLang `/metrics`:
  `sglang:num_running_reqs`, `sglang:num_queue_reqs`,
  `sglang:token_usage` (KV utilisation), the retraction counter — confirm
  the exact names against the running image; `--enable-metrics` is NOT
  on today, `sglang_extra_args: []` in the role; vLLM equivalents
  `num_requests_running`, `num_requests_waiting`, `gpu_cache_usage_perc`,
  `num_preemptions_total`) **and host memory and swap** — on a
  Spark-class box GPU and CPU share unified memory, so host swap-in is
  model pressure too. The loop admits while queue ≈ 0, KV use is under
  threshold and swap-in is 0; it backs off when queue climbs,
  retractions tick or swap-in appears. Control variable: the row's
  capacity (units once S2b lands); the server flag never moves at
  runtime — one fixed server config, mixed batching, no time-sliced
  phases (a flag change is a restart and a model reload). Target: about
  32 running.
- **Back-off order is class-aware** (Reto 2026-10-23: "Do we get swap
  alerts? Seems like PID: if swap, bump large down to 1, then start
  bumping mediums down to 1, then add empty lanes? Dynamically per
  box."). On pressure: fillers are cut first of all; then the big class
  is capped at 1 lane; then medium at 1; then the pool shrinks. On
  recovery, grow back in reverse: add lanes, then raise medium, then
  big — AIMD-style, per box. Interactive floors are never cut. The
  integral term is added only if the box oscillates.
- **Alerting first (S0).** Nothing scrapes SGLang metrics today and
  `workers/conditions.py` has no KV, preemption or swap probe, so there
  are no swap alerts. A condition row `local-serving-pressure` — sustained
  KV usage over threshold, retractions > 0, or host swap-in rate > 0 for
  N minutes on a serving host — rides the existing hourly registry and
  alert router; useful before the controller exists and is its first
  consumer of the scrape.
- **Ceiling today is 32**, not 64: castor's SGLang role runs
  `--mem-fraction-static 0.80 --max-running-requests 32` because the
  0.86/64 trial hung castor (gr485635, do not raise). Reto's channels
  ruling (2026-10-03, local-compute-15) — "the server is sized for 64
  streams (`--max-num-seqs 64`). The feedback controller holds about 32
  while interactive tiers use it and fills to 64 with graph maintenance" —
  predates the vLLM→SGLang switch; the fill-to-ceiling rule stands with
  the ceiling the role sets.
- **Overflow to cloud.** A non-filler class that cannot hold a lane within
  its wait takes the chain's cloud rung as now (`fell_to_cloud` in
  `/placement`), never an unbounded queue wait for a latency-bound
  caller. Fillers never overflow.
- Measured by `/placement` (`kept_local`, `fell_to_cloud` per tier) and a
  running-sequences-over-time series from the per-beat heartbeat counters
  (`vllm-per-node-serving.md` Slice 3 lands the scrape).

### Allocator

A work-conserving allocator per model pool. One config row per endpoint
(`llm_endpoints`: `host`, `endpoint`, `model`, `slots`, `capabilities`);
a second host serving the same model is a second row, so its slots add
(see Endpoints and castor2).

Classes, in DB config (`llm_lane_classes`: `endpoint`, `name`,
`priority`, `min_lanes`, `max_lanes`, `filler bool`). Starting set for
castor, Reto's numbers where he gave them:

| class | priority | min | max | filler |
|---|---|---|---|---|
| `user` (interactive: MCP session calls, chat, agent turns) | 1 | 0 | 4 | no |
| `classify` | 2 | 2 | ? | no |
| `llm_summarize` | 3 | ? | ? | no — Reto's numbers needed |
| `castor_agent` (`castor-agent-lane.md` agent lane; Reto 2026-10-22: one channel at a time) | 2 | 0 | 1 | no |
| `castor_batch` (`castor-agent-lane.md` batch lane, `llm.op.castor_lane:*`) | 3 | 0 | 16 | no |
| `graph_garden` | 4 | 2 | slots | yes |

A request carries its class, mapped from `req.source` (`llm.op.<source>`,
`utils/llm/operations.py`) through a `source → class` map in the same
config; an unmapped source is `background` (priority last, min 0, no
filler). The castor agent lane's shim requests carry `source
castor_lane:agent` so they land in their class rather than in `user`.

### Work sources

Reto, 2026-10-23: "Sources for castor can be queues, or code requests
(run this on castor, come back) or search (find next xxx that is
uncategorized). Other sources? … Also we may get castor2." A class is
source kind × priority. Kinds:

| kind | shape | priority | bumpable |
|---|---|---|---|
| interactive | a human or agent session waits (MCP calls, chat, Discord bot) | highest, small max | n/a (short) |
| code request / RPC | "run this on castor and come back" from a workspace script or job; the caller blocks, a deadline is required | high | no |
| queue | minted jobs (summarise, classify drain) | per class | between items |
| pull generator ("search") | an idle lane asks the generator for the next item ("next uncategorized chunk", "next gardener candidate"); nothing is pre-queued | per class; fillers MUST be this kind | between items |
| event-triggered | on ingest or on change (new paper → summary); a queue with freshness priority | per class | between items |
| re-evaluation / backfill | re-run old outputs after a model or prompt change, or re-judge a sample | low; filler candidate | between items |
| evaluation / shadow | castor beside a cloud model on a sample to measure agreement (today's classify probe) | a small fixed share | between items |
| agent loop | multi-turn tool-using agents on castor; hold a lane for many turns | per class, with a max | at turn boundaries |

Each source declares `kind`, `class`, a deadline or latency target, and
whether it can be bumped between items. Fillers are pull generators by
rule: a filler that mints work pre-queues it, which is gr506771's
pile-up. "Queued work" for the dispatch rule means: a queue or event
source has rows, a generator answers "yes, next", an RPC or interactive
caller is waiting.

### Endpoints and castor2

The pool is config rows per endpoint: `{host, model, slots,
capabilities}`. castor2 or a spark adds slots to the same model pool, or
serves a different model (a small model for classify). Reto 2026-10-22
(`castor-agent-lane.md` ruling): "a second spark-class host with the same
model is the option for more coding capacity" — that host is a second
row in this pool, nothing else. The router matches a class's required
capability or model to endpoints; floors and maxes apply per pool, not
per host. An endpoint down (heartbeat gone, `resource_slots` row stale)
takes its slots out of the pool and floors re-apply over what is left;
floors that no longer fit are honoured in priority order.

### Dispatch rule

When a slot frees (or a request arrives and a slot is free), grant it to:

1. the highest-priority class with queued work that is below its
   `min_lanes`; else
2. the highest-priority class with queued work below its `max_lanes`,
   provided the grant leaves `sum(max(0, min_c − inflight_c))` free for
   every other class with queued work; else
3. a filler, subject to the same reservation.

Non-preemptive: an in-flight request (~10–20 s) finishes; "bump" means
the next free slot goes to the higher class. Fillers never hold more than
`slots − sum(floors of classes with queued work)`. Floors sum must be
≤ `slots` (config check).

### Where it runs: Postgres lease (recommended)

(b) extend `resource_slots`/`resource_slot_holds` with `class` and a
waiter row (`llm_lane_waiters`: endpoint, class, enqueued_at), so the
grant statement can see queued demand per class. The grant is one
`UPDATE … WHERE` under the rule above, the same atomic shape as
`rate_limit._CONSUME_SQL`; waiters poll with jitter as `acquire` does
today.

Why over (a) a gateway on castor with per-class queues: a call lasts
10–20 s, so a DB round trip is noise; every caller already holds the
store and `local_serving.acquire` already reserves there, so no new
daemon, deploy role or single point of failure in front of the model;
and the failure mode is the existing dark path — DB unreachable → the
caller goes straight to SGLang under a new per-process cap
(`PRECIS_LOCAL_LANE_FALLBACK_CAP`, default 4), logged once per hour.
The gateway's one advantage, true queueing without polling, is not worth
a second process until p95 queue wait shows polling slack matters.

Server-side priority scheduling (SGLang `--enable-priority-scheduling`,
proposed in `castor-agent-lane.md` §6) is not a second allocator: at most
the lane class maps to a `priority` the local wire forwards so the
server's retraction policy agrees with the lanes. Off until a measured
retraction storm says otherwise.

### Observability

Per class and endpoint: in-flight, queued, wait p50/p95, lanes used vs
floor, **hold time mean and p95** (the data for the weighting question
below). `get(kind='llm', id='/lanes')` (sibling of `/placement`) and
`precis llm lanes`; `llm_call_log` gains `queue_wait_ms` and `lane_class`.

### Filler yield guard (noted, not built)

A filler's value is measured — the gardener's applied/proposed yield
(gr504843, `graph-gardener-value.md`) — so "always gardens" is gated on
that yield staying above its floor. Wire the gate when the yield metric
is stable; until then the filler flag is config. `local-mesh-upkeep.md`
§3 supplies per-action eligibility (which actions may run local, at which
bar); its review lane is cloud spend on its own line, and the "fillers
never overflow to cloud" rule covers the maintenance units only.

## Explicitly NOT in scope

- Cloud overflow policy per class beyond today's `fell_to_cloud` path.
- Rate/timeout policy and the `llm-api` (paid) concurrency hold:
  `outbound-policy.md`.
- The yield guard itself.
- MCP agent-session admission (`mcp-concurrency-load-test.md` slice 2
  prototypes a loop over the same `/metrics` that tops agent sessions
  up; this item reuses its scrape and thresholds, and builds no second
  scraper).
- Choosing the model or the server; the metrics scrape into
  `host_heartbeat_log`: `vllm-per-node-serving.md`.

## Slices

| slice | delivers |
|---|---|
| S0 | `--enable-metrics` on castor's role (Reto deploys); the heartbeat scrape of SGLang `/metrics` + host swap into `host_heartbeat_log` (shared with `vllm-per-node-serving.md` Slice 3 — one scrape); condition row `local-serving-pressure` |
| S1 | allocator + config tables + classes for today's castor callers (user, classify, llm_summarize, castor_agent, castor_batch, graph_garden, background) over the static capacity (32); `local_serving.acquire(class=…)`; source kinds interactive, queue and pull generator (the gardener filler becomes a generator) |
| S2 | observability: `/lanes` view, CLI, `llm_call_log` columns, hold-time per class |
| S2b | mix benchmark on castor off-peak: throughput and latency per size class across mixes; sets the pool size in units and the unit weights, checks `max_running_requests`, chunked prefill and mem fraction; weighted units (`size_class` on the `llm.op` row) switch on only if S2 shows count-based lanes misallocate |
| S3 | capacity controller over S0's signals: capacity moved between a floor and the server limit in the class-aware back-off order, the one-writer test |
| S4 | multiple endpoints: castor2/spark rows, capability routing, pool-level floors, endpoint-down re-apply |
| later | agent loops (lane max, turn-boundary yield) and shadow evaluation share; RPC deadlines ride `outbound-policy.md` S2 |

S1 does not wait on S0 or S3: lanes divide whatever capacity the row
holds. The old `blocked-by: vllm-per-node-serving` on the controller is
stale — castor serves gpt-oss-120b on SGLang today; what S3 waits on is
S0's scrape, nothing else.

## Acceptance criteria

- S1: a test saturates an endpoint of 8 slots with gardener + classify
  waiters, then enqueues 3 user requests: each user request holds a
  lane within one request duration, classify never drops below 2
  in-flight while it has waiters, the gardener holds exactly the
  remainder; with no other waiters the gardener holds all 8. Prod:
  castor's slots stay ≥90 % busy over an hour while an MCP session's
  local call waits < one request duration at p95.
- S2: `/lanes` shows per-class in-flight/queued/wait/hold time and lanes
  vs floor for castor; `llm_call_log.queue_wait_ms` is set on every local
  call.
- S0: `host_heartbeat_log` on castor carries running/queued/KV/retraction
  and swap-in per beat; `local-serving-pressure` fires in a test with a
  synthetic swap-in series and on prod under a deliberate 66k-context
  burst.
- S3: under a synthetic ramp of 1 to 64 concurrent dispatches the serving
  box holds near the setpoint, retractions/preemptions stay flat, and the
  excess shows as `fell_to_cloud`; under injected swap-in the filler
  drops to 0, then the big class to 1, then medium to 1, then the pool,
  in that order, with the interactive floor intact throughout; card `max_parallel`, the slot capacity
  and the server limit cannot disagree (one source, a test fails if a
  second writer appears); with no interactive load, the filler keeps the
  box at the setpoint (the "is the box hot" number, gr458727).
- S4: a second row for castor's model doubles the measured concurrent
  calls with floors holding per pool; a class whose capability only one
  endpoint has never lands on the other; stopping one endpoint's
  heartbeat removes its slots within one poll and no floor is granted
  against a dead host.

## Target + blast radius

`utils/llm/local_serving.py` (`acquire`, `_reserve_one`),
`store/_resource_slots_ops.py`, `utils/llm/operations.py` (source →
class), `workers/heartbeat.py` (scrape on the serving host, S0),
`workers/conditions.py` (`local-serving-pressure`, S0),
`deploy/roles/sglang` (`--enable-metrics`, Reto deploys),
`llm_catalog.py` (card `max_parallel` from the row, S3), `handlers/llm.py`
(`/lanes`), `cli/`, one migration. Every local LLM dispatch goes through
it; an allocator fault degrades to the per-process fallback cap, never
to a failed call.

## Open questions / decisions log

- `llm_summarize` floor/max and whether `classify` gets a max: Reto.
- Whether `priority` ties are round-robin or config order (proposed:
  round-robin among equal priority, so "schedule evenly" holds within a
  tier).
- **Count requests or weight them?** Reasoning effort is per request on
  castor: SGLang batches low, medium and high together, and today the
  gardener runs at medium while summarise runs at low, concurrently. A
  high-effort call can hold a lane 10–50× longer (probe: low ~55,
  medium ~150 completion tokens; high can be thousands) and uses more KV
  cache. Should a lane count requests in flight (simple), or be weighted
  by expected output tokens or reasoning effort (fairer under mixed
  effort)? Refined with Reto 2026-10-23:
  - **No time-slicing.** No phases like "8 lanes of big jobs for 30 min,
    then 64 lanes of tiny jobs": SGLang's max running requests is a
    launch flag, so changing it is a restart and a model reload; phases
    starve interactive work; and continuous batching of mixed sizes is
    where the throughput comes from (decode is memory-bandwidth-bound, so
    many small requests share each weight read; chunked prefill keeps big
    prompts from stalling decode).
  - **One fixed server config, mixed batching, lanes as weighted units.**
    The pool is set from KV-cache capacity and each request holds units
    by size class. Strawman: tiny (classify) = 1, medium (summarise, the
    gardener at medium) = 2, big/high-reasoning or long-context = 8.
    Floors and maxes are in units. Example mix: 2 big + 10 medium + 30
    tiny ≈ 66 units.
  - **Size class is declared per operation** in its `llm.op.<source>` row
    beside `reasoning_effort` and `max_tokens`, and is corrected from
    measured hold time and tokens (`/lanes`, S2).
  - **Order:** S1 counts requests (every unit 1). A benchmark slice (S2b)
    runs mixes on castor off-peak and measures throughput and latency per
    class; that sets the pool size and the unit weights and checks
    SGLang's `max_running_requests`, chunked prefill and mem fraction.
    Weights switch on only if the data shows count-based lanes
    misallocate.
- **Per-request reasoning-effort override?** Effort is configured per
  chain rung today (`router.py::Rung.reasoning_effort`, local transport
  only, set in `llm.op.<source>` / `llm.chain.*` rows). Should a request
  be able to override it — an escalation retry at higher effort for a
  boundary case (a verify verdict in the fuzzy `partial` stratum, a
  classify refusal)? If yes, the override is an `LlmRequest` field the
  local wire forwards, the lane class stays the source's, and the
  weighting question above becomes live sooner, since one source then
  mixes efforts. If no, escalation is a second rung in the chain at
  higher effort, which keeps effort visible in config and `/placement`.
- Carried from `llm-dispatch-feedback-controller.md` (deleted
  2026-10-23): setpoint source — a fixed ~32 from Reto's ruling, or
  re-derived from the Slice 0 curve (the concurrency where aggregate
  tok/s plateaus; vLLM knee at 64, SGLang KV-full at 64). Proposal: the
  plateau, capped at the role's hard limit (32) until measured otherwise.
- Carried: PID on running sequences vs bang-bang on waiting/KV
  thresholds. Start with the threshold loop the admission prototype
  describes, and add the integral term only if the box oscillates.
- Carried from `castor-agent-lane.md` §6: the SGLang per-request
  `priority` field name is undocumented in the OpenAI-compatible body;
  probe `/v1/chat/completions` before forwarding anything.
