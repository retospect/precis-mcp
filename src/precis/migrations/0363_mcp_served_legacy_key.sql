-- 0363_mcp_served_legacy_key.sql
--
-- gr764909: drop the legacy single ``release.mcp_served`` app_settings
-- row. Pre-gr762430 session-MCP servers wrote one last-writer record
-- there; the rollout read it as one more instance, the shape that let a
-- stray laptop stack mask the live servers. Every stamping server now
-- writes ``release.mcp_served.<host:pid>`` and no reader consults the
-- bare key. Only the exact key goes — the per-instance rows under the
-- ``release.mcp_served.`` prefix are live data and stay. The delete is
-- audited like settings.clear_setting's (old_value kept, new_value
-- NULL). Forward-only (ADR 0005). Idempotent.

BEGIN;

INSERT INTO app_settings_audit (key, old_value, new_value, actor, reason)
SELECT key, value, NULL, 'migration 0363', 'legacy mcp_served key removed (gr764909)'
FROM app_settings
WHERE key = 'release.mcp_served';

DELETE FROM app_settings WHERE key = 'release.mcp_served';

COMMIT;

-- End of 0363_mcp_served_legacy_key.sql
