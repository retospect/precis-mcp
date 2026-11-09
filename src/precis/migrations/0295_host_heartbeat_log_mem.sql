-- 0295_host_heartbeat_log_mem.sql
--
-- Host memory utilisation sample on ``host_heartbeat_log`` — gr592932's
-- host utilisation folded into the same beat that already logs
-- load1/5/15 (and gr595536's footprint-admission headroom series:
-- cluster-scheduling.md Pillar 3b admits a job whose predicted peak RSS
-- fits free memory − ~10% reserve; this column is what "free" has been
-- doing over time).
--
-- Shape follows 0232: nullable ``DOUBLE PRECISION``, NULL = "not
-- collected this beat" (a platform the probe can't read). The writer
-- (``workers/heartbeat.py::_collect_and_upsert``) computes
-- 100*(1 − MemAvailable/MemTotal) from ``/proc/meminfo`` on Linux, or
-- 100 − ``memory_pressure``'s free percentage on macOS.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- at release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

-- The runner sets lock_timeout = 0; bound the DDL wait behind a long
-- reader instead (convention from 0224/0226).
SET LOCAL lock_timeout = '5s';

ALTER TABLE host_heartbeat_log
    ADD COLUMN IF NOT EXISTS mem_used_pct DOUBLE PRECISION;

COMMENT ON COLUMN host_heartbeat_log.mem_used_pct IS
    'Host memory utilisation 0..100 at beat time: 100*(1 -
    MemAvailable/MemTotal) on Linux, 100 - memory_pressure "free
    percentage" on macOS; NULL when unmeasured. The per-beat headroom
    series for footprint admission (gr595536).';

COMMIT;

-- End of 0295_host_heartbeat_log_mem.sql
