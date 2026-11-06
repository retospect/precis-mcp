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

`llm:devin:acu` meters Devin session ACUs — each devin_api poll adds the
increment over the job's `usage.acus_fed` high-water mark — and resets
on a weekly calendar rule (Sunday 08:00 Etc/UTC). Its limit is unknown
(NULL), as is `llm:openrouter:credits`'s: that pool accumulates
per-call `usage.cost` dollars reported by calls to an `openrouter.ai`
host and never resets; a price-table estimate is not spend, and calls
through `run_oss_tool_loop` are not metered yet. Both figures are
estimates from per-call and per-session usage, not provider-reported
balances — Devin has no dollar pool (on-demand dollars stay a hard
stop in job meta) and there is no OpenRouter management key for a
credit-balance read — so both pools read `pacing=unknown` by design.

## What would the new router have chosen? /decisions and llm.decide

`llm.decide.mode` defaults to `legacy`; `shadow` and `on` compute and log
the decision while executing the legacy chain in this slice. Shadow changes
nothing executed. `get(kind='llm', id='/decisions')` groups calls by task kind,
showing agreement, legacy and shadow Claude-window counts, actual and projected
USD, and how many calls have known projected prices. Reto sets the global
`llm.decide.mode` row and optional `llm.decide.<prefix>` rows, where the prefix
ends at the first dot or colon in the task kind; `legacy`, `shadow`, and `on`
are the accepted values. `precis llm decide` previews a choice without an LLM call;
`precis llm decide-mode <prefix|default> <legacy|shadow|on|clear>` writes or
clears those rows.

## Which offerings suit which task kinds? /suitability

`get(kind='llm', id='/suitability')` shows the suitability ledger — the
stored Beta posteriors over validated outcomes, rolled up nightly by the
`llm_suitability` cadence per `model@adapter` × task kind × difficulty
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
`observed-telemetry` review, both idempotent; `precis llm suitability
[--model M] [--days N] [--dry-run]` recomputes on demand. decide() never
reads the ledger — it is reviewer-facing evidence, not routing input.

## See also

`pool_refresh` copies Codex weekly usage from fleet host reports and Devin's
session pause into the pool ledger every 600 seconds, without model calls.
It is dark until `service_config` lights it. Claude subscription pools receive
stream and quota-probe reports; Kimi and OpenRouter have no refresh leg here.

- [[precis-llm-help]] — the model catalog: choose, compare, review
- [[precis-minter-help]] — meta.llm_tier → minter worker → model
