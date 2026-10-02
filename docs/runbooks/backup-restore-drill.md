# Backup restore drill — results and standing blockers

Ledger of prod Postgres restore drills — what each one measured, and what
currently stops one from finishing. The point is a recovery time objective
that is a measured number rather than an assumption. To run a drill, use the
`backup-restore-drill` skill (`.claude/skills/backup-restore-drill/SKILL.md`).

**Current answer to "how long to get back": about 3 hours**, measured
2026-10-01 — ~65 min to get a 21 GB dump onto the restore host from outside
the cluster LAN, then 110 min for `pg_restore`. The restore itself passed
every check. Two caveats: the staging time collapses to minutes if the host
can read the dump over the cluster's own network, and the 110 min is
dominated by one index build that has an untried tuning lever.

## Standing blockers

| Blocker | State | Fix |
|---|---|---|
| castor cannot read a dump at speed | open | ssh trust caspar→castor, or the NFS mount, or pre-stage out of band |
| `/mnt/cluster` NFS hangs on castor and pollux | open | `ls` times out at 60 s; the caspar lockd wedge is the candidate |
| DGX nodes cannot pull from Docker Hub | worked around | TLS handshake timeout to registry-1.docker.io; `docker save` on an operator Mac → `docker load` on the node. `pgvector/pgvector:pg17` (arm64) is already loaded on castor |
| Restore spends 1h55m on one HNSW index build | open, untried lever | default 64 MB `maintenance_work_mem`; raise it and `max_parallel_maintenance_workers` for the restore session, then measure |
| B2 offsite copy absent | repo fix landed 2026-10-01 (gr460347) | flags renamed to kebab case and the CLI pinned `b2[full]>=4,<5`; the **cluster step is still pending** — `deploy/playbooks/11-backups.yml`, one manual sync, then confirm `b2 ls b2://openclaw-backups/backups/postgres/` |

The tailnet path caspar→castor is **direct, ~2 ms** — the slow transfer is not
a cluster network problem. An operator Mac outside the cluster LAN measured
~3.4 MB/s to caspar, which is why relaying 21 GB through it takes ~1.7 h. The
cheap permanent fix is ssh key access from caspar to castor.

## Drill log

### 2026-10-01 — attempt 1: did not restore

- **Copy**: NAS `precis_prod_2026-10-01.tar.zst`, 21 GB; dump taken 03:30 UTC,
  finished 04:28 UTC, NAS archive completed 04:31 UTC.
- **Prod baseline** (read-only, 20:4x UTC): PG 17.9; extensions vector 0.8.2,
  pg_trgm 1.6, pgcrypto 1.3, btree_gist 1.7, pg_stat_statements 1.11,
  plpgsql 1.0; `refs` 457,677; `chunks` 4,113,651; `chunk_embeddings`
  reltuples 3,773,829. Newest migrations 0174_taxon_seed (precis) and
  0017_se_optics_channels (precis_se).
- **Stopped before `pg_restore`** on two blockers: no fast path from the dump
  to castor, and no pullable `pgvector/pgvector:pg17` image.
- **Transfer time, restore time, verify result: none.** Nothing was restored.
- **Teardown confirmed**: scratch directory removed, castor root volume back
  to 3.1 TB free / 13% used. Reads on caspar and the NAS only; nothing written
  to prod.
- The drill read the B2 log's wrong end and reported `b2: command not found`
  as the live failure. Both strings are in that log: 102 `command not found`
  lines are older history, and the 77 most recent (through 2026-10-01
  04:30 UTC) are `unrecognized arguments: --keepDays --replaceNewer`. The
  binary is installed (`/usr/local/bin/b2` → `/opt/b2/bin/b2`). Read the
  **tail** of that log, and check which error is newest before fixing.
- Incidental: `/opt/shared/backups/postgres/` on caspar also holds 56
  `openclaw_*.sql.gz` files from 2026-04-07 to 2026-05-31. That series stopped
  four months ago and is outside the 7-day prune.

### 2026-10-01 — groundwork after attempt 1

- `pgvector/pgvector:pg17` (linux/arm64, matching castor's aarch64) mirrored
  onto castor by `docker save | ssh castor docker load`, 18 s. The image
  blocker no longer applies to castor.
- The 2026-10-01 local dump directory (21 GB, 134 files) pre-staged to castor
  `/var/tmp/restore-drill-20261001/dump` over the operator-Mac relay, file by
  file with size verification so it resumes: **65 min**, 133 copied, 0
  failures. Note this is the **local** copy, not the NAS copy — a drill from it
  proves restorability but not the off-host copy. Restore from the NAS tarball
  on the next attempt. The staged dump is still on castor, read-only
  throughout; delete it when the next drill supersedes it.

### 2026-10-01 — attempt 2: PASS

The first completed restore. `pg_restore` exited 0 with a **zero-byte error
log**.

- **Copy**: the pre-staged local dump above (dump taken 03:30 UTC).
- **Restore**: `pg_restore -j 8 --no-owner --no-privileges` into a throwaway
  `pgvector/pgvector:pg17` container (PG 17.10; prod is 17.9). Started
  22:22:48Z, finished 00:13:02Z — **real 110m14s**. Restored database 70 GB.
- **Container start to verified: ~1h52m**; verification itself ~3 min.
- **Where the time goes**: the HNSW index `chunk_embeddings_vec_hnsw_idx` on
  3.77 M vectors was the last item running, one `CREATE INDEX` with 3 parallel
  workers, for **1h55m**, at the default 64 MB `maintenance_work_mem`, spent in
  "loading tuples". Raising `maintenance_work_mem` and
  `max_parallel_maintenance_workers` for the restore session is the obvious
  lever and has not been tried.
- **Verify, all green**: extensions match prod exactly (vector 0.8.2, pg_trgm
  1.6, pgcrypto 1.3, btree_gist 1.7, pg_stat_statements 1.11, plpgsql 1.0);
  `refs` 456,492 vs prod 457,677 (−0.26%); `chunks` 4,098,641 vs 4,113,651
  (−0.36%); `chunk_embeddings` reltuples 3,768,936 vs 3,773,829 after a 10 s
  `vacuum analyze` — all below prod and within a day's growth, as a morning
  dump should be.
- **Migrations**: newest per plugin on the restore was precis
  `0172_design_states_occupancy_pose` (applied 2026-09-30 10:59Z). This is
  *correct*, not a gap: prod applied 0173/0174 at 17:53Z and 0175/0176 at
  23:06Z, all after the 03:30Z dump. Checking `applied_at` is what turns an
  apparent discrepancy into a confirmation.
- **Vector search works**: cosine query via the HNSW index, 4.3 ms, Index Scan
  in the plan. (The L2 form sequential-scans in 7.8 s — see the skill's step 5.)
- **Teardown**: container removed, pgdata deleted from inside the image
  (uid 999 mode 700 defeats `rm -rf` as `deploy`), `df -h /` back to 3.1 T
  available / 14% — unchanged from before the drill. Prod was read once,
  read-only, for the `_migrations` cross-check. Nothing ran on caspar or
  melchior.
