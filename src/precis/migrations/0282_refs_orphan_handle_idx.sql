-- 0282_refs_orphan_handle_idx.sql
--
-- Partial index for the ssh_node orphan-reap lookup (gr573350, first
-- half; the hybrid-search `WITH cand` statements stay open on the
-- gripe).
--
-- ``ssh_node._orphaned_detached_jobs`` runs once per pass per node —
-- ~312k calls at ~0.94 s mean on prod (queryid 5708117170685222755).
-- Nothing indexes its predicate, so the plan HashAggregates ~164k
-- terminal STATUS ``ref_tags`` rows (stats guess 507/tag, actual
-- 54,701) and then probes ``refs_ref_id_kind_key`` once per row,
-- filtering every meta predicate — to return 0 rows: of ~124k live
-- jobs only ~442 carry a ``boot_id`` handle. The partial index starts
-- from that handful instead; the call rate is by design (sibling
-- ``_polling_jobs`` does 502k calls at 0.2 ms), so the fix is the
-- index, not the rate.
--
-- The predicate text mirrors the query's WHERE clause verbatim — the
-- planner only uses a partial index when the query implies the
-- predicate, and the Python statement lives in
-- ``ssh_node._ORPHANED_DETACHED_SQL`` with a test pinning the two
-- together. Rows leave the index as soon as the reaper stamps
-- ``detached_orphan_reaped``/``_skipped`` or the handle is released,
-- so it stays near-empty. ``meta`` already sits inside the
-- ``refs_rxn_*`` expression indexes (0157), so this adds no new
-- non-HOT update class.
--
-- Plain CREATE INDEX: Migrator.apply_all() wraps each file in a
-- transaction (CONCURRENTLY cannot run there), and migrations apply
-- in the deploy's single-writer window. refs carries ~568k rows /
-- 383 MB on prod; the build scans the heap once but inserts only the
-- unreleased-handle rows, so the SHARE lock holds for seconds, per
-- the 0255 precedent.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline
-- snapshot at release time (ADR 0031): `scripts/bump` /
-- `precis db dump-schema`.

BEGIN;

SET LOCAL lock_timeout = '5s';

CREATE INDEX IF NOT EXISTS refs_orphan_handle_idx
    ON refs (ref_id)
    WHERE kind = 'job'
      AND retired_at IS NULL
      AND meta->'compute_handle' ? 'boot_id'
      AND (meta->>'compute_handle_released') IS DISTINCT FROM 'true'
      AND NOT (meta ? 'detached_orphan_reaped')
      AND NOT (meta ? 'detached_orphan_skipped');

COMMIT;

-- End of 0282_refs_orphan_handle_idx.sql
