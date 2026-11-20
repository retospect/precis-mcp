---
id: precis-llm-ops-help
family: work
title: precis — LLM ops: call failures, deferred calls, the budget breaker, provider accounts and profiles
summary: why an LLM call failed (quota / budget / rate / transport / content / unparseable / subscription_* reason_class + retry horizon), the deferred_llm_call job that re-runs a failed call, the budget breaker's alerts, pool pacing, and the owner door for a hosted provider account or profile (add, enable, list)
answers:
  - why did an LLM call fail, and will retrying help (quota, rate limit, budget)?
  - what is deferred_llm_call?
  - where do I see a tripped LLM budget or quota window?
  - which model chain is stored for an operation, and when was it set?
  - how do I add a hosted provider account or profile, and enable it?
  - how do I list llm accounts?
applies-to: get/search (kind='llm'); reason_class; deferred_llm_call job; /budget
tags: troubleshooting, workflow
kinds: llm
status: active
---

# precis-llm-ops-help — failures, deferred calls, the budget breaker, provider accounts and profiles

The ops side of the `llm` kind — what happens when a call fails, plus
the owner door for provider accounts and profiles below. Choosing a model is [[precis-llm-help]].

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

Every LLM call goes through one router, which stamps each failure with a
`reason_class` and a `retry_at` time. The class says whether waiting helps:

| class | meaning | retry horizon |
|---|---|---|
| `quota` | Claude subscription window used up | the reset time in the notice (2 h if none) |
| `budget` | spend cap, credit balance, HTTP 402, or the dollar breaker | 2 h |
| `rate` | bare 429 / overloaded / 529 | 15 min |
| `transport` | timeout, connection failure, 5xx | 30 min |
| `content` | the model refused or a filter fired | none: another model gets the same answer |
| `unparseable` | the call completed but the reply carried no parseable JSON block — `claude -p`'s output contract broke (prose answer, truncated object, a non-string envelope `result`) | none: a fresh sample from the next rung can still parse — it is not `content` |
| `subscription_cap` | a named provider profile's subscription window/pool is exhausted | the profile window's own expiry |
| `subscription_bulk_refused` | a subscription-profile rung refused bulk (SMALL-tier) work | none: the call's shape can't change |
| `subscription_op_refused` | a subscription-profile rung refused the op (`source` not in its `ops` allowlist) | none: another rung must take it |
| `subscription_worker_refused` | legacy Kimi-subscription refusal of worker calls, retired 2026-11-07 | none — kept so stored rows still classify |

Unrecognised wording stays unclassified and keeps the generic cool-down.
A failover chain falls through to the next model on every class except
`content` — including `unparseable`: it is a
per-sample format flake, so another rung's fresh sample may parse even
though it is the same model on a different wire.

## What is deferred_llm_call? The budget breaker's alerts

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
rate-limit window is exhausted. `get(kind='llm', id='/pools')` is the
live view; nothing LLM-backed runs until the window rolls or the cap is raised.
A related per-host check: `quota_check:auth/<host>:claude-oauth` fires
when a host's Claude OAuth credential is expired or unreadable; agent
ticks on that host fail until an operator replaces the vault token
`CLAUDE_CODE_OAUTH_TOKEN` (see the alert's triage line).

## Why is background Claude work paused? Pool pacing

`get(kind='llm', id='/pools')` shows each pool's `pacing`: `behind`,
`on-pace` (within one point under target), `ahead`, `exhausted` (used up,
or parked until its `retry_at`) or `unknown` (no usage report, or no reset
or expiry ahead). A pool may use 95 % of its limit until six hours before
it resets (or its credit expires), then 100 %. While `llm:claude-sub:5h`
or `llm:claude-sub:7d` reads `ahead` or `exhausted`, background Claude
calls pause with a `quota`-class `budget: claude pool <key> is <state>`
refusal whose `retry_at` is when the pool opens; deliverable and
interactive work keep running under the quota taper. A subscription
profile's window cap counts in its own pools, `llm:<profile>:calls` and
`llm:<profile>:tokens`, shared by every process and listed on `/pools`:
an exhausted row skips the profile's rung, an ahead one skips it for
background calls (`subscription_cap`).

`llm:devin:acu` meters Devin session ACUs and resets weekly;
`llm:openrouter:credits` accumulates per-call dollar cost and never
resets. Neither has a known limit and both are estimates, not
provider-reported balances, so both read `pacing=unknown` by design.

## What would the new router have chosen? /decisions

`get(kind='llm', id='/decisions')` groups calls by task kind —
agreement between the new chooser and the legacy ladder, legacy and
shadow Claude-window counts, actual and projected USD.

## Which offerings suit which task kinds? /suitability

`get(kind='llm', id='/suitability')` shows the suitability ledger — the
stored Beta posteriors over validated outcomes, rolled up nightly per `model@adapter` × task kind × difficulty
band (`low`/`mid`/`high` from the recipe's `difficulty_prior`, else
`any`). One line per bucket: label, `n`, the posterior's 90% lower bound
and its 80% interval. Labels: `unqualified` (under 5 outcomes with no
measured eval — unseen, not inadequate), `not good enough`, `marginal`,
`appropriate`, `vast overkill` (appropriate with headroom ≥ 2 over the
requirement). Only a call that ran and parsed counts as an outcome —
quota, rate, budget, transport and content rows carry no signal, and a
call logged `local-only` keeps no replay blob. `identity=observed` means
the wire reported what actually answered (the response's model field for
HTTP transports, `modelUsage` for agent transports) rather than just
what was requested; `drift=<date>` marks the last day the observed
fingerprint changed, and outcomes before it count half. The rollup
writes `meta.suitability` on each model card plus one
`observed-telemetry` review, both idempotent. decide() never
reads the ledger — it is reviewer-facing evidence, not routing input.

## Add a hosted OpenAI-compatible provider
## How do I add or enable an llm provider account or profile?

`put(kind='llm', args={'account': {...}})` is the owner door for a hosted
provider (Groq, OpenRouter, Cerebras). One put carries the whole ledger
entry: `endpoint={'base_url': 'https://<provider host>/v1'}` (the door
writes the matching `llm.profiles` entry itself — the endpoint host must
be the one bound to `api_key_ref`, and a bare `base_url` key still
refuses), `api_key_ref` naming the vault secret, `models`,
`entitlement.models.<id>` with `price_in`/`price_out` in USD per 1M
tokens, `privacy.training_use` (`no` / `yes` / `unknown`) and `limits` —
`{pool, unit, limit, reset_rule, source_url, verified_at}` objects whose
pool keys are `llm:<account>:<window>`. Every fact needs its `source_url`
(https) and `verified_at`; a limit's `reset_rule` is `rolling:<seconds>`
or a `calendar:` rule, never `expires:`.

The account lands **disabled**. A second put —
`{'name': '<account>', 'enabled': True}` — turns it on; enabling refuses
while its `llm.profiles` entry, the `training_use` stance or a non-empty
`models` list is missing. An enabled account adds one cloud offering per
model to `decide()` — priced from the entitlement entry, `free` at 0/0,
unpriced (never free) when the price is unknown — and a non-default
privacy class rejects its offerings unless `training_use` is `no`.

The recorded limits are evidence, not enforcement: nothing meters them
yet, so the pools read `pacing=unknown` until a usage report lands.
`get(kind='llm', id='/accounts')` lists the account names.

## See also

- [[precis-llm-help]] — the model catalog: choose, compare, review
- [[precis-minter-help]] — meta.llm_tier → minter worker → model
