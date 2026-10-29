-- 0266_chunks_keywords_pending_idx.sql
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
--             'card_description', 'card_title', 'equation', 'figure', 'paper_note',
--             'references', 'table')
--         AND length(text) >= 150
--         AND (meta->>'no_index') IS DISTINCT FROM 'true';
--
-- The guard below refuses an INVALID index (a failed CONCURRENTLY build
-- that IF NOT EXISTS would skip silently), refuses a pre-built index
-- whose recorded predicate differs from the one above (the claim could
-- never use it — the planner must prove the claim WHERE implies the
-- index predicate), and refuses to build inline on a chunks table over
-- 1M reltuples. Small dev/test DBs still get it in-transaction — cheap
-- at that size.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- at release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

SET LOCAL lock_timeout = '5s';

DO $$
DECLARE
    pred text;
    -- The predicate as pg_get_indexdef/pg_get_expr PRINT it (PG 17):
    -- NOT IN deparses to <> ALL over an ARRAY literal, and every
    -- literal carries an explicit ::text cast.
    want_pred constant text := '((keywords IS NULL) AND (chunk_kind <> ALL (ARRAY[''card_authors''::text, ''card_combined''::text, ''card_description''::text, ''card_title''::text, ''equation''::text, ''figure''::text, ''paper_note''::text, ''references''::text, ''table''::text])) AND (length(text) >= 150) AND ((meta ->> ''no_index''::text) IS DISTINCT FROM ''true''::text))';
    rebuild constant text := 'CREATE INDEX CONCURRENTLY chunks_keywords_pending_idx ON chunks (chunk_id DESC) WHERE keywords IS NULL AND chunk_kind NOT IN (''card_authors'', ''card_combined'', ''card_description'', ''card_title'', ''equation'', ''figure'', ''paper_note'', ''references'', ''table'') AND length(text) >= 150 AND (meta->>''no_index'') IS DISTINCT FROM ''true''';
BEGIN
    IF EXISTS (SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
                WHERE c.relname = 'chunks_keywords_pending_idx' AND NOT i.indisvalid) THEN
        RAISE EXCEPTION '0266: index chunks_keywords_pending_idx exists but is INVALID (a failed CONCURRENTLY build)'
            USING HINT = 'DROP INDEX CONCURRENTLY chunks_keywords_pending_idx; ' || rebuild || ' — outside a transaction, then re-apply.';
    END IF;
    -- A pre-built index with a DIFFERENT predicate passes a name-only
    -- check yet can never serve the claim: the planner must prove the
    -- claim WHERE implies the index predicate, so a stale shape (say,
    -- no length floor) leaves the claim seq-scanning forever. Compare
    -- the recorded predicate to the deparsed form of ours.
    SELECT pg_get_expr(i.indpred, i.indrelid) INTO pred
      FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
     WHERE c.relname = 'chunks_keywords_pending_idx';
    IF FOUND AND pred IS DISTINCT FROM want_pred THEN
        RAISE EXCEPTION '0266: chunks_keywords_pending_idx exists with an unexpected predicate — the claim cannot use it: %',
                COALESCE(pred, '(none — the index is not partial)')
            USING HINT = 'DROP INDEX CONCURRENTLY chunks_keywords_pending_idx; ' || rebuild || ' — outside a transaction, then re-apply.';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'chunks_keywords_pending_idx')
       AND (SELECT reltuples FROM pg_class WHERE relname = 'chunks') > 1000000 THEN
        RAISE EXCEPTION '0266: chunks_keywords_pending_idx missing on a large chunks table'
            USING HINT = rebuild || ' — outside a transaction, then re-apply.';
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS chunks_keywords_pending_idx
    ON chunks (chunk_id DESC)
    WHERE keywords IS NULL
      AND chunk_kind NOT IN ('card_authors', 'card_combined',
          'card_description', 'card_title', 'equation', 'figure', 'paper_note',
          'references', 'table')
      AND length(text) >= 150
      AND (meta->>'no_index') IS DISTINCT FROM 'true';

COMMIT;

-- End of 0266_chunks_keywords_pending_idx.sql
