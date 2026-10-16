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

## Deploy steps (cluster-desk)

1. Confirm the precis secret store holds both DSNs under the names in
   `deploy/roles/db_exporters/defaults/main.yml`
   (`db_exporters_pg_dsn_secret`, `db_exporters_pgbouncer_dsn_secret`), or
   override those two vars in the private inventory. The postgres DSN is a
   libpq URL; the pgbouncer one connects to database `pgbouncer` on the
   pgbouncer port as a `stats_users` member.
2. Run playbook `02-postgres.yml` tags `pgbouncer` first: the template now sets
   `ignore_startup_parameters = extra_float_digits`, which pgbouncer_exporter
   needs, and applying it restarts pgbouncer (pick a quiet moment).
   Then run playbook `18a-db-exporters.yml` (tags `db_exporters`) on the DB node.
   Needs the precis venv there (playbook 20b).
3. Check the launcher env names against the pinned releases (pgbouncer_exporter
   reads `PGBOUNCER_EXPORTER_CONNECTION_STRING` per upstream; unverified here;
   if `pgbouncer_up` stays 0 check `--help`). Check `:9187/metrics` shows `pg_up 1` and `:9127/metrics` answers.
4. Set `monitoring_db_exporters: true` for the `monitoring` host in the private
   inventory and re-run playbook 08 (tag `monitoring`).
5. Acceptance: Prometheus `/api/v1/targets` shows `postgres` and `pgbouncer`
   `up`; a second run of 18a reports no changed tasks.

## Open points (Reto decides)

- Login: the role assumes `prometheus_reader` (in `stats_users` already). Who
  creates it in Postgres with `pg_monitor`; nothing in `deploy/` or
  `src/precis/migrations/` does. A builder cannot mint prod credentials.
- Secret-store names: the role's defaults are guesses.
- Bind address: all interfaces today (`db_exporters_listen_host` empty); tailnet
  only?
- New alert rules beyond `PostgresDown` (connections, replication lag,
  pgbouncer pool waits).
- Pinned release tarballs are used because homebrew-core has no formula for
  either exporter and the DB node is macOS; confirm that is acceptable.

Out of scope: the accepted trade-off that these alerts are silent while Postgres
or asa_bot is down (thread `monitors-that-go-quiet.md`).
