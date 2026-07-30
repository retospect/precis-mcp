-- 0097_hot_table_autovacuum.sql
--
-- Per-table autovacuum tuning for three tiny, extreme-churn tables that a
-- prod audit flagged as badly dead-tuple-bloated despite ~100% HOT updates:
--   scheduler_leases   93.8% dead tuples
--   resource_slots     78.6% dead tuples
--   host_heartbeat     77.8% dead tuples
--
-- All three are updated up to ~1M times and are ~100% HOT (no indexed column
-- ever changes), so a HOT-prune vacuum is cheap and touches no indexes — the
-- bloat is purely autovacuum lagging behind the update rate, not an inherent
-- cost of vacuuming them. The global thresholds
-- (autovacuum_vacuum_scale_factor = 0.2, i.e. vacuum only after 20% of the
-- table is dead) are sized for large, slowly-changing tables and are far too
-- loose for a table this small and this hot: on a 100-row table, 20% dead is
-- only 20 rows before autovacuum even considers it, so bloat run away between
-- passes. At 78-94% dead, every read on these tables is walking ~4-16x the
-- live row count in dead tuples it must skip — a hot lease/heartbeat table
-- read amplified this way costs real latency on every scheduling and
-- liveness check.
--
-- Fix: set scale_factor = 0 and a small fixed threshold (200 dead tuples) so
-- autovacuum triggers on absolute churn instead of a percentage of a tiny
-- table, and drop the vacuum cost delay to 0 so the (already cheap, HOT-only)
-- vacuum isn't throttled and completes promptly. This makes autovacuum run
-- on these three tables near-continuously, which is fine — a HOT-prune pass
-- on a small table costs very little I/O, and the whole point is to trade a
-- little continuous vacuum work for eliminating the read amplification.
--
-- ALTER TABLE IF EXISTS so a dev/test DB that hasn't (yet) created one of
-- these tables doesn't fail. Storage params ARE captured by pg_dump, so this
-- correctly rolls into the baseline snapshot at the next `scripts/bump`.
--
-- Forward-only (ADR 0005). Idempotent (ALTER TABLE SET storage params is
-- re-runnable and re-asserting the same values is a no-op).

BEGIN;

ALTER TABLE IF EXISTS scheduler_leases SET (
    autovacuum_vacuum_scale_factor = 0,
    autovacuum_vacuum_threshold    = 200,
    autovacuum_vacuum_cost_delay   = 0
);

ALTER TABLE IF EXISTS resource_slots SET (
    autovacuum_vacuum_scale_factor = 0,
    autovacuum_vacuum_threshold    = 200,
    autovacuum_vacuum_cost_delay   = 0
);

ALTER TABLE IF EXISTS host_heartbeat SET (
    autovacuum_vacuum_scale_factor = 0,
    autovacuum_vacuum_threshold    = 200,
    autovacuum_vacuum_cost_delay   = 0
);

COMMIT;

-- End of 0097_hot_table_autovacuum.sql
