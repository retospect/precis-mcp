-- 0210_tool_calls_client.sql
--
-- Caller attribution for the tool_calls ledger (gr484070): which MCP
-- client made the call, so the call_errors pass can report "models seen"
-- per failure cluster instead of guessing.
--
-- Column:
--   * client: the MCP client's own declaration —
--     clientInfo.name/version from the initialize handshake
--     (e.g. "claude-code/2.1.0"), plus " (model:<id>)" when the client
--     sent a model id in the initialize _meta. Exactly what the client
--     sent — never inferred. NULL for CLI / inproc / direct-dispatch
--     calls (those identify by agentlog_id / source already).
--
-- Forward-only (ADR 0005). No backfill — pre-0210 rows keep NULL and the
-- mining/filing passes report them as unattributed.

BEGIN;

ALTER TABLE tool_calls
    ADD COLUMN client TEXT;

COMMIT;
