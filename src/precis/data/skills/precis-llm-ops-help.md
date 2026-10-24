---
id: precis-llm-ops-help
family: work
title: precis — LLM call failures, deferred calls and the budget breaker
summary: why an LLM call failed (quota / budget / rate / transport / content reason_class + retry horizon), the deferred_llm_call job that re-runs a failed call, the budget breaker's alerts, and the catalog-reconcile / model-swap daemons
answers:
  - why did an LLM call fail, and will retrying help (quota, rate limit, budget)?
  - what is deferred_llm_call?
  - where do I see a tripped LLM budget or quota window?
applies-to: get/search (kind='llm'); llm_call_log reason_class; deferred_llm_call job; /budget
tags: troubleshooting, workflow
kinds: llm
status: active
---

# precis-llm-ops-help — failures, deferred calls, the budget breaker

The ops side of the `llm` kind — what happens when a call fails.
Choosing a model is [[precis-llm-help]].

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
(warn). `llama_swap` is the per-node VRAM model-swapper serving local
inference — the daemon behind the local rungs.

## See also

- [[precis-llm-help]] — the model catalog: choose, compare, review
- [[precis-minter-help]] — meta.llm_tier → minter worker → model
