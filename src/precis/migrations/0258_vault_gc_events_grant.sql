-- 0258_vault_gc_events_grant.sql
--
-- Close the last PUBLIC EXECUTE hole on a vault write path (gr523021 c4).
--
-- 0257 revoked PUBLIC on the vault verbs but kept vault.gc_events PUBLIC.
-- gc_events is SECURITY DEFINER and DELETEs vault.events rows older than
-- p_keep_days, so any connect-capable role — agent_ro included — could run
-- SELECT vault.gc_events(0) and erase the reveal/set/delete audit trail,
-- the record of who read which secret. Verified on prod 2026-10-27 (proacl
-- still held =X/agent_rw).
--
-- The only caller is the sweeper's _gc_vault_events, which connects as
-- agent_rw like every worker, so the grant moves to agent_rw alone. The
-- function also gets a 30-day retention floor: no legitimate prune needs a
-- shorter window, and the floor keeps one call from wiping recent history
-- even from a role that keeps the grant.
--
-- The GRANT is pg_roles-guarded for the same reason as 0257: agent_rw is
-- provisioned out-of-tree, and a fresh dev/test DB has no such role.
--
-- Forward-only (ADR 0005). Idempotent (CREATE OR REPLACE, REVOKE, GRANT).
-- Regenerate the baseline snapshot at release time (ADR 0031).

BEGIN;

CREATE OR REPLACE FUNCTION vault.gc_events(p_keep_days integer DEFAULT 180)
    RETURNS bigint
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = vault, public, pg_temp AS $$
DECLARE n bigint;
BEGIN
    DELETE FROM vault.events
     WHERE at < now() - make_interval(days => GREATEST(coalesce(p_keep_days, 180), 30));
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END
$$;

REVOKE EXECUTE ON FUNCTION vault.gc_events(integer) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_rw') THEN
        EXECUTE 'GRANT EXECUTE ON FUNCTION vault.gc_events(integer) TO agent_rw';
    END IF;
END
$$;

COMMIT;
