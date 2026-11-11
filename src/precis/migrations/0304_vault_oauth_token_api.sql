-- 0304_vault_oauth_token_api.sql
--
-- CLAUDE_CODE_OAUTH_TOKEN moves from class infra to class api (Reto
-- 2026-11-11, reversing the 0285 placement for this one name).
--
-- 0285 classed the token infra so only precis_system could reveal it ("Agents
-- never see these values", Reto Q5). The session MCP runs as agent_rw and
-- calls claude -p in-process for the finding mint door's dedup judge
-- (taproot.canon.dedup_judge -> router MEDIUM -> claude_p), so the vault
-- refused it and every put(kind='finding') failed with "role agent_rw is not
-- allowed to reveal CLAUDE_CODE_OAUTH_TOKEN (class infra)". The alternative
-- was a host file outside the vault (hand-rotated second copy, same reach);
-- the token belongs in the vault, so the class moves.
--
-- Consequence: any agent_rw holder can reveal AND overwrite the token (the api
-- write gate is agent_rw). The other infra names (HPC key, AnkiWeb and mailbox
-- passwords) keep their class. gr610977 tracks moving the judge to a
-- credentialed worker, after which the name can return to infra.
--
-- The BEFORE INSERT/UPDATE trigger re-derives class from the name, so the
-- UPDATE below only has to touch the row. Forward-only (ADR 0005). Idempotent.

BEGIN;

CREATE OR REPLACE FUNCTION vault._class_for(p_name text) RETURNS text
    LANGUAGE sql IMMUTABLE SET search_path = vault, public, pg_temp AS $$
    SELECT CASE
        WHEN p_name LIKE 'svc/infra/%'            THEN 'infra'
        WHEN p_name LIKE 'svc/api/%'              THEN 'api'
        WHEN p_name ~ '^user/[0-9]+/.'            THEN 'user'
        -- legacy names (prod inventory 2026-10-22), classed infra.
        -- CLAUDE_CODE_OAUTH_TOKEN left this list in 0304 (falls to api).
        WHEN p_name LIKE 'MELUXINA_SSH\_%'        THEN 'infra'  -- HPC key (D1)
        WHEN p_name LIKE 'ANKI_PASSWORD:%'        THEN 'infra'  -- AnkiWeb passwords
        WHEN p_name LIKE 'email.%.password'       THEN 'infra'  -- mailbox passwords
        ELSE 'api'
    END;
$$;

UPDATE vault.secrets SET class = vault._class_for(name)
 WHERE name = 'CLAUDE_CODE_OAUTH_TOKEN'
   AND class IS DISTINCT FROM vault._class_for(name);

COMMIT;
