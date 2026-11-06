-- scripts/db-own/verify.sql — DB ownership + least-privilege canary (db-own).
--
-- One read-only SELECT: a row per invariant with ok = true/false. Runs as any
-- role that can read pg_catalog (agent_ro on prod: `scripts/prod-psql --ro`),
-- inside scripts/db-own-cutover before COMMIT, and from
-- tests/test_db_ownership.py. Every check is false-safe when a role is
-- missing: CASE guarantees has_*_privilege never sees an unknown role name.
--
-- The invariants (Fable review findings 1 and 3, Reto 2026-10-22):
--   * agent_rw owns nothing in precis_prod — precis_owner (the owner DSN) does;
--   * agent_rw/agent_ro cannot read vault.secrets (keys flow via vault.reveal);
--   * agent_rw cannot write web_users (only precis_ingress, the web daemon),
--     and neither agent_rw nor agent_ro reads web_users.password_hash;
--   * vault writes are agent_rw's (not PUBLIC's) and class-gated: agent_rw
--     cannot overwrite or delete an infra-class secret;
--   * agent_rw cannot create objects or become the owner;
--   * agent_rw keeps the DML it legitimately needs.

WITH r AS (
    SELECT to_regrole('agent_rw')       IS NOT NULL AS rw,
           to_regrole('agent_ro')       IS NOT NULL AS ro,
           to_regrole('precis_owner')   IS NOT NULL AS own,
           to_regrole('precis_ingress') IS NOT NULL AS ing,
           to_regrole('precis_system')  IS NOT NULL AS sys
),
-- Relations by OID from the catalog: a qualified name like 'vault.secrets'
-- needs USAGE on the schema to resolve, which agent_ro may lack.
rel AS (
    SELECT n.nspname || '.' || c.relname AS name, c.oid
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname IN ('public', 'vault')
),
rw_owned AS (
    SELECT count(*) AS n FROM (
        SELECT c.relowner AS o FROM pg_class c
         WHERE c.relnamespace IN (SELECT oid FROM pg_namespace
                                   WHERE nspname IN ('public', 'vault'))
        UNION ALL
        SELECT p.proowner FROM pg_proc p
         WHERE p.pronamespace IN (SELECT oid FROM pg_namespace
                                   WHERE nspname IN ('public', 'vault'))
        UNION ALL
        SELECT t.typowner FROM pg_type t
         WHERE t.typnamespace IN (SELECT oid FROM pg_namespace
                                   WHERE nspname IN ('public', 'vault'))
        UNION ALL
        SELECT n.nspowner FROM pg_namespace n
         WHERE n.nspname IN ('public', 'vault')
        UNION ALL
        SELECT d.datdba FROM pg_database d WHERE d.datname = current_database()
        UNION ALL
        SELECT e.extowner FROM pg_extension e
    ) o
    WHERE (SELECT rw FROM r) AND o.o = to_regrole('agent_rw')
),
checks(ord, check_name, ok) AS (
    SELECT 1, 'role precis_owner exists',
           (SELECT own FROM r)
    UNION ALL
    SELECT 2, 'role precis_ingress exists, member of agent_rw',
           CASE WHEN (SELECT ing AND rw FROM r)
                THEN pg_has_role('precis_ingress', 'agent_rw', 'MEMBER')
                ELSE false END
    UNION ALL
    SELECT 3, 'precis_owner is not superuser, CREATEROLE or BYPASSRLS',
           EXISTS (SELECT 1 FROM pg_roles
                    WHERE rolname = 'precis_owner'
                      AND NOT (rolsuper OR rolcreaterole OR rolbypassrls))
    UNION ALL
    SELECT 4, 'agent_rw owns nothing in this database (objects, schemas, db)',
           (SELECT n = 0 FROM rw_owned)
    UNION ALL
    SELECT 5, 'protected tables owned by precis_owner',
           CASE WHEN (SELECT own FROM r)
                THEN (SELECT bool_and(c.relowner = to_regrole('precis_owner'))
                        AND count(*) = 4
                        FROM pg_class c
                       WHERE c.oid IN (SELECT oid FROM rel
                                        WHERE name IN ('vault.secrets', 'vault.events',
                                                       'public.web_users',
                                                       'public.app_settings')))
                ELSE false END
    UNION ALL
    SELECT 6, 'agent_rw cannot SELECT vault.secrets',
           CASE WHEN (SELECT rw FROM r)
                THEN NOT has_any_column_privilege('agent_rw', (SELECT oid FROM rel WHERE name = 'vault.secrets'), 'SELECT')
                ELSE true END
    UNION ALL
    SELECT 7, 'agent_rw cannot SELECT vault.events',
           CASE WHEN (SELECT rw FROM r)
                THEN NOT has_any_column_privilege('agent_rw', (SELECT oid FROM rel WHERE name = 'vault.events'), 'SELECT')
                ELSE true END
    UNION ALL
    SELECT 8, 'agent_ro cannot SELECT vault.secrets',
           CASE WHEN (SELECT ro FROM r)
                THEN NOT has_any_column_privilege('agent_ro', (SELECT oid FROM rel WHERE name = 'vault.secrets'), 'SELECT')
                ELSE true END
    UNION ALL
    SELECT 9, 'agent_rw cannot INSERT/UPDATE/DELETE/TRUNCATE web_users',
           CASE WHEN (SELECT rw FROM r)
                THEN NOT (has_any_column_privilege('agent_rw', (SELECT oid FROM rel WHERE name = 'public.web_users'), 'INSERT')
                       OR has_any_column_privilege('agent_rw', (SELECT oid FROM rel WHERE name = 'public.web_users'), 'UPDATE')
                       OR has_table_privilege('agent_rw', (SELECT oid FROM rel WHERE name = 'public.web_users'), 'DELETE')
                       OR has_table_privilege('agent_rw', (SELECT oid FROM rel WHERE name = 'public.web_users'), 'TRUNCATE'))
                ELSE true END
    UNION ALL
    SELECT 10, 'agent_rw cannot write the _migrations ledger',
           CASE WHEN (SELECT rw FROM r)
                THEN NOT (has_any_column_privilege('agent_rw', (SELECT oid FROM rel WHERE name = 'public._migrations'), 'INSERT')
                       OR has_any_column_privilege('agent_rw', (SELECT oid FROM rel WHERE name = 'public._migrations'), 'UPDATE')
                       OR has_table_privilege('agent_rw', (SELECT oid FROM rel WHERE name = 'public._migrations'), 'DELETE'))
                ELSE true END
    UNION ALL
    SELECT 11, 'agent_rw cannot CREATE in schemas public/vault',
           CASE WHEN (SELECT rw FROM r)
                THEN NOT (has_schema_privilege('agent_rw', 'public', 'CREATE')
                       OR has_schema_privilege('agent_rw', 'vault', 'CREATE'))
                ELSE true END
    UNION ALL
    SELECT 12, 'no daemon role can become precis_owner',
           CASE WHEN (SELECT own FROM r)
                THEN NOT EXISTS (
                    SELECT 1 FROM pg_roles x
                     WHERE x.rolname IN ('agent_rw', 'agent_ro',
                                         'precis_ingress', 'precis_system')
                       AND pg_has_role(x.oid, to_regrole('precis_owner'), 'SET'))
                ELSE true END
    UNION ALL
    SELECT 13, 'agent_rw keeps DML on refs/chunks and SELECT on web_users.login',
           CASE WHEN (SELECT rw FROM r)
                THEN has_table_privilege('agent_rw', (SELECT oid FROM rel WHERE name = 'public.refs'),
                                         'SELECT,INSERT,UPDATE,DELETE')
                 AND has_table_privilege('agent_rw', (SELECT oid FROM rel WHERE name = 'public.chunks'),
                                         'SELECT,INSERT,UPDATE,DELETE')
                 AND has_column_privilege('agent_rw', (SELECT oid FROM rel WHERE name = 'public.web_users'),
                                          'login', 'SELECT')
                 AND has_table_privilege('agent_rw', (SELECT oid FROM rel WHERE name = 'public.app_settings'),
                                         'SELECT,INSERT,UPDATE,DELETE')
                ELSE false END
    UNION ALL
    SELECT 14, 'agent_rw keeps EXECUTE on vault.reveal/set_secret/delete_secret/list',
           CASE WHEN (SELECT rw FROM r)
                THEN has_schema_privilege('agent_rw', 'vault', 'USAGE')
                 AND (SELECT count(*) = 4 AND bool_and(has_function_privilege(
                                 'agent_rw', p.oid, 'EXECUTE'))
                        FROM pg_proc p
                       WHERE p.pronamespace = (SELECT oid FROM pg_namespace
                                                WHERE nspname = 'vault')
                         AND ((p.proname = 'reveal' AND p.pronargs = 1)
                           OR (p.proname = 'set_secret' AND p.pronargs = 2)
                           OR (p.proname = 'delete_secret' AND p.pronargs = 1)
                           OR (p.proname = 'list' AND p.pronargs = 0)))
                ELSE false END
    UNION ALL
    SELECT 15, 'precis_ingress can write web_users and read password_hash',
           CASE WHEN (SELECT ing FROM r)
                THEN has_table_privilege('precis_ingress',
                                         (SELECT oid FROM rel WHERE name = 'public.web_users'),
                                         'SELECT,INSERT,UPDATE,DELETE')
                 AND has_column_privilege('precis_ingress',
                                          (SELECT oid FROM rel WHERE name = 'public.web_users'),
                                          'password_hash', 'SELECT')
                ELSE false END
    UNION ALL
    SELECT 16, 'precis_owner default privileges grant new tables to agent_rw',
           EXISTS (SELECT 1 FROM pg_default_acl d
                    WHERE d.defaclrole = to_regrole('precis_owner')
                      AND d.defaclnamespace = 'public'::regnamespace
                      AND d.defaclobjtype = 'r'
                      AND d.defaclacl::text LIKE '%agent_rw=arwd%')
    UNION ALL
    SELECT 17, 'agent_rw/agent_ro cannot SELECT web_users.password_hash',
           CASE WHEN (SELECT rw FROM r)
                THEN NOT has_column_privilege('agent_rw',
                                              (SELECT oid FROM rel WHERE name = 'public.web_users'),
                                              'password_hash', 'SELECT')
                ELSE true END
           AND CASE WHEN (SELECT ro FROM r)
                THEN NOT has_column_privilege('agent_ro',
                                              (SELECT oid FROM rel WHERE name = 'public.web_users'),
                                              'password_hash', 'SELECT')
                ELSE true END
    UNION ALL
    -- proacl NULL is the default ACL: EXECUTE to PUBLIC.
    SELECT 18, 'vault.set_secret/delete_secret not executable by PUBLIC or agent_ro',
           (SELECT count(*) = 2
                   AND bool_and(p.proacl IS NOT NULL
                                AND NOT EXISTS (SELECT 1 FROM aclexplode(p.proacl) a
                                                 WHERE a.grantee = 0)
                                AND CASE WHEN (SELECT ro FROM r)
                                         THEN NOT has_function_privilege('agent_ro', p.oid, 'EXECUTE')
                                         ELSE true END)
              FROM pg_proc p
             WHERE p.pronamespace = (SELECT oid FROM pg_namespace WHERE nspname = 'vault')
               AND ((p.proname = 'set_secret' AND p.pronargs = 2)
                 OR (p.proname = 'delete_secret' AND p.pronargs = 1)))
    UNION ALL
    -- The write gate ships with the class column (migration 0285); the
    -- cutover runs before that release on prod, so no column = not yet due.
    SELECT 19, 'vault.set_secret/delete_secret enforce the class gate',
           CASE WHEN EXISTS (SELECT 1 FROM pg_attribute a
                              WHERE a.attrelid = (SELECT oid FROM rel WHERE name = 'vault.secrets')
                                AND a.attname = 'class' AND NOT a.attisdropped)
                THEN (SELECT count(*) = 2 AND bool_and(p.prosrc LIKE '%vault._write_check(p_name)%')
                        FROM pg_proc p
                       WHERE p.pronamespace = (SELECT oid FROM pg_namespace WHERE nspname = 'vault')
                         AND ((p.proname = 'set_secret' AND p.pronargs = 2)
                           OR (p.proname = 'delete_secret' AND p.pronargs = 1)))
                ELSE true END
)
-- NULL (a table that does not exist) is red, never silently green.
SELECT ord, check_name, coalesce(ok, false) AS ok FROM checks ORDER BY ord;
