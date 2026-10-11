---
id: precis-llm-help
family: work
title: precis — the model catalog (choose the right LLM)
summary: model choice as a queryable resource — read a model's facts + capability, express a requirement, let the policy pick
answers:
  - what model should I use for a given task?
  - how do I see whether a model has actually been good, cheap, or reliable here?
  - how do I leave a note about how a model performed?
  - what's the difference between meta.llm_tier and a specific model?
  - why did an LLM call fail, and will retrying help (quota, rate limit, budget)?
  - what is deferred_llm_call?
applies-to: get/search/put (append a review)/tag/link (kind='llm')
tags: verbs, workflow
kinds: llm
status: active
---

# precis-llm-help — model choice as a queryable, learnable resource

The `llm` kind is a **catalog** of the models precis can run: one catalog
card per model (a model fact-sheet, not an embedding card), carrying its
context window, price, capability, and a ledger of how it has
actually performed on precis's own work. It exists so model choice is an
*informed, window-safe, budget-aware* decision instead of a hardcoded constant.

The canonical address is the handle `lm<id>` (e.g. `lm7`), but the **model slug
is the human key** — `get(kind='llm', id='claude-opus-4-8')` resolves it.

Cards are **machine-maintained** (a reconcile pass keeps facts true against the
live model feed); you *read* the catalog, you don't hand-author it.

## What model should I use for X? / Which LLM is good at Y?
## Compare models / see a model's window, price, capability

```python
search(kind="llm", q="careful SQL and multi-file refactors")
get(kind="llm", id="claude-opus-4-8")
```

`search` matches your phrasing against each card's capability prose. `get` shows
the facts: `tier_floor`, `offerings` (operating points — effort, transport,
window, price), the coarse **1–5 capability axes** (`code`,
`long-context-recall`, `tool-structured`, `reasoning-convergence`,
`summarize-extract`), and provenance.

## Has this model actually been good / cheap / reliable here?
## See a model's realized cost + error rate + reviews

```python
get(kind="llm", id="claude-opus-4-8", view="tote")
get(kind="llm", id="claude-opus-4-8", view="reviews")
```

`view='tote'` rolls up `llm_call_log` — realized calls, cost, error rate, p50
duration — the model's track record on precis's own workload. `view='reviews'` is
the append-only, dated ledger of typed observations, each tagged by **evidence
band**: `observed-telemetry` (measured on your traffic) > `measured-eval` (your
own golden sets) > `published-benchmark` (vendor numbers, low-trust). These
never blend — a vendor MMLU score never outweighs a measured result.

## How much LLM traffic runs locally

```python
get(kind="llm", id="/placement")
```

Last 7 days of `llm_call_log`, per tier and per UTC day: calls landed local
(the rung that ran), and for rows that record it, calls routed local, the share
kept local, and how many fell back to cloud (local rung skipped, slot busy, or
failover). `billed_usd` leaves out calls that landed local, because their cost
is a priced estimate, not money spent.

## Leave a note about how a model did
## Record that a model was great / weak at something

```python
put(
    kind="llm",
    id="claude-opus-4-8",
    text="excellent at SQL-migration reasoning",
    entry="agent-review",
    by="agent",
)
```

Appends one WORM review entry. `entry=` is one of `agent-review` (a subjective
observation), `measured-eval`, `observed-telemetry`, or `published-benchmark`.

## Which model should the policy pick for my task?

**Don't pick a model from the list — describe what the task *needs*.** A frontier
model is good at judging *task → requirement* but biased at *requirement → model*
(it's price-/window-blind and reaches for the biggest model). So the discipline
is: infer a **requirement** (dominant capability axis + how strong + window +
whether it needs tools), and a deterministic policy maps that to the cheapest
model that fits — filtered by window, budget, and availability, with a Pareto
"next better" rung for an escalation. An empty catalog degrades to today's tier
default, so this is always safe.

You express the requirement; the policy owns the pick. That keeps selection cheap
and unbiased even with a smart model in the loop.

## `meta.llm_tier` — model tier vs. model

`meta.llm_tier` on a `kind='todo'` — `opus`, `sonnet`, `haiku`, or
`local` — picks a **model tier**, not a specific model. The dispatch
worker reads that field off the todo and routes the planner tick to
whichever model currently backs the tier (`local` runs the cluster's
served open model, e.g. a Qwen build). This catalog is where you check
what a tier resolves to today and how it has performed — the field is
the router's input, a card here is the router's output.

An optional `meta.llm_select` dict rides alongside `llm_tier` for finer
control: `{placement: 'local'|'cloud', thinking: bool, effort:
'low'|'medium'|'high', temperature: 0..2}`, every key independently
optional. `placement` is **strict** — pinning `'local'` and having no local
rung for the tier fails the call rather than silently falling back to cloud.

## Tag and link a card

`tag`/`link` work like any other kind — bookmark a card or cross-
reference it from the ref that motivated the note. `get()` echoes
tags back in the rendered card (and the generic links section), but
there's no llm-specific facet UI or search-filter semantics beyond that:

```python
tag(kind="llm", id="claude-opus-4-8", add=["topic:sql-migration"])
link(kind="llm", id="claude-opus-4-8", target="memory:me812", rel="see-also")
```


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

## See also

- [[precis-minter-help]] — meta.llm_tier → minter worker → model
- [[precis-todo-tree-help]] — facet vocabulary on the todo tree
