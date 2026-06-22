-- 0035_chunk_blobs.sql
--
-- ADR 0034 — figure assets in the database, attached to the chunk.
--
-- Draft figures (chunk_kind='figure') carry binary image bytes. Those
-- bytes cannot live in `chunks.text` (NOT NULL, feeds a GENERATED
-- tsvector — base64 there would poison full-text search and the embed
-- cascade), so they get a side table keyed 1:1 on the chunk. The figure
-- chunk's `text` stays the caption (the embedded, searchable face); the
-- image is here. Postgres TOASTs `bytes` out-of-line, so a normal
-- `SELECT text, meta FROM chunks` never drags the blob — only an explicit
-- blob fetch (render / export) does.
--
-- `sha256` is the content address: identity now, optional dedup later
-- (not enforced — drafts rarely reuse an image; revisit if they do).
-- Export plumbing (writing blobs out to pics/) is a later phase; this
-- migration only gives the bytes a home in the graph.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- after merge (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

CREATE TABLE IF NOT EXISTS chunk_blobs (
    chunk_id   BIGINT PRIMARY KEY
        REFERENCES chunks (chunk_id) ON DELETE CASCADE,
    bytes      BYTEA  NOT NULL,
    mime       TEXT   NOT NULL,
    sha256     CHAR(64) NOT NULL,
    size_bytes BIGINT NOT NULL,
    width      INT,
    height     INT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- content-address lookup (identity / future dedup)
CREATE INDEX IF NOT EXISTS chunk_blobs_sha256_idx
    ON chunk_blobs (sha256);

COMMIT;

-- End of 0035_chunk_blobs.sql
