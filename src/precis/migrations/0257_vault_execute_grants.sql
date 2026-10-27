-- 0257_vault_execute_grants.sql
--
-- Close the PUBLIC EXECUTE hole on the vault verbs (gr523021).
--
-- 0059 granted vault.reveal / set_secret / delete_secret TO PUBLIC under ADR
-- 0055 v1 ("anyone who can connect is trusted" — one shared role then); 0111
-- and 0248 kept the pattern for the newer overloads. Since 0079 the cluster
-- has agent_ro — the role behind prod-psql --ro, the read-only sandbox MCP
-- and every write:none envelope — and PUBLIC covers it too: a pure SELECT,
-- which every --ro guard allows, could reveal plaintext
-- (SELECT vault.reveal('<name>')) or write the vault
-- (SELECT vault.delete_secret('<name>')). The DSN-is-the-boundary argument
-- died when agent_ro was provisioned; this restores it.
--
-- Every legitimate vault caller connects as agent_rw (session MCP, workers,
-- precis_web, asa_bot, operator CLIs — the deploy DSN templates render no
-- other role; inventory on the gripe). That grant stays SHARED-WITH-AGENTS:
-- agent sessions reach the vault through the MCP's own agent_rw login, same
-- as today — splitting worker from agent identity is the pending db-own
-- cutover's job, not this migration's. agent_ro gets nothing: no caller
-- needs it, and _reveal already degrades to the env/file fallback on any
-- vault error, so ro lanes lose the vault leg cleanly.
--
-- The GRANT is a pg_roles-guarded DO block: agent_rw is provisioned
-- out-of-tree (ansible), so a fresh dev/test DB has no such role to grant
-- to — a bare GRANT would abort the migration there. The guard shape
-- follows deploy/roles/postgres/tasks/main.yml.
--
-- Same treatment for three sibling SECURITY DEFINER functions agent_ro has
-- no use for (all trigger-internal or migration/owner-maintenance paths):
-- refs_retire_event_trail fires only on a refs write agent_ro can't make;
-- precis_kinds_refresh_guarded is PERFORMed by the kinds triggers — but
-- those trigger functions are SECURITY INVOKER, so EXECUTE is checked
-- against whoever writes kinds (agent_rw's boot upsert) and it keeps its
-- grant; precis_revision_triggers_refresh runs inside the definer wrapper
-- and from migrations/tests as the owner. bump_salience is NOT touched: it
-- is 0137's deliberate agent_ro carve-out (ro lanes heat salience on every
-- read). vault.list / vault.mask / vault.gc_events stay PUBLIC as decided.
--
-- Forward-only (ADR 0005). Idempotent (REVOKE/GRANT are). Regenerate the
-- baseline snapshot at release time (ADR 0031).

BEGIN;

REVOKE EXECUTE ON FUNCTION
    vault.reveal(text),
    vault.reveal(text, text, text, integer, integer, text),
    vault.set_secret(text, text),
    vault.set_secret_if_absent(text, text),
    vault.delete_secret(text)
    FROM PUBLIC;

REVOKE EXECUTE ON FUNCTION
    public.refs_retire_event_trail(),
    public.precis_revision_triggers_refresh(),
    public.precis_kinds_refresh_guarded()
    FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_rw') THEN
        EXECUTE 'GRANT EXECUTE ON FUNCTION
            vault.reveal(text),
            vault.reveal(text, text, text, integer, integer, text),
            vault.set_secret(text, text),
            vault.set_secret_if_absent(text, text),
            vault.delete_secret(text)
            TO agent_rw';
        EXECUTE 'GRANT EXECUTE ON FUNCTION
            public.refs_retire_event_trail(),
            public.precis_revision_triggers_refresh(),
            public.precis_kinds_refresh_guarded()
            TO agent_rw';
    END IF;
END
$$;

COMMIT;
