-- 0011_chunk_keywords.sql
--
-- F20: Replace static ref_segments segmentation with dynamic clustering
-- on per-chunk KeyBERT keywords.
--
-- 1. DROP the static discovery layer (ref_segments + ref_segment_sentences).
--    These were computed at ingest by the segment_toc worker and cached;
--    the new model computes clusters at query time from per-chunk keyword
--    sets, so the precomputed segments become dead weight.
--
-- 2. ADD chunks.keywords (TEXT[]) — canonical short forms for GIN-indexed
--    lexical filtering and Jaccard-distance clustering at query time.
--
-- 3. ADD chunks.keywords_meta (JSONB) — rich form with version, embedder,
--    and per-keyword {short, long, score} entries. ``version`` lets the
--    worker lazy-update when the algorithm changes (claim query filters on
--    stale version). Mirrors the SEGMENTATION_VERSION pattern.
--
-- 4. GIN index on the TEXT[] column for fast `keywords @> ARRAY['mof']`
--    lookups — enables future lexical-keyword search modifier (F21).
--
-- Refs.meta gains an 'abbrevs' key by convention (no schema change —
-- ``refs.meta`` is JSONB). Populated lazily by the chunk_keywords
-- worker at first pass over a paper.

DROP TABLE IF EXISTS ref_segment_sentences;
DROP TABLE IF EXISTS ref_segments;

ALTER TABLE chunks ADD COLUMN keywords TEXT[];
ALTER TABLE chunks ADD COLUMN keywords_meta JSONB;

CREATE INDEX chunks_keywords_gin ON chunks USING GIN (keywords);

-- Registry entry for the new derived artifact, mirroring the
-- chunk_embeddings / chunk_summaries convention in 0004.
INSERT INTO artifact_kinds (slug, target, storage, output_table, description) VALUES
    ('keybert:chunks', 'chunk', 'typed', 'chunks',
     'KeyBERT phrases per chunk; abbrev-aware via refs.meta[abbrevs]')
ON CONFLICT (slug) DO NOTHING;

-- End of 0011_chunk_keywords.sql
