-- 0037_draft_list_chunks.sql
--
-- Register the container-list draft chunk kinds (ADR 0033 lineage): a
-- list is a `ulist`/`olist` container whose children are `item` chunks
-- (mirrors heading→paragraphs), so list items are first-class chunks —
-- individually addressable, reorderable with the same edit(move=…) verb,
-- and searchable. The LaTeX→draft importer (precis.draftimport) emits
-- these; without the lookup rows the chunks_chunk_kind_fkey rejects them.
--
-- Forward-only (ADR 0005). Idempotent (ON CONFLICT DO NOTHING). Regenerate
-- the baseline snapshot after merge (ADR 0031): scripts/bump.

BEGIN;

INSERT INTO chunk_kinds (slug, is_card, description) VALUES
    ('ulist', FALSE,
     'Draft unordered-list container; its children are `item` chunks '
     '(renders to itemize on export).'),
    ('olist', FALSE,
     'Draft ordered-list container; its children are `item` chunks '
     '(renders to enumerate; meta may carry start/label style).'),
    ('item',  FALSE,
     'Draft list item — a first-class child chunk under a `ulist`/`olist` '
     '(may itself contain nested lists / sub-paragraphs).')
ON CONFLICT (slug) DO NOTHING;

COMMIT;
