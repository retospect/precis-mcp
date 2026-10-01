# Prod Postgres: topology, UTC, backups, disk-full

**When.** Touching the prod DB node's server config, taking a manual backup,
chasing a `unknown PostgreSQL timezone` warning, or prod writes fail with
`DiskFull`. Related: [`prod-db-down-after-reboot`](./prod-db-down-after-reboot.md),
[`migration-deploy-window`](./migration-deploy-window.md).

## Topology

PG 17 (Homebrew) runs **locally on caspar** (`/opt/homebrew/opt/postgresql@17/bin`,
data `/opt/homebrew/var/postgresql@17`, user `deploy`, peer auth over the local
socket). pgbouncer (transaction mode, port 6432) forwards to the local
Postgres. caspar's **default** `pg_dump` is v16 (Intel `/usr/local`) — the
wrong major; always put pg17 first on PATH
(`export PATH=/opt/homebrew/opt/postgresql@17/bin:$PATH`) or `pg_dump` refuses
the v17 server.

## Timezone is UTC throughout

- Symptom that surfaced it: psycopg `unknown PostgreSQL timezone: 'GB'; will
  use UTC` during a briefing-audio publish.
- Cause: `initdb` baked the host zone into the base `postgresql.conf`
  (`timezone` / `log_timezone`); the ansible `conf.d/cluster.conf` override
  (loaded via `include_dir='conf.d'`, so it wins) never set it.
- Fix: `timezone='UTC'` / `log_timezone='UTC'` in
  `deploy/roles/postgres/templates/postgresql.conf.j2`, applied live with
  `pg_reload_conf()` (SIGHUP-reloadable, no restart). Plus a defensive
  `SET TIME ZONE 'UTC'` in `src/precis/store/pool.py::_configure_connection`
  so every pooled session is UTC regardless of server default.
- **pgbouncer stale-TZ gotcha (gr160580).** pgbouncer caches the server's
  startup ParameterStatus for the **life of the process** and advertises it to
  clients; server-connection recycling does not refresh it. A pgbouncer that
  started before the fix still reports the old zone to app clients (cosmetic
  warning) until `RECONNECT` (needs the `deploy` admin md5 password;
  `admin_users=deploy`) or a restart. Masked by the pool pin.
- Config drift: live pgbouncer runs `/usr/local/etc/pgbouncer/pgbouncer.ini`
  while ansible manages `/opt/homebrew/etc/pgbouncer.ini` — ansible pgbouncer
  changes are no-ops against the live process.

## Nightly schedule (caspar, UTC)

| time | what |
|------|------|
| 03:00 | `daily-update-reboot.sh` (root cron, ansible-managed): OS updates → graceful PG drain → reboot. The nightly fleet-wide `PoolTimeout` blips at ~03:02 are this reboot, not the backup. |
| 03:30 | `pg_dump` (`/opt/shared/scripts/pg_backup.sh` from `deploy/roles/backups/templates/pg_backup.sh.j2`; log `/opt/shared/logs/backup-pg.log`). Moved *behind* the reboot 2026-08-11Z: a ~23 h growth budget instead of a 60-min ceiling. ~48 min. |
| 04:30 | B2 offsite sync. |

Rotation: 7 days local, 90 days NAS; the backup dir sits steady at ~110 GB
(~14 GB per dump). **No migration deploys 03:00–~04:20Z** — `pg_dump` blocks
`ADD COLUMN`; see [`migration-deploy-window`](./migration-deploy-window.md).

## Backup traps and recipes

- The nightly dump was **dead for weeks** (last good ~Jun 8) because cron's
  minimal PATH lacks the Homebrew pg17 bin dir (`pg_dump: command not found`),
  and the script still named a renamed-away database. Fixed in the template
  (export pg17 PATH; `-d precis_prod`), deployed with
  `ansible-playbook playbooks/11-backups.yml`. A cron script on a Mac needs an
  explicit PATH.
- `playbooks/02-postgres.yml` goes RED on its DB-management tasks (wrong `/tmp`
  socket, gr160582) but the config half applies.
- **Manual on-demand backup**, on caspar with pg17 on PATH:

      pg_dump -U deploy -d precis_prod -Fc -Z6 \
        -f /opt/shared/backups/postgres/precis_prod_manual_<ts>.dump
      pg_restore --list /opt/shared/backups/postgres/precis_prod_manual_<ts>.dump

## Disk-full (`DiskFull` stalls all prod writes)

Seen 2026-08-04Z, when backups had no rotation: the dump dir reached 122 GB
(~10 daily dirs of ~12 GB) and filled caspar's volume to 100% → psycopg
`DiskFull` stalled all prod writes for ~1.5 h.

1. **Diagnose first:** prod writes failing / `DiskFull` in a worker log →
   `ssh caspar 'df -h /System/Volumes/Data'`.
2. The backups dir is on caspar's **local** volume, **not** the NAS. A 174 GB
   dir named `/opt/nfs` is *also* local and misleadingly named; the real NAS
   mounts under `/opt/nas`. Live PGDATA is a legitimate ~89 GB — don't touch.
3. Mitigation (`deploy` has passwordless sudo on caspar): `rm -rf` the oldest
   `/opt/shared/backups/postgres/precis_prod_<YYYYMMDD>_<HHMMSS>` dirs; keep ~5
   recent plus any manual dump (deleting 4 freed ~50 GB).
4. Durable follow-ups owed (gr191008, beyond the now-live rotation):
   investigate/relocate the 174 GB `/opt/nfs`, logrotate the unrotated worker
   logs (~1.4 GB), a disk-space alert before 100%.

## Verifying a post-deploy change

Judge a serving/deploy change from **steady state**, never the minute after —
the llama-swap reload window right after a bounce reads as
Connection-refused/400 and self-heals. Use **absolute UTC cutoffs** on DB `ts`
(a relative `now()-20min` spanned the pre-deploy window and read as "fix
failed"). Node log timestamps are in the node's local zone (melchior's are not
UTC) and are repeatedly mis-converted — trust the DB `ts`.
