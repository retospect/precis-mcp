-- 0116_ref_embeddings.sql
--
-- Whole-ref embeddings for refs with no chunk body to borrow a vector
-- from — paper stubs (title + abstract only, no PDF ingested yet have
-- no `card_combined` chunk). The `stub_rank` pass (workers/stub_rank.py)
-- embeds `title + abstract` directly per stub and needs somewhere to
-- park that vector; `chunk_embeddings` is keyed to a `chunk_id` a stub
-- doesn't have, so this is a parallel, chunk-less table keyed straight
-- to `refs(ref_id)`. One row per (ref, embedder) — `ON DELETE CASCADE`
-- so a merged/deleted stub's vector goes away with it, no orphan sweep
-- needed.
--
-- Forward-only. Idempotent (`CREATE TABLE IF NOT EXISTS`).

BEGIN;

CREATE TABLE IF NOT EXISTS ref_embeddings (
    ref_id bigint NOT NULL REFERENCES refs(ref_id) ON DELETE CASCADE,
    embedder text NOT NULL,
    embedding public.vector(1024) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (ref_id, embedder)
);

COMMIT;

-- End of 0116_ref_embeddings.sql
