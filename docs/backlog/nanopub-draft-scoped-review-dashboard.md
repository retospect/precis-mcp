---
status: draft
title: "nanopub review UX: draft-scoped workbench filter + claim-page abbreviation glossing"
model: sonnet
---

# Nanopub review UX gaps (Reto, 2026-08-17, nanobud campaign)

Two author-workflow gaps surfaced while sign-reviewing ~131 hubs for
dr173020.

## 1. Draft-scoped workbench: "did I check and sign everything my paper cites?"

`/nanopub` is the working surface (claim forest + per-state tally +
disputed strip; `/nanopub/tree` redirects there) but it is **global** —
`precis.nanopub.overview.hub_tree/hub_rows` take no scope. An author
signing off a draft needs the complement of the campaign's hand-run SQL
("127 findings cited by dr173020 → 1 signed + 51 reviewed + 75
without"): the forest pruned to hubs the draft actually cites.

Wanted: `/nanopub?draft=dr173020` (and a "review claims" link from the
draft/smartdraft page) that

- filters `hub_rows` to hubs cited by the draft's chunks (the citer
  edge already exists — `claim_citers` walks it inbound; this is the
  same walk outbound from the draft),
- keeps the per-state tally strip for just that set (the "N reviewed /
  M unminted / K blocked" readout IS the answer to "am I done?"),
- highlights the not-done buckets: unminted, candidate, disputed, and
  withheld-evidence (publishable-blocked) hubs first,
- links each row to `/nanopub/fi<id>` for the sign action.

## 2. Claim page doesn't gloss paper abbreviations

`/claim/fi<id>` renders evidence quotes straight from paper chunks —
full of source abbreviations (CNB, CSI/CSA, GNB, FPPG, …) that the page
never defines. The draft reader already solves this for draft text:
`drafts.defined_terms` / `_abbrevs_cached` +
`linkify._highlight_abbrevs` hover-gloss. The claim page uses none of
it, and the needed definitions are the **source paper's**, not the
draft's.

Wanted: reuse the highlight machinery on the claim page's quote blocks,
sourcing the map from (a) the term registry and (b) the quoted paper's
own defined terms (same extractor the draft path uses, run per source
ref, memoised). Degrade silently when no map exists.

Note: `/claim` is NOT retired — it's the reader-facing evidence page
(and `/refs/finding/<hub>` redirects to it); `/nanopub/fi<id>` is the
review-and-sign surface. The claim page's nanopub state chip (slice 4)
already cross-links the two.
