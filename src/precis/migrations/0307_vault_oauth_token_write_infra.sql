-- 0307_vault_oauth_token_write_infra.sql
--
-- CLAUDE_CODE_OAUTH_TOKEN: agent_rw may READ it, never WRITE it (Reto
-- 2026-11-11). 0304 moved the name to class api so the session MCP (agent_rw)
-- can reveal it and run claude -p; but the api write gate is agent_rw, so any
-- agent_rw holder could also overwrite or delete the token. Reveal keeps the
-- stored class (api); writes now gate on a separate write class, infra for
-- this name: precis_system, precis_ingress, or the owner DSN (operator
-- rotation via `precis secret set` on the owner DSN keeps working).
--
-- vault._write_class_for is the write-side class; every other name maps to
-- its read class unchanged. Forward-only (ADR 0005). Idempotent.

BEGIN;

CREATE OR REPLACE FUNCTION vault._write_class_for(p_name text) RETURNS text
    LANGUAGE sql IMMUTABLE SET search_path = vault, public, pg_temp AS $$
    SELECT CASE
        WHEN p_name = 'CLAUDE_CODE_OAUTH_TOKEN' THEN 'infra'  -- read api, write infra
        ELSE vault._class_for(p_name)
    END;
$$;

CREATE OR REPLACE FUNCTION vault._write_check(p_name text) RETURNS void
    LANGUAGE plpgsql STABLE SECURITY DEFINER
    SET search_path = vault, public, pg_temp AS $$
DECLARE
    c       text := vault._write_class_for(p_name);
    writers text[];
BEGIN
    IF pg_has_role(session_user,
                   (SELECT relowner FROM pg_class
                     WHERE oid = 'vault.secrets'::regclass), 'MEMBER') THEN
        RETURN;
    END IF;
    SELECT array_agg(w) INTO writers
      FROM unnest(CASE c WHEN 'infra' THEN ARRAY['precis_system', 'precis_ingress']
                         WHEN 'api'   THEN ARRAY['agent_rw']
                         WHEN 'user'  THEN ARRAY['precis_ingress'] END) AS w
     WHERE to_regrole(w) IS NOT NULL;
    IF writers IS NULL THEN
        RETURN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM unnest(writers) AS w
                    WHERE pg_has_role(session_user, w, 'MEMBER')) THEN
        RAISE EXCEPTION 'vault: role "%" is not allowed to write "%" (class %); '
                        'writers: %, or the owner DSN', session_user, p_name, c,
                        array_to_string(writers, ', ')
            USING ERRCODE = 'insufficient_privilege';
    END IF;
END
$$;

REVOKE ALL ON FUNCTION vault._write_class_for(text) FROM PUBLIC;

COMMIT;
