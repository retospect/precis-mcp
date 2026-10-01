# Backup restore drill — results and standing blockers

Ledger of prod Postgres restore drills — what each one measured, and what
currently stops one from finishing. The point is a recovery time objective
that is a measured number rather than an assumption. To run a drill, use the
`backup-restore-drill` skill (`.claude/skills/backup-restore-drill/SKILL.md`).

**Current answer to "how long to get back": unknown.** No drill has completed
a restore yet.

## Standing blockers

| Blocker | State | Fix |
|---|---|---|
| castor cannot read a dump at speed | open | ssh trust caspar→castor, or the NFS mount, or pre-stage out of band |
| `/mnt/cluster` NFS hangs on castor and pollux | open | `ls` times out at 60 s; the caspar lockd wedge is the candidate |
| DGX nodes cannot pull from Docker Hub | worked around | TLS handshake timeout to registry-1.docker.io; `docker save` on an operator Mac → `docker load` on the node |
| B2 offsite copy absent | open (gr460347) | the v4 CLI renamed `--keepDays`/`--replaceNewer` to kebab case; the template still passes the old spelling |

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
  file with size verification so it resumes. Note this is the **local** copy,
  not the NAS copy — a drill from it proves restorability but not the
  off-host copy. Restore from the NAS tarball on the next attempt.
