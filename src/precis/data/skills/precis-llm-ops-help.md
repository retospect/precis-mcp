---
id: precis-llm-ops-help
family: work
title: precis — LLM call failures, deferred calls and the budget breaker
summary: why an LLM call failed (quota / budget / rate / transport / content reason_class + retry horizon), the deferred_llm_call job that re-runs a failed call, the budget breaker's alerts, and the catalog-reconcile / model-swap daemons
answers:
  - why did an LLM call fail, and will retrying help (quota, rate limit, budget)?
  - what is deferred_llm_call?
  - where do I see a tripped LLM budget or quota window?
  - which model chain is stored for an operation, and when was it set?
applies-to: get/search (kind='llm'); llm_call_log reason_class; deferred_llm_call job; /budget
tags: troubleshooting, workflow
kinds: llm
status: active
---

# precis-llm-ops-help — failures, deferred calls, the budget breaker

The ops side of the `llm` kind — what happens when a call fails.
Choosing a model is [[precis-llm-help]].

## Read an operation's stored model route

`get(kind='llm', id='/ops')` lists registered operations and stored
`llm.op.*` overrides, including unregistered rows marked inert.
`get(kind='llm', id='graph_garden:propose')` (or `id='llm_summarize'`)
shows the code default, stored primary/fallback rungs in order, model
ids, max_tokens, reasoning knobs, quota_fallback and the UTC last-set
time. Without an override it says so; this is stored configuration,
not a claim about the effective route after request pins and runtime
gates. Endpoint/auth fields are omitted. Both reads make no LLM calls.
Per-op call telemetry stays at `id='op:<source>', view='stats'`.

## Why did an LLM call fail? Quota, rate limit, retry
## What is deferred_llm_call?

Every LLM call goes through one router, which stamps each failure with a
`reason_class` and a `retry_at` time. The class says whether waiting helps:

| class | meaning | retry horizon |
|---|---|---|
| `quota` | Claude subscription window used up | the reset time in the notice (2 h if none) |
| `budget` | spend cap, credit balance, HTTP 402, or the dollar breaker | 2 h |
| `rate` | bare 429 / overloaded / 529 | 15 min (the SMALL lane retries these in-process first) |
| `transport` | timeout, connection failure, 5xx | 30 min (also retried in-process by SMALL) |
| `content` | the model refused or a filter fired | none: another model gets the same answer |

Unrecognised wording stays unclassified and keeps the generic cool-down.
A failover chain falls through to the next model on `quota`, `rate`,
`budget` and `transport`, never on `content`.

**Quota stamping.** Claude reports an exhausted quota as ordinary answer
text, so the router recognises the wording ("hit your session limit ...
resets 3pm (UTC)", "out of extra usage") when it makes up the whole reply.
On a live `quota` failure it also writes the exhaustion into the shared
quota snapshot, so the next Claude call pauses instead of every caller
rediscovering the limit. Only subscription transports do this; a
pay-per-token rung does not.

**`deferred_llm_call`** is the job a synchronous surface (a web follow-up)
queues when its call fails with `quota` or `budget`. The human's turn stays
as the resume point, the job sleeps until `retry_at`, re-runs the saved
request and appends the real answer to the thread. A repeat `quota` or
`budget` failure defers again, up to 6 times; any other failure, or the
bound, appends a `system` turn naming the failure and fails the job. A
failure is never stored as the assistant's answer.

**The breaker's alerts.** Two critical alerts surface the pause to the
alert surface: `budget/cap-<window>` (`cap-hourly`, `cap-daily`) when a
dollar spend-cap window trips, and `budget:quota/quota-<window>`
(`quota-five_hour`, `quota-seven_day`) when a Claude subscription
rate-limit window is exhausted. `get(kind='llm', id='/budget')` is the
live view; nothing LLM-backed runs until the window rolls, the cap is
raised, or an operator resumes on `/budget`. A related per-host check:
`quota_check:auth/<host>:claude-oauth` fires when a host's Claude OAuth
credential is expired or unreadable (re-login there).

**`llm_reconcile` and `llama_swap`.** `llm_reconcile` diffs the
catalogue against the proxy's served list — a catalogued model the proxy
no longer serves raises `llm_reconcile:drift/proxy-missing:<model_id>`
(warn). It also refreshes the `devin` `llm_account` row's entitlement
from Devin's AI Gateway models list, stamping `stale_since` on a model
whose price changed. `llama_swap` is the per-node VRAM model-swapper
serving local inference — the daemon behind the local rungs.

**`offer_discovery`.** Every 3 days, one Perplexity call surfaces a free
model Groq/Cerebras/OpenRouter are currently offering; a model not yet
tracked is a lead, filed as one `ask-user` todo (never more than one per
lead). An approved lead gets a dark `llm_account` row + card, a golden
eval against the tier's incumbent recorded on its card, and a second
`ask-user` todo before anything is ever enabled. Groq/Cerebras/OpenRouter
free models enter the catalog ONLY this way — never hand-seed one.

## See also

`pool_refresh` copies Codex weekly usage from fleet host reports and Devin's
session pause into the pool ledger every 600 seconds, without model calls.
It is dark until `service_config` lights it. Claude subscription pools receive
stream and quota-probe reports; Kimi and OpenRouter have no refresh leg here.

- [[precis-llm-help]] — the model catalog: choose, compare, review
- [[precis-minter-help]] — meta.llm_tier → minter worker → model
