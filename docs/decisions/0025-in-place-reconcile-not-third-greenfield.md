# ADR 0025 — In-place reconcile of cluster drift (not a third greenfield)

- **Status**: accepted (2026-06-07)
- **Deciders**: Reto + agent
- **References**: ADR 0019 (second greenfield) — applies its stated
  guidance that post-seal drift be fixed by a corrective reconcile,
  "not to greenfield again". Does not supersede 0019.

## Context

After ADR 0019 re-baselined the migration chain into a single sealed
`0001_initial.sql` (2026-06-05), the project shipped three further
forward migrations on top:

- `0002_chunk_keywords.sql`
- `0003_drop_legacy_segments.sql`
- `0004_drop_quest_kind.sql`, `0005_gripe_first_class_and_jobs.sql`,
  `0006_fix_gripe_relation.sql`, `0007_dreaming.sql`

The cluster master (`precis_prod` on caspar) had drifted from its own
ledger: `public._migrations` recorded only `{0001, 0002, 0003}`, yet
the live schema was **missing** objects those very migrations create
(e.g. `ref_events`, `tag_embeddings`, `patent_watches`) and also
lacked everything from `0004`–`0007`. The runner's checksum gate and
forward-only model could not safely roll the DB forward from this
state, because the recorded history did not describe the actual
schema. An untracked working-tree file
(`0003a_reconcile_squash_drift.sql`) had also accumulated as a
half-finished corrective attempt.

ADR 0019 anticipated exactly this situation and prescribed the remedy:
post-seal drift from a sustained bug-fix run should be fixed with a
corrective forward migration / reconcile, **not** a third greenfield.
None of 0019's three "justify another re-baseline" signals applied (no
storage-vN redesign, no secret-in-sealed-SQL, no wholesale schema
replacement).

## Decision

Reconcile the live database in place to the canonical target — the
schema produced by applying `0001`–`0007` to an empty DB — and make
its ledger honest, rather than greenfielding again.

Procedure actually executed:

1. **Backup**: quiesced dump
   `precis_prod_premigrate_20260607_155206.dump` (custom format,
   restore-tested with PG 17 `pg_restore --list`).
2. **Build the target**: applied `0001`–`0007` to a throwaway DB
   (`precis_migtarget`) — the canonical schema.
3. **Diff**: `migra` produced the exact DDL gap between live and
   target (`reconcile.sql`): the missing tables, the `chunks`
   salience columns (`accesses`, `last_seen`, `last_dreamt`), indexes
   (incl. `chunk_embeddings_vec_hnsw_idx`), constraints, and FKs.
4. **Rehearse on real data**: restored the prod dump into a clone and
   applied the reconcile, then re-diffed clone→target = **empty**.
   This proved the reconcile brings real prod data exactly to target.
5. **Apply to prod**: one atomic transaction with all touched-table
   locks acquired up front
   (`LOCK TABLE chunks, chunk_embeddings, artifact_kinds, refs IN
   ACCESS EXCLUSIVE MODE`), `lock_timeout` for clean rollback,
   `maintenance_work_mem = '8GB'` + parallel workers for the HNSW
   build. Verified post-apply with `migra prod→target` = **empty**.
6. **Make the ledger honest**: inserted ledger rows `0004`–`0007`
   with their file checksums, so `public._migrations` records
   `{0001…0007}` and every recorded checksum matches the repo file
   byte-for-byte.
7. **Repo cleanup**: deleted the untracked stray
   `0003a_reconcile_squash_drift.sql`.

## Why up-front table locks

Two earlier apply attempts deadlocked: the long DDL transaction held
`AccessExclusive` on `chunks` (from `ADD COLUMN`) and later requested
a lock on `artifact_kinds`, while a concurrent ingest writer held the
opposite order. PostgreSQL's deadlock detector aborted the migration
(~1s), before `lock_timeout` could apply. Acquiring **all** table
locks atomically at `BEGIN` removes the incremental lock ordering a
writer can cross: any writer that wakes mid-build queues behind us
instead of deadlocking. The root operational cause was incomplete
quiescence — the `precis watch` auto-ingest daemon runs on every
node (caspar, balthazar, melchior via launchd; spark via systemd),
and only caspar had been stopped. All watchers must be down for a
clean window; the up-front lock makes the apply robust even if one
slips through.

## Consequences

- **No schema or ledger ambiguity remains.** Two independent proofs:
  `migra prod→target` is empty, and every ledger checksum equals its
  repo file. `precis migrate` from a clean checkout is a no-op.
- **History is preserved, not discarded.** Unlike a greenfield, the
  `0001`–`0007` chain stays intact and continues to describe fresh
  installs; `archive/` is untouched.
- **Operational lesson recorded**: cluster-wide quiescence for schema
  work means stopping `precis-watch` on *all* nodes
  (`launchctl bootout system/com.precis.watch` on darwin;
  `systemctl stop precis-watch` on linux), and large schema applies
  should pre-acquire table locks and raise `maintenance_work_mem` for
  the HNSW index build.
- **No version bump.** This is an operational reconciliation of the
  deployed DB to the already-released schema (`v8.5.0`), not a
  user-visible change.
- Scratch databases used during the reconcile (`precis_migclone`,
  `precis_migtarget`, `precis_mig_test`) were dropped afterward.
