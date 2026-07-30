-- 0098_capture_chunk_autovacuum.sql
--
-- Captures per-table autovacuum reloptions on the chunk family (``chunks``,
-- ``chunk_embeddings``, ``chunk_summaries``) that were applied manually,
-- out-of-band, directly against prod's catalog — they exist in NO prior
-- migration. That means a rebuild-from-migrations (a fresh DB replaying the
-- migration tail onto the baseline snapshot) would silently lose prod's
-- tuning and start from the untuned global defaults. This migration is a
-- no-op on prod (the settings already match what's below) and only matters
-- for reproducing that tuning on a fresh DB.
--
-- These tables are high-churn (chunk inserts/updates plus the
-- embedding/summary cascade), so a looser vacuum threshold lets dead tuples
-- accumulate faster than on a slowly-changing table; tightening the vacuum
-- and analyze scale factors and dropping the vacuum cost delay keeps
-- autovacuum running promptly against the churn instead of lagging behind it.
--
-- ALTER TABLE IF EXISTS so a dev/test DB that hasn't (yet) created one of
-- these tables doesn't fail. Storage params ARE captured by pg_dump, so this
-- correctly rolls into the baseline snapshot at the next `scripts/bump`.
--
-- Forward-only (ADR 0005). Idempotent (ALTER TABLE SET storage params is
-- re-runnable and re-asserting the same values is a no-op).

BEGIN;

ALTER TABLE IF EXISTS chunks SET (
    autovacuum_vacuum_scale_factor  = 0.02,
    autovacuum_analyze_scale_factor = 0.01,
    autovacuum_vacuum_cost_delay    = 0
);

ALTER TABLE IF EXISTS chunk_embeddings SET (
    autovacuum_vacuum_scale_factor  = 0.02,
    autovacuum_analyze_scale_factor = 0.01,
    autovacuum_vacuum_cost_delay    = 0
);

ALTER TABLE IF EXISTS chunk_summaries SET (
    autovacuum_vacuum_scale_factor  = 0.02,
    autovacuum_analyze_scale_factor = 0.01,
    autovacuum_vacuum_cost_delay    = 0
);

COMMIT;

-- End of 0098_capture_chunk_autovacuum.sql
