-- 0016 — HNSW index on chunk_embeddings.vector
--
-- The on-disk comment in 0001 deferred this index to "the application
-- layer" on the theory that building it before first ingest would cost
-- nothing and a later rebuild was unavoidable. In practice no startup
-- hook ever created it, so every `search(kind='paper', q=...)` did a
-- sequential scan + 1024-d cosine over chunk_embeddings — fine for
-- dozens of chunks, painful at scale.
--
-- pgvector HNSW builds on an empty table are cheap; subsequent inserts
-- maintain the structure as they arrive. We index only ``status='ok'``
-- vectors so the (chunk_id, embedder) failure-marker rows
-- (vector IS NULL) don't bloat the index.
--
-- The runner wraps each migration in a transaction so CONCURRENTLY is
-- not available here. Operators with already-populated chunk_embeddings
-- can pre-create the index manually with CONCURRENTLY before applying
-- the migration; the IF NOT EXISTS clause turns this statement into a
-- no-op in that case.

CREATE INDEX IF NOT EXISTS chunk_embeddings_vec_hnsw_idx
    ON chunk_embeddings
    USING hnsw (vector vector_cosine_ops)
    WHERE status = 'ok' AND vector IS NOT NULL;

ANALYZE chunk_embeddings;
