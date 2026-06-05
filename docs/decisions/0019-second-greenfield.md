# ADR 0019 — Second greenfield: re-baseline migrations to cluster head

- **Status**: accepted (2026-06-05)
- **Deciders**: Reto + agent
- **Supersedes**: nothing (does not invalidate ADR 0005; reuses its
  precedent for a one-time re-baseline)

## Context

Between ADR 0005 (first greenfield, 2026-05-21) and today, the
project shipped 17 forward migrations on top of the sealed
`0001_initial.sql`. During the sustained bug-fix run in late May /
early June 2026, four sealed files were edited after seal:

- `0001_initial.sql` — phase-4 catchup added rows to
  `cache_state`, `relations`, `providers` in place
  (commit `f27458d`).
- `0002_provenance.sql` — checksum drift verified against the
  cluster `_migrations` table (file hash `4d7f5af4fadb`, DB
  hash `94153cea17af`).
- `0009_chunk_kind_table.sql` — explicit `BEGIN;` / `COMMIT;`
  that the runner already wraps; sealed copy still ships them.
- `0010_strip_noisy_segment_keywords.sql` — `BEGIN;`/`COMMIT;`
  removed in-place (commit `fd613e6`).

The migration runner's checksum gate now refuses to apply pending
migrations against the cluster master because it sees the drift
on `0002_provenance` and stops before reaching `0016_chunk_embeddings_hnsw`
and `0017_tag_embeddings`. The drift is real but the local cluster
is the only deployed DB; there is no second instance whose history
needs to be preserved.

ADR 0007 (derived-queue) reshaped chunk handling. ADR 0018
(persistent discovery layer) was superseded by F20 less than two
weeks after it landed, leaving `ref_segments` and
`ref_segment_sentences` as dropped tables in the middle of the
migration chain. F20 also added `chunks.keywords` /
`chunks.keywords_meta` to the canonical schema. ADRs 0016 (HNSW)
and 0017 (tag_embeddings) extend the schema further. The
cumulative state on the cluster master is the intended schema; the
historical migration sequence no longer represents anything a
fresh DB needs.

## Decision

Re-baseline. Snapshot the cluster master's `public` schema (PG
17.10) via `pg_dump --schema-only`, dump the seed-vocabulary tables
(`actors`, `kinds`, `relations`, `providers`, `chunk_kinds`,
`embedders`, `summarizers`, `artifact_kinds`) via
`pg_dump --data-only`, prepend `CREATE EXTENSION` for `vector`,
`pg_trgm`, `btree_gist`, and write the combined output as the new
sealed `0001_initial.sql`. Move the original
`0001_initial.sql` through `0017_tag_embeddings.sql` to
`src/precis/migrations/archive/` so the runner's
`migrations_dir.glob("*.sql")` no longer discovers them. On the
cluster master, rewrite `_migrations` in a single transaction:
delete every prior row, insert one row recording the new sealed
`0001_initial` with its fresh checksum.

The greenfield captures everything the cluster master had after
0016 + 0017 were applied directly via psql (the runner refused, so
the SQL was applied out-of-band before the snapshot).

## Consequences

### Positive

- Migration runner's checksum gate is satisfied. `precis migrate
  --dry-run` reports "nothing to apply" against master, and a
  `precis migrate` against a fresh DB applies one file cleanly.
- No more lingering inconsistency between the file tree and the
  cluster's `_migrations` ledger.
- New developers/agents reading the schema in one file see the
  current state without chasing seventeen forward layers, four of
  which were edited after seal.
- ADR 0018's dead tables (`ref_segments`,
  `ref_segment_sentences`) are gone from the schema entirely; no
  one has to read the F20 supersession note to understand why
  they're absent.
- The 0009 / 0010 BEGIN/COMMIT issues and the four post-seal
  edits all disappear with the rebase.

### Negative

- Any external DB (none today) that had migrated to some
  intermediate state cannot be carried forward via the runner.
  Recovery path is to restore from a pre-greenfield backup and
  manually skip to the new baseline.
- The historical sequence of `0002`-`0017` is no longer applied
  in order on fresh DBs. The archive directory preserves the SQL
  but not the ordered application history.
- This is the **second** greenfield. ADR 0005 implicitly carried
  a "we did this once because v1→v2 was a clean break"
  understanding; doing it again risks normalising the pattern.
  See "Discipline" below.

### Discipline going forward

This is the **last** acceptable greenfield without a brand-new
storage-vN line. If a future bug-fix run produces another
post-seal drift, the fix is to ship a corrective forward migration
or re-checksum the affected entries manually — not to greenfield
again. Three signals would justify a third re-baseline:

1. A schema redesign substantial enough to warrant a new
   `storage-vN.md` design document (akin to v1→v2).
2. A vendor migration (e.g. PostgreSQL → SQLite, or pgvector
   replaced by something else) that breaks the existing migration
   syntax wholesale.
3. A security or compliance issue requiring deletion of historical
   migration content (e.g. a credential leaked into a sealed
   `INSERT`).

None of those apply today. The re-baseline today is a one-shot
cleanup after sustained bug-fix activity, not a new pattern.

### Pre-flight backup

Before the re-baseline, the cluster was dumped to
`~/work/backups/precis-cluster-20260605-143621.dump` (151 MB,
custom format, gzip-compressed, 306 TOC entries, restore-tested
via `pg_restore --list`). Restoration instructions are in
the cluster repo's `backups/README.md`.

### Code-side cleanup

Source comments that pointed at specific archived migrations
(`0002_provenance.sql`, `0003_app_state.sql`,
`0004_finding_and_queue_family.sql`, `0007_citation_kind.sql`,
`0009_*.sql`, etc.) were updated to point at `0001_initial.sql`
(the canonical sealed file) plus a parenthetical note naming the
archived original where the symbol/table was first introduced.
`tests/test_initial_migration.py` was updated with the cumulative
seed counts (actors=4, kinds=32, relations=24, providers=14,
chunk_kinds=60, embedders=1, summarizers=1, artifact_kinds=6) and
the 27-table EXPECTED set.

## Implementation notes

- The schema dump was taken from cluster master *after* `0016`
  and `0017` were applied directly via `psql -f` (the runner's
  checksum gate refused). This means the new sealed `0001`
  includes the HNSW index on `chunk_embeddings` and the
  `tag_embeddings` table.
- `pg_dump` emits psql-only `\restrict`/`\unrestrict` directives
  (PG 17 feature). These were stripped from the new sealed
  `0001_initial.sql` because the runner uses psycopg's
  `cur.execute(sql)` path, not psql.
- `CREATE SCHEMA public` was rewritten to
  `CREATE SCHEMA IF NOT EXISTS public` so fresh DBs that already
  have the default `public` don't fail.
- Extensions are prepended manually (`CREATE EXTENSION IF NOT
  EXISTS vector / pg_trgm / btree_gist`) because `--schema=public`
  excludes them.
- The cluster master DB also hosts an `edugraph` schema from a
  separate application; that schema was *not* dumped or included.
  Only `public` (precis-mcp) lands in the new sealed `0001`.
