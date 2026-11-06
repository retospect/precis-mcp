-- scripts/db-own/cutover.sql — the superuser half of the db-own cutover.
--
-- Run ONLY through scripts/db-own-cutover, which wraps this file, then
-- src/precis/migrations/0286_db_ownership.sql (the privilege layout), then
-- the verify.sql gate in ONE transaction: any failure or a red gate rolls the
-- whole cutover back. Needs a superuser (CREATE ROLE, REASSIGN OWNED, ALTER
-- DATABASE OWNER). Idempotent: a second run finds nothing owned by agent_rw.
--
-- Roles (multi-user-overhaul.md §DB ownership):
--   precis_owner    owns the database, schemas public/vault and every object
--                   in them. The owner DSN: DDL and `precis migrate` only,
--                   never a daemon's login; its password sits on the gateway.
--   precis_ingress  the web daemon. Member of agent_rw; writes web_users.
-- Both are created NOLOGIN here when missing; the postgres play (overlay
-- postgres_roles + vault_pg_precis_{owner,ingress}_pass) gives them LOGIN and
-- a password — same split as precis_system (0285). No daemon role is ever
-- granted membership in precis_owner.

DO $cut$
DECLARE
    bad text;
BEGIN
    IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) THEN
        RAISE EXCEPTION 'db-own cutover: % is not a superuser', current_user;
    END IF;
    IF to_regrole('agent_rw') IS NULL THEN
        RAISE EXCEPTION 'db-own cutover: role agent_rw does not exist here';
    END IF;

    -- Scope guard. REASSIGN OWNED covers the whole current database plus the
    -- databases agent_rw owns; refuse unless everything it owns is precis's:
    -- objects in public/vault (and their toast tables), extensions, and this
    -- database.
    SELECT string_agg(x, ', ') INTO bad FROM (
        SELECT format('relation %s', c.oid::regclass) AS x
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE c.relowner = 'agent_rw'::regrole
           AND n.nspname NOT IN ('public', 'vault', 'pg_toast')
           AND n.nspname NOT LIKE 'pg_temp%' AND n.nspname NOT LIKE 'pg_toast_temp%'
        UNION ALL
        SELECT format('function %s', p.oid::regprocedure)
          FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE p.proowner = 'agent_rw'::regrole
           AND n.nspname NOT IN ('public', 'vault')
        UNION ALL
        SELECT format('schema %s', n.nspname)
          FROM pg_namespace n
         WHERE n.nspowner = 'agent_rw'::regrole
           AND n.nspname NOT IN ('public', 'vault')
           AND n.nspname NOT LIKE 'pg_temp%' AND n.nspname NOT LIKE 'pg_toast_temp%'
        UNION ALL
        SELECT format('database %s', d.datname)
          FROM pg_database d
         WHERE d.datdba = 'agent_rw'::regrole AND d.datname <> current_database()
    ) s;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'db-own cutover: agent_rw owns objects outside precis: %', bad
            USING HINT = 'Move or drop them first; REASSIGN OWNED would take them too.';
    END IF;

    IF to_regrole('precis_owner') IS NULL THEN
        CREATE ROLE precis_owner NOLOGIN;
    END IF;
    IF to_regrole('precis_ingress') IS NULL THEN
        CREATE ROLE precis_ingress NOLOGIN;
    END IF;

    EXECUTE format('ALTER DATABASE %I OWNER TO precis_owner', current_database());
END
$cut$;

GRANT agent_rw TO precis_ingress;

REASSIGN OWNED BY agent_rw TO precis_owner;

-- REASSIGN leaves agent_rw's old default-privilege rows behind; they would
-- re-fire if agent_rw ever created a table again. Clear them.
DO $acl$
BEGIN
    IF to_regrole('agent_ro') IS NOT NULL THEN
        ALTER DEFAULT PRIVILEGES FOR ROLE agent_rw IN SCHEMA public
            REVOKE ALL ON TABLES FROM agent_ro;
        ALTER DEFAULT PRIVILEGES FOR ROLE agent_rw IN SCHEMA public
            REVOKE ALL ON SEQUENCES FROM agent_ro;
    END IF;
END
$acl$;
