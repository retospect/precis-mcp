---
status: draft
title: "deploy: a role that runs postgres_exporter and pgbouncer_exporter on the DB node, then flip monitoring_db_exporters"
pillar: platform
prio: low
---

# DB exporters role

Source: gr492114 (option (b) of gr465126, ruled D2 (a) by Reto 2026-10-16).
Option (a) shipped in 5691792fa (`feat(deploy): monitoring_db_exporters
gates DB scrapes + PostgresDown`, branch `db-exporters-gate`; verify it is
on main before building). This item is only what that commit leaves open.

## Prerequisite (blocks the build)

**Needs a vault DSN from Reto.** A read-only monitoring login for the
exporters, supplied by Reto into the ansible vault. A builder cannot mint
it: prod DB credentials are not the builder's to create. The example vault
already names `vault_pg_prometheus_pass`
(`deploy/inventory.example/group_vars/all/vault.yml.example`) and
`pgbouncer.ini.j2` already lists `prometheus_reader` in `stats_users`;
nothing in `deploy/` or `src/precis/migrations/` creates that Postgres role.
Reto decides: is `prometheus_reader` the login to use (and who creates the
role with `pg_monitor`), or a different one.

## What 5691792fa already does (do not redo)

- `monitoring_db_exporters: false` default in
  `deploy/roles/monitoring/defaults/main.yml`.
- `prometheus.yml.j2` renders the `postgres` (:9187) and `pgbouncer` (:9127)
  scrape jobs only when the var is true; `alert_rules.yml.j2` renders
  `PostgresDown` (`pg_up == 0`) only then.
- `hosts.example.yml` documents the var; `tests/test_deploy_alert_rules.py`
  covers the gate; `docs/backlog/threads/monitors-that-go-quiet.md` says to
  set it true only once the DB node runs both exporters.

## In scope

- An Ansible role `postgres_exporter` (or two roles) that installs and runs
  `postgres_exporter` on :9187 and `pgbouncer_exporter` on :9127 on the node
  named by `postgres_host`, as service units owned by the role
  (`service_unit` role is the existing pattern; read it first).
- The exporter DSNs come from vault variables only, never literals in the
  tree. Postgres exporter connects to Postgres; pgbouncer exporter connects
  to pgbouncer's admin/stats database as a `stats_users` member.
- A playbook entry in the `monitoring` / DB-node play order, tagged like
  `18-node-exporter.yml`, run before playbook 08 renders the scrape jobs.
- Flip `monitoring_db_exporters: true` in the private inventory once both
  targets answer. The public example inventory keeps `false`.
- Replace the `monitoring_db_exporters` comment and the thread's
  "set it true only once" wording with the post-activation state in the same
  commit that ships the role.

## Explicitly NOT in scope

- Any Postgres role creation or grant on prod (Reto's prerequisite).
- New alert rules beyond `PostgresDown`. Reto decides: which exporter metrics
  (connections, replication lag, pgbouncer pool waits) deserve rules.
- Changing the accepted trade-off that these alerts are silent while
  Postgres or asa_bot is down (Reto 2026-10-04, thread
  `monitors-that-go-quiet.md`): the receiver writes through Postgres.
- Hostnames or addresses in the repo (public). Targets resolve through
  `hostvars[postgres_host]['ansible_host']` as the template already does.

## Acceptance criteria

- With the var true on a monitoring host, Prometheus
  `/api/v1/targets` shows `postgres` and `pgbouncer` jobs `up`, and
  `pg_up` is 1.
- With the var false everything renders as before (existing gate test).
- No literal address, hostname or secret in the diff
  (`tests/test_deploy_tree_no_secrets.py` green).
- The role is idempotent on a second playbook run (no changed tasks).

## Target + blast radius

`deploy/roles/<new>/`, a playbook under `deploy/playbooks/`, the DB node's
service list, `deploy/inventory.example/`. Touches only the DB node and the
monitoring host's rendered config. A bad DSN makes the exporter fail to
connect, so `pg_up` reads 0 and `PostgresDown` pages: check the DSN before
flipping the var.

## Open questions / decisions log

- Reto decides: supply the vault DSN and name the login (prerequisite).
- Reto decides: packaged binaries (apt, as `node_exporter` does on Linux) or
  a pinned release download. Check the DB node's OS family first; the
  `pgbouncer` role has a macOS branch, so the node may be darwin.
- Reto decides: does the exporter listen on the tailnet interface only.
