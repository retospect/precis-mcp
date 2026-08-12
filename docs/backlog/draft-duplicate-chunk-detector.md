---
status: draft
title: Near-duplicate chunk detector for drafts (reuse per-chunk embeddings) — flag recapped / repeated paragraphs
---

# Near-duplicate chunk detector for drafts

## Motivation / why
The nanobuds coherence pass found three content duplications a per-section
review structurally cannot catch: a whole Sensing paragraph in Future
Perspectives recapping the Applications section (both citing the same finding),
the Nicholls in-situ result stated in both Synthesis and Future, and a
graphene-electrochemical sentence misplaced+duplicated across two subsections.
Finding these needed a whole-document agent read. But every draft chunk is
**already embedded** (the reactive embed on write) — a cosine pass over the
chunk vectors would surface duplicate/near-duplicate paragraph pairs for near
zero cost, turning an expensive agent read into a cheap deterministic report.

## In scope
- A `view='duplicates'` (or a Hygiene footer line) that computes pairwise
  similarity over the draft's prose-chunk embeddings and lists pairs above a
  threshold, most-similar first, with both `dc<id>` handles and their section
  paths so "same claim in two sections" is obvious.
- Flag intra-section near-dupes (likely redundancy) distinctly from
  cross-section (likely a recap that belongs in one place).
- Cite-overlap boost: two chunks citing the same `[fi…]`/`[pc…]` AND textually
  similar rank higher (the recap signature).

## Explicitly NOT in scope
- Auto-merging or deletion — report only; the author decides which copy stays
  (deleting authored paragraphs is a human call).
- Cross-draft dedup — scope is one draft.
- Table/figure chunks — prose only.

## Acceptance criteria
- On the nanobuds draft (pre-cut), the B9/B12 recap pair and the Nicholls pair
  both appear in the top results; unrelated paragraphs do not.
- Runs from stored embeddings with no re-embed; whole-draft in well under the
  cost of an agent read.

## Target + blast radius
New read-only draft view in `src/precis/handlers/draft.py`; reads existing
chunk-embedding rows. No schema/migration, no write path.

## Open questions / decisions log
- Threshold + max-pairs default (tune against a few real drafts to keep the
  report signal-dense).
