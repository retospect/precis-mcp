---
status: draft
title: draft_refresh residuals — external lit-search leg + refinements
---

# draft_refresh residuals

The core shipped (scan cadence `draft_refresh_scan` in
`src/precis/workers/scheduler.py`, job type
`src/precis/workers/job_types/draft_refresh.py`, section-insertion skill
`precis-draft-section-insertion`): staleness-driven per-section refresh of
`meta.draft_refresh`-opted drafts — prose-only rewrite (tables/figures/terms
preserved), growth-ratchet-gated apply, corpus-side backfill + research-arm
(serves-graph frontier) evidence, logbook + attempt-tree process memory.
Design detail lives in the `precis.workers` docstring and the job module.
Remaining open work:

- **External lit-search leg** (deferred from v1): per-section S2 query;
  genuinely new papers queued for ingest, budget ≤3 per run. Corpus-side
  backfill candidates cover the gap until then.
- **Event-triggered staleness for own-results sections**: pure age treats a
  frontier update like any other month — a frontier/finding change could mark
  the own-results section stale immediately. v1 is age-only; revisit once the
  qu202467 survey book has accumulated refreshes.
- **Nested-substructure rewrite**: v1's rewrite output is flat paragraphs; a
  section's sub-headings are preserved but their prose is not restructured
  across the boundary. Revisit if stalest-pick keeps landing on deeply
  structured sections.
- **Logbook roll-up/compaction policy** (deferred until volume hurts —
  append-only is acceptable for now).
- **Whether a quest must be STATUS:active for its book to refresh** — shipped
  answer: meta flag alone suffices (decoupled from quest tick); revisit only
  if that proves wrong in practice.
