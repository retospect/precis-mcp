-- 0279_db_ownership.sql
--
-- DB ownership and least privilege (db-own; Fable review findings 1 and 3,
-- Reto 2026-10-22; docs/backlog/multi-user-overhaul.md §DB ownership).
--
-- Before: agent_rw owned every object in precis_prod — the database, schema
-- vault, vault.secrets, web_users, app_settings — so any agent_rw shell could
-- SELECT the vault ciphertext (the key is a readable GUC) and UPDATE its own
-- web_users row to admin. After scripts/db-own-cutover (superuser: creates
-- precis_owner, REASSIGN OWNED BY agent_rw TO precis_owner) agent_rw owns
-- nothing and holds only the grants below. This file is the privilege layout,
-- idempotent; the cutover runs it inside its own transaction and `precis
-- migrate` (on the owner DSN, precis_owner) stamps it.
--
--   agent_rw        SELECT/INSERT/UPDATE/DELETE (+MAINTAIN, PG17) on every
--                   public table and view, sequence USAGE/SELECT/UPDATE;
--                   no CREATE on public/vault; web_users SELECT on every
--                   column but password_hash; _migrations SELECT only;
--                   nothing on vault.* tables (keys flow through
--                   vault.reveal, SECURITY DEFINER, which refuses infra-class
--                   names to agent_rw — 0278); EXECUTE on vault.set_secret /
--                   delete_secret, no longer PUBLIC (their class gate, 0278,
--                   refuses agent_rw infra-class writes).
--   agent_ro        SELECT on public (as before) minus web_users.password_hash;
--                   nothing on vault.*, no vault writes.
--   precis_ingress  member of agent_rw (cutover/deploy) + web_users DML and
--                   password_hash: the web daemon is the one writer of the
--                   login roster and the one reader of the hash.
--   owner           the role owning public._migrations: precis_owner, the
--                   owner login (cluster cutover, and the invoking login of
--                   `precis setup --roles`, out-of-box-setup.md O7). Default
--                   privileges re-grant the above on every table/sequence a
--                   later migration creates.
--
-- No agent_rw role, or agent_rw itself the owner with no precis_owner role
-- (single-login installs; prod before the cutover) → no-op. agent_rw owning
-- anything once precis_owner exists → refuse: half a layout (grants without
-- the REASSIGN) leaves agent_rw an owner, which re-grants itself anything.
--
-- No BEGIN/COMMIT: Migrator.apply_all wraps every file in a transaction
-- (precedent 0200), and scripts/db-own-cutover splices this file into its own
-- single transaction.
-- Forward-only (ADR 0005). Squawk: GRANT/REVOKE only, no DDL on live tables.

DO $mig$
DECLARE
    rel        record;
    owner_role regrole;
    has_ro     boolean := to_regrole('agent_ro') IS NOT NULL;
    has_ing    boolean := to_regrole('precis_ingress') IS NOT NULL;
    cols       text;
    maintain   text := CASE WHEN current_setting('server_version_num')::int >= 170000
                            THEN ', MAINTAIN' ELSE '' END;
BEGIN
    IF to_regrole('agent_rw') IS NULL THEN
        RAISE NOTICE '0279: no agent_rw role — single-login install, privilege layout skipped';
        RETURN;
    END IF;
    SELECT c.relowner::regrole INTO owner_role
      FROM pg_class c WHERE c.oid = 'public._migrations'::regclass;
    -- A superuser owner is a dev/test/scratch install (the baseline dump
    -- replays with stand-in agent_rw/agent_ro roles, schema_dump.APP_ROLES):
    -- the layout is for installs with a dedicated, unprivileged owner role.
    IF (SELECT rolsuper FROM pg_roles WHERE oid = owner_role) THEN
        RAISE NOTICE '0279: owner % is a superuser — layout skipped', owner_role;
        RETURN;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_class c
                WHERE c.relnamespace IN ('public'::regnamespace, 'vault'::regnamespace)
                  AND c.relowner = 'agent_rw'::regrole) THEN
        IF to_regrole('precis_owner') IS NULL AND owner_role = 'agent_rw'::regrole THEN
            RAISE NOTICE '0279: agent_rw owns the schema and no precis_owner role '
                'exists — layout skipped (scripts/db-own-cutover applies it)';
            RETURN;
        END IF;
        RAISE EXCEPTION '0279: agent_rw still owns relations in public/vault'
            USING HINT = 'Run scripts/db-own-cutover (superuser) first; it '
                         'reassigns ownership to precis_owner and applies this file.';
    END IF;

    -- Schemas: use, never create.
    GRANT USAGE ON SCHEMA public, vault TO agent_rw;
    REVOKE CREATE ON SCHEMA public FROM agent_rw;
    REVOKE CREATE ON SCHEMA vault FROM agent_rw;
    IF has_ro THEN
        GRANT USAGE ON SCHEMA public TO agent_ro;
    END IF;

    -- public tables, views, matviews: DML (+MAINTAIN on tables, for the
    -- nightly VACUUM ANALYZE). Extension members (pg_stat_statements views)
    -- keep their own ACLs.
    FOR rel IN
        SELECT c.oid::regclass AS name, c.relkind
          FROM pg_class c
         WHERE c.relnamespace = 'public'::regnamespace
           AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
           AND NOT EXISTS (SELECT 1 FROM pg_depend d
                            WHERE d.classid = 'pg_class'::regclass
                              AND d.objid = c.oid AND d.deptype = 'e')
    LOOP
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE%s ON %s TO agent_rw',
                       CASE WHEN rel.relkind IN ('r', 'p', 'm') THEN maintain ELSE '' END,
                       rel.name);
        IF has_ro THEN
            EXECUTE format('GRANT SELECT ON %s TO agent_ro', rel.name);
        END IF;
    END LOOP;

    FOR rel IN
        SELECT c.oid::regclass AS name
          FROM pg_class c
         WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'S'
           AND NOT EXISTS (SELECT 1 FROM pg_depend d
                            WHERE d.classid = 'pg_class'::regclass
                              AND d.objid = c.oid AND d.deptype = 'e')
    LOOP
        EXECUTE format('GRANT USAGE, SELECT, UPDATE ON SEQUENCE %s TO agent_rw', rel.name);
        IF has_ro THEN
            EXECUTE format('GRANT SELECT ON SEQUENCE %s TO agent_ro', rel.name);
        END IF;
    END LOOP;

    -- The login roster: read-only to agent_rw; precis_ingress writes it and
    -- is the one reader of password_hash (Reto 2026-10-22, db-own finding 3:
    -- the hash plus the vault's api-class pepper is an offline attack).
    -- Column grants: a later migration adding a web_users column grants it
    -- to agent_rw/agent_ro itself — default privileges don't reach columns.
    REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.web_users FROM agent_rw;
    SELECT string_agg(quote_ident(a.attname), ', ' ORDER BY a.attnum) INTO cols
      FROM pg_attribute a
     WHERE a.attrelid = 'public.web_users'::regclass
       AND a.attnum > 0 AND NOT a.attisdropped
       AND a.attname <> 'password_hash';
    REVOKE SELECT ON public.web_users FROM agent_rw;
    EXECUTE format('GRANT SELECT (%s) ON public.web_users TO agent_rw', cols);
    IF has_ro THEN
        REVOKE SELECT ON public.web_users FROM agent_ro;
        EXECUTE format('GRANT SELECT (%s) ON public.web_users TO agent_ro', cols);
    END IF;
    IF has_ing THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON public.web_users TO precis_ingress;
    END IF;

    -- The migration ledger: written by the owner only.
    REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public._migrations FROM agent_rw;

    -- vault: tables owner-only; the SECURITY DEFINER functions are the API.
    REVOKE ALL ON ALL TABLES IN SCHEMA vault FROM PUBLIC, agent_rw;
    REVOKE ALL ON FUNCTION vault._key(), vault._hint(text) FROM PUBLIC, agent_rw;
    IF has_ro THEN
        REVOKE ALL ON ALL TABLES IN SCHEMA vault FROM agent_ro;
    END IF;
    -- Writes: agent_rw and its members only (precis_ingress, precis_system);
    -- which names each may write is the class gate in the functions (0278).
    REVOKE ALL ON FUNCTION vault.set_secret(text, text), vault.delete_secret(text)
        FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION vault.set_secret(text, text), vault.delete_secret(text)
        TO agent_rw;
    IF has_ro THEN
        REVOKE ALL ON FUNCTION vault.set_secret(text, text), vault.delete_secret(text)
            FROM agent_ro;
    END IF;

    -- Re-grant what 0257/0258 gave agent_rw while it was the owner. Those
    -- ACLs are explicit (PUBLIC was revoked), and REASSIGN OWNED moves the
    -- old owner's own entry to the new owner — agent_rw would silently lose
    -- EXECUTE on the reveal overloads, the first-mint verb, the audit-trail
    -- sweep and the trigger-internal helpers.
    GRANT EXECUTE ON FUNCTION
        vault.reveal(text),
        vault.reveal(text, text, text, integer, integer, text),
        vault.reveal(text, bigint),
        vault.reveal(text, bigint, text, text, integer, integer, text),
        vault.set_secret_if_absent(text, text),
        vault.gc_events(integer),
        public.refs_retire_event_trail(),
        public.precis_revision_triggers_refresh(),
        public.precis_kinds_refresh_guarded()
        TO agent_rw;

    -- New objects a later migration creates in public keep working. Only the
    -- owner itself (or a superuser) may set its defaults; precis migrate runs
    -- as the owner, the cutover as superuser.
    IF NOT pg_has_role(current_user, owner_role, 'MEMBER') THEN
        RAISE EXCEPTION '0279: % cannot set %''s default privileges',
            current_user, owner_role
            USING HINT = 'Apply on the owner DSN (precis_owner) or as a superuser.';
    END IF;
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %s IN SCHEMA public '
                   'GRANT SELECT, INSERT, UPDATE, DELETE%s ON TABLES TO agent_rw',
                   owner_role, maintain);
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %s IN SCHEMA public '
                   'GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO agent_rw', owner_role);
    IF has_ro THEN
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %s IN SCHEMA public '
                       'GRANT SELECT ON TABLES TO agent_ro', owner_role);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %s IN SCHEMA public '
                       'GRANT SELECT ON SEQUENCES TO agent_ro', owner_role);
    END IF;
END
$mig$;
