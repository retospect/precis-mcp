---
status: draft
pillar: local-compute
title: Write local rungs under the SMALL/MEDIUM tiers, once Slice 0 picks a model
prio: normal
---

# Local rungs under SMALL/MEDIUM

## Decisions (Reto 2026-10-21)

- Apply the staged `llm.chain.small` write now: rung 0 local
  `openai/gpt-oss-120b`, cloud glm overflow. Model pick is settled (gpt-oss
  120B, Reto 2026-10-11), so the `vllm-per-node-serving` blocker is gone.
- `llm.chain.medium` stays on Claude. Trial MEDIUM per-op via
  `llm.op.chase:verify` before touching the tier chain (40-task compare:
  gpt-oss 0.812 vs haiku 0.875, gap entirely in the `partial` stratum).
- After the small write lands, read `/placement` after 24 h into this file.
  What stays open here: the maintenance-lane `placement='local'` pin (needs
  `graph-gardener-value.md` slice 5 (idle filler)) and the sensitivity precondition (needs
  `content-sensitivity-placement.md`).

Mining pass, 2026-09-30. The compiled tier ladder has **zero local rungs**
by default — `src/precis/utils/llm/live_config.py`: "a tier left with no
[local] rung ... pauses (skip-not-fail) until cloud is re-enabled" (comment
at lines 57-59). The 2026-08-15 cutover applied
`SMALL = z-ai/glm-4.7-flash`, cloud-only. The DGX-pair
`llama-server` has been stopped and disabled since 2026-08-23
(`vllm-per-node-serving.md:17`). No lane is local today (last local LLM call 2026-09-10, the melchior
summariser model; the shipped local summarise chain (`workers/job_types/derived_drain.py`) is the dedicated slice that brings it
back) — everything in the compiled ladder either runs cloud or pauses.

## Motivation / why

The local-compute pillar has no SMALL/MEDIUM local capacity at all, despite
the local-serving eval work (`local-serving-eval.md`,
`vllm-per-node-serving.md`) being actively designed. Once that work picks a
model, nothing currently plans to wire it into the tier ladder for the
obvious bulk lanes — summarise, classify, pre-search/condense — leaving the
eval's output as a standalone capability with no consumer.

## In scope

- Once `vllm-per-node-serving.md`'s Slice 0 (concurrency-scale measurement,
  the model-choice gate — that item owns Slice 0, not `local-serving-eval.md`)
  picks a model that fits one spark, write local rungs under SMALL and
  MEDIUM for: chunk summarise, classify, and pre-search/condense (the
  pre-filter tier `router-cost-coverage.md` already names as wanted, ahead
  of paid escalation).
- Pin `placement='local'` for the `graph-gardener-value.md` slice 5 (idle filler) lanes
  specifically — that item's whole premise is local-capacity consumption,
  so its calls should never silently fall back to cloud
  (the `fell_to_cloud` column of `get(kind='llm', id='/placement')` is
  exactly the failure mode to avoid here).
- `content-sensitivity-placement.md` as the precondition for routing
  anything proprietary through these new local rungs — sensitivity gating
  must exist before proprietary content is eligible for a local-only path
  that could otherwise be mistaken for "safe because local."

- Embedder N-client load test once these LLM rungs share the box with the
  embedder (from the closed embedder-capacity-ownership; its provisional
  floor was 12.7–13.6 texts/s mixed with query p50 ~2 s / p95 ~5 s).

## Explicitly NOT in scope

- Picking the model itself, or measuring concurrency-scale — that's
  `vllm-per-node-serving.md` Slice 0's job; this item starts after.
- BIG/FRONTIER tiers — those stay cloud (sonnet/opus) per the applied
  ladder; this item is SMALL/MEDIUM only.
- Reviving the DGX-pair `llama-server` — that's a separate decision
  (`spark-distributed-llm-serving` runbook territory), not assumed by this
  item.

## Acceptance criteria

- SMALL has a live local rung serving real traffic (`llm.chain.small`
  rung 0). MEDIUM gets one only after a per-op trial passes.
- `graph-gardener-value.md` slice 5 (idle filler)'s lanes show `placement='local'` pinned, not
  inferred, in `llm_call_log`.
- A proprietary-content job routed through a new local rung passes
  `content-sensitivity-placement.md`'s gate before landing there — no
  local rung is reachable by sensitivity-gated content until that
  precondition ships.

## Target + blast radius

`live_config.py` (tier ladder rungs), `src/precis/utils/llm/router.py`
(placement pinning for the maintenance-queue lanes); no change to
BIG/FRONTIER.

Closest existing items: the shipped local summarise chain (`workers/job_types/derived_drain.py`) (the dedicated summariser
slice; this item is the general SMALL/MEDIUM rung), `router-cost-coverage.md` (the pre-filter-tier ask
this item is one instance of), `vllm-per-node-serving.md` (Slice 0, the
hard blocker), `content-sensitivity-placement.md` (precondition for
proprietary content on these rungs).
