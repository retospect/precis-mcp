-- 0243_chunk_embeddings_kind_comment.sql
--
-- gr515170: ``chunk_embeddings.kind`` surprised a test author — the 0226
-- trigger fills it with the *ref* kind (``refs.kind``, e.g. 'paper'),
-- not the row's ``chunk_kind``, and nothing said so. The generated
-- schema doc (``precis schema-doc`` → docs/reference/schema.md) renders
-- ``pg_catalog.col_description``, so ``COMMENT ON COLUMN`` is the source
-- of truth — set it here, never in the generated file.
--
-- Forward-only (ADR 0005). Idempotent (COMMENT ON is a replace).

BEGIN;

-- The runner sets lock_timeout = 0 for the apply (store/migrate.py); bound
-- the DDL wait behind a long reader instead (convention from 0224/0226).
SET LOCAL lock_timeout = '5s';

COMMENT ON COLUMN chunk_embeddings.kind IS
    'Copy of the parent ref''s refs.kind (e.g. ''paper''), filled by the '
    '0226 trigger chunk_embeddings_fill_ref_kind when NULL — NOT the '
    'row''s chunk_kind. Backs the per-kind partial HNSW indexes; FK '
    '(ref_id, kind) -> refs and ON UPDATE CASCADE keep it honest.';

COMMIT;

-- End of 0243_chunk_embeddings_kind_comment.sql
