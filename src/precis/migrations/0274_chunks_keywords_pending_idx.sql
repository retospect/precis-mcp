-- 0274_chunks_keywords_pending_idx.sql
--
-- Guard-only migration for the chunk_keywords dedicated drain lane's
-- fresh-backlog partial index (gr520236 / gr550973;
-- docs/backlog/chunk-keywords-pending-index.md).
--
-- chunks carries ~4.7M rows on prod; a plain in-transaction
-- CREATE INDEX takes a SHARE lock for a full heap scan and stalls
-- ingest writes for the duration (0226/0255 precedent). Ops pre-builds
-- the index CONCURRENTLY, outside a transaction, BEFORE this migration
-- ships. The guard below:
--   * refuses an INVALID index (a failed CONCURRENTLY build) — the
--     next ops action is DROP INDEX CONCURRENTLY + rebuild, not a
--     silent IF NOT EXISTS skip;
--   * refuses a pre-built index whose recorded predicate differs from
--     the literal text below — a drifted
--     precis.workers.chunk_keywords._eligibility_sql would otherwise
--     be silently accepted, and the claim's DESC walk would stop
--     matching the index (the planner only picks a partial index when
--     it can prove the query's WHERE implies the index's predicate);
--   * refuses to build inline when chunks has grown past 1M rows
--     (prod-sized) without a pre-built index.
-- A small table (fresh/test/dev DB) gets it in-transaction via the
-- CREATE INDEX IF NOT EXISTS below.
--
-- The predicate literals are owned by
-- precis.workers.chunk_keywords._eligibility_sql / PENDING_INDEX_DDL
-- (test_claim_literals_match_pending_index_ddl pins them to the
-- claim's WHERE text) — never edit them here independently of that
-- module.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline
-- snapshot at release time (ADR 0031): `scripts/bump` / `precis db
-- dump-schema`. Delete docs/backlog/chunk-keywords-pending-index.md
-- once this has shipped on prod.

BEGIN;

SET LOCAL lock_timeout = '5s';

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
         WHERE c.relname = 'chunks_keywords_pending_idx' AND NOT i.indisvalid
    ) THEN
        RAISE EXCEPTION
            '0274: chunks_keywords_pending_idx exists but is INVALID (a failed CONCURRENTLY build)'
            USING HINT = 'DROP INDEX CONCURRENTLY chunks_keywords_pending_idx; '
                         'rebuild it CONCURRENTLY; re-apply.';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'chunks_keywords_pending_idx')
       AND NOT EXISTS (
        SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
         WHERE c.relname = 'chunks_keywords_pending_idx'
           AND pg_get_expr(i.indpred, i.indrelid) = '((keywords IS NULL) AND '
               '(chunk_kind <> ALL (ARRAY[''card_authors''::text, ''card_combined''::text, '
               '''card_description''::text, ''card_title''::text, ''equation''::text, '
               '''figure''::text, ''paper_note''::text, ''references''::text, '
               '''scan_geometry''::text, ''table''::text])) AND (length(text) >= 150) AND '
               '((meta ->> ''no_index''::text) IS DISTINCT FROM ''true''::text))'
    ) THEN
        RAISE EXCEPTION
            '0274: chunks_keywords_pending_idx exists but its predicate does not '
            'match precis.workers.chunk_keywords._eligibility_sql (drifted literals)'
            USING HINT = 'DROP INDEX CONCURRENTLY chunks_keywords_pending_idx; rebuild it '
                         'CONCURRENTLY with the current PENDING_INDEX_DDL text; re-apply.';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'chunks_keywords_pending_idx')
       AND (SELECT reltuples FROM pg_class WHERE relname = 'chunks') > 1000000 THEN
        RAISE EXCEPTION
            '0274: chunks_keywords_pending_idx missing on a large chunks table'
            USING HINT = 'Pre-build it CONCURRENTLY outside a transaction '
                         '(docs/backlog/chunk-keywords-pending-index.md has the exact DDL), '
                         'then re-apply.';
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS chunks_keywords_pending_idx
    ON chunks (chunk_id DESC)
    WHERE keywords IS NULL
      AND chunk_kind NOT IN ('card_authors', 'card_combined',
          'card_description', 'card_title', 'equation', 'figure', 'paper_note',
          'references', 'scan_geometry', 'table')
      AND length(text) >= 150
      AND (meta->>'no_index') IS DISTINCT FROM 'true';

COMMIT;

-- End of 0274_chunks_keywords_pending_idx.sql
