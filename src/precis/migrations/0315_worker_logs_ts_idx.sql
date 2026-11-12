-- 0315_worker_logs_ts_idx.sql
--
-- gr573439 (db-thrash review, 2026-11-06): worker_logs at ~3.9M rows /
-- 2.6 GB took 40,795 seq scans averaging ~802k rows each. Every index
-- leads with an equality column (host_ts, pass_ts, level_ts) or is
-- partial on `payload ? 'handler'` (0124), so a read that bounds ts and
-- filters only on columns no index leads with has no index path:
--
--   * sweeper._gc_worker_logs — the per-minute retention batch,
--     `ts < now() - 30d LIMIT n`. Pruned heap space is reused by new
--     inserts, so aged rows are not clustered at the heap head and the
--     unordered LIMIT scan reads most of the table each pass;
--   * nursery dead-worker host_alive (`ts > now() - 3 min`), restart
--     storm (message + ts), last_seen (process + ts), unregistered
--     claimer (ts + message regex);
--   * conditions rescue-cadence (`payload->>'handler' = ANY` without
--     the `?` the 0124 partial needs);
--   * web status host rollup (7d) and failed passes (24h); factory
--     error strip (level list includes CRITICAL, outside the level_ts
--     partial);
--   * the /logs, alert context and `precis logs` reads when no host or
--     pass filter is given.
--
-- A plain b-tree, not BRIN: retention deletes leave free space that
-- later inserts fill, so physical order stops tracking ts and BRIN
-- ranges would widen to the whole table. The b-tree also serves the
-- `ORDER BY ts DESC LIMIT` reads backwards.
--
-- Plain CREATE INDEX: Migrator.apply_all() wraps each file in a
-- transaction (CONCURRENTLY cannot run there), and migrations apply in
-- the deploy's single-writer window, the 0208 precedent (~11.5M-row
-- llm_call_log). The DB log handler buffers rows and flushes on its own
-- connection, so the SHARE lock held for the build delays flushes; a
-- flush that fails demotes its batch to the file log. A 5s lock_timeout
-- keeps the build from queueing behind open writers.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- at release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

SET LOCAL lock_timeout = '5s';

CREATE INDEX IF NOT EXISTS worker_logs_ts_idx
    ON worker_logs (ts);

COMMIT;

-- End of 0315_worker_logs_ts_idx.sql
