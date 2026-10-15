---
status: draft
pillar: memory-graph
title: claim conflict search — remainder: tuning, provenance
model: opus
---

# Claim conflict search — remainder

Reto, 2026-09-02: *"when we make a new nanopub, llm should search for
conflicting info and either incorporate in nanopub or also write that down
as nanopub and link it."* The machinery shipped — `workers/conflict_search.py`
(negated-paraphrase ANN → `paper_rank`-budgeted verify → `disputes` edge →
`meta.conflict_search` ledger), the `conflict_sweep` job, the mint and
freshness doors, `mint_counter_claim`, the approve-page coverage panel
(53b0a595e and `git log` are the record). What is left:

## Open

1. **Tuning on prod data.** Verify budget per hub (6), floor fraction
   (0.2), freshness window (90 days), topk (8). All are starting points;
   the ledger's `skipped_covered` counts tell how much a refresh actually
   re-spends.
2. **Provenance question — a design call.** Does the searched-at negative
   statement ("no known conflict as of <date>, method v<n>") ride into the
   *published* provenance graph, or stay a local honesty record? Close to
   the negative-results pathway in `claim-publication-nanopub-ots.md` —
   possibly the same artifact shape.

## Still ruled (do not reopen without a new argument)

- No conflict-search verdict ever hard-blocks a mint.
- Trust ranks but never rejects; `paper_rank` is consumed as-is.
- Adjudication is Part 2 of the disputes spec, not this item.
- A counter-hub must ground in a held passage; corpus-only until an
  external hunt is its own follow-on.
