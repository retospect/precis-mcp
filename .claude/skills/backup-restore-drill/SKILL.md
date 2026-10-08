---
name: backup-restore-drill
description: "Use to test prod Postgres backups or answer 'could we actually restore?': restores a nightly dump into a throwaway container, verifies, times, tears down."
---

# backup-restore-drill — prove the backups restore

A backup nobody has restored is a hope. This drill restores one real nightly
dump end to end and compares it with prod, so the answer to "how long to get
back, and what would be missing" is a measured number with a date on it.

**Never on caspar** (the prod DB node): its disk filled to 100% once
(2026-08-04) and a 90 GB restore there would do it again. **Not on
melchior** either: it runs the prod workers and the in-process agent lane and
is memory-fragile. Use an idle DGX node (castor or pollux: Docker, ~3 TB free,
121 GB RAM). Never write to prod; the drill only reads the dump files.

## Prerequisites — check these FIRST, they blocked the 2026-10-01 attempt

A drill that discovers these at step 2 wastes an hour. Verify all three before
staging anything:

1. **A fast path from the dump to the restore host.** As of 2026-10-01 castor
   has none: the NAS is not mounted there, `ssh caspar` from castor/pollux
   fails (host-key verification, then publickey), and `/mnt/cluster` (NFS from
   caspar) hangs — `ls` times out at 60 s on both DGX nodes
   (`caspar_nfs_lockd_wedge` is the candidate). Relaying through an operator
   Mac (`ssh caspar cat | ssh castor zstd -dc | tar -x`) measured ~3 MB/s, so
   the 21 GB NAS tarball would take ~2 hours. Fix one of: key access
   castor→caspar, the NFS mount, or pre-stage the dump out of band.
2. **The Postgres image, already on the host.** `docker pull
   pgvector/pgvector:pg17` fails on castor and pollux with a TLS handshake
   timeout to registry-1.docker.io, and neither node caches it. Mirror it:
   `docker save` on the Mac → `docker load` on the restore host.
3. **Scratch space, by its real path.** There is no `/scratch` on castor — one
   root NVMe at `/` (3.7 TB). Pick a directory under a volume you have
   confirmed with `df -h`, and create it as `deploy`.

## Where the backups are

| Copy | Location | Kept | Format |
|---|---|---|---|
| Local | caspar `/opt/shared/backups/postgres/precis_prod_<YYYYMMDD>_033000/` | ~3 days | `pg_dump -Fd`, 4 jobs, zlib 6 |
| NAS | `/opt/nas/botshome/backups/postgres/precis_prod_<YYYY-MM-DD>.tar.zst` | 90 days | the directory dump, tar + zstd |
| Offsite | B2 bucket `openclaw-backups`, prefix `backups/postgres/` | 7 days | synced by `b2_sync.sh` 04:30 UTC |

Local retention is deliberately short — `pg_backup.sh.j2` prunes at
`-mtime +2` (~3 dumps) because Postgres runs on that host and each dump is
~21 GB. Three local dumps is correct, not a prune bug; depth comes from the
NAS copy.

Schedule: caspar reboots 03:00 UTC, `pg_dump` 03:30–~04:20, NAS copy and B2
sync after. Don't start a drill copy from caspar inside that window. Config:
`deploy/roles/backups/`. Logs on caspar: `/opt/shared/logs/backup-pg.log`,
`backup-b2.log` — **read both first**; a drill that only proves the local copy
says nothing about the offsite one. **The B2 copy does not exist right now**
(gr460347, prio 1): `b2_sync.sh` line 16 has logged `b2: command not found`
102 times and the log has no success line, so the offsite copy has been absent
since mid-July 2026. Until that gripe closes, "restore the B2 copy instead"
is not an option — confirm from the log, don't assume a fix landed.

Prod facts to compare against, measured 2026-10-01 20:4x UTC: server PG 17.9;
extensions vector 0.8.2, pg_trgm 1.6, pgcrypto 1.3, btree_gist 1.7,
pg_stat_statements 1.11, plpgsql 1.0; `refs` 457,677; `chunks` 4,113,651;
`chunk_embeddings` reltuples 3,773,829 (~48 GB); PGDATA ~90 GB. Re-read them
at drill time (step 1) — they grow.

## Steps

Prefer the **NAS copy**: it is the one you would restore from if caspar died.
Every few drills, restore the B2 copy instead.

1. **Baseline from prod (read-only).** Through `scripts/prod-psql`:
   exact `count(*)` for `refs`, `chunks`; `reltuples` for `chunk_embeddings`;
   `select extname, extversion from pg_extension`; the newest applied
   migration from the migration ledger table (see
   `docs/reference/schema.md`). Note the dump's timestamp — prod has moved on
   since, so counts on the restore should be ≤ prod and close.
2. **Stage on the restore host** (castor). Scratch dir
   `/scratch/restore-drill-<YYYYMMDD>` on the big volume. Unpack the NAS
   tarball there (`zstd -dc … | tar -x`), or `rsync` the local dump dir from
   caspar. Time it.
3. **Start a throwaway server:** `docker run -d --name restore-drill
   --shm-size=8g -e POSTGRES_PASSWORD=<random> -v <scratch>/pgdata:/var/lib/postgresql/data
   -v <scratch>/dump:/dump:ro pgvector/pgvector:pg17` (must be PG 17 and a
   pgvector ≥ prod's version). Bind no host port — use `docker exec`.
4. **Restore:** `createdb precis_restore`; `pg_restore -j 8 --no-owner
   --no-privileges -d precis_restore /dump`, under `time` (`/dump` is the dump
   directory itself when mounted as in step 3 — no subdirectory). Authenticate
   with `docker exec -u postgres`. Errors other than missing roles are
   findings; keep the full log. **Budget ~110 min** (measured 2026-10-01) and
   expect one `CREATE INDEX` to dominate: the HNSW build on ~3.8 M vectors
   took 1h55m of it at the default 64 MB `maintenance_work_mem`. Raising
   `maintenance_work_mem` to several GB and `max_parallel_maintenance_workers`
   for the restore session is the obvious lever — untested, so measure it
   rather than assuming.
5. **Verify:** extensions and versions match prod; the counts from step 1
   (restore ≤ prod, within a day's growth); the newest migration **per plugin**
   matches what prod had *at dump time*, not now — a dump taken 03:30Z predates
   migrations applied later that day, so check `_migrations` on prod with
   `applied_at` before calling a difference a fault; `vacuum analyze`
   completes; and a vector query uses the index:
   `select chunk_id from chunk_embeddings order by vector <=> (select vector
   from chunk_embeddings where status='ok' and vector is not null limit 1)
   limit 5`. The columns are `chunk_id, embedder, vector, status` — there is no
   `id` or `embedding`. The HNSW index is **cosine** (`vector_cosine_ops`,
   partial on `status='ok' AND vector IS NOT NULL`), so `<=>` hits the index
   (~4 ms) while `<->` sequential-scans (~8 s) and proves nothing about it.
6. **Record** in `docs/runbooks/backup-restore-drill.md` §Drill log: date,
   copy used, dump timestamp, transfer time, restore time, verify result,
   anything that broke. A failure is a gripe at `prio=1`.
7. **Tear down:** `docker rm -f restore-drill`, then delete the pgdata dir and
   confirm the space is back (`df -h`). The restored DB holds prod data; it
   must not outlive the drill. **A plain `rm -rf` fails**: pgdata ends up owned
   by uid 999 mode 700, so even `du` is denied to `deploy`. Delete it from
   inside the image instead —
   `docker run --rm --entrypoint find -v <pgdata>:/p pgvector/pgvector:pg17 /p
   -mindepth 1 -delete`, then `rmdir` — or use `sudo` where available. Keep a
   pre-staged *dump* directory if one exists: it is read-only throughout and
   costs an hour to re-copy.

## What counts as a pass

Restore exits clean (role-ownership noise aside), every check in step 5
holds, and the total time from "start copy" to "verified" is written down.
That time is the recovery time objective we actually have.

**Last drill: 2026-10-01, PASS.** ~65 min to stage the dump plus 110 min to
restore — about 3 h end to end, with the HNSW build the single biggest term.
Numbers and the standing blockers: `docs/runbooks/backup-restore-drill.md`.
