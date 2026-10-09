---
status: ready
pillar: platform
title: LLM failure classification — remaining: MEDIUM ladder proof, log column
prio: low
model: sonnet
---

# LLM failure classification — remaining: MEDIUM ladder proof, log column

Design session 2026-09-17 (Reto + agent), rippling-zooming-taco worktree.
Trigger: a web follow-up on fi191121 (conv co345510) stored a Claude
session-limit 429 verbatim as the answer turn (gr345578).

Decision (Reto, 2026-09-17): quota exhaustion is a roughly monthly event
on the current numbers, so the cheap router-side pieces ship; the
free-tier zoo, new backends, and a FRONTIER fallback do NOT (see NOT in
scope).

## Shipped

Router-owned classification (`utils/llm/failure.py`: every error result
carries `reason_class` ∈ quota|budget|rate|transport|content and
`retry_at`; the executor's parked-job backoff reads the same table; a
`content` error never falls through a chain); the reactive quota stamp
(`budget/quota.stamp_exhausted`, one early probe per exhausted window via
`probe_due`, a window past its `resets_at` no longer pauses); the
SMALL-lane in-process retry (`router._run_with_retry`,
`PRECIS_LLM_RETRY_ATTEMPTS`); the `deferred_llm_call` coordinator job and
the web follow-up's enqueue path on a quota/budget failure (closes
gr345578); `ExtractionUnavailable` carrying class + horizon so the taproot
backfill names why/when instead of a bare "unavailable". `git log` is the
record.

## Remaining scope

1. **Ladder proof on MEDIUM only** (config, no code). Set a
   `llm.chain.medium` override on prod with a paid OpenRouter
   no-training-provider rung below claude, so `FailoverProvider` runs on
   prod for the first time on a low-stakes tier. The fall-through gate
   (`content` never falls through) is already in the router.
   Acceptance: one `llm-failover: fell back to rung 1` warning observed in
   worker logs during the next quota window, with the result placement
   stamped `cloud`.
2. **`llm_call_log.reason_class` column** (optional). The class and
   horizon are stored today under `llm_call_log.features`
   (`features->>'reason_class'`, `features->>'retry_at'`), which already
   answers the 30-day by-class query without a regex. Promote to a real
   nullable column (forward migration) only if the jsonb path proves too
   slow or a dashboard wants an index.

## Explicitly NOT in scope

- No free-tier scouting, no new backends (Google AI Studio, Groq,
  Mistral free, direct EU providers). Deferred until a second month shows
  quota exhaustion recurring; EU candidates already listed in
  `eu-llm-providers.md`.
- No FRONTIER or BIG fallback to OSS. Reviewer-grade agentic work parks
  with `retry_at`; the OSS tools loop and the golden-task harness
  (`llm-catalog-wire-policy.md`) are not proven on our jobs.
- No privacy-class filter on endpoints (data policy on catalog cards,
  proprietary → local-only). That is the precondition for any free
  tier and belongs with the catalog wiring item, not here.
- No change to the dollar breaker or token-rate caps
  (`token-based-budget-caps.md`).
- Not diagnosing the 2026-09-07 SMALL-lane 429 storm's cause (the retry
  only makes it survivable); file separately if it recurs.

## Open questions / decisions log

- DECIDED: OpenRouter stays the single aggregator rung; at most one
  direct EU provider later, for the proprietary class and outage
  independence. Not in this item.
- DECIDED: a silent tier downgrade on reviewer-grade output is the one
  way this can do harm; FRONTIER parks, never falls through.
- DECIDED (coder, 2026-10-09): `deferred_llm_call` is its own coordinator
  job type minted through the canonical todo shape, not the todo lane's
  park machinery — the `at_time` Yield already is a park, and the todo
  auto-resolves on success.
