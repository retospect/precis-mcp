# Rebuilding prod Postgres from a backup

**When.** The data node's database is gone or unusable (disk loss, node
replaced) and a nightly dump is the source. This starts where
[`backup-restore-drill`](./backup-restore-drill.md) ends: a dump restored and
verified in a throwaway container. It ends at "this is prod again".
**Reto-only**: playbook runs against the fleet are refused to agents. A stale
`postmaster.pid` after reboot is a different, much cheaper problem:
[`prod-db-down-after-reboot`](./prod-db-down-after-reboot.md).

No addresses or hostnames here (public repo): node coordinates come from the
gitignored overlay `deploy/inventory/hosts.yml` (resolved by
`scripts/lib/pgb-host.sh`); secrets live under `~/.secrets/pw/`, by name only.
Budget: staging plus restore is about 3 hours measured 2026-10-01, and the
database is about 70 GB, so size the target volume first.

Steps marked **unverified** are derived from the deploy tree and not run
end-to-end; the whole procedure has never been performed.

## 0. Preconditions

- A dump you trust: restored and verified per the drill (step 5 there). Note
  its timestamp: everything written after it is lost, and migrations applied
  after it must be re-applied (step 6).
- The overlay (`deploy/inventory/`) and `deploy/.vault-pass` reachable
  ([`cluster-overlay-sync`](./cluster-overlay-sync.md); it needs an off-cluster
  backup for exactly this case).
- Reach to the nodes: [`cluster-ssh-remote-access`](./cluster-ssh-remote-access.md).

## 1. Choose the node and point the overlay at it

The new node needs Postgres 17 with pgvector >= the dump's (0.8.2 at the last
drill), enough disk, and membership in the overlay's `data` inventory group
(the `02-postgres.yml` play targets `hosts: data`; how the example overlay
assigns it is `deploy/inventory.example/hosts.example.yml`).

1. In `deploy/inventory/hosts.yml` set `postgres_host:` to the new node's
   inventory name and make sure that node has `ansible_host:` and is in `data`.
   Every role that builds a DSN reads `hostvars[postgres_host]['ansible_host']`,
   so this one variable repoints web, workers, asa and the agent worker at the
   next deploy; `scripts/lib/pgb-host.sh::resolve_pgb_host` reads the same key
   for `scripts/prod-psql`.
2. Sync the overlay to the deploy controller:
   [`cluster-overlay-sync`](./cluster-overlay-sync.md).

## 2. Stop writers

Stop every daemon that writes to the old database so nothing half-writes while
the target changes: workers and watch (`scripts/restart-worker-and-watch` is
the restart pair; for a stop use the host's launchd/systemd verb, **unverified**
as one command), the web app, asa-bot, the agent worker. The shared MCP server
can stay up (its DB pool just errors).

## 3. Install Postgres and pgbouncer on the node

From `deploy/`, Reto's terminal (play: `deploy/playbooks/02-postgres.yml`,
roles `postgres` then `pgbouncer`, separately taggable):

    ansible-playbook -i inventory/hosts.yml playbooks/02-postgres.yml --tags postgres
    ansible-playbook -i inventory/hosts.yml playbooks/02-postgres.yml --tags pgbouncer

Known: the `postgres` role's DB-management tasks go RED (wrong `/tmp` socket,
gr160582, noted in [`prod-db-backup-and-timezone`](./prod-db-backup-and-timezone.md))
while the config half (data dir, `conf.d`, `pg_hba.conf`, guarded launcher,
launchd plist) applies. Expect red at "Create precis_prod database" and role
creation; do those by hand in step 4 and do not treat the red as a stop.
The pgbouncer role renders `userlist.txt` with the `agent_rw` password from the
vault (`vault_pg_agent_rw_pass`): see
[`rotate-agent-rw-credential`](./rotate-agent-rw-credential.md) for why Postgres
and pgbouncer must hold the same value.

## 4. Create roles and database, restore

Per the drill's step 4, but into the real server: create the application roles
(names and passwords come from `postgres_roles` in the role defaults and the
vault; `deploy/roles/postgres/tasks/main.yml` lists them) *before*
`pg_restore`, since the drill restores with `--no-owner --no-privileges`.
Then `createdb precis_prod`, `CREATE EXTENSION` for those in the dump (vector,
pg_trgm, pgcrypto, btree_gist, pg_stat_statements), and
`pg_restore -j 8 --no-owner --no-privileges -d precis_prod <dump dir>`.
Raise `maintenance_work_mem` for the session (the HNSW build dominates the
time). Afterwards re-grant ownership/privileges to `agent_rw` and the other
roles (**unverified**: the exact GRANT set; compare with `\dp` on the restored
throwaway container versus the role tasks), set the statement timeouts and
`app.secret_key` the role sets with ALTER (the role tasks at the end of
`main.yml`; re-run the play and read which tasks went green), `VACUUM ANALYZE`.

## 5. Pgbouncer and direct checks

From a controller that can reach the node, through pgbouncer as `agent_rw`:

    scripts/prod-psql --ro "SELECT count(*) FROM refs"

(works once the overlay points at the new node; `agent_ro` lacks SELECT on the
precis tables, see [`prod-db-access`](./prod-db-access.md), so a non-ro call
may be needed for counts). Compare with the drill's counts.

## 6. Migrations

Dumps predate later migrations. The deploy's migrate-first play
(`deploy/playbooks/07-precis-migrate.yml`, run first by
`redeploy-precis.yml`) applies them; see
[`migration-deploy-window`](./migration-deploy-window.md). Check
`_migrations.applied_at` against the dump time to know what is pending. Do
this before restarting workers.

## 7. Repoint clients

- Roles that build DSNs from `postgres_host` pick it up on the next deploy:
  `scripts/deploy` (Reto's / the release owner's) re-renders and bounces every
  daemon (`redeploy-precis.yml`). This is the step that restarts workers and the
  web app against the new node.
- SSH tunnels where used: the overlay's `ssh_tunnels` list and
  `playbooks/17-ssh-tunnels.yml` (the agent worker unit is ordered after
  `autossh@<postgres_host>`). **Unverified** that any tunnel targets the data
  node in the current overlay.
- Each dev machine that runs the shared MCP: update
  `~/.secrets/pw/PRECIS_MCP_DB_HOST` (one line, the node's address, see
  `deploy/mcp-http/README.md`), and the DSN file `~/.secrets/pw/PRECIS_DATABASE_URL`
  (`scripts/prod-psql` and `scripts/prod-precis` take it from the overlay /
  that file; **unverified** which of them reads which, run each once).
- Restart the MCP server: `precis-mcp-http-ensure.sh --recreate` (agents may run
  it after a deploy; see "Who may run what" in `deploy/mcp-http/README.md`).
  An MCP started before the DB changed holds the old address until recreated.

## 8. Verify "this is prod again"

- `scripts/prod-psql --ro "SELECT now(), count(*) FROM refs"` and the newest
  `_migrations` rows.
- `get(kind='skill', id='precis-status')` shows build/sha/DB.
- A write round trip: file and delete a throwaway gripe via the MCP.
- Workers claiming jobs (a `job` leaves `queued`), asa-bot answers, the web app
  loads. Nightly backups: confirm the next 03:30 UTC dump runs from the new node
  (`deploy/playbooks/11-backups.yml` provisions it; **unverified** that the
  backups role follows `postgres_host`).
- Record the rebuild's real timings in
  [`backup-restore-drill`](./backup-restore-drill.md)'s drill log.
