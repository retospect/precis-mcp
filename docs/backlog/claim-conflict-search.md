---
status: ready
pillar: memory-graph
title: claim conflict search — remainder: counter-claim mint, backfill ordering, tuning, provenance
model: opus
---

# Claim conflict search — remainder

Reto, 2026-09-02: *"when we make a new nanopub, llm should search for
conflicting info and either incorporate in nanopub or also write that down
as nanopub and link it."* Run at **claim mint** and **retroactively**, keep
track of coverage, rank candidates by the **existing** `paper_rank` score
(never a new one) without rejecting small voices.

## Shipped (code is the record; this file holds only what is open)

`workers/conflict_search.py` — one sweep (negated-paraphrase ANN →
`paper_rank`-budgeted verify with a 20% small-voice floor → `disputes`
edge on a confirmed contradicts → `meta.conflict_search` ledger with
`covered` per-passage verdicts, never re-verifying a covered chunk at the
same version) behind three doors on one `service_config` switch
(`conflict_search`): the standing watermark pass (retro backfill),
the `conflict_sweep` job type queued from `taproot.hub.mint_hub`
(`reason='mint'`), and the approve surface's freshness check
(`nanopub/freshness.py::request_conflict_resweep`, one `refresh` job
per hub per UTC day when the ledger is missing, from an older method
version, or older than `CONFLICT_SEARCH_FRESH_DAYS`). The claim approve
page renders the ledger as a dated statement with the covered passages
(`precis_web/nanopub_render.py::_conflict_coverage_panel`); approve is
never blocked. `coverage_counts()` is the swept/total query.

## Open

1. **Counter-claim + link (item 5).** If a confirmed conflicting passage
   is a genuine opposing claim with its own grounding in a held source,
   mint it as its own hub through the normal directed-mint path (own
   sentence, own grounding passage) and ensure a `disputes` edge joins
   the pair — a deliberate backstop, since directed-mint's `block()` →
   `dedup_judge()` → `place()` cascade may or may not file it: the
   negated-paraphrase candidate is often one `block()`'s same-phrasing
   search never surfaces. Acceptance: after a review sitting confirms a
   genuine opposing claim, the counter-hub exists and a `disputes` edge
   joins the pair (whichever path wrote it); re-sweeping does not
   duplicate it. Both hubs stay mintable — `disputes` is non-blocking.
   Scope/caveat conflicts (the disputes spec's expected-majority
   `scope-mismatch`) are a wording fix on the claim, not a counter-hub.
2. **Backfill order.** The standing pass walks `ORDER BY ref_id`; the
   spec wants dense topic neighbourhoods first (MOF conduction, DNA
   bricks, molecular switches) — conflicts hide where coverage is
   thickest. Needs a cohort ordering knob, not a separate tool.
3. **Tuning on the first dense neighbourhood** — verify budget per hub
   (6), floor fraction (0.2), freshness window (90 days), topk (8). All
   are starting points; the ledger's `skipped_covered` counts tell how
   much a refresh actually re-spends.
4. **Provenance question.** Does the searched-at negative statement
   ("no known conflict as of <date>, method v<n>") ride into the
   *published* provenance graph, or stay a local honesty record? Close
   to the negative-results pathway in `claim-publication-nanopub-ots.md`
   — possibly the same artifact shape.

## Explicitly NOT in scope

- **Blocking.** No conflict-search verdict ever hard-blocks a mint.
- **Adjudication** — Part 2 of the disputes spec.
- **Changing `paper_rank`** — consumed as-is.
- **External search** (semanticscholar / websearch / perplexity): a
  counter-hub must ground in a held passage; corpus-only until an
  external hunt is its own follow-on.

## Decisions log

- 2026-09-02 (Reto): trigger is claim-mint + retro backfill via one
  watermarked pass; coverage tracked; trust ranks but never rejects;
  trust ordering is the existing `paper_rank` score.
- 2026-09-02: negated-paraphrase retrieval is slice 1; no imports of
  `hub_refine` privates, share only `workers/_chase_llm.py`.
- 2026-09-03: Part 1 of the disputes spec deployed, so the sweep files
  `disputes` directly instead of parking verdicts.
- 2026-10-09: the one-hub door is a job type (`conflict_sweep`), not a
  second pass — the standing pass stays the backfill, and every door
  shares the `conflict_search` service switch so a dark service mints
  nothing. The ledger grew `covered` so an approve-time refresh never
  re-verifies; a version bump still discards it (method changed).
