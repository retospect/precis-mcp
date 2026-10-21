---
status: draft
title: "deploy: activate the db_exporters role (secrets, run, flip monitoring_db_exporters)"
pillar: platform
prio: low
---

# DB exporters role: activation

Source: gr492114 (option (b) of gr465126). The role `deploy/roles/db_exporters`
and playbook `18a-db-exporters.yml` shipped (site.yml runs it after 20b);
the `monitoring_db_exporters` gate shipped earlier. This item is what is left
to turn it on. Delete it when `pg_up` is 1 in prod.

## Prerequisites (Reto)

1. Two vault vars in the private inventory (`group_vars/all/vault.yml`):
   `vault_pg_exporter_dsn` (libpq URL) and `vault_pgbouncer_exporter_dsn`
   (database `pgbouncer`, pgbouncer port, a `stats_users` member). Names and
   shape: `inventory.example/group_vars/all/vault.yml.example`; override the var
   names via `db_exporters_pg_dsn_var` / `db_exporters_pgbouncer_dsn_var`.
2. The login the DSNs use (`prometheus_reader`) exists in Postgres with
   `pg_monitor`; nothing in `deploy/` or `src/precis/migrations/` creates it.

## Deploy steps (cluster-desk)

The DB node has no precis CLI and gets none. Ansible resolves the DSNs from the
vault on the control side and renders them into `/etc/precis/db-exporters.env`
(root, 0600); the launchers read that file and the unit runs as root.

1. Run playbook `02-postgres.yml` tags `pgbouncer` first: the template now sets
   `ignore_startup_parameters = extra_float_digits`, which pgbouncer_exporter
   needs, and applying it restarts pgbouncer (pick a quiet moment).
   Then run playbook `18a-db-exporters.yml` (tags `db_exporters`) on the DB node.
2. Check the launcher env names against the pinned releases (pgbouncer_exporter
   reads `PGBOUNCER_EXPORTER_CONNECTION_STRING` per upstream; unverified here;
   if `pgbouncer_up` stays 0 check `--help`). Check `:9187/metrics` shows `pg_up 1` and `:9127/metrics` answers.
3. Set `monitoring_db_exporters: true` for the `monitoring` host in the private
   inventory and re-run playbook 08 (tag `monitoring`).
4. Acceptance: Prometheus `/api/v1/targets` shows `postgres` and `pgbouncer`
   `up`; a second run of 18a reports no changed tasks. Rotation: change the vault
   var, re-run 18a (the changed env file restarts both units).

## Decisions (Reto 2026-10-21)

- Reto adds the two vault DSNs and creates the `prometheus_reader`
  (`pg_monitor`) login on 2026-10-22; until then this item stays open,
  waiting on Reto. After that, cluster-desk runs playbooks 02 (tag
  `pgbouncer`) + 18a and flips the gate per the steps above.
- Bind the exporters to the tailnet address only (set
  `db_exporters_listen_host` in the private inventory).
- Pinned release tarballs on the macOS DB node are fine.
- New alert rules beyond `PostgresDown`: later, not this item.

Out of scope: the accepted trade-off that these alerts are silent while Postgres
or asa_bot is down (thread `monitors-that-go-quiet.md`).
