-- 0261_chunks_keywords_pending_idx.sql
--
-- chunk_keywords drain fix (gr520236): the fresh-backlog claim is
-- `WHERE keywords IS NULL ... ORDER BY chunk_id DESC` (newest-first, so
-- fresh ingests get labels first). Without an index covering it, every
-- claim re-walks the index entries between the id top and the live
-- backlog — the same "rescan" cost the old ascending claim paid at id
-- 1, mirrored.
--
-- The predicate carries the claim's STATIC eligibility too — the
-- skip-kinds, the 150-char floor, meta.no_index — because ~1.4M of the
-- NULL-keywords rows can never be keyworded (skip-kinds and short text
-- stay that way forever) and would otherwise sit in the index forever,
-- walked by every claim. The literals are owned in Python
-- (workers/chunk_keywords.py::_eligibility_sql) and mirrored here; a
-- test pins the two to the same text, and the claim inlines the same
-- literals so the planner can prove the query implies the predicate.
-- The conv/draft ref-kind clause stays claim-side only (a partial index
-- cannot name another table's column).
--
-- Prod rollout (0226 precedent): a plain CREATE INDEX on prod's chunks
-- (~4.7M rows) takes a SHARE lock for a full heap scan and stalls
-- ingest, so ops pre-builds it CONCURRENTLY, outside a transaction,
-- BEFORE the deploy:
--
--   CREATE INDEX CONCURRENTLY chunks_keywords_pending_idx
--       ON chunks (chunk_id DESC)
--       WHERE keywords IS NULL
--         AND chunk_kind NOT IN ('card_authors', 'card_combined',
--             'card_description', 'card_title', 'equation', 'figure',
--             'references', 'table')
--         AND length(text) >= 150
--         AND (meta->>'no_index') IS DISTINCT FROM 'true';
--
-- The guard below refuses an INVALID index (a failed CONCURRENTLY build
-- that IF NOT EXISTS would skip silently) and refuses to build inline
-- on a chunks table over 1M reltuples. Small dev/test DBs still get it
-- in-transaction — cheap at that size.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- at release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

SET LOCAL lock_timeout = '5s';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
                WHERE c.relname = 'chunks_keywords_pending_idx' AND NOT i.indisvalid) THEN
        RAISE EXCEPTION '0261: index chunks_keywords_pending_idx exists but is INVALID (a failed CONCURRENTLY build)'
            USING HINT = 'DROP INDEX CONCURRENTLY chunks_keywords_pending_idx; rebuild it CONCURRENTLY; re-apply.';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'chunks_keywords_pending_idx')
       AND (SELECT reltuples FROM pg_class WHERE relname = 'chunks') > 1000000 THEN
        RAISE EXCEPTION '0261: chunks_keywords_pending_idx missing on a large chunks table'
            USING HINT = 'CREATE INDEX CONCURRENTLY chunks_keywords_pending_idx ON chunks (chunk_id DESC) WHERE keywords IS NULL AND chunk_kind NOT IN (''card_authors'', ''card_combined'', ''card_description'', ''card_title'', ''equation'', ''figure'', ''references'', ''table'') AND length(text) >= 150 AND (meta->>''no_index'') IS DISTINCT FROM ''true'' — outside a transaction, then re-apply.';
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS chunks_keywords_pending_idx
    ON chunks (chunk_id DESC)
    WHERE keywords IS NULL
      AND chunk_kind NOT IN ('card_authors', 'card_combined',
          'card_description', 'card_title', 'equation', 'figure',
          'references', 'table')
      AND length(text) >= 150
      AND (meta->>'no_index') IS DISTINCT FROM 'true';

COMMIT;

-- End of 0261_chunks_keywords_pending_idx.sql
