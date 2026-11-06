-- scripts/db-own/inventory.sql — what the db-own cutover would move (read-only).
--
-- Objects agent_rw owns in this database, by kind and schema. REASSIGN OWNED
-- moves every row of this table to precis_owner; anything outside
-- public/vault/pg_toast (or another database it owns) makes cutover.sql
-- refuse. Runs as agent_ro (`scripts/db-own-cutover` dry run).

WITH owned AS (
    SELECT 'relation:' || c.relkind::text AS kind, n.nspname::text AS schema
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE c.relowner = to_regrole('agent_rw')
    UNION ALL
    SELECT 'function', n.nspname
      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE p.proowner = to_regrole('agent_rw')
    UNION ALL
    SELECT 'type', n.nspname
      FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
     WHERE t.typowner = to_regrole('agent_rw')
       AND t.typrelid = 0 AND t.typelem = 0
    UNION ALL
    SELECT 'schema', n.nspname FROM pg_namespace n
     WHERE n.nspowner = to_regrole('agent_rw')
    UNION ALL
    SELECT 'extension', e.extname FROM pg_extension e
     WHERE e.extowner = to_regrole('agent_rw')
    UNION ALL
    SELECT 'database', d.datname FROM pg_database d
     WHERE d.datdba = to_regrole('agent_rw')
    UNION ALL
    SELECT 'default-acl', d.defaclnamespace::regnamespace::text
      FROM pg_default_acl d WHERE d.defaclrole = to_regrole('agent_rw')
)
SELECT kind, schema, count(*) AS n FROM owned GROUP BY 1, 2 ORDER BY 1, 2;
