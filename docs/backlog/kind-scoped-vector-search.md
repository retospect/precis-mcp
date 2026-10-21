---
status: ready
title: "kind-scoped semantic search: per-kind partial HNSW for big kinds, exact scan for small"
pillar: platform
thread: session-mcp-shared-server
prio: high
---

# Kind-scoped semantic search

Root cause of gr495190. Code and migration are built (0226 +
`precis backfill-embedding-kinds`); what remains is the prod rollout below.

## Motivation / why

`chunk_embeddings` has one HNSW index (`chunk_embeddings_vec_hnsw_idx`) over
3.94M rows, and `refs.kind` sits two joins away. A per-kind semantic search
walks the global graph and filters after the join (strict_order iterative
scan). Prod, 2026-10-17, LIMIT 20, `scripts/prod-psql --ro`:

| kind | embedded chunks | current plan | time |
|---|---|---|---|
| memory | 24,527 | global HNSW, 30,066 rows scanned for 20 | 33.6 s |
| paper | 3,410,501 | global HNSW, 21 scanned | 0.32 s |
| oracle | 827 | planner already picks refs→chunks→pkey + sort | 3.6 ms |

Embedded chunks per kind on prod: paper 3.41M, job 179k, anki 100k, orcid
57k, draft 54k, memory 24.5k, news 19.5k, perplexity-research 18.8k,
websearch 14.6k, patent 10.1k, quest 9.6k, semanticscholar 7.6k, tex 7.6k,
gripe 6.5k, conv 5.2k, finding 4.8k, then 30 kinds under 3k.

Exact scan over a kind's own chunks (filter first, then distance), prod:
finding 4.4k rows 0.21 s, gripe 4.3k rows 0.33 s, memory 11.9k rows
1.3–2.1 s, job 135k rows 7.6 s. That is ~50–170 us per row (vector detoast
plus distance), so the exact path only stays under ~0.3 s up to ~5k rows.

## Design (Reto 2026-10-17, ~15:30Z and ~15:35Z)

- **Threshold 5,000 embedded chunks.** Below it: exact scan. At or above it:
  a partial HNSW index `WHERE kind = '<k>' AND status = 'ok' AND vector IS
  NOT NULL` (14 kinds). Paper stays on the global index: it is 92% of it, so
  the post-filter costs nothing.
- **Postgres owns the kind copy.** `chunk_embeddings` gains `ref_id` and
  `kind`. A BEFORE INSERT/UPDATE trigger fills them from chunks → refs when
  NULL; no Python sets them. `FK (chunk_id, ref_id) → chunks` and
  `FK (ref_id, kind) → refs`, both ON UPDATE/DELETE CASCADE, over new
  `UNIQUE (chunk_id, ref_id)` on chunks and `UNIQUE (ref_id, kind)` on refs.
  A wrong copy fails on insert; a kind rename follows. refs.kind is not
  updated by application code today (only migration 0018 renamed kinds;
  the refs triggers `refs_revision_kind` / `gripe_status_refs_kind`
  anticipate it), and chunks.ref_id is never updated.
- **Router** (`store/_chunks_ops.py::_ann_route`), single-kind only, in
  `search_chunks_semantic` and the fused query's semantic CTE. A kind in
  `_ANN_PARTIAL_KINDS` takes its partial index only when that index exists,
  is `indisvalid` (read from `pg_index` predicates) and the
  `chunk_embeddings_ref_kind_filled` CHECK is validated; otherwise it stays
  on the global index (today's path). Any other kind takes the exact scan
  (query-only, live from deploy). Paper, multi-kind and no-kind stay global.
- **Indexes are not in the migration.** The 14 partial HNSW indexes and
  the `(ref_id, kind)` btree would each take a SHARE lock and scan ~3.9M
  rows inside the migration transaction. `precis backfill-embedding-kinds`
  builds them `CONCURRENTLY` after the fill, once from data, and checks
  `indisvalid` (an INVALID leftover is dropped and rebuilt). Small DBs
  (fresh, test, dev) get the btree in the migration. Composes with g-search-slow's per-kind fan-out
  (f27a5811d): that splits multi-kind calls into single-kind ones, and every
  route still calls `_prepare_filtered_ann`, where its statement_timeout lives.

## Rollout (release owner; Reto chose option 1, 2026-10-17)

Fill every kind including paper, then rebuild the global HNSW.

**Scheduled (Reto 2026-10-21): the weekend of 2026-10-24/25, after a
fresh prod DB backup is verified.** Steps 1–2 are live on prod (search is
correct, only the per-kind fast path is missing); steps 3–7 wait for that
window.

1. **Pre-build the two parent keys (mandatory)**, outside a transaction:
   `CREATE UNIQUE INDEX CONCURRENTLY refs_ref_id_kind_key ON refs (ref_id,
   kind)` and `... chunks_chunk_id_ref_id_key ON chunks (chunk_id, ref_id)`;
   confirm `indisvalid`. The runner wraps each file in a transaction
   (`Migrator.apply_all`), so 0226 cannot use CONCURRENTLY. Its guard raises
   on an INVALID copy (IF NOT EXISTS would skip it) and refuses to build the
   chunks key itself when chunks has over 1M rows (a SHARE lock over a
   6.2 GB heap scan, est. 10–30 s of blocked chunk writes).
2. **Deploy 0226.** `SET LOCAL lock_timeout = '5s'` bounds every DDL wait
   (the runner sets 0); on timeout the file rolls back and re-applies
   cleanly. ADD COLUMN (no default) is metadata-only; FKs and CHECK are NOT
   VALID; no index is built.
3. **Backfill, non-paper then paper:** `precis backfill-embedding-kinds
   --apply --no-indexes` (500-id windows, 60 s statement_timeout per window,
   0.5 s pause; resumable with `--after`). Cost: every row is a non-HOT
   update (dev: 0 of 5,000 HOT) and is re-inserted into the 29 GB global
   HNSW. Dev, 62k-vector index, one stream: 8.6 ms/row paper, 9.3 ms/row
   memory. Extrapolated: ~1.3 h for the 534k non-paper rows, ~8 h for 3.41M
   paper rows, more on prod's 60× larger graph.
4. **Validate:** the same command validates the three constraints when no
   row is unfilled (SHARE UPDATE EXCLUSIVE; inserts keep flowing).
5. **Build the per-kind indexes:** `precis backfill-embedding-kinds --apply
   --skip-fill --database-url <direct postgres DSN>`: btree plus 14 partial
   HNSW, `CREATE INDEX CONCURRENTLY`, ~534k vectors, ~4 GB. The connection
   passes `options=-c statement_timeout=0`, so it needs a direct Postgres
   DSN, not pgbouncer. Each kind switches to its index within 60 s of its
   index turning valid.
6. **REINDEX the global HNSW (required):** `... --skip-fill --no-indexes
   --reindex-global` runs `REINDEX INDEX CONCURRENTLY
   chunk_embeddings_vec_hnsw_idx`, clearing the 3.94M dead entries step 3
   left. Needs ~29 GB free disk while both copies exist; a failure leaves an
   INVALID `_ccnew` index to drop.
7. **Regenerate `docs/reference/schema.md`** from prod (`scripts/gen-schema`).

Trigger + FK cost on the write path (dev, 10k-row INSERT ... SELECT): with
the HNSW indexes dropped, 0.22–0.24 s without 0226 vs 0.31–0.45 s with it
(+9–23 us/row); with them, 72–101 s either way (HNSW insert is ~7 ms/row),
so the trigger is under 0.5% of an embedding write. Extra index sizes:
`refs (ref_id, kind)` ~20 MB (refs_pkey is 18 MB for 492k rows);
`chunks (chunk_id, ref_id)` ~140 MB (chunks_pkey 139 MB, 4.28M rows);
`chunk_embeddings (ref_id, kind)` ~150 MB; the 14 partial HNSW indexes hold
~534k vectors, ~4 GB at the global index's ~7.4 KB/vector.

## Acceptance criteria

- `search(kind='memory', q=...)` on prod: plan uses
  `chunk_embeddings_vec_hnsw_memory_idx`, under 1 s. (Dev at 2k memory rows
  chose an exact sort over the partial index on cost, 21 ms; check the
  prod plan at 24k rows.)
- A small kind (finding) returns the brute-force top-k in under 0.5 s.
- paper unchanged (~0.3 s).
- `tests/test_kind_scoped_ann.py` green: fill on insert, mismatched kind or
  ref_id fails, kind update cascades, backfill validates, indexes build
  valid (and an invalid one is rebuilt), the router refuses a missing or
  invalid index and an unvalidated CHECK, partial index in the plan, small
  kind takes no HNSW, results equal brute force.

## Explicitly NOT in scope

- Multi-kind ANN (stays on the global index; the fan-out splits it).
- `dreamable_region` / angle neighbours (kind lists).
- Automatic index creation when a kind crosses 5k: add the kind to
  `_ANN_PARTIAL_KINDS` and re-run the index phase. The exact path degrades
  linearly (~60 us/row) until then.

## Target + blast radius

`store/_chunks_ops.py` (`search_chunks_semantic`, `search_chunks_fused`),
migration 0226, `cli/embedding_kind_backfill.py`; every embedding INSERT
(the worker's `EmbedHandler.write_ok`, `ChunkStore` embedding writes) now
fires the trigger and four FK checks.
