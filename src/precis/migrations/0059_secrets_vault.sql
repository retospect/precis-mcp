-- 0059_secrets_vault.sql
--
-- Secrets vault (ADR 0055, design docs/design/secrets-vault.md).
--
-- Move leaf secrets (API keys, OAuth tokens) out of env / ~/.secrets/pw and
-- launchd plists into an encrypted DB table. Values are pgcrypto-encrypted;
-- the passphrase lives in server config (`ALTER SYSTEM SET app.secret_key`) —
-- a file pg_dump never emits — so a logical pg_dump is safe to share
-- (ciphertext + function source that only *names* the key).
--
-- Trust model (v1, deliberately simple — see ADR 0055 "Roles deferred"):
-- the DB connection itself is the boundary. Anyone who can connect (holds the
-- DSN password) may read secrets; the DSN is a process parameter scrubbed from
-- subprocess env at boot, and in-process code is ours. So we do NOT add
-- per-service roles yet — that only adds complexity for minimal gain while
-- everything already shares one role. The role split (precis_secrets /
-- precis_web / asa + per-name ACL) is designed in the ADR and can be layered
-- on later without reworking callers.
--
-- What locks it down even without roles: the tables are REVOKEd from PUBLIC and
-- reachable ONLY through SECURITY DEFINER functions, so the sole decrypt path is
-- vault.reveal() — which writes an audit row. A caller cannot bulk-SELECT and
-- decrypt around the log. There is deliberately NO bulk-plaintext function.
--
--   vault.list()        → (name, hint, updated_at)  — masked, NEVER plaintext
--   vault.mask(name)    → single masked hint
--   vault.reveal(name)  → plaintext of ONE named secret, audited
--   vault.set_secret / vault.delete_secret → write side, audited
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot at
-- release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

-- pgcrypto is a contrib extension (superuser to CREATE). On prod the migration
-- runs as the deploy superuser and creates it. On a non-superuser test DB it is
-- pre-installed by the admin (see tests/conftest.py) so IF NOT EXISTS no-ops;
-- if neither holds we degrade with a NOTICE rather than abort the migration —
-- the schema still installs, only runtime reveal/set need the extension.
DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS pgcrypto;
EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE 'vault: pgcrypto absent and not creatable here (non-superuser); '
        'install it as admin or vault reveal/set will fail at runtime';
END
$$;

CREATE SCHEMA IF NOT EXISTS vault;

-- ── tables ───────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS vault.secrets (
    name       text        PRIMARY KEY,
    ciphertext bytea       NOT NULL,   -- pgp_sym_encrypt output; the ONLY dumped secret material
    hint       text        NOT NULL,   -- masked preview, stamped at write time (no plaintext)
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Audit trail. Only the SECURITY DEFINER functions write it (no direct grant),
-- so a caller can't forge or erase its own reveals.
CREATE TABLE IF NOT EXISTS vault.events (
    at   timestamptz NOT NULL DEFAULT now(),
    who  text        NOT NULL,   -- session_user (the connecting role)
    verb text        NOT NULL,   -- 'reveal' | 'set' | 'delete'
    name text        NOT NULL
);
CREATE INDEX IF NOT EXISTS vault_events_name_at_idx ON vault.events (name, at DESC);

-- ── helpers ──────────────────────────────────────────────────────────────

-- Masked preview: at most min(3, len/5) chars per side; fully masked under 12.
-- chr(8226)=•  chr(8230)=…
CREATE OR REPLACE FUNCTION vault._hint(v text) RETURNS text
    LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN v IS NULL OR length(v) = 0 THEN '(empty)'
        WHEN length(v) < 12 THEN repeat(chr(8226), 6) || ' (' || length(v) || ')'
        ELSE left(v, least(3, length(v) / 5)) || chr(8230)
             || right(v, least(2, length(v) / 5))
    END;
$$;

-- The passphrase, from server config. Wrapped so every crypto call site is
-- identical and a missing key gives a legible error, not a cryptic one.
CREATE OR REPLACE FUNCTION vault._key() RETURNS text
    LANGUAGE plpgsql STABLE AS $$
DECLARE k text;
BEGIN
    k := current_setting('app.secret_key', true);
    IF k IS NULL OR k = '' THEN
        RAISE EXCEPTION 'vault: app.secret_key is not set on this server '
            '(ALTER SYSTEM SET app.secret_key = ...; SELECT pg_reload_conf())';
    END IF;
    RETURN k;
END
$$;

-- ── verbs (SECURITY DEFINER; owned by the migrating role) ─────────────────

CREATE OR REPLACE FUNCTION vault.list()
    RETURNS TABLE(name text, hint text, updated_at timestamptz)
    LANGUAGE sql SECURITY DEFINER SET search_path = vault, public, pg_temp AS $$
    SELECT s.name, s.hint, s.updated_at FROM vault.secrets s ORDER BY s.name;
$$;

CREATE OR REPLACE FUNCTION vault.mask(p_name text) RETURNS text
    LANGUAGE sql SECURITY DEFINER SET search_path = vault, public, pg_temp AS $$
    SELECT s.hint FROM vault.secrets s WHERE s.name = p_name;
$$;

CREATE OR REPLACE FUNCTION vault.reveal(p_name text) RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = vault, public, pg_temp AS $$
DECLARE v text;
BEGIN
    SELECT pgp_sym_decrypt(s.ciphertext, vault._key())
        INTO v FROM vault.secrets s WHERE s.name = p_name;
    IF v IS NULL THEN
        RETURN NULL;   -- unknown name; not an error (callers fall back)
    END IF;
    INSERT INTO vault.events(who, verb, name) VALUES (session_user, 'reveal', p_name);
    RETURN v;
END
$$;

CREATE OR REPLACE FUNCTION vault.set_secret(p_name text, p_value text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = vault, public, pg_temp AS $$
BEGIN
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
    DELETE FROM vault.secrets WHERE name = p_name;
    INSERT INTO vault.events(who, verb, name) VALUES (session_user, 'delete', p_name);
END
$$;

-- ── lock it down ─────────────────────────────────────────────────────────
--
-- Tables & key are unreachable directly; everything flows through the definer
-- functions (so reveal is the only decrypt path and it always audits). The
-- functions themselves are granted to PUBLIC: v1 trusts anyone who can connect
-- (ADR 0055). Tightening to a dedicated role is a later one-liner.

REVOKE ALL ON SCHEMA vault FROM PUBLIC;
REVOKE ALL ON ALL TABLES    IN SCHEMA vault FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA vault FROM PUBLIC;

GRANT USAGE ON SCHEMA vault TO PUBLIC;
GRANT EXECUTE ON FUNCTION
    vault.list(), vault.mask(text), vault.reveal(text),
    vault.set_secret(text, text), vault.delete_secret(text)
    TO PUBLIC;

COMMIT;
