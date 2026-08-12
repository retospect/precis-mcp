-- 0124_worker_logs_handler_ts.sql
--
-- Partial index for the condition registry's per-pass liveness probes
-- (self-healing-spine Layer 2, slice 3 — workers/conditions.py).
-- `pass-dead-on-host` / `rescue-pass-cadence` aggregate the last 7 days of
-- per-cycle BatchResult rows (`payload ? 'handler'`) hourly; the existing
-- worker_logs indexes all lead with an equality column these probes don't
-- filter on (host / pass / level), so without this the hourly singleton
-- pass seq-scans the whole 30-day table. Partial on the handler-carrying
-- rows: that's exactly the probe's row set, and boot/error rows stay out.
--
-- Forward-only (ADR 0005): additive, no data migration.

CREATE INDEX IF NOT EXISTS worker_logs_handler_ts_idx
    ON worker_logs (ts)
 WHERE payload ? 'handler';
