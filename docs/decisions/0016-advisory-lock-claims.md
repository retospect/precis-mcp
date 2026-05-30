# ADR 0016 — Postgres advisory-lock work claims (replaces file-based locks)

- **Status**: accepted (2026-05-30)
- **Deciders**: Reto + agent
- **Supersedes**: the filesystem lock-recovery design in ADR 0014's
  "Crash-lock recovery" follow-up (the file-based `.processing/*.lock`
  mechanism in `src/precis/cli/watch.py`)
- **Related**:
  - ADR 0006 — tri-identifier scheme (`ref_identifiers`)
  - ADR 0014 — PDF metadata write-back
  - ADR 0015 — Marker leak mitigation triad

## Context

ADR 0014's follow-up introduced filesystem locks under
`/inbox/.processing/<hash>.lock`. Each ingest wrote its lock before
invoking Marker and deleted it on completion; container restart
swept any leftover locks into `errors/crashed/`, breaking the OOM
restart loop on individual hosts.

This worked for the single-host case but blocks the planned 4-Mac
distributed-ingest scenario (shared SMB inbox + central Postgres):

1. **File-lock semantics across SMB are murky.** Atomic rename
   exists in SMB 3.x but not all server implementations honour it
   reliably; we can't depend on lock acquisition being conflict-free
   when two hosts both target the same lockfile name.
2. **Stale-lock recovery requires bookkeeping.** With four hosts,
   detecting "this lock is from a host that's dead" needs heartbeats
   or shared coordination state that the filesystem can't provide
   cheaply.
3. **`/inbox/.processing` mounted across hosts wastes coordination
   round-trips** that already need to hit the DB anyway for dedup.

## Decision

Move work claims to a Postgres **session-scoped advisory lock**.
`precis.ingest.claim.Claim` opens a dedicated (non-pooled) psycopg
connection, calls `pg_try_advisory_lock(key)` where `key` is the
first 64 bits of `pdf_sha256` interpreted as signed bigint, and
holds the connection open for the duration of the ingest. On exit
(success, exception, or process death), the connection closes and
Postgres releases the lock automatically.

```python
with Claim(dsn, pdf_sha256) as claim:
    if not claim.acquired:
        return None    # another host owns the work
    # ... run Marker, write_paper, etc.
```

`precis_add` returns `None` on claim contention; the watcher's
`process_pdf` treats `None` as "leave the file alone, the owning
host will handle it" — no error path, no move.

## Why advisory locks specifically

- **Auto-release on disconnect.** The whole point. A Mac crashes,
  loses network, gets `kill -9`'d, OOMs, or just hits Ctrl-C — the
  TCP socket dies, Postgres notices, the lock is gone. No
  heartbeat, no TTL reaper, no stale-claim sweeper code to
  maintain.
- **No schema migration.** Advisory locks are an in-memory feature
  of the Postgres server; no `inbox_claims` table to migrate, no
  constraints to evolve.
- **Single round-trip per lifecycle.** `pg_try_advisory_lock` is
  non-blocking (returns true/false) and `pg_advisory_unlock` is one
  more round-trip. Connection establishment is the dominant cost
  (~10 ms over a LAN), still negligible against Marker's 1–15 min
  per PDF.
- **Cross-host correct by construction.** Every host points at the
  same Postgres; `pg_try_advisory_lock` serialises across all of
  them with the same semantics it gives a single host.

## Why a dedicated connection (not the pool)

Session-scoped advisory locks live for the lifetime of the
*Postgres session*. If `Claim` used a pooled connection and the
pool returned it to a different caller after `precis_add` finished,
the lock would persist on that connection and silently grant the
claim to whatever next checks it out. Disasters.

A dedicated connection has exactly the right lifetime — opened on
claim entry, closed on claim exit. Cost: ~10 ms per ingest for
connection setup. Negligible.

## Removals

The following are deleted from `src/precis/cli/watch.py`:

- `_LOCK_DIR_NAME` constant
- `_acquire_lock` / `_release_lock` / `_lock_path_for` /
  `_recover_crashed` helpers
- The recovery sweep at the top of `watch()`
- `lock_dir` parameter on `_PdfHandler`, `process_pdf`,
  `_spawn_batch_subprocess`, and the hidden `_watch_batch_ingest`
  CLI

`/inbox/.processing/` directories left behind from earlier
deployments are harmless — the watcher just doesn't write or read
them anymore. Operators can `rm -rf` at their convenience.

## Multi-host semantics

A representative race on a 4-Mac shared inbox:

```
Mac A: scans inbox, picks file X, computes pdf_sha256, tries claim → ACQUIRED
Mac B: scans inbox, picks file X (same shared file), computes same hash,
       tries claim → BUSY → returns None from precis_add → leaves file
                     untouched → moves to next file in its scan order
Mac A: runs Marker, write_paper, moves file to /corpus, releases claim
```

Round-robin partitioning across hosts is no longer needed — each Mac
just walks the inbox smallest-first; the DB serialises which Mac
owns which file. Load balances automatically: a fast Mac claims and
processes more; a slow Mac claims less.

## Failure modes preserved by Claim semantics

| Failure | Old (file lock) | New (advisory lock) |
|---|---|---|
| Process exits cleanly | `_release_lock` runs | `__exit__` runs |
| Process SIGKILLed (OOM) | Stale lock + `_recover_crashed` sweep on next start | Postgres detects socket close, releases instantly |
| Mac power-off | Stale lock survives reboot | Postgres detects TCP timeout, releases automatically |
| Postgres crash | N/A | All claims released on PG restart — Macs re-attempt; idempotent |
| Network partition | N/A | TCP keepalive eventually trips; lock released; the partitioned Mac sees the next claim attempt fail with a connection error |

## Alternatives considered

- **Row-table claims with heartbeat.** `inbox_claims (pdf_sha256,
  claimed_by, claimed_at)` plus a per-Mac heartbeat thread that
  updates `claimed_at` every minute. Stale-claim reaper sweeps
  rows older than 5 min. Works but adds a table, a heartbeat
  thread, a reaper, migration overhead. All for a property that
  advisory locks give us for free.
- **Hash-shard partitioning.** Each Mac processes only PDFs where
  `hash(path) % K == my_mac_id`. No coordination needed but
  doesn't balance load (a Mac with mostly small PDFs in its shard
  finishes early and sits idle) and breaks when adding a 5th Mac
  (rehash invalidates assignments).
- **Single coordinator + worker queue.** SQS / Redis / RabbitMQ.
  Adds a dependency Postgres already covers.
