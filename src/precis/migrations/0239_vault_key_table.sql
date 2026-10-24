-- 0239_vault_key_table.sql
--
-- Move the vault passphrase out of the cluster-wide ``app.secret_key`` GUC
-- into a single-row ``vault._key`` table only the owning role can read
-- (gr505334; Reto 2026-10-24, option (a)).
--
-- A custom GUC cannot be ACL'd: ``ALTER SYSTEM SET app.secret_key`` made
-- the passphrase readable by EVERY role through ``current_setting()`` —
-- agent_ro included — so a logical dump plus one SELECT decrypted the
-- vault. The per-role alternative, ``ALTER ROLE <owner> SET``, is no
-- better: ``pg_db_role_setting`` is world-readable too (verified 2026-10-24
-- as agent_ro — it happily shows agent_rw's ALTER ROLE timeouts). The only
-- place left that honours grants is a table.
--
-- ``vault._key`` lives inside the already-locked-down schema: no PUBLIC
-- grants, reachable only by the object owner — the role that runs
-- ``precis migrate`` (``precis_owner`` after the db-own cutover, agent_rw
-- until then; the migration deliberately names no role). The SECURITY
-- DEFINER verbs run as that owner, so ``vault._key()`` resolves the
-- passphrase for them while nothing else can SELECT it.
--
-- ``_key()`` itself stays SECURITY INVOKER on purpose: it runs only inside
-- the definer verbs (no EXECUTE grant), where the effective user is the
-- function owner — and even if EXECUTE ever leaked, a direct call still
-- dies on the table's missing SELECT grant. Two walls, not one.
--
-- The dump trade-off moves, not disappears: the passphrase is now a table
-- row, so a *logical* pg_dump would carry it next to the ciphertext it
-- protects. The nightly backup therefore dumps ``vault._key``'s DDL but not
-- its row (``--exclude-table-data``, deploy/roles/backups), and the deploy
-- play re-provisions the row on a rebuild — exactly the hole a restored
-- dump always had, since postgresql.auto.conf was never in a dump either.
--
-- The row is seeded from the still-set GUC so prod migrates without a
-- decrypt gap; the deploy play then writes the row authoritatively and
-- ``ALTER SYSTEM RESET``s the GUC, ending the exposure. Forward-only
-- (ADR 0005). Idempotent.

BEGIN;

-- Singleton: the constant-PK pattern makes a second row a constraint
-- violation rather than a silent "which key wins".
CREATE TABLE IF NOT EXISTS vault._key (
    one boolean PRIMARY KEY DEFAULT true CHECK (one),
    k   text    NOT NULL
);

-- Carry the existing passphrase over so the cluster keeps decrypting
-- between this migration and the deploy play's ALTER SYSTEM RESET.
INSERT INTO vault._key (one, k)
SELECT true, current_setting('app.secret_key', true)
WHERE nullif(current_setting('app.secret_key', true), '') IS NOT NULL
ON CONFLICT (one) DO NOTHING;

-- Read the passphrase from the table. Wrapped so every crypto call site is
-- identical and an un-provisioned vault gives a legible error.
CREATE OR REPLACE FUNCTION vault._key() RETURNS text
    LANGUAGE plpgsql STABLE AS $$
DECLARE k text;
BEGIN
    SELECT kk.k INTO k FROM vault._key kk;
    IF k IS NULL OR k = '' THEN
        RAISE EXCEPTION 'vault: no passphrase provisioned — vault._key is empty '
            '(the deploy play writes it; before 0239 it was ALTER SYSTEM '
            'SET app.secret_key)';
    END IF;
    RETURN k;
END
$$;

-- Defense in depth, matching 0059's posture: CREATE TABLE grants PUBLIC
-- nothing already, and the row must stay that way.
REVOKE ALL ON TABLE vault._key FROM PUBLIC;

COMMIT;
