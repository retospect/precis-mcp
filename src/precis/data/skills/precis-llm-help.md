---
id: precis-llm-help
title: precis — the model catalog (choose the right LLM)
summary: model choice as a queryable resource — read a model's facts + capability, express a requirement, let the policy pick
applies-to: get/search (kind='llm')
status: active
---

# precis-llm-help — model choice as a queryable, learnable resource

The `llm` kind is a **catalog** of the models precis can run: one card per
model, carrying its context window, price, capability, and a ledger of how it has
actually performed on precis's own work. It exists so model choice is an
*informed, window-safe, budget-aware* decision instead of a hardcoded constant.

The canonical address is the handle `lm<id>` (e.g. `lm7`), but the **model slug
is the human key** — `get(kind='llm', id='claude-opus-4-8')` resolves it.

Cards are **machine-maintained** (a reconcile pass keeps facts true against the
live model feed); you *read* the catalog, you don't hand-author it.

## What model should I use for X? / Which LLM is good at Y?
## Compare models / see a model's window, price, capability

```python
search(kind='llm', q='careful SQL and multi-file refactors')
get(kind='llm', id='claude-opus-4-8')
```

`search` matches your phrasing against each card's capability prose. `get` shows
the facts: `tier_floor`, `offerings` (operating points — effort, transport,
window, price), the coarse **1–5 capability axes** (`code`,
`long-context-recall`, `tool-structured`, `reasoning-convergence`,
`summarize-extract`), and provenance.

## Has this model actually been good / cheap / reliable here?
## See a model's realized cost + error rate + reviews

```python
get(kind='llm', id='claude-opus-4-8', view='tote')
get(kind='llm', id='claude-opus-4-8', view='reviews')
```

`view='tote'` rolls up `llm_call_log` — realized calls, cost, error rate, p50
duration — the model's track record on precis's own workload. `view='reviews'` is
the append-only, dated ledger of typed observations, each tagged by **evidence
band**: `observed-telemetry` (measured on your traffic) > `measured-eval` (your
own golden sets) > `published-benchmark` (vendor numbers, low-trust). These
never blend — a vendor MMLU score never outweighs a measured result.

## Leave a note about how a model did
## Record that a model was great / weak at something

```python
put(kind='llm', id='claude-opus-4-8', text='excellent at SQL-migration reasoning', entry='agent-review', by='agent')
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
