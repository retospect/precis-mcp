-- 0266_refs_body_bytes.sql
--
-- Stored per-ref body byte count: sum(octet_length(text)) over the ref's
-- live body chunks (ord >= 0 AND retired_at IS NULL) — the same quantity
-- ChunkStore.body_byte_counts computed at read time, which is too slow
-- for the fisheye and hub rings: a ring is not bounded by page_size, so
-- per-ring-row aggregates over chunks do not fit a get's latency.
--
-- A SIDE table, not a refs column: keeping the sum current with chunk
-- triggers would put a NO KEY UPDATE lock on the refs row for every
-- body-chunk write, held to commit — a long ingest transaction on ref R
-- then makes any concurrent UPDATE refs WHERE ref_id=R hit prod's 5 s
-- lock_timeout (the failure class gr537784/0265 just fixed), and each
-- apply re-fires the whole refs trigger stack. Writers must not pay;
-- maintenance runs async instead:
--
--   workers/ref_body_bytes.py — the `ref_body_bytes` scheduler cadence
--   finds refs whose body chunks changed since a stored watermark
--   (app_settings `ref_body_bytes:watermark`; chunks.created_at covers
--   inserts, retired_at covers retires and replace's insert half) and
--   recomputes them in small ref_id-ordered batches. The daily
--   `ref_body_bytes_verify` cadence re-walks every ref for the changes
--   no timestamp can see (a pure chunk DELETE). `precis
--   backfill-ref-bytes` fills the table directly for pre-0266 refs.
--
-- No row = "not yet computed"; readers render no size. Sizes are
-- display hints — minutes of staleness are fine.
--
-- The two chunks indexes keep the incremental scan off the heap: a
-- BRIN on created_at (append-ordered — near-free per insert, tiny) and
-- a partial btree on retired_at (sparse — touched only on the retire
-- path). The scan itself is the UNION of two range probes.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- at release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

-- Bound every DDL wait behind a long reader (0226 precedent).
SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS ref_body_bytes (
    ref_id bigint PRIMARY KEY REFERENCES refs ON DELETE CASCADE,
    body_bytes bigint NOT NULL,
    computed_at timestamptz NOT NULL
);

COMMENT ON TABLE ref_body_bytes IS
    'Stored per-ref body byte count (0266): sum(octet_length(text)) over '
    'the ref''s live body chunks (ord >= 0, retired_at IS NULL). Side '
    'table, not a refs column — recomputed off the write path by the '
    'ref_body_bytes / ref_body_bytes_verify cadences (and '
    '`precis backfill-ref-bytes`); no row = not yet computed = no size.';

COMMENT ON COLUMN ref_body_bytes.computed_at IS
    'When the row was last recomputed — the staleness read for a display '
    'hint that tolerates minutes of lag.';

-- Incremental-scan support: `created_at > wm` rides the BRIN (appends
-- cluster at the heap tail); `retired_at > wm` rides the sparse partial
-- btree (the retire path is the rare one).
CREATE INDEX IF NOT EXISTS chunks_created_at_brin
    ON chunks USING brin (created_at);

CREATE INDEX IF NOT EXISTS chunks_retired_at_idx
    ON chunks (retired_at) WHERE retired_at IS NOT NULL;

COMMIT;

-- End of 0266_refs_body_bytes.sql
