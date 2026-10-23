---
status: draft
title: Class lanes on each local LLM endpoint — floors, caps, fillers, user work first
pillar: local-compute
prio: high
---

# Class lanes on each local LLM endpoint

Reto, 2026-10-23: "can we say castor always gardens, but at least 2
lanes. If classify, at least two lanes, … if user up to 4 lanes
available? That sort of 'use all the cycle for forever op but bump if
others exist'". Earlier ruling: "we want to schedule evenly, with human
jobs taking precedence over background."

This is the `llm-local` class of `outbound-policy.md`: no rate, slots.
It divides the slots an endpoint has; the number of slots is the
feedback controller's (`llm-dispatch-feedback-controller.md`), today the
`resource_slots` `llm:<model>` capacity (`local_serving.acquire`).

## Motivation / why

`local_serving.acquire` is one semaphore per model: 32 slots on castor
SGLang, first come first served, a bounded wait then fall to cloud. A
background flood (classify, llm_summarize at ~32.7k calls/day, ~7.5
concurrent on average) takes every slot, so an interactive request waits
behind it or pays the cloud rung, and a "forever" job (graph_garden) has
no way to soak idle slots without starving everyone when they return.

## In scope

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
| `graph_garden` | 4 | 2 | slots | yes |

A request carries its class, mapped from `req.source` (`llm.op.<source>`,
`utils/llm/operations.py`) through a `source → class` map in the same
config; an unmapped source is `background` (priority last, min 0, no
filler).

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
pile-up (recurring ticks minted gardener jobs that queued for hours).
"Queued work" for the dispatch rule means: a queue or event source has
rows, a generator answers "yes, next", an RPC or interactive caller is
waiting.

### Endpoints and castor2

The pool is config rows per endpoint: `{host, model, slots,
capabilities}`. castor2 or a spark adds slots to the same model pool, or
serves a different model (a small model for classify). The router
matches a class's required capability or model to endpoints; floors and
maxes apply per pool, not per host. An endpoint down (heartbeat gone,
`resource_slots` row stale) takes its slots out of the pool and floors
re-apply over what is left; floors that no longer fit are honoured in
priority order.

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
caller goes straight to SGLang under a per-process cap
(`PRECIS_LOCAL_LANE_FALLBACK_CAP`, default 4), logged once per hour.
The gateway's one advantage, true queueing without polling, is not worth
a second process until p95 queue wait shows polling slack matters.

### Observability

Per class and endpoint: in-flight, queued, wait p50/p95, lanes used vs
floor. `get(kind='llm', id='/lanes')` (sibling of `/placement`) and
`precis llm lanes`; `llm_call_log` gains `queue_wait_ms` and `lane_class`.

### Filler yield guard (noted, not built)

A filler's value is measured — the gardener's applied/proposed yield
(gr504843, `graph-gardener-value.md`) — so "always gardens" is gated on
that yield staying above its floor. Wire the gate when the yield metric
is stable; until then the filler flag is config.

## Explicitly NOT in scope

- The slot count per endpoint and its feedback loop:
  `llm-dispatch-feedback-controller.md`.
- Cloud overflow policy per class (today's `fell_to_cloud` path stays:
  a non-filler class that cannot get a lane within its wait falls to
  cloud as now; fillers never do).
- Rate/timeout policy: `outbound-policy.md`.
- The yield guard itself.

## Slices

| slice | delivers |
|---|---|
| S1 | allocator + config tables + classes for today's castor callers (user, classify, llm_summarize, graph_garden, background); `local_serving.acquire(class=…)`; source kinds interactive, queue and pull generator (the gardener filler becomes a generator) |
| S2 | observability: `/lanes` view, CLI, `llm_call_log` columns |
| S3 | multiple endpoints: castor2/spark rows, capability routing, pool-level floors, endpoint-down re-apply |
| later | agent loops (lane max, turn-boundary yield) and shadow evaluation share; RPC deadlines ride `outbound-policy.md` S2 |

## Acceptance criteria

- S1: a test saturates an endpoint of 8 slots with gardener + classify
  waiters, then enqueues 3 user requests: each user request holds a
  lane within one request duration, classify never drops below 2
  in-flight while it has waiters, the gardener holds exactly the
  remainder; with no other waiters the gardener holds all 8. Prod:
  castor's slots stay ≥90 % busy over an hour while an MCP session's
  local call waits < one request duration at p95.
- S2: `/lanes` shows per-class in-flight/queued/wait and lanes vs floor
  for castor; `llm_call_log.queue_wait_ms` is set on every local call.
- S3: a second row for castor's model doubles the measured concurrent
  calls with floors holding per pool; a class whose capability only one
  endpoint has never lands on the other; stopping one endpoint's
  heartbeat removes its slots within one poll and no floor is granted
  against a dead host.

## Target + blast radius

`utils/llm/local_serving.py` (`acquire`, `_reserve_one`),
`store/_resource_slots_ops.py`, `utils/llm/operations.py` (source →
class), `handlers/llm.py` (`/lanes`), `cli/`, one migration. Every local
LLM dispatch goes through it; an allocator fault degrades to the
per-process fallback cap, never to a failed call.

## Open questions / decisions log

- `llm_summarize` floor/max and whether `classify` gets a max: Reto.
- An agent-lane job (`castor-agent-lane.md`) is the `agent loop` kind;
  its class and max are open.
- Whether `priority` ties are round-robin or config order (proposed:
  round-robin among equal priority, so "schedule evenly" holds within a
  tier).
