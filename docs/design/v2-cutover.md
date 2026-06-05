# V2 cutover — operations runbook

Walk-through for switching the cluster from v1 (`precis-mcp` against
the `acatome` postgres DB) to v2 (`precis-mcp` v2 against the new
`precis` DB).

## TL;DR

```bash
# 0. Pre-flight: verify v1 is still serving and bundles exist.
ls ~/.acatome/papers/ | wc -l                        # ~2500 .acatome files

# 1. Provision the v2 database.
createdb precis

# 2. Apply migrations (creates extensions + schema).
precis migrate --database-url postgresql://localhost/precis

# 3. Backfill from .acatome bundles.
precis jobs ingest-bundles ~/.acatome/papers/ \
    --database-url postgresql://localhost/precis

# 4. Smoke-test reads against v2.
PRECIS_DATABASE_URL=postgresql://localhost/precis \
  precis serve  # then run a couple of get/search calls from an agent

# 5. Flip the agent config to point at v2.
# 6. After 1 week of stable v2 operation, drop the v1 acatome DB.
```

## Pre-conditions

- Postgres reachable on the target host with `CREATE DATABASE` rights
  for the deploy role.
- `vector` and `pg_trgm` extensions installable. `precis migrate` will
  `CREATE EXTENSION IF NOT EXISTS` both — make sure `pgvector` is on
  the path.
- The `.acatome` bundle directory is on the same machine (or NFS-
  mounted) so ingest doesn't pay network round-trips per file.
- Active embedding model is **BAAI/bge-m3** with **dim=1024**. Bundles
  whose stored vectors don't match get re-embedded at ingest cost.
- Set ``PRECIS_EMBEDDER=bge-m3`` (or pass it in your launchd / systemd
  unit) **before** running ingest in production. The default is
  ``mock`` — deterministic and dependency-free, suitable for smoke
  tests and CI but **meaningless for semantic search**. The real
  backend requires ``pip install 'precis-mcp[paper]'`` so
  ``sentence-transformers`` is available.

## Step 1 — provision

```bash
createdb precis
psql precis -c 'CREATE EXTENSION IF NOT EXISTS vector;'
psql precis -c 'CREATE EXTENSION IF NOT EXISTS pg_trgm;'
```

(`precis migrate` does both anyway; this is just to fail fast on
permissions.)

## Step 2 — migrate

```bash
precis migrate --database-url postgresql://localhost/precis --dry-run
# expected: "would apply 1 migration(s):  - 0001_initial"

precis migrate --database-url postgresql://localhost/precis
# expected: "applied 1 migration(s):  - 0001_initial"
```

The migration is **forward-only**. Re-runs are no-ops; checksum
mismatches refuse to apply (sealed migrations are immutable).

## Step 3 — backfill bundles

The repo's `.acatome` bundles are the canonical interchange format.
v1's `acatome` postgres DB is **not** migrated — we re-ingest from
bundles into the fresh v2 DB.

```bash
# Validate parsing (no writes, ~few seconds for the whole corpus):
precis jobs ingest-bundles ~/.acatome/papers/ --dry-run

# Real ingest (~minutes for ~25 papers, ~hour for ~2500):
PRECIS_EMBEDDER=bge-m3 \
PRECIS_DATABASE_URL=postgresql://localhost/precis \
    precis jobs ingest-bundles ~/.acatome/papers/
```

Output per file:

```
  ok    wang2020state  (47 blocks)
  skip  kim2024electrocatalytic  (already present)
  FAIL  borked.acatome  — bundle missing required `header` / `blocks` fields
ingest-bundles: inserted=2412  skipped=0  failed=3  [embedder=bge-m3]
```

Failures are logged but don't abort the run. If any failed, the
process exits non-zero. Re-running picks up from where it left off
(idempotent on DOI), so failed bundles can be fixed and retried.

`--limit N` is useful for partial runs while iterating.

## Step 4 — smoke test

Connect any MCP-speaking agent to the v2 server and run:

```python
get(kind='paper')                              # list papers
get(kind='paper', id='wang2020state')          # overview
get(kind='paper', id='wang2020state/abstract') # abstract only
get(kind='paper', id='wang2020state~3..5')     # blocks 3-5
get(kind='paper', id='wang2020state/cite/bib') # BibTeX
search(kind='paper', q='nitrate reduction')    # block-level RRF search
```

If the embedding model differs from what the bundles were built with,
expect re-embedding to happen at first ingest. Subsequent reads are
fast.

> **Sanity check**: the trailing `[embedder=bge-m3]` line on the
> ingest-bundles summary confirms the production model loaded. If it
> reads `[embedder=mock]`, the env var didn't propagate — semantic
> search will be deterministic but semantically meaningless. Stop
> and fix before flipping the agent config.

## Step 5 — flip agent config

Update the cluster's MCP launcher (Hermes / Asa profiles, the agent's
`mcp.json`, etc.) to point its `precis` MCP at the v2 server. The wire
protocol is the same (FastMCP stdio, four verbs); only the
`PRECIS_DATABASE_URL` env var changes.

## Step 6 — drop v1

After **at least one week** of stable v2 operation:

```bash
dropdb acatome  # the v1 db
```

The v1 source tree at `pips/packages/precis-mcp/` and its `.acatome`
bundle inbox are kept indefinitely — bundles are the canonical
interchange.

## Rollback

If v2 misbehaves, revert the agent config back to v1 — the v1 server
is unmodified and still wired to its `acatome` DB. v2 is additive
during the transition period; nothing v1 reads is touched.

## Re-embed when changing models

Out of scope for cutover; documented in `docs/user-facing/paper_ingest.md` under
"Re-embed flow".
