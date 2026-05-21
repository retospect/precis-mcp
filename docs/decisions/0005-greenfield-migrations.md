# ADR 0005 — Greenfield migrations for storage-v2

- **Status**: accepted (2026-05-21)
- **Deciders**: Reto + agent
- **Supersedes**: nothing (the existing migrations are deleted, not
  superseded — see "Consequences" for rationale)

## Context

`storage-v2.md` originally proposed adding migrations
`0010_pub_id_and_verification.sql`,
`0011_pdfs_and_pages.sql`,
`0012_chunks_and_embeddings.sql`,
`0013_block_jobs.sql`, and a follow-up
`0014_drop_legacy_columns.sql` on top of the existing migrations
`0001`–`0009`. That respects the AGENTS.md rule "forward-only SQL
migrations; sealed once applied".

After surveying the live state, that rule is moot here: the only
populated database is the user's local `precis` DB, which holds
2 181 throwaway test ingests from the in-flight backfill. There
are no external users, no production writes to preserve, and the
plan already includes wiping the DB at step H of the storage-v2
cutover.

The user explicitly opened the door to wiping migrations:

> "the schema, we can start over from the point where we want to,
> there's nothing in there now. So we can wipe migrations and do
> new setup if so desired and if efficiencies can be gained."

## Decision

Replace the existing migrations `0001_initial.sql` through
`0009_*.sql` with **one** new `0001_initial.sql` defining the
storage-v2 schema directly. No layered ALTERs.

## Consequences

### Positive

- **One file to read.** A new contributor reads the v2 schema in
  one sitting; with the layered approach they would chase six
  ALTER TABLEs to find a column's final shape.
- **Tests get simpler.** Many existing tests pin intermediate
  schema states (e.g., "embeddings live on `blocks`" before
  `0012` moves them to `chunk_embeddings`). Greenfield removes
  the intermediate states; the tests get rewritten once instead
  of patched per-migration.
- **No NULL-then-NOT-NULL dances.** Columns added in `ALTER`
  flows must be NULL-able initially (existing rows have no
  default), forcing a backfill, then a follow-up
  `ALTER ... NOT NULL`. Greenfield columns are NOT NULL from
  birth.
- **Migration runner stays simple.** `precis migrate` walks
  `*.sql` in lex order; one file makes the runner's behaviour
  trivially observable.

### Negative

- **No migration path from a populated v1 DB.** If anyone (a
  collaborator, a fork) has a populated `precis` DB on the v1
  schema, they cannot upgrade in place — they must dump, drop,
  re-create, re-ingest. We accept this because: (a) the user is
  the only known operator, (b) the watch loop re-ingests in days
  not weeks, (c) a `pg_dump` of the old DB can be archived for
  reference if needed.
- **Loses the historical trail.** ADR 0001 of `acatome-extract`
  explained why migrations 0007 and 0008 existed (mojibake
  patches, manual-content tagging). That history is preserved
  in the ADR log; the SQL files themselves don't need to keep
  it.
- **A new contributor reading old git blame on
  `0001_initial.sql` will see two unrelated schemas at different
  commit hashes.** We mitigate by referencing this ADR in the
  file's header comment and tagging the pre-greenfield commit
  (`pre-storage-v2-greenfield`) so blame archaeology is still
  possible.

## What gets thrown away

The deleted migration files:

| File | What it added | Lost data path |
|---|---|---|
| `0001_initial.sql` | refs, blocks, ref_identifiers, embedding column | wipe + re-ingest |
| `0002_links.sql` | links table (citation graph; never populated) | empty, no loss |
| `0003_tags.sql` | tags + ref_tags | empty, no loss |
| `0004_cache.sql` | cache_state for paid-tool memoisation | preserved as-is in v2 (no shape change) |
| `0005_*.sql` | dedupe indexes | replicated in v2 |
| `0006_*.sql` | block-level embeddings dim probe | replaced by `embedders` table |
| `0007_*.sql` | mojibake repair backfill | re-runs at re-ingest |
| `0008_*.sql` | manual_content flag (renamed to human_verified_at in v2) | wipe + re-flag |
| `0009_*.sql` | tag-pair index | replicated in v2 |

Tags and links never carried real production data. The cache
state is preserved (still keyed by `(provider, key)` so existing
cache hits survive).

## What lives in `0001_initial.sql` (v2)

In one file:

- Extensions: `vector`, `pg_trgm`, `btree_gin`.
- Core tables: `refs` (with `pub_id`), `ref_identifiers`,
  `pdfs` (with `content_hash`), `blocks`, `chunks`,
  `chunk_embeddings`, `chunk_summaries`, `embedders` (seeded
  with `bge-m3`), `block_jobs`.
- Graph: `links`, `tags`, `ref_tags`.
- Cache: `cache_state`.
- All indexes: per-table btree, GIN on JSONB, HNSW on the default
  embedder.
- All CHECK constraints (retraction status enum, chunk kind enum).

## How to apply

```bash
# Wipe and re-create
docker compose --profile dev run --rm precis-dev \
    bash -lc "psql \$PRECIS_DATABASE_URL -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;'"

# Apply v2
docker compose --profile dev run --rm precis-dev \
    uv run precis migrate
```

## Roll-back

There isn't one. `git revert` of the greenfield commit restores
the old SQL files but they will not apply against a v2-populated
database (column types diverge). The roll-back path is
"export-ingest-state-into-bundle, drop, re-create with old
migrations, re-ingest from bundles" — i.e., a full restore. We
accept this; the cost of a rollback is high but the v2 design has
been reviewed and the data is throwaway anyway.
