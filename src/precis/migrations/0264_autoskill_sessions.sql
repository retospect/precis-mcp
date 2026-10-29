-- 0264_autoskill_sessions.sql
--
-- Router-side warm start from similar sessions (docs/backlog/autoskill.md).
-- Every tool-using ``router.route`` call is embedded once; the router
-- looks up the nearest past routed sessions here and injects the skills
-- and memories those sessions actually used. One row per routed call:
-- the query embedding, what was injected (or that the call was a
-- hold-out), and -- snapshotted when the call returns -- what the
-- session used (``used`` is derived from ``tool_calls``, which keeps only
-- 30 days; this table keeps 60).
--
-- Operational telemetry, not corpus: no chunks, no FK to refs (a memory
-- id in ``injected``/``used`` may outlive its ref; titles are read live
-- at injection time). ``agentlog_id`` joins ``tool_calls`` and, with
-- ``ts``, ``llm_call_log`` for the measurement queries in the spec.
-- ``used IS NULL`` = no attribution (no agentlog id on that transport).
--
-- Retention (60 days) is pruned by ``route_log.gc``, batched on the ts
-- b-tree.
--
-- Forward-only (ADR 0005). Idempotent (IF NOT EXISTS). No backfill: the
-- history builds itself from deploy.

BEGIN;

CREATE TABLE IF NOT EXISTS autoskill_sessions (
    id bigserial PRIMARY KEY,
    ts timestamptz NOT NULL DEFAULT now(),
    source text NOT NULL DEFAULT '',
    agentlog_id bigint,
    embedding vector(1024) NOT NULL,
    holdout boolean NOT NULL DEFAULT false,
    injected jsonb NOT NULL,
    used jsonb,
    errored boolean
);

COMMENT ON TABLE autoskill_sessions IS
    'Autoskill history (docs/backlog/autoskill.md): one row per tool-using '
    'router.route call -- query embedding, injected core/tail/memories, '
    'hold-out flag, and the skills/memories the session actually used '
    '(snapshot from tool_calls; NULL = no attribution). 60-day retention.';

COMMENT ON COLUMN autoskill_sessions.injected IS
    '{"core":[slug],"tail":[slug],"memories":[id],"fallback":slug|null}';

COMMENT ON COLUMN autoskill_sessions.used IS
    '{"skills":[slug],"memories":[id]} derived from tool_calls at call end; '
    'NULL = no attribution.';

CREATE INDEX IF NOT EXISTS autoskill_sessions_embedding_hnsw
    ON autoskill_sessions USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS autoskill_sessions_ts_idx
    ON autoskill_sessions (ts);

COMMIT;

-- End of 0264_autoskill_sessions.sql
