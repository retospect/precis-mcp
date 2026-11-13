-- 0318_llm_call_log_run.sql
--
-- Link every dispatch attempt to its run (gr595543).
--
-- `llm_call_log` already writes one row per logged attempt — each failed
-- rung the failover ladder walks past, every gate early-out, and the call's
-- final outcome — but nothing ties those rows together: "small failed, big
-- succeeded on the same run" is unanswerable, and escalation is invisible.
--
-- `run_id` identifies one logical router run — one `route()` /
-- `dispatch_async()` invocation. `attempt` is the 1-based ordinal of the
-- row within that run: escalation is attempt N+1 on the same run on a
-- bigger rung, and an in-process retry of the same rung is attempt N+1 on
-- the same model (gr576133's invisible retry, now logged).
--
-- Both nullable; NULL = a pre-0317 row or a writer outside the router.
-- Not backfilled: old rows' grouping was never recorded.
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

ALTER TABLE llm_call_log ADD COLUMN IF NOT EXISTS run_id text;
ALTER TABLE llm_call_log ADD COLUMN IF NOT EXISTS attempt integer;

COMMENT ON COLUMN llm_call_log.run_id IS
    'One logical router run — a route()/dispatch_async() invocation '
    '(uuid hex). Every logged attempt of the run — ladder walk, in-process '
    'retry, gate early-out — shares it. NULL = pre-0317 or a non-router writer.';

COMMENT ON COLUMN llm_call_log.attempt IS
    '1-based ordinal of this row within its run: escalation is attempt N+1 '
    'on a bigger rung, a same-rung retry is attempt N+1 on the same model. '
    'NULL = pre-0317 or a non-router writer.';

-- Per-run lookups ("what did run X try, in order"); partial — pre-0317
-- rows never carry a run, so the index stays small and the build writes
-- nothing for them. Plain CREATE INDEX (never CONCURRENTLY — apply_all
-- wraps each file in a transaction); the 5s lock_timeout keeps the build
-- from queueing behind open writers on this ~10M-row table (0315).
SET LOCAL lock_timeout = '5s';

CREATE INDEX IF NOT EXISTS llm_call_log_run_id_idx
    ON llm_call_log (run_id) WHERE run_id IS NOT NULL;

COMMIT;
