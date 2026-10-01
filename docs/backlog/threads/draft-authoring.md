# draft authoring — drafts, export and the tex layer

**Status:** ends when a draft can be written, linted and exported to every
format without a human working around the tool. Today the cluster of drafts
items is untriaged and the ordering rule is not age or prose: Do-next is
ordered by what the month's preprint hits (quest qu459585; October = td459586).
**Last reviewed:** 2026-10-01
**Worktree:** `draft-authoring`
**Active:** yes — Reto 2026-10-01: "draft authoring must work".

Created 2026-10-01 from the memory-graph pillar review.

## Do next

1. **Triage the drafts cluster against the October paper** (td459586) — mark
   each item below hit / not hit by the paper's draft, export and cite path;
   hits rank in the order the paper reaches them, the rest drop to Horizon or
   Parked. Strongest candidate before the triage runs:
   `backlog/draft-write-latency-whole-draft-rescan.md` (p95 182 s — every draft
   write rescans the whole draft; it taxes every author, human or agent).

## Horizon

All `backlog/<slug>.md`; unranked inside each group until Do-next 1 runs.

- **Authoring correctness** — `draft-hygiene-lints` · `authoring-tex-lint` ·
  `degree-spacing-canon-vs-draft-lint-collision` ·
  `draft-text-route-dc-handle-wrap` · `draft-doi-completeness-check` ·
  `draft-cite-groundwork-prepass` · `tex-layer2-fixer-fate`.
- **Export** — `draft-export-panel-per-format-tabs` · `endnote-export-validation`
  · `export-glyph-allowlist` · `draft-poster-genre-and-themes` ·
  `draft-section-styles`.
- **Content model** — `draft-table-structured-enrichment` ·
  `draft-footnotes-annotations` · `diagram-editing-and-chunk-binding` ·
  `draft-refresh` · `draft-inline-editor` · `smartdraft-review-parity`.

## Parked

- **convert-remaining-markdown-list-drafts** — unparks when a markdown-list draft
  is next needed as a real draft.
- **dr42995-boxel-draft** · **dft-hbond-draft-27-paywalled-dois** — one-draft
  content chores, not tooling; unpark with Reto's word on that draft.
- **Source-backfill follow-ups** — a HyDE query lens, a Tier-1 relevance cull for
  candidate lists, and an `integrate` planner coroutine that walks accepted
  candidates into the draft; owner `src/precis/backfill/candidates.py`, each
  independently shippable, needs design per piece. Was
  `source-backfill-followups`.

## No action needed

- (none)

## Seam

- `knowledge-mesh.md` Horizon holds draft-linearization (the draft as a graph
  view); this thread holds authoring and export of the draft itself. Neither
  reorders the other.
- `graph-memory-consumers.md` parks gr454753/gr454749 (export gate) behind its
  `draft-authoring-graph-affordances`; if the October triage hits them they move
  here.
- `claims-and-evidence.md` owns the claim side of cite-time attach.
