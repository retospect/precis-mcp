-- 0089_paper_of_relation.sql
--
-- The `paper-of` / `has-paper` link relation. A quest (or any other
-- dossier-owning process, ADR 0064 §B) may have a reader-facing PAPER
-- draft — a projection of its dossier's living synthesis, but a SEPARATE
-- draft from the dossier itself: the dossier is internal thinking
-- substrate (rewritten every research cycle), the paper is what a human
-- reads (docs/decisions/0064-dossier-thinking-substrate-and-paper-
-- projection.md). Modeled 1:1 on `dossier-of` (migration 0067): asymmetric,
-- auto-mirrored, one paper per owner by convention (no dup-guard here —
-- resolution is read-only via `quest.dossier.paper_ref_id`, which just
-- takes the first match).
--
-- This migration adds ONLY the relation vocabulary row — nothing yet
-- creates a paper draft or links one in with this relation; that pipeline
-- is unbuilt (docs/design/paper-writing-pipeline.md). Kept in sync with
-- the `Relation` Literal + `_INVERSE_RELATIONS` in store/types.py.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot at
-- release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('paper-of',  FALSE, 'has-paper',
     'Source draft is the reader-facing paper projection of the target '
     'quest/process''s dossier — a separate draft from the dossier itself.'),
    ('has-paper', FALSE, 'paper-of',
     'Source quest/process has the target draft as its reader-facing paper.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0089_paper_of_relation.sql
