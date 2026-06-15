-- 0017_host_heartbeat.sql
--
-- Per-host liveness + sensor snapshot for the web Status tab.
--
-- Background: the Status tab can already infer "which machines are
-- alive" from ``worker_logs`` (last log line per host) and roll up
-- Claude spend from ``ref_events.cost_usd``. What it cannot show is
-- a machine's *physical* state — CPU temperature and load — because
-- nothing in the tree collects it. This table is the sink for a
-- lightweight reporter (``precis heartbeat``) that each host runs on
-- a timer (launchd / systemd / cron).
--
-- Shape: latest-snapshot-per-host, NOT a time series. ``host`` is the
-- primary key and the reporter UPSERTs, so the table stays one row
-- per machine — enough to answer "is caspar hot right now?" without
-- a retention policy. A future time-series (for graphs) would be a
-- separate append-only table; this one deliberately stays small.
--
-- All sensor columns are NULLABLE: ``temp_c`` in particular is
-- best-effort (trivial to read on Linux via /sys/class/thermal, hard
-- on macOS without sudo), so a host that can't read its temperature
-- still reports load + liveness with ``temp_c IS NULL``.

BEGIN;

CREATE TABLE IF NOT EXISTS host_heartbeat (
    host    TEXT PRIMARY KEY,
    ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
    temp_c  DOUBLE PRECISION,
    load1   DOUBLE PRECISION,
    load5   DOUBLE PRECISION,
    load15  DOUBLE PRECISION,
    meta    JSONB
);

COMMIT;

-- End of 0017_host_heartbeat.sql
