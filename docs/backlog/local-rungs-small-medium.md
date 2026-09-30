---
status: draft
pillar: local-compute
title: Write local rungs under the SMALL/MEDIUM tiers, once Slice 0 picks a model
prio: normal
blocked-by: vllm-per-node-serving
---

# Local rungs under SMALL/MEDIUM

Mining pass, 2026-09-30. The compiled tier ladder has **zero local rungs**
by default — `src/precis/utils/llm/live_config.py`: "a tier left with no
[local] rung ... pauses (skip-not-fail) until cloud is re-enabled" (comment
at lines 57-59). `llm-tier-ladder-cloud-cutover.md` applied
`SMALL = z-ai/glm-4.7-flash`, cloud-only, on 2026-08-15. The DGX-pair
`llama-server` has been stopped and disabled since 2026-08-23
(`vllm-per-node-serving.md:17`). The only lane demonstrably local today is
chunk summarise on melchior (`llm-summarize-throughput.md`) — everything
else in the compiled ladder either runs cloud or pauses.

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
- Pin `placement='local'` for the `graph-maintenance-queue.md` lanes
  specifically — that item's whole premise is local-capacity consumption,
  so its calls should never silently fall back to cloud
  (`local-cloud-share-report.md`'s routed-vs-landed gap is exactly the
  failure mode to avoid here).
- `content-sensitivity-placement.md` as the precondition for routing
  anything proprietary through these new local rungs — sensitivity gating
  must exist before proprietary content is eligible for a local-only path
  that could otherwise be mistaken for "safe because local."

## Explicitly NOT in scope

- Picking the model itself, or measuring concurrency-scale — that's
  `vllm-per-node-serving.md` Slice 0's job; this item starts after.
- BIG/FRONTIER tiers — those stay cloud (sonnet/opus) per the applied
  ladder; this item is SMALL/MEDIUM only.
- Reviving the DGX-pair `llama-server` — that's a separate decision
  (`spark-distributed-llm-serving` runbook territory), not assumed by this
  item.

## Acceptance criteria

- SMALL and MEDIUM each have at least one live local rung serving real
  traffic on summarise, classify, or pre-search/condense.
- `graph-maintenance-queue.md`'s lanes show `placement='local'` pinned, not
  inferred, in `llm_call_log`.
- A proprietary-content job routed through a new local rung passes
  `content-sensitivity-placement.md`'s gate before landing there — no
  local rung is reachable by sensitivity-gated content until that
  precondition ships.

## Target + blast radius

`live_config.py` (tier ladder rungs), `src/precis/utils/llm/router.py`
(placement pinning for the maintenance-queue lanes); no change to
BIG/FRONTIER.

## Open questions / decisions log

- None yet — blocked entirely on `local-serving-eval.md`/`vllm-per-node-serving.md`
  Slice 0's model choice.

Closest existing items: `llm-tier-ladder-cloud-cutover.md` (the ladder this
item adds rungs to), `router-cost-coverage.md` (the pre-filter-tier ask
this item is one instance of), `vllm-per-node-serving.md` (Slice 0, the
hard blocker), `content-sensitivity-placement.md` (precondition for
proprietary content on these rungs).
