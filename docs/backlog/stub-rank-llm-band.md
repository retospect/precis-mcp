---
status: draft
title: Stub ranking Tier-2: small-LLM band classification + mint-time metadata pass-through
---

# Stub ranking Tier-2: small-LLM band classification + mint-time metadata pass-through

## Motivation / why

The stub_rank worker pass ranks pending paper stubs into `refs.prio` (canon 1=hot..10=cold) via S2 metadata enrichment + bge-m3 embedding similarity against interest anchors (active quests, recently-opened papers, time-decayed). For stubs in the uncertain middle band (~30th–70th percentile similarity), structural signals alone are insufficient to confidently assign prio; similarly, inbound citations and quest lit-search already fetch full S2 metadata at stub-mint time but discard it, requiring a later round-trip to enrich. Two complementary improvements were deferred from the initial ship:

1. **Tier-2 LLM band**: a SMALL-tier call (llm.chain.small → glm-4.7-flash route) labels uncertain stubs with {core|adjacent|explore|off} plus a one-line reason stored in `refs.meta`, adjusting prio within its band; decision log tracks features, label, and later outcome (fetched/opened/cited/dismissed) to eventually distill the band into a logistic head.

2. **Mint-time metadata pass-through**: pass S2 response (abstract, s2FieldsOfStudy, citation count) into `refs.meta` at stub-mint so those stubs skip the enrich round-trip.

## In scope

- Classify stubs landing in the uncertain similarity band via a SMALL-tier LLM call with title, abstract, and a compact interest profile.
- Store classification label, reason, and decision metadata in `refs.meta` for visibility in the stubs view.
- Build a queryable decision log (label, features, downstream outcome: fetched/opened/cited/dismissed) to enable future band-distillation training.
- Plumb S2 metadata (abstract, fields, citation count) through mint-time stub creation so stubs don't require a later enrich round-trip.
- Implement cost guard: bound LLM calls per pass run (e.g., by concurrent process limit or daily quota).

## Explicitly NOT in scope

- Training or deploying a bespoke classifier model; the decision log is *data collection* for future training, not training itself.
- Retroactively labeling stubs already minted without S2 metadata; mint-time pass-through applies only to new stubs.
- Changing the core similarity-percentile thresholds or prio assignment logic; this is a *refinement* within the uncertain band.

## Acceptance criteria

- Stubs in the uncertain-similarity band display their LLM label (core/adjacent/explore/off) and reason in the stubs view.
- Decision log is queryable (`refs.meta` JSON structure is documented; typical query surface: `WHERE meta->>'llm_label' IS NOT NULL`).
- S2-enriched stubs at mint show `s2_enriched_at` timestamp in `refs.meta` and skip the later enrich pass.
- LLM call cost is visible and bounded (pass runs do not spike API spend; cost logs include decision count and model token usage).
- End-to-end test: mint a stub with S2 metadata, verify it skips enrich; mint a stub in uncertain band, verify label is assigned and logged.

## Target + blast radius

- Worker: `src/precis/workers/stub_rank.py` (uncertain-band classification), `src/precis/ingest/pipeline.py` (mint-time metadata handler).
- Routes: stubs view (metadata display), worker pass cost instrumentation.
- Schema: `refs.meta` JSON schema update (llm_label, llm_reason, s2_enriched_at, decision_log fields).
- Cluster config: LLM call budget / concurrency limits (worker service_config, executor affinity for glm-4.7-flash).

## Open questions / decisions log

- *Decision*: which percentile bounds define "uncertain band"? Current sketch ~30th–70th; confirm against real embedding distribution.
- *Decision*: how many LLM calls per pass run? Scope: run-wide quota vs. per-process limit; tradeoff between responsiveness and cost stability.
- *Decision*: decision log retention / archival policy? Persist all indefinitely in `refs.meta` JSON, or rotate to an audit table?
- *Question*: does the interest-profile compaction (title+abstract+quests) fit a SMALL-tier token budget? Spike/measure; may need synopsis extraction.
