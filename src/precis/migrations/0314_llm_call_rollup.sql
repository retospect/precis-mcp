-- 0314_llm_call_rollup.sql
--
-- gr575088 (db-thrash review): llm_call_log at ~9.5M rows / 5.5 GB took
-- ~23 near-full seq scans a day — the status page's per-source rollup
-- grouped the whole table per poll — and the table had no aggregate
-- preservation ahead of route_log.gc's 90-day retention, so every
-- historical rollup question had to rescan raw rows. This is the
-- rollup half of docs/backlog/llm-cost-accounting.md §llm_call_log
-- rollup and retention policy: one row per
-- (hour, source, tier, transport, model, placement) carrying the call
-- counts and spend/volume sums the operational reads consume.
--
-- route_log.rollup() advances an app_state watermark, aggregating
-- completed hours oldest-first ahead of the GC horizon; the web status
-- reads history from this table and only the trailing 7 days from the
-- raw log.
--
-- Dimension columns mirror the raw table's nullability — a NULL tier /
-- transport / model / source / placement is a legitimate bucket, so the
-- upsert key uses NULLS NOT DISTINCT (same trick as
-- links_endpoints_relation_idx, 0001) to let a replayed hour replace
-- rather than duplicate. Gate-out rows (features.gated, gr516941) are
-- excluded at rollup time to match the live reads: a paused attempt is
-- neither activity nor spend.
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

CREATE TABLE IF NOT EXISTS llm_call_rollup (
    hour        timestamptz NOT NULL,
    source      text,
    tier        text,
    transport   text,
    model       text,
    placement   text,
    calls       bigint NOT NULL,
    errored     bigint NOT NULL,
    cost_usd    double precision,
    billed_usd  numeric,
    req_chars   bigint NOT NULL,
    resp_chars  bigint NOT NULL,
    wall_ms     bigint NOT NULL,
    turns_used  bigint NOT NULL,
    input_tokens            bigint NOT NULL,
    output_tokens           bigint NOT NULL,
    cache_read_tokens       bigint NOT NULL,
    cache_creation_tokens   bigint NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS llm_call_rollup_key
    ON llm_call_rollup (hour, source, tier, transport, model, placement)
    NULLS NOT DISTINCT;
CREATE INDEX IF NOT EXISTS llm_call_rollup_source_idx
    ON llm_call_rollup (source, hour);

COMMIT;

-- End of 0314_llm_call_rollup.sql
