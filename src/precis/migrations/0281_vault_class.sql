-- 0278_vault_class.sql
--
-- Vault name classes — multi-user overhaul S1, built directly in the S1b
-- shape (docs/backlog/multi-user-overhaul.md §Mechanisms "Vault name
-- classes"; Reto 2026-10-22: no pattern table lands).
--
-- Until now vault.reveal was granted to PUBLIC: every DSN holder (every worker,
-- the web app, any stdio session as agent_rw) could decrypt ANY secret —
-- including the HPC SSH key (slullama-hpc-placement.md D1), the Claude OAuth
-- token, per-user AnkiWeb passwords and mailbox passwords.
--
-- One column, vault.secrets.class, NOT NULL, one of:
--   infra  → session_user must be a member of precis_system (workers, agent
--            launchers): HPC key, Claude OAuth token, anki/mailbox passwords.
--            Agents never see these values (Reto Q5, 2026-10-21).
--   api    → member of agent_rw (dev-agent code may call paid APIs; Q5).
--   user   → member of precis_ingress AND the name is user/<p_user_id>/…,
--            p_user_id passed by the ingress daemon from its authenticated
--            principal — never read from a GUC.
-- Classed by name on every write (vault._class_for, a BEFORE trigger): the
-- prefixes svc/infra/, svc/api/, user/<id>/, then the explicit legacy infra
-- list below, else api — today's reach for the daemons' roles, minus agent_ro.
-- Names are not renamed; reveal and the writes (set_secret/delete_secret,
-- below) are gated by class. No unclassed state exists,
-- so there is no default-allow branch.
--
-- session_user, not current_user: the connecting role is who the class binds
-- (SET LOCAL ROLE never widens or narrows it). A gate whose role does not
-- exist on this install (single-login setups, the test template) passes —
-- there is no tier to enforce. Denied reveals raise; they cannot write an
-- audit row (the exception would roll it back).
--
-- Forward-only (ADR 0005). Idempotent. Regenerate baseline at release (ADR 0031).

BEGIN;

-- ── class of a name ───────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION vault._class_for(p_name text) RETURNS text
    LANGUAGE sql IMMUTABLE SET search_path = vault, public, pg_temp AS $$
    SELECT CASE
        WHEN p_name LIKE 'svc/infra/%'            THEN 'infra'
        WHEN p_name LIKE 'svc/api/%'              THEN 'api'
        WHEN p_name ~ '^user/[0-9]+/.'            THEN 'user'
        -- legacy names (prod inventory 2026-10-22), classed infra:
        WHEN p_name = 'CLAUDE_CODE_OAUTH_TOKEN'   THEN 'infra'  -- claude -p OAuth
        WHEN p_name LIKE 'MELUXINA_SSH\_%'        THEN 'infra'  -- HPC key (D1)
        WHEN p_name LIKE 'ANKI_PASSWORD:%'        THEN 'infra'  -- AnkiWeb passwords
        WHEN p_name LIKE 'email.%.password'       THEN 'infra'  -- mailbox passwords
        ELSE 'api'
    END;
$$;

ALTER TABLE vault.secrets ADD COLUMN IF NOT EXISTS class text;
UPDATE vault.secrets SET class = vault._class_for(name)
 WHERE class IS DISTINCT FROM vault._class_for(name);
ALTER TABLE vault.secrets ALTER COLUMN class SET NOT NULL;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'vault_secrets_class_check'
                      AND conrelid = 'vault.secrets'::regclass) THEN
        ALTER TABLE vault.secrets ADD CONSTRAINT vault_secrets_class_check
            CHECK (class IN ('infra', 'api', 'user'));
    END IF;
END
$$;

-- Every write re-derives the class from the name: no caller can store an
-- infra name under a weaker class.
CREATE OR REPLACE FUNCTION vault._stamp_class() RETURNS trigger
    LANGUAGE plpgsql SET search_path = vault, public, pg_temp AS $$
BEGIN
    NEW.class := vault._class_for(NEW.name);
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS vault_secrets_stamp_class ON vault.secrets;
CREATE TRIGGER vault_secrets_stamp_class
    BEFORE INSERT OR UPDATE ON vault.secrets
    FOR EACH ROW EXECUTE FUNCTION vault._stamp_class();

-- ── the gate ──────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION vault._class_check(p_name text, p_class text,
                                              p_user_id bigint)
    RETURNS void
    LANGUAGE plpgsql STABLE SECURITY DEFINER
    SET search_path = vault, public, pg_temp AS $$
DECLARE gate text;
BEGIN
    gate := CASE p_class WHEN 'infra' THEN 'precis_system'
                         WHEN 'api'   THEN 'agent_rw'
                         WHEN 'user'  THEN 'precis_ingress' END;
    IF gate IS NULL THEN
        RAISE EXCEPTION 'vault: "%" has unknown class %', p_name, p_class;
    END IF;
    IF to_regrole(gate) IS NOT NULL
       AND NOT pg_has_role(session_user, gate, 'MEMBER') THEN
        RAISE EXCEPTION 'vault: role "%" is not allowed to reveal "%" (class %)',
            session_user, p_name, p_class
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF p_class = 'user'
       AND (p_user_id IS NULL OR p_name NOT LIKE 'user/' || p_user_id || '/%') THEN
        RAISE EXCEPTION 'vault: "%" is not a secret of user %', p_name,
            coalesce(p_user_id::text, '(none)')
            USING ERRCODE = 'insufficient_privilege';
    END IF;
END
$$;

-- ── the write gate (Reto 2026-10-22, db-own finding 2) ───────────────────
--
-- set_secret/delete_secret were ungated: any agent_rw shell could overwrite
-- the Claude OAuth token or the HPC key with its own value, or delete them.
-- Writers by the class of the name being written:
--   infra  → precis_system or precis_ingress (the web daemon: /account
--            stores a user's AnkiWeb password, the admin vault editor is
--            write-only for every name — multi-user-overhaul.md §roles)
--   api    → member of agent_rw
--   user   → precis_ingress (no p_user_id on the write side yet; S1b)
-- The owner of vault.secrets passes (it owns the rows anyway): precis_owner
-- after db-own, so operator writes of infra names (`precis secret set`,
-- `precis email add`) go on the owner DSN; before the db-own cutover agent_rw
-- is that owner and nothing changes. None of the writer roles existing on
-- this install → pass, as for reveal.

CREATE OR REPLACE FUNCTION vault._write_check(p_name text) RETURNS void
    LANGUAGE plpgsql STABLE SECURITY DEFINER
    SET search_path = vault, public, pg_temp AS $$
DECLARE
    c       text := vault._class_for(p_name);
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

CREATE OR REPLACE FUNCTION vault.set_secret(p_name text, p_value text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = vault, public, pg_temp AS $$
BEGIN
    PERFORM vault._write_check(p_name);
    IF p_value IS NULL OR length(p_value) = 0 THEN
        RAISE EXCEPTION 'vault: refusing to store an empty value for %', p_name;
    END IF;
    INSERT INTO vault.secrets(name, ciphertext, hint)
    VALUES (p_name,
            pgp_sym_encrypt(p_value, vault._key()),
            vault._hint(p_value))
    ON CONFLICT (name) DO UPDATE
        SET ciphertext = EXCLUDED.ciphertext,
            hint       = EXCLUDED.hint,
            updated_at = now();
    INSERT INTO vault.events(who, verb, name) VALUES (session_user, 'set', p_name);
END
$$;

CREATE OR REPLACE FUNCTION vault.delete_secret(p_name text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = vault, public, pg_temp AS $$
BEGIN
    PERFORM vault._write_check(p_name);
    DELETE FROM vault.secrets WHERE name = p_name;
    INSERT INTO vault.events(who, verb, name) VALUES (session_user, 'delete', p_name);
END
$$;

-- ── reveal: (p_name, p_user_id) core with client identity; old overloads
-- delegate with p_user_id NULL so every existing caller keeps working ─────

CREATE OR REPLACE FUNCTION vault.reveal(
    p_name    text,
    p_user_id bigint,
    p_host    text,
    p_os_user text,
    p_pid     integer,
    p_ppid    integer,
    p_process text
) RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = vault, public, pg_temp AS $$
DECLARE
    c text;
    v text;
BEGIN
    SELECT s.class INTO c FROM vault.secrets s WHERE s.name = p_name;
    IF c IS NULL THEN
        RETURN NULL;
    END IF;
    PERFORM vault._class_check(p_name, c, p_user_id);
    SELECT pgp_sym_decrypt(s.ciphertext, vault._key())
        INTO v FROM vault.secrets s WHERE s.name = p_name;
    INSERT INTO vault.events(who, verb, name, host, os_user, pid, ppid, process)
    VALUES (session_user, 'reveal', p_name,
            p_host, p_os_user, p_pid, p_ppid, p_process);
    RETURN v;
END
$$;

CREATE OR REPLACE FUNCTION vault.reveal(p_name text, p_user_id bigint) RETURNS text
    LANGUAGE sql SECURITY DEFINER SET search_path = vault, public, pg_temp AS $$
    SELECT vault.reveal(p_name, p_user_id, NULL::text, NULL::text,
                        NULL::integer, NULL::integer, NULL::text);
$$;

CREATE OR REPLACE FUNCTION vault.reveal(
    p_name    text,
    p_host    text,
    p_os_user text,
    p_pid     integer,
    p_ppid    integer,
    p_process text
) RETURNS text
    LANGUAGE sql SECURITY DEFINER SET search_path = vault, public, pg_temp AS $$
    SELECT vault.reveal(p_name, NULL::bigint, p_host, p_os_user,
                        p_pid, p_ppid, p_process);
$$;

CREATE OR REPLACE FUNCTION vault.reveal(p_name text) RETURNS text
    LANGUAGE sql SECURITY DEFINER SET search_path = vault, public, pg_temp AS $$
    SELECT vault.reveal(p_name, NULL::bigint, NULL::text, NULL::text,
                        NULL::integer, NULL::integer, NULL::text);
$$;

-- The gate and the class helpers are internal. The two old reveal overloads
-- keep the ACL 0257 gave them (CREATE OR REPLACE preserves it: agent_rw
-- only); the two new ones get the same, not PUBLIC — the class gate decides
-- *which names*, EXECUTE decides *who may ask at all* (gr523021). precis_system
-- and precis_ingress are agent_rw members, so they inherit it. The grant is
-- role-guarded like 0257's: a fresh dev/test DB has no agent_rw.
REVOKE ALL ON FUNCTION vault._class_for(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION vault._class_check(text, text, bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION vault._stamp_class() FROM PUBLIC;
REVOKE ALL ON FUNCTION vault._write_check(text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION
    vault.reveal(text, bigint),
    vault.reveal(text, bigint, text, text, integer, integer, text)
    FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_rw') THEN
        EXECUTE 'GRANT EXECUTE ON FUNCTION
            vault.reveal(text, bigint),
            vault.reveal(text, bigint, text, text, integer, integer, text)
            TO agent_rw';
    END IF;
END
$$;

COMMIT;
