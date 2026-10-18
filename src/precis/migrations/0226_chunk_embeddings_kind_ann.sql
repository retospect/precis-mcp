-- 0226_chunk_embeddings_kind_ann.sql
--
-- Kind-scoped semantic search (gr495190; docs/backlog/kind-scoped-vector-search.md).
--
-- chunk_embeddings had one HNSW index over every kind (~3.9M rows, 92% paper).
-- refs.kind sits two joins away, so `search(kind='memory', ...)` walked the
-- global graph and filtered after the join: 33 s on prod (2026-10-17) to find
-- 20 memory rows among 30k scanned papers. A partial index can only name
-- columns of its own table, so the kind is copied here, and Postgres owns
-- the copy:
--
--   * ref_id + kind columns, filled by the BEFORE INSERT/UPDATE trigger
--     below when NULL. The trigger is the only definition of the copy; the
--     embedding worker's INSERTs do not name the columns.
--   * FK (chunk_id, ref_id) -> chunks and FK (ref_id, kind) -> refs, both
--     ON UPDATE / ON DELETE CASCADE. A wrong copy fails on insert; a ref
--     whose kind changes (0018-style rename) carries its embeddings along.
--     Each needs a UNIQUE on the parent pair: trivially unique, since
--     chunk_id and ref_id are the parents' primary keys.
--   * chunk_embeddings_ref_kind_filled CHECK (both NOT NULL), added NOT
--     VALID. Its pg_constraint.convalidated flag is the router's gate
--     (store/_chunks_ops.py::_ann_route): the per-kind index path is used
--     only once every row carries its kind. Until then searches run as
--     before.
--   * The per-kind partial HNSW indexes and the (ref_id, kind) btree are NOT
--     built here: on prod each would take a SHARE lock and scan ~3.9M rows
--     inside this transaction, stalling embedding writes. They are built
--     CONCURRENTLY, once, from filled data, by `precis
--     backfill-embedding-kinds` (cli/embedding_kind_backfill.py), which owns
--     the kind list with the router (store/_chunks_ops.py::_ANN_PARTIAL_KINDS).
--     A kind without a valid index stays on the global index.
--
-- Prod rollout (docs/backlog/kind-scoped-vector-search.md has the full order):
--   1. MANDATORY first: build refs_ref_id_kind_key and
--      chunks_chunk_id_ref_id_key with CREATE UNIQUE INDEX CONCURRENTLY
--      outside a transaction (the runner wraps this file in one, so it
--      cannot; 0016 precedent). The guard below refuses an INVALID copy
--      (IF NOT EXISTS would skip it silently) and refuses to build the
--      chunks key in-transaction on a large table.
--   2. Apply. lock_timeout 5 s bounds every DDL wait behind a long reader;
--      on timeout the whole file rolls back and can be re-applied.
--   3. `precis backfill-embedding-kinds --apply`: fill, VALIDATE, build the
--      per-kind indexes CONCURRENTLY; then REINDEX the global HNSW.
-- A small table (fresh, test or dev DB) is filled and validated here, and
-- gets the FK-covering btree in-transaction (cheap at that size).
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot at
-- release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

-- The runner sets lock_timeout = 0 for the apply (store/migrate.py); bound it
-- here so ADD COLUMN / trigger / FK statements fail fast instead of queueing
-- behind a long ANN reader and blocking every writer queued behind them.
SET LOCAL lock_timeout = '5s';

-- Parent keys for the composite FKs. Pre-built CONCURRENTLY on prod (step 1).
DO $$
DECLARE
    ix text;
BEGIN
    FOREACH ix IN ARRAY ARRAY['refs_ref_id_kind_key', 'chunks_chunk_id_ref_id_key'] LOOP
        IF EXISTS (SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
                    WHERE c.relname = ix AND NOT i.indisvalid) THEN
            RAISE EXCEPTION '0226: index % exists but is INVALID (a failed CONCURRENTLY build)', ix
                USING HINT = 'DROP INDEX CONCURRENTLY ' || ix || '; rebuild it CONCURRENTLY; re-apply.';
        END IF;
    END LOOP;
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'chunks_chunk_id_ref_id_key')
       AND (SELECT reltuples FROM pg_class WHERE relname = 'chunks') > 1000000 THEN
        RAISE EXCEPTION '0226: chunks_chunk_id_ref_id_key missing on a large chunks table'
            USING HINT = 'CREATE UNIQUE INDEX CONCURRENTLY chunks_chunk_id_ref_id_key '
                         'ON chunks (chunk_id, ref_id) (and refs_ref_id_kind_key ON refs '
                         '(ref_id, kind)) outside a transaction, then re-apply.';
    END IF;
END
$$;
CREATE UNIQUE INDEX IF NOT EXISTS refs_ref_id_kind_key ON refs (ref_id, kind);
CREATE UNIQUE INDEX IF NOT EXISTS chunks_chunk_id_ref_id_key ON chunks (chunk_id, ref_id);

ALTER TABLE chunk_embeddings
    ADD COLUMN IF NOT EXISTS ref_id bigint,
    ADD COLUMN IF NOT EXISTS kind text;

CREATE OR REPLACE FUNCTION chunk_embeddings_fill_ref_kind()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.ref_id IS NULL THEN
        SELECT c.ref_id INTO NEW.ref_id FROM chunks c WHERE c.chunk_id = NEW.chunk_id;
    END IF;
    IF NEW.kind IS NULL THEN
        SELECT r.kind INTO NEW.kind FROM refs r WHERE r.ref_id = NEW.ref_id;
    END IF;
    RETURN NEW;
END
$$;

COMMENT ON FUNCTION chunk_embeddings_fill_ref_kind() IS
    'Fills chunk_embeddings.ref_id/kind from chunks -> refs when NULL. The '
    'one definition of the copy; FKs to chunks(chunk_id, ref_id) and '
    'refs(ref_id, kind) check it (0226).';

DROP TRIGGER IF EXISTS chunk_embeddings_fill_ref_kind ON chunk_embeddings;
CREATE TRIGGER chunk_embeddings_fill_ref_kind
    BEFORE INSERT OR UPDATE ON chunk_embeddings
    FOR EACH ROW
    WHEN (NEW.ref_id IS NULL OR NEW.kind IS NULL)
    EXECUTE FUNCTION chunk_embeddings_fill_ref_kind();

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'chunk_embeddings_chunk_ref_fkey') THEN
        ALTER TABLE chunk_embeddings
            ADD CONSTRAINT chunk_embeddings_chunk_ref_fkey
            FOREIGN KEY (chunk_id, ref_id) REFERENCES chunks (chunk_id, ref_id)
            ON UPDATE CASCADE ON DELETE CASCADE NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'chunk_embeddings_ref_kind_fkey') THEN
        ALTER TABLE chunk_embeddings
            ADD CONSTRAINT chunk_embeddings_ref_kind_fkey
            FOREIGN KEY (ref_id, kind) REFERENCES refs (ref_id, kind)
            ON UPDATE CASCADE ON DELETE CASCADE NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'chunk_embeddings_ref_kind_filled') THEN
        ALTER TABLE chunk_embeddings
            ADD CONSTRAINT chunk_embeddings_ref_kind_filled
            CHECK (ref_id IS NOT NULL AND kind IS NOT NULL) NOT VALID;
    END IF;
END
$$;

-- Small table (fresh/test/dev DB): fill, validate and add the btree covering
-- the refs-side FK now. Prod's 3.9M rows go through
-- `precis backfill-embedding-kinds`, which builds the same btree CONCURRENTLY.
-- The chunks-side FK is served by the primary key's leading chunk_id
-- (tests/test_schema_design.py).
DO $$
BEGIN
    IF (SELECT count(*) FROM (SELECT 1 FROM chunk_embeddings LIMIT 50001) s) <= 50000 THEN
        CREATE INDEX IF NOT EXISTS chunk_embeddings_ref_id_kind_idx
            ON chunk_embeddings (ref_id, kind);
        UPDATE chunk_embeddings SET ref_id = NULL
         WHERE ref_id IS NULL OR kind IS NULL;
        ALTER TABLE chunk_embeddings VALIDATE CONSTRAINT chunk_embeddings_chunk_ref_fkey;
        ALTER TABLE chunk_embeddings VALIDATE CONSTRAINT chunk_embeddings_ref_kind_fkey;
        ALTER TABLE chunk_embeddings VALIDATE CONSTRAINT chunk_embeddings_ref_kind_filled;
    END IF;
END
$$;

COMMIT;

-- End of 0226_chunk_embeddings_kind_ann.sql
