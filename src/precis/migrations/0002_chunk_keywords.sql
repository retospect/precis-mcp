-- 0002_chunk_keywords.sql
--
-- Forward migration: add the per-chunk KeyBERT storage on top of
-- whatever schema state prod is currently in. Pairs with F20 (the
-- chunk_keywords worker + dynamic TOC).
--
-- Context. The "second greenfield" (ADR 0019, 2026-06-05) squashed
-- the historical chain 0001..0017 into a fresh ``0001_initial.sql``.
-- The intent was for the squashed file to mirror the cluster
-- master's actual schema; in practice prod sat at a much earlier
-- state, so the squash and prod diverged on multiple tables
-- (``ref_segments``, ``ref_events``, ``patent_watches``,
-- ``tag_embeddings`` …). This migration intentionally addresses
-- ONLY the bit needed to make the chunk_keywords worker and the
-- dynamic TOC viable: the two new columns on ``chunks`` plus the
-- GIN index. The other table-level deltas are out of scope and
-- can be reconciled in follow-up forward migrations when their
-- features actually start mattering on prod.
--
-- This migration is additive. It does NOT drop ``ref_segments``
-- or ``ref_segment_sentences`` even though F20-era code no longer
-- reads them — leaving them in place means the old MCP server
-- image keeps working until it's swapped out, and a subsequent
-- forward migration can drop them after a clean traffic cutover.

ALTER TABLE chunks
    ADD COLUMN IF NOT EXISTS keywords TEXT[],
    ADD COLUMN IF NOT EXISTS keywords_meta JSONB;

CREATE INDEX IF NOT EXISTS chunks_keywords_gin
    ON chunks USING GIN (keywords);

-- Register the new derived-artifact slug so ``precis worker
-- --status`` enumerates it. ``ON CONFLICT DO NOTHING`` because
-- the squashed 0001 (when it does land on prod) seeds the same
-- row — this lets the same migration apply cleanly on both the
-- pre-squash prod schema and a freshly-bootstrapped DB.
INSERT INTO artifact_kinds (slug, target, storage, output_table, description) VALUES
    ('keybert:chunks', 'chunk', 'typed', 'chunks',
     'KeyBERT phrases per chunk; abbrev-aware via refs.meta[abbrevs]')
ON CONFLICT (slug) DO NOTHING;

-- End of 0002_chunk_keywords.sql
