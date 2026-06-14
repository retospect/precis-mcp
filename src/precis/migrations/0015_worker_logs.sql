-- 0015_worker_logs.sql
--
-- Centralised worker log storage. Replaces the per-host
-- /var/log/precis-*.log scattered files as the primary surface for
-- "what's the cluster doing right now?" The text files stay in place
-- as a bootstrap + fallback channel (Python logging continues to
-- emit there), but the structured, queryable view of pass activity
-- across hosts lives in this table.
--
-- Why a dedicated table and not ref_events:
-- ref_events is per-ref (the FK enforces ref existence). Worker
-- pass summaries ("dispatch claimed=3 ok=2 failed=0 in 145ms"),
-- worker startup events, connection errors — none of those scope
-- to a single ref naturally, and we'd be bending the table's
-- design to host them.
--
-- The Python-side handler (utils/db_log_handler.py) buffers
-- records in-process (flush every 5s or 50 records), uses a
-- dedicated psycopg connection in autocommit mode so logging
-- doesn't fight with the worker's main pool, and demotes to the
-- file handler on flush failure.
--
-- Column shape:
--   * ts: now() at log time. Server-side default; clients usually
--     don't override.
--   * host: PRECIS_HOST_NAME env or socket.gethostname() fallback.
--     Required so cross-host queries can WHERE host = 'caspar'.
--   * process: PRECIS_PROCESS env. Set by the LaunchDaemon plist
--     EnvironmentVariables ('precis-worker' / 'precis-worker-agent'
--     / 'precis-cron-tick' / asa-bot processes). NULL when unset
--     (interactive CLI, tests).
--   * pass: derived from the logger name when it matches
--     'precis.workers.<X>' → '<X>'. NULL otherwise (handler logs,
--     startup chatter).
--   * level: INFO / WARNING / ERROR / DEBUG. The standard
--     stdlib names.
--   * logger: the full dotted name (e.g. 'precis.workers.dispatch')
--     so operators can filter further if 'pass' isn't granular
--     enough.
--   * message: the rendered log line. Truncate happens client-side
--     to keep the row size bounded.
--   * payload: structured fields when the caller passes
--     extra={'payload': {...}}. BatchResult rows carry
--     {'claimed':N,'ok':M,'failed':K,'duration_ms':D,'cost_usd':C}.
--     Exception rows carry {'error_class':..., 'error_msg':...,
--     'traceback':...}. JSONB stays NULL when nothing structured.
--
-- Forward-only (ADR 0005).

BEGIN;

CREATE TABLE IF NOT EXISTS worker_logs (
    log_id   BIGSERIAL PRIMARY KEY,
    ts       TIMESTAMPTZ NOT NULL DEFAULT now(),
    host     TEXT NOT NULL,
    process  TEXT,
    pass     TEXT,
    level    TEXT NOT NULL,
    logger   TEXT,
    message  TEXT NOT NULL,
    payload  JSONB
);

-- ``WHERE host = X AND ts > now() - interval '1 hour'`` is the
-- most common query. (host, ts DESC) covers it; the planner walks
-- backward from the most recent row.
CREATE INDEX IF NOT EXISTS worker_logs_host_ts_idx
    ON worker_logs (host, ts DESC);

-- Per-pass filters. Partial index because ``pass IS NULL`` rows
-- (startup, handler chatter) aren't useful to filter by pass
-- and we don't want them bloating the btree.
CREATE INDEX IF NOT EXISTS worker_logs_pass_ts_idx
    ON worker_logs (pass, ts DESC) WHERE pass IS NOT NULL;

-- Error-only queries. Partial index keeps the surface small;
-- the index covers what the operator actually grep-walks.
CREATE INDEX IF NOT EXISTS worker_logs_level_ts_idx
    ON worker_logs (level, ts DESC) WHERE level IN ('WARNING', 'ERROR');

COMMIT;

-- End of 0015_worker_logs.sql
