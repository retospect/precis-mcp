-- 0297_tool_calls_result_tokens_est.sql
--
-- Per-call response-token estimate on the tool_calls ledger (gr604903,
-- quest qu604835's memory-ablation bench): the SQL-queryable half of the
-- per-call token/response-size logger. The plain-text ``mcp_call`` line
-- already carried ``response_tokens_est`` (memory-ablation-bench slice 1),
-- but only for MCP-surface calls and only greppable — the bench's own
-- in-process dispatches never emitted it, so tokens-to-first-gold could
-- not be confirmed from the system's own traces. This column records the
-- same estimate for EVERY dispatch (MCP, CLI, in-process) at the
-- dispatch chokepoint, so a window function over ``(session_id, seq)``
-- computes cumulative tokens per session directly:
--
--     SELECT session_id, seq, sum(result_tokens_est) OVER
--         (PARTITION BY session_id ORDER BY seq) AS tokens_so_far
--     FROM tool_calls WHERE session_id = '...';
--
-- Column:
--   * result_tokens_est: response-token estimate — result_bytes // 4,
--     the same estimator as the ``mcp_call`` log line, the scripted bench
--     harness (bench TOKEN_ESTIMATOR) and mesh_eval. A count, never
--     content.
--
-- Forward-only (ADR 0005). No backfill — pre-0297 rows keep NULL and an
-- ad-hoc pass can derive the same number as ``result_bytes // 4`` (the
-- bytes are on every row since 0209).

BEGIN;

ALTER TABLE tool_calls
    ADD COLUMN result_tokens_est INT;

COMMIT;
