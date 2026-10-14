-- 0209_tool_calls_session_seq_sample.sql
--
-- Widen the tool_calls ledger (0133) so session mining can order and
-- correlate calls by the *actual* session, not a synthesized day bucket
-- (mcp-introspection gap closures, Reto 2026-10-14 ruling: the ledger
-- includes arg_sample). Before this, every mined "session" was the whole
-- fleet's calls for a calendar day — 26 unrelated jobs interleaved read
-- as one agent reformulating — and the detectors had no call-level
-- payload signal at all (arg names only, no outcome bytes).
--
-- Columns:
--   * session_id: opaque per-MCP-session key minted at serve_ledger bind
--     (hostname-pid-uuid). NULL for CLI / inproc / direct-dispatch calls —
--     those correlate by agentlog_id, which is already a column.
--   * seq: per-session_id call ordinal (in-process counter).
--   * result_bytes: byte length of the rendered response body — the
--     "did this call actually return anything" signal the payload-free
--     ledger lacked.
--   * error_head: first line of the rendered error body, whitespace-
--     collapsed, masked, bounded. The D1-style failure-family signature
--     without an err payload.
--   * arg_sample: bounded JSONB of the call's scalar args — strings
--     masked + truncated, credential-shaped keys dropped, payload keys
--     (text=/body=) summarized by length. Still "no raw payloads": the
--     sample exists to identify the arg SHAPE that confused an agent.
--
-- Index: (session_id, seq) is the session mining ORDER BY.
--
-- Forward-only (ADR 0005). No backfill — pre-0209 rows keep NULLs and
-- mining falls back to the day-bucket session for them.

BEGIN;

ALTER TABLE tool_calls
    ADD COLUMN session_id   TEXT,
    ADD COLUMN seq          INT,
    ADD COLUMN result_bytes INT,
    ADD COLUMN error_head   TEXT,
    ADD COLUMN arg_sample   JSONB;

CREATE INDEX IF NOT EXISTS tool_calls_session_seq_idx
    ON tool_calls (session_id, seq);

COMMIT;
