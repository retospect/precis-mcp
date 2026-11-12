-- 0313_ref_tags_ref_id_created_idx.sql
--
-- gr575088 (db-thrash review, 2026-10-22 → 2026-11-06): ref_tags at
-- ~1.08M rows took 109k seq scans averaging 339k rows each over the
-- window; the review's note: "a ref_id-keyed read has no index". The
-- primary key does lead with ref_id — (ref_id, tag_id) — so a point
-- probe is already served, but the hot per-ref reads the sweeps and
-- view renderers run ask for a ref's tags *in time order* or the newest
-- one: `max(rt.created_at)`, `ORDER BY rt.created_at` inside
-- correlated subqueries (memory_thread_sweep scratch-owner status,
-- todo view status lookups, dispatch_score EXISTS probes). Those take
-- the pkey probe and still scan/sort the ref's whole tag set; a
-- dedicated (ref_id, created_at) b-tree answers the ref_id-keyed probe
-- *and* returns a ref's tags pre-sorted by insertion, index-only for
-- the max(created_at) shapes. It is narrower than the pkey for the
-- common path and leaves the tag_id-led reads to the existing indexes.
--
-- Non-concurrent like 0282: migrations run in the deploy's
-- single-writer window; a 5s lock_timeout keeps the build from wedging
-- behind a queue of tag writes.
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

SET LOCAL lock_timeout = '5s';

CREATE INDEX IF NOT EXISTS ref_tags_ref_id_created_idx
    ON ref_tags (ref_id, created_at);

COMMIT;

-- End of 0313_ref_tags_ref_id_created_idx.sql
