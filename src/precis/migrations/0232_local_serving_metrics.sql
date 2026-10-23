-- 0232_local_serving_metrics.sql
--
-- llm-lane-scheduler slice S0 (shared with vllm-per-node-serving slice 3 —
-- one scrape, both consumers): per-beat local-serving pressure signals on
-- ``host_heartbeat_log``. The heartbeat pass scrapes the serving host's
-- SGLang ``/metrics`` (deploy/roles/sglang ``--enable-metrics``) and reads
-- ``/proc/vmstat`` ``pswpin`` each beat; the ``local-serving-pressure``
-- condition and later the S3 capacity controller read the series.
--
-- Shape follows 0113: nullable ``DOUBLE PRECISION`` columns, NULL = "not
-- collected this beat" (a host with no serving endpoint, a non-Linux box
-- for swap). Gauges as ratios/counts; retractions and swap-in as
-- MONOTONIC totals — rollups take deltas (a counter reset reads as 0
-- delta, never negative).
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot at
-- release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

-- The runner sets lock_timeout = 0; bound the DDL wait behind a long
-- reader instead (convention from 0224/0226).
SET LOCAL lock_timeout = '5s';

ALTER TABLE host_heartbeat_log
    ADD COLUMN IF NOT EXISTS serving_running           DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS serving_queued            DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS serving_kv_usage          DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS serving_retractions_total DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS swap_in_pages_total       DOUBLE PRECISION;

COMMENT ON COLUMN host_heartbeat_log.serving_running IS
    'SGLang sglang:num_running_reqs (vLLM num_requests_running) at scrape
    time; NULL when no serving endpoint answered.';

COMMENT ON COLUMN host_heartbeat_log.serving_queued IS
    'SGLang sglang:num_queue_reqs (vLLM num_requests_waiting) at scrape
    time; NULL when no serving endpoint answered.';

COMMENT ON COLUMN host_heartbeat_log.serving_kv_usage IS
    'KV cache pool utilisation 0..1 (SGLang sglang:token_usage, vLLM
    gpu_cache_usage_perc); NULL when no serving endpoint answered.';

COMMENT ON COLUMN host_heartbeat_log.serving_retractions_total IS
    'Monotonic requests-retracted counter (SGLang
    sglang:num_retracted_requests_total, vLLM num_preemptions_total);
    deltas are the signal — resets read as zero.';

COMMENT ON COLUMN host_heartbeat_log.swap_in_pages_total IS
    'Monotonic /proc/vmstat pswpin (pages swapped in since boot). On
    unified-memory serving boxes host swap-in IS model pressure. Linux
    only; NULL elsewhere.';

COMMIT;

-- End of 0232_local_serving_metrics.sql
