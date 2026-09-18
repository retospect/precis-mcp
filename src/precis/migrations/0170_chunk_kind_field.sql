-- 0170_chunk_kind_field.sql
--
-- Register chunk_kind='field' — the sampled signed-distance grid of a cad
-- `field:<sha256>` leaf (docs/backlog/cad-sdf-rounding-and-field-export.md,
-- slice 2). The grid itself is a `chunk_blobs` payload (ADR 0034: chunk-keyed
-- bytea, TOASTed, content-addressed by sha256 — the index that lookup needs,
-- `chunk_blobs_sha256_idx`, exists since 0035); this row is the chunk that
-- payload hangs off: `text` is the one-line human summary (shape, pitch,
-- origin, source), `meta.field` the payload's JSON header, so a shape/pitch
-- read never de-TOASTs the samples. Written by `Store.put_field`, one per
-- distinct grid, never updated in place — a changed grid is a new chunk, so
-- an older design's `field:` reference stays valid.
--
-- Body-kind (`ord >= 0`, not `card_%`): the `chunks_check` constraint
-- requires a non-card name at a non-negative ord, and `chunks.chunk_kind`
-- is FK'd to `chunk_kinds(slug)` — without this row the first put_field
-- trips the FK.
--
-- Additive, forward-only (ADR 0005), idempotent. Regenerate the baseline
-- snapshot after merge (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

INSERT INTO chunk_kinds (slug, is_card, description) VALUES
    ('field', FALSE,
     'Sampled signed-distance grid of a cad field:<sha256> leaf. text = the '
     'one-line summary (shape, pitch, origin, source); meta.field = the '
     'payload header; the float32 samples live in chunk_blobs, '
     'content-addressed by sha256. Written by Store.put_field, never updated '
     'in place. See docs/backlog/cad-sdf-rounding-and-field-export.md.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;
