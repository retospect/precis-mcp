---
status: ready
claimed-by: gf-backup1
title: Incremental DB backups — pgBackRest primary, uncompressed dump via restic second
pillar: platform
prio: high
---

# Incremental DB backups — pgBackRest primary, uncompressed dump via restic second

## Motivation / why

Today (read 2026-10-23):

- `pg_backup.sh` on the DB node dumps `-Fd -Z 6` (22 GB), then tar+zstd's
  the directory onto the NAS as one dated archive (24 GB; zstd adds
  nothing on top of zlib). The NAS keeps 90 days of these.
- The NAS runs `restic-weekly.sh` (Sundays 05:30Z) to B2 bucket
  `precis-backup`: the newest archive plus the papers tree. Because each
  archive is fresh compressed bytes, every weekly snapshot uploads ~21 GiB
  of new data; restic's chunk dedup never hits.
- The only restore points are 03:30Z nightly; anything written after that
  is lost on restore, and a restore is a multi-hour `pg_restore`.
- The archives live on the `botshome` share, writable by every agent host.

Reto 2026-10-23: back up so that only diffs travel ("rsync-like, copy and
apply diff"); a directory of table files, not a tar; pgBackRest as the
primary, an uncompressed logical dump through restic as the independent
second.

## In scope

### A. pgBackRest (primary, physical, point-in-time)

- WAL archiving on the DB node (`archive_mode=on`, `archive_command` =
  `pgbackrest archive-push`, **async with a local spool** so a slow or hung
  NAS never stalls Postgres; `roles/backups` already warns about NFS lockd
  hangs). Needs one Postgres restart.
- Two repos: repo1 on the NAS `dbbackup` share (fast restore), repo2 on B2
  over its S3 endpoint (offsite). Block incremental on.
- Schedule: full weekly, incremental daily, outside the 03:00–04:20 window
  or replacing the logical dump inside it (decide in review).
- Retention: repo1 4 fulls, repo2 2 fulls + 12 monthly (cost check below).
- Restore = `pgbackrest restore` (optionally `--type=time`) onto a scratch
  data dir; same major version only — which is what B covers.

### B. Logical dump through restic (second, version-independent)

- `pg_dump -Fd -Z0 -j4` into a **fixed** directory (`precis_prod_current/`,
  not dated), so successive snapshots compare the same paths.
- Dump locally on the DB node (no NFS in the dump path), then
  `rsync --delete --inplace` the directory onto `dbbackup` — only changed
  blocks cross the LAN. Then the local copy is deleted (cluster-fileserver
  -move 4a: no retained backups on the DB node).
- On the NAS, restic backs the directory up into a **local NAS restic
  repo** (the 90-day history, compressed + deduplicated), then
  `restic copy` to the B2 repo. Nightly, also after A is live (Reto
  2026-10-23).
- `restic forget --keep-daily 14 --keep-weekly 8 --keep-monthly 12 --prune`
  on both repos.
- Papers (`/Volume1/botshome/papers`, 160 GB) and `precis_root` stay in the
  B2 restic repo as today.

### C. Separation

- `dbbackup` share: written only by a backup user (uid 806 already owns the
  empty share) — the DB node's dump/rsync and pgBackRest write as it; every
  other host and every agent reads only. Restic repos owned by root on the
  NAS; agents cannot delete snapshots.

### D. Retire

A and B **replace** the current backup (Reto 2026-10-23); nothing of the
old chain stays as a third path. Removed once B has 14 days of snapshots
and one restore drill passed:

- the tar+zstd step in `pg_backup.sh.j2` and the dated NAS archives
  (`/Volume1/botshome/backups/postgres/`, 90-day prune);
- the NAS `restic-weekly.sh` cron (its papers leg moves into B's nightly
  NAS script);
- `b2_sync.sh` (already off, td461209) and its role vars.

### E. Monitors and drill

- Each leg writes a last-ok stamp (`restic-last-ok` exists; add
  pgBackRest's `info` check). One `health_digest` check reads the stamps
  — fold into `b2-offsite-sync-dark-alert.md` instead of a second check.
- `restore_test` / `drill_pull`: restore from repo1 (pgBackRest) weekly and
  from the restic dump monthly into a throwaway container
  (`backup-restore-drill` skill).

## Explicitly NOT in scope

- Per-role DB access (db-own cutover) — independent.
- The cluster share move itself (`cluster-fileserver-move.md`).

## Slices

1. **Dump → directory + restic** (B, C minus pgBackRest): `pg_backup.sh.j2`
   to `-Z0` fixed dir + rsync; NAS restic local repo + copy to B2. No DB
   restart. Measure the uncompressed size and the first incremental
   upload.
2. **pgBackRest** (A): install on the DB node, WAL archive, both repos,
   first full, restore drill to a scratch data dir. Needs the restart and
   Reto present.
3. **Retire + monitors** (D, E).

## Acceptance criteria

- A second restic snapshot of the dump uploads well under 21 GiB (target:
  a few GiB) — record the number.
- `pgbackrest info` shows a full and an incremental on both repos;
  a restore to a timestamp between two backups succeeds in a scratch
  container and row counts match the source at that time.
- The restic dump restores with `pg_restore -j` into a different Postgres
  major version (smoke: table count, a few row counts).
- An agent host cannot delete or overwrite anything on `dbbackup` or in a
  restic repo.
- Killing the NAS mid-archive does not stop Postgres writes (async spool).

## Target + blast radius

`deploy/roles/backups/` (pg_backup, restore_test, drill_pull, new
pgbackrest templates), `deploy/roles/postgres` (archive settings),
overlay vars, NAS scripts (outside the repo: `/usr/local/sbin/
restic-weekly.sh`). No migrations. Credentials: B2 keys for pgBackRest go
into the vault/overlay; restic secrets stay in `/etc/restic` on the NAS,
never printed.

## Open questions / decisions log

- Reto 2026-10-23: incremental, directory not tar, pgBackRest primary +
  restic logical second — decided.
- Reto 2026-10-23: pgBackRest runs straight on the macOS DB node (no
  container); the logical dump stays nightly; B2 cost is fine for now;
  A + B replace the current backup chain (section D).
- Reto 2026-10-24 (superseded same day): the logical dump runs every few
  hours, not nightly. Later that day Reto ruled phase 1 runs **nightly**
  ("nightly is fine. It never went down yet"), scheduled outside the
  03:00–04:20 UTC window with a no-overlap lock; the every-few-hours idea
  is parked. The tar+zstd chain keeps running in parallel until the first
  restic restore drill passes (skill backup-restore-drill); nothing of
  the old chain is removed in slice 1, so section D's retire stays a
  slice-3 item.
- Reto 2026-10-24 (overlap, gr505334): the dump excludes
  `vault._key` table data whether or not the gf-dbvault branch has
  landed — a logical dump must never carry the vault passphrase next to
  the ciphertext it protects.
- Slice 1 shipped 2026-10-24 (gf-backup1): `pg_backup_dir.sh` on the DB
  node (nightly 21:45 UTC, mkdir lock, `-Fd -Z0 -j4`, fixed dir +
  `rsync -rt --delete --inplace --checksum` → `dbbackup`, local copy
  deleted) and `restic-dbdump.sh` as the NAS-side template (hourly;
  quiescence check, local repo + `restic copy` to B2 + `forget` on both,
  last-ok stamps). The NAS is not Ansible-managed — the release owner
  copies the rendered script from the DB node's scripts dir to
  `/usr/local/sbin/` on the NAS and schedules it. Uncompressed dump size:
  heap+toast measured **47 GB** on prod 2026-10-24 (`pg_database_size` is
  89 GB incl. indexes); `-Z6` nightly runs 62 min, `-Z0` should run less.
  Open for the deploy: `dbbackup` was not yet mounted on the DB node at
  ship time (`/mnt/dbbackup` absent — the nfs_client dbbackup play needs
  `nas_dbbackup_export` + `nas_lan_*` in the overlay), and the
  backup-window deploy guard does not cover the 21:45 slot.
- 2026-10-26: the 21:45 window-guard gap above is closed — landed
  2026-10-25 (gr513885, unrelated harness commit) as
  `backup_dir_window_start_utc`/`_end_utc` (21:30–23:30) in
  `deploy/roles/backups/defaults/main.yml`; `scripts/lib/backup-window.sh`
  now reads every `<X>_window_*_utc` pair instead of only the primary one
  (`tests/test_deploy_backup_window.py` covers 21:40/23:00). The
  `dbbackup` mount gap is not a code defect: `nfs_client`'s `dbbackup.yml`
  play and the `01-nfs.yml` `data`-group play are wired and correct, dark
  by design until the overlay sets `nas_dbbackup_export` + `nas_lan_name`
  + `nas_lan_ip` (`deploy/inventory.example/hosts.example.yml:70-77`); it
  stays open until the release owner sets those three in the live
  overlay, confirms the DB node is in group `data`, and re-runs
  `01-nfs.yml --tags dbbackup`.
