# Broaden llm_call_log ref linkage for attribution

Currently ~0.04% of LLM calls carry `ref_id` (plan_tick's todo/quest parents).
Ambient worker passes (summarize, classify, glossary) record no parent
attribution. Decide which call sites should thread `ref_id`/job attribution
through `LlmRequest`, so per-question vs amortized-infrastructure cost can
be split by query source instead of by convention.

## Motivation

- Per-call token accounting now exists (shipped 2026-08-11); reporting needs
  `ref_id` to attribute cost to the right quest/todo parent.
- Current practice: high-level dispatch (plan_tick) tags rows; pass-level
  batches (dream, review, classify, summarize) don't.
- Aggregate-by-`features->>'source'` covers most reporting today, but finer
  grain attribution requires knowing the originating question.
- Not urgent: existing rollups work for high-level billing; this unlocks
  finer per-quest spend views.

## Open questions

1. Which passes should be threaded?
   - plan_tick children (obvious — already happens).
   - Ambient classify/summarize (per-doc, amortized cost?).
   - chase/verify judgments (child job type — inherit parent?).
   - figure/mermaid render (attributed to the turn, not a quest).
2. Should ambient passes share cost across their batch, or charge each call?
3. What is the ground truth for "this call serves this quest"?

## Out of scope

- Retroactive attribution for past NULL rows.
- Changes to the dispatch signature.
- Per-task hard budgets (a separate backlog item).
