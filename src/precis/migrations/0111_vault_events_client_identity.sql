-- 0111_vault_events_client_identity.sql
--
-- Answer "WHICH PROCESS asked for this key", not just "a key was revealed".
--
-- vault.events has audited every reveal since 0059, but the only identity it
-- records is `session_user` — and every precis process connects as the same
-- role, so in prod every one of the 287k reveal rows says `agent_rw`. The log
-- proves a secret was read; it cannot say by what. That is the gap: a leaked
-- credential, or a surprising read volume, is untraceable to a daemon.
--
-- Why the client has to tell us. The obvious server-side answer,
-- `inet_client_addr()`, is useless here: prod reaches postgres through
-- pgbouncer on caspar, so every connection appears to originate from the
-- pooler, not from the machine that wanted the key. Pooling also breaks
-- `pg_backend_pid()` as a client identifier — backends are shared and
-- reassigned. So identity is passed in by the caller, and is therefore
-- self-reported: it identifies cooperating processes (all of ours), and is
-- evidence, not proof, against a hostile one. That is the honest trust
-- boundary and it matches 0059's model, where holding the DSN already means
-- holding the secrets.
--
-- Shape: a second `vault.reveal(name, client)` overload. The 1-arg form stays
-- and delegates, so every existing caller keeps working and an un-migrated
-- process degrades to a NULL-client row rather than an error.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot at
-- release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

-- ── client identity on the audit row ─────────────────────────────────────

-- Nullable throughout: 287k pre-existing rows have no client, and a caller on
-- old code still writes through the 1-arg overload.
ALTER TABLE vault.events ADD COLUMN IF NOT EXISTS host    text;
ALTER TABLE vault.events ADD COLUMN IF NOT EXISTS os_user text;
ALTER TABLE vault.events ADD COLUMN IF NOT EXISTS pid     integer;
ALTER TABLE vault.events ADD COLUMN IF NOT EXISTS ppid    integer;
-- A compact argv summary ("precis worker --profile agent"), not just argv[0]:
-- every daemon is some flavour of `python3.13`, and the subcommand is the part
-- that names which one.
ALTER TABLE vault.events ADD COLUMN IF NOT EXISTS process text;

-- "What has been reading the OAuth token, and from where" is the query this
-- table exists to answer.
CREATE INDEX IF NOT EXISTS vault_events_name_host_at_idx
    ON vault.events (name, host, at DESC);

-- ── reveal, with identity ────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION vault.reveal(
    p_name    text,
    p_host    text,
    p_os_user text,
    p_pid     integer,
    p_ppid    integer,
    p_process text
) RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = vault, public, pg_temp AS $$
DECLARE v text;
BEGIN
    SELECT pgp_sym_decrypt(s.ciphertext, vault._key())
        INTO v FROM vault.secrets s WHERE s.name = p_name;
    IF v IS NULL THEN
        RETURN NULL;   -- unknown name; not an error (callers fall back)
    END IF;
    INSERT INTO vault.events(who, verb, name, host, os_user, pid, ppid, process)
    VALUES (session_user, 'reveal', p_name,
            p_host, p_os_user, p_pid, p_ppid, p_process);
    RETURN v;
END
$$;

-- The 1-arg form delegates rather than duplicating the decrypt+insert, so the
-- two can never drift. Existing callers (and any process still on old code)
-- keep working and land a NULL-client row.
CREATE OR REPLACE FUNCTION vault.reveal(p_name text) RETURNS text
    LANGUAGE sql SECURITY DEFINER SET search_path = vault, public, pg_temp AS $$
    SELECT vault.reveal(p_name, NULL::text, NULL::text,
                        NULL::integer, NULL::integer, NULL::text);
$$;

-- ── retention ────────────────────────────────────────────────────────────

-- This table grows without bound and is the fastest-growing audit surface we
-- have (287k rows in ~2 weeks, ~22k/day, dominated by OPENROUTER_API_KEY).
-- Prune on read-side sweeps; deliberately generous, since the point of an
-- access log is to still be there when you finally go looking.
CREATE OR REPLACE FUNCTION vault.gc_events(p_keep_days integer DEFAULT 180)
    RETURNS bigint
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = vault, public, pg_temp AS $$
DECLARE n bigint;
BEGIN
    DELETE FROM vault.events
     WHERE at < now() - make_interval(days => p_keep_days);
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END
$$;

REVOKE ALL ON FUNCTION vault.gc_events(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION vault.gc_events(integer) TO PUBLIC;

REVOKE ALL ON FUNCTION vault.reveal(text, text, text, integer, integer, text)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION vault.reveal(text, text, text, integer, integer, text)
    TO PUBLIC;

COMMIT;
