-- 0017_tag_embeddings.sql
--
-- New table for the kind='tag' discovery surface: one row per
-- (namespace, value) ever used, plus its embedding for semantic
-- search. Populated lazily by the tag_embeddings worker.
--
-- Embeds every tag — open AND closed (STATUS:done, CACHE:fresh, …) —
-- so semantic search can surface either. The handler exposes
-- get/search; no put/edit/delete/tag/link.
--
-- ``version`` mirrors the chunk_keywords lazy-update pattern: the
-- worker re-claims rows whose stored version is below the module
-- constant ``TAG_EMBEDDINGS_VERSION``. ``embedder`` is the model name
-- (currently ``bge-m3``).

CREATE TABLE tag_embeddings (
    namespace      TEXT NOT NULL,
    value          TEXT NOT NULL,
    vector         VECTOR(1024),     -- bge-m3 dim
    version        INTEGER NOT NULL DEFAULT 1,
    embedder       TEXT NOT NULL,
    embedded_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (namespace, value)
);

CREATE INDEX tag_embeddings_vector_hnsw
    ON tag_embeddings USING hnsw (vector vector_cosine_ops);

-- Allow ``target='tag'`` on artifact_kinds. Pre-0017 the CHECK only
-- listed the four ref/chunk/link/pdf shapes; the tag-discovery surface
-- targets the unified tags row (one artifact per (namespace, value)).
ALTER TABLE artifact_kinds
    DROP CONSTRAINT IF EXISTS artifact_kinds_target_check;
ALTER TABLE artifact_kinds
    ADD CONSTRAINT artifact_kinds_target_check
    CHECK (target IN ('chunk', 'ref', 'link', 'pdf', 'tag'));

INSERT INTO artifact_kinds (slug, target, storage, output_table, description) VALUES
    ('embed:tags', 'tag', 'typed', 'tag_embeddings',
     'bge-m3 embeddings of every tag in use, for semantic discovery')
ON CONFLICT (slug) DO NOTHING;

-- End of 0017_tag_embeddings.sql
