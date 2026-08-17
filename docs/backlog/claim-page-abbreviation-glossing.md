---
status: draft
title: "claim page: gloss paper abbreviations in quote blocks"
model: sonnet
---

# Claim page doesn't gloss paper abbreviations (Reto, 2026-08-17, nanobud campaign)

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

Note (nanopub-light-up, shipped): `/claim/fi<id>` is now the **one**
claim page — reader evidence plus, merged in, the review-and-sign
section that used to live at `/nanopub/fi<id>` (now a redirect here).
This item is purely about the quote blocks' abbreviation glossing; it
doesn't touch that merge.
