# ADR 0031 — Per-release baseline snapshot (dual-track migrations)

- **Status**: accepted (2026-06-19)
- **Deciders**: Reto + agent
- **Builds on**:
  - [ADR 0005 — Greenfield migrations for storage-v2](./0005-greenfield-migrations.md)
  - [ADR 0019 — Second greenfield: re-baseline migrations to cluster head](./0019-second-greenfield.md)
- **Does NOT supersede**: nothing. The numbered migrations stay sealed
  in the tree. This is explicitly **not** a third greenfield.

## Context

A fresh `precis migrate` replays the whole numbered chain
(`0001_initial.sql` … head). Two greenfields (ADR 0005, ADR 0019) have
already collapsed history into a snapshot `0001_initial.sql`, so the
chain is short today — but it grows with every forward migration, and
ADR 0019 spent the "last acceptable greenfield" token. Replaying the
chain on every fresh install is also where flakiness has historically
accumulated: ADR 0019's root cause was sealed files edited post-seal,
drifting their checksums until the runner refused to proceed.

We want a **major release that comes up cleanly** on a fresh database —
one file, one transaction — and we want the snapshot it loads to stay
current automatically rather than by ritual.

## Decision

Adopt a dual-track scheme (Rails `schema.rb` / Ecto `structure.sql`),
without deleting or archiving anything:

- **`migrations/baseline/schema.sql`** — a generated snapshot of the
  schema, *sourced from the migration chain itself*: a throwaway DB is
  built by replaying every numbered migration from scratch, then
  `pg_dump` captures the schema + seed-vocabulary data. The
  `_migrations` ledger block is **synthesised** from the migration
  files (version + checksum, with a fixed `applied_at` sentinel) so the
  snapshot is byte-stable across regenerations and self-stamps the
  ledger when loaded. The file lives in a subdirectory so the runner's
  `glob("*.sql")` never mistakes it for a numbered migration.

- **Runner** (`Migrator(..., baseline=<path>)`): on a *truly fresh* DB
  (no `_migrations` table), load `schema.sql` in one transaction — which
  stamps the ledger to the baked-in head — then apply only the
  post-snapshot tail. On any existing DB, do nothing new: migrate
  forward exactly as before. `--from-scratch` ignores the baseline and
  replays the full chain (used by the convergence test and by
  `dump-schema` itself). The baseline is optional: when the file is
  absent the runner falls back to full replay, so the feature degrades
  safely.

- **`precis db dump-schema`** regenerates the snapshot. It is a
  container op (needs `pg_dump` + a CREATEDB role).

- **`scripts/bump <version>`** ties regeneration to the version bump:
  it sets the version in `pyproject.toml` *and* `src/precis/__init__.py`
  (closing a pre-existing drift the `precis-status` skill surfaces) and
  runs `dump-schema`. The snapshot is thus a **release artifact**,
  matched to the release it ships with. Mid-cycle the committed
  snapshot may sit behind head; a fresh dev checkout then loads it and
  replays the short tail — the original "install the snapshot, then
  migrate from there" model, pinned per release.

### Why this is not a greenfield

A greenfield *deletes* the historical migrations and rewrites the
ledger. Here the numbered migrations stay sealed in the tree as the
upgrade path for existing databases (prod, the cluster). The snapshot
is a derived, regenerable cache of "the chain compiled" — throwing it
away and rebuilding it from the chain is a no-op. ADR 0019's discipline
clause is untouched: a real schema redesign still warrants a new
`storage-vN` line, not another rebase.

## Keeping it honest

The risk of any schema-load track (the documented Rails footgun) is the
snapshot drifting from the chain it claims to represent. Three guards,
matched to where each can run:

1. **Ledger synth↔parse closure** (always, no DB) — the checksums baked
   into the snapshot are exactly the values the runner's integrity gate
   recomputes from the files.
2. **Baseline integrity** (always, no DB) — every version baked into the
   snapshot maps to an unedited migration file, and the baked set is a
   contiguous prefix. This runs in CI, which has no Postgres.
3. **Schema convergence** (DB + `pg_dump`, in the /endsession gate) —
   `load baseline + apply tail` produces the *identical* schema and
   ledger as a full from-scratch replay. This is the deep proof that the
   snapshot is not lying about the chain.

Plus a **release gate** in CI (`check.yml`, tag builds only):
`assert_baseline_at_head` fails the tagged build if the snapshot is
behind head, so a release can never ship a stale baseline.

## Consequences

### Positive

- Fresh install of a tagged release is one file, one transaction — no
  chain replay, no exposure to per-file checksum drift on install.
- The snapshot stays current by construction: `scripts/bump` regenerates
  it, the convergence test proves it, the release gate enforces it. No
  discipline required.
- The upgrade path (forward migrations on a populated DB) keeps being
  exercised: the full-replay test suite and the convergence test both
  run the chain end-to-end, so it does not bit-rot.
- No greenfield spent; existing DBs are untouched.

### Negative

- `dump-schema` needs `pg_dump` + a CREATEDB role — a container op, not
  a pure-Python one. CI's release gate avoids this (text-only check);
  only regeneration needs the tooling.
- A second representation of the schema now exists. The convergence test
  and the release gate are the cost of keeping the two in sync; they are
  mandatory, not optional.
- Mid-cycle the committed snapshot can lag head. This is by design (the
  tail covers it) but means a dev checkout's "fresh install" replays a
  few migrations until the next bump.

## Implementation notes

- The snapshot prepends `CREATE EXTENSION IF NOT EXISTS` for
  `vector` / `pg_trgm` / `btree_gist` (pg_dump `--schema=public` omits
  them) and `CREATE SCHEMA IF NOT EXISTS public`, mirroring the manual
  fix-ups ADR 0019 applied to `0001_initial.sql` — now scripted in
  `schema_dump._clean_dump` / `_assemble` rather than done by hand.
- The runner loads the snapshot through the same `_execute_dump_sql`
  driver the numbered migrations use (it already handles `\restrict`
  markers and `COPY … FROM stdin` blocks), then `RESET search_path`
  because the dump body sets it to `''` for the session.
- Seed-vocabulary tables dumped: `actors`, `kinds`, `relations`,
  `providers`, `chunk_kinds`, `embedders`, `summarizers`,
  `artifact_kinds` (same set as ADR 0019).
