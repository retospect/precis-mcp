-- 0113_host_heartbeat_log.sql
--
-- Append-only CPU/sensor history — the time-series companion 0017 reserved.
--
-- ``host_heartbeat`` is latest-snapshot-per-host (UPSERT on ``host`` PK), so
-- every 60s beat collects load1/5/15 + temp fleet-wide and then throws the
-- previous reading away. That makes "how high was CPU utilization throughout
-- the day / was the cluster always hot?" unanswerable retroactively — the
-- LLM half reconstructs from ``llm_call_log``, the CPU half had no log.
--
-- Shape: one narrow row per beat, no ``meta`` (top_cpu etc. churn and stay
-- snapshot-only on ``host_heartbeat``). The writer
-- (``workers/heartbeat.py::_collect_and_upsert``) INSERTs alongside the
-- snapshot UPSERT and prunes rows older than
-- ``PRECIS_HEARTBEAT_HISTORY_DAYS`` (default 14) — at 4 hosts x 1/min x 14
-- days that is ~80k rows, so no partitioning needed. Read by
-- ``precis stats --utilization`` (hourly rollups).
--
-- One btree on (ts): serves the prune (``ts < cutoff``) directly; the hourly
-- rollup groups the whole retention window anyway, so a (host, ts) index
-- would buy nothing.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot at
-- release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

CREATE TABLE IF NOT EXISTS host_heartbeat_log (
    host    TEXT NOT NULL,
    ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
    temp_c  DOUBLE PRECISION,
    load1   DOUBLE PRECISION,
    load5   DOUBLE PRECISION,
    load15  DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS host_heartbeat_log_ts_idx
    ON host_heartbeat_log (ts);

COMMENT ON TABLE host_heartbeat_log IS
    'Append-only per-beat sensor history (load + temp). Written by the '
    'heartbeat pass alongside the host_heartbeat snapshot UPSERT; pruned '
    'to PRECIS_HEARTBEAT_HISTORY_DAYS. Read by precis stats --utilization.';

COMMIT;

-- End of 0113_host_heartbeat_log.sql
