-- 0333_host_heartbeat_log_gpu_cpu.sql
--
-- Host CPU and GPU utilisation samples on ``host_heartbeat_log`` —
-- gr592809's utilisation series, folded into the same beat that already
-- logs load1/5/15 and mem_used_pct. The capability probe already shells
-- out to nvidia-smi to size GPU seed slots (``_probe_gpu``), so per-card
-- utilisation and memory are one ``--query-gpu`` away; the beat records
-- the card mean so "are the GPUs still sitting idle?" (me538374:
-- one NEB seed keeps a GB10 16-37 % busy) is answerable fleet-wide.
--
-- Shape follows 0295: nullable ``DOUBLE PRECISION``, NULL = "not
-- collected this beat" (no nvidia-smi / platform the probe can't read /
-- per-card field unreadable). ``gpu_util_pct`` is the mean over the
-- host's cards' ``utilization.gpu`` — per-card detail stays on the
-- latest-snapshot ``host_heartbeat.meta.gpu_stats``.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- at release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

-- The runner sets lock_timeout = 0; bound the DDL wait behind a long
-- reader instead (convention from 0224/0226).
SET LOCAL lock_timeout = '5s';

ALTER TABLE host_heartbeat_log
    ADD COLUMN IF NOT EXISTS cpu_used_pct DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS gpu_util_pct DOUBLE PRECISION;

COMMENT ON COLUMN host_heartbeat_log.cpu_used_pct IS
    'Host CPU utilisation 0..100 at beat time, from /proc/stat deltas on
    Linux; NULL when unmeasured. The CPU half of the per-beat
    utilisation sample (gr592809).';

COMMENT ON COLUMN host_heartbeat_log.gpu_util_pct IS
    'Mean per-card GPU utilisation 0..100 at beat time (nvidia-smi
    utilization.gpu averaged over the host''s cards, NULL cards
    excluded); NULL when no card reported. The GPU half of the per-beat
    utilisation sample (gr592809).';

COMMIT;

-- End of 0333_host_heartbeat_log_gpu_cpu.sql
