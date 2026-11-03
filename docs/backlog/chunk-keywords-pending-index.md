---
status: in-progress
pillar: platform
---

# Pre-build chunks_keywords_pending_idx on prod, then ship its migration

Ops item, not a code change — gr520236 / gr550973 (chunk_keywords drain).

**Code drafted, blocked on the ops step.** The guard-only migration
(`src/precis/migrations/0274_chunks_keywords_pending_idx.sql`) and its
test (`tests/test_migration_0274_chunks_keywords_pending_idx.py`) are
committed on `fix/chunk-keywords-pending-index`. Do NOT land/merge
0274 until ops has pre-built `chunks_keywords_pending_idx`
CONCURRENTLY on prod (DDL below) — on prod's ~4.7M-row `chunks`, the
migration's guard refuses to build it inline and the apply fails,
blocking the deploy pipeline until the pre-build lands.

The dedicated chunk_keywords lane claims the fresh backlog
`WHERE keywords IS NULL … ORDER BY chunk_id DESC` (newest-first, so
fresh ingests get labels first). Its cheapness depends on a partial
index that must NOT be shipped as an inline `CREATE INDEX` migration:
a plain build on prod's `chunks` (~4.7M rows) takes a SHARE lock for a
full heap scan and stalls ingest. Pre-build it CONCURRENTLY, outside a
transaction, BEFORE the migration ships:

```sql
CREATE INDEX CONCURRENTLY chunks_keywords_pending_idx
    ON chunks (chunk_id DESC)
    WHERE keywords IS NULL
      AND chunk_kind NOT IN ('card_authors', 'card_combined',
          'card_description', 'card_title', 'equation', 'figure', 'paper_note',
          'references', 'scan_geometry', 'table')
      AND length(text) >= 150
      AND (meta->>'no_index') IS DISTINCT FROM 'true';
```

The predicate is owned in code —
`src/precis/workers/chunk_keywords.py::_eligibility_sql` renders the
same literals into the claim WHERE (the planner can only use the
partial index when it proves the claim implies the predicate), and
`PENDING_INDEX_DDL` in the same module carries this statement text; a
test pins claim and DDL to the same literals. The claim works without
the index — just slower — so the lane can ship ahead of the build.

Once the index exists and is valid, ship the migration as a guard-only
migration (the salvage branch `salvage/agent-a726694d1046371e6` had the
full shape: refuse an INVALID index, refuse a pre-built index whose
recorded predicate differs, refuse to build inline when `chunks`
reltuples > 1M; `CREATE INDEX IF NOT EXISTS` so small dev/test DBs get
it in-transaction). Get a migration number from the coordinator; never
edit the predicate literals — they must stay identical to the claim's.

## Verify the claim plans on prod before enabling (0266 review)

Every tier of `claim_chunks_without_keywords` is two-level: an inner
`kw_cand` candidate scan (`LIMIT claim_limit *
_CANDIDATE_SCAN_MULT`, 4 → 2000 rows at the default 500 batch) that
applies the embedding gate itself as an `EXISTS` on
`chunk_embeddings`, and an outer query that re-joins the embedding
for the vector payload and takes the row locks. Because the gate is
inside the cap, the cap counts only claimable rows: a
not-yet-embedded head block (ingest landed, `embed` behind — or
permanently failed embeddings) is walked past, never counted, and the
claim returns exactly the rows and order the unbounded query would.
An earlier shape filtered embeddings in the outer query only, so a
>cap unembedded head evicted every claimable row and the lane slept
while 1.6M embedded rows waited deeper. The claim runs on every pass
INCLUDING empty polls, so verify the plans before
`precis_worker_kwdrain_enabled=true`. Plain `EXPLAIN`, not `EXPLAIN
ANALYZE` — ANALYZE executes the `FOR UPDATE` and takes real row
locks.

**Tier 1 (conv/draft priority).** Must drive from `refs`, not walk
`chunks_pkey` backward hunting rare conv/draft rows — expect
`CTE kw_refs` materialized from `refs_kind_idx`, then a nested loop
into `chunks_ref_id_idx` inside `kw_cand`:

```sql
EXPLAIN
WITH kw_refs AS MATERIALIZED (
    SELECT ref_id FROM refs WHERE kind IN ('conv', 'draft')
),
kw_cand AS MATERIALIZED (
    SELECT c2.chunk_id
      FROM chunks c2
      JOIN kw_refs kr ON kr.ref_id = c2.ref_id
     WHERE c2.chunk_kind NOT IN ('card_authors', 'card_combined',
           'card_description', 'card_title', 'equation', 'figure',
           'paper_note', 'references', 'scan_geometry', 'table')
       AND length(c2.text) >= 150
       AND (c2.meta->>'no_index') IS DISTINCT FROM 'true'
       AND EXISTS (
           SELECT 1 FROM chunk_embeddings ce2
            WHERE ce2.chunk_id = c2.chunk_id
              AND ce2.embedder = 'bge-m3' AND ce2.status = 'ok'
              AND ce2.vector IS NOT NULL
              AND ce2.content_sha IS NOT DISTINCT FROM c2.content_sha)
       AND (c2.keywords IS NULL
         OR (c2.keywords_meta->>'version') IS DISTINCT FROM '1.0'
         OR (c2.keywords_meta->>'content_sha') IS DISTINCT FROM c2.content_sha)
     ORDER BY c2.chunk_id DESC
     LIMIT 2000
)
SELECT c.chunk_id
  FROM kw_cand q
  JOIN chunks c ON c.chunk_id = q.chunk_id
  LEFT JOIN chunk_embeddings ce
    ON ce.chunk_id = c.chunk_id AND ce.embedder = 'bge-m3' AND ce.status = 'ok'
 WHERE ce.vector IS NOT NULL
   AND ce.content_sha IS NOT DISTINCT FROM c.content_sha
 ORDER BY c.chunk_id DESC
 LIMIT 500
   FOR UPDATE OF c SKIP LOCKED;
```

**Tier 2 (fresh backlog).** Must walk `chunks_keywords_pending_idx`
backward (once the index exists — without it, a `chunks_pkey`
backward scan plus predicate filter is the expected slower path and
the reason the pre-build comes first), bounded by `LIMIT 2000` on
`kw_cand`, with `NOT IN (SELECT ref_id FROM kw_refs)` as a hashed
subplan, not a per-row `refs` join:

```sql
EXPLAIN
WITH kw_refs AS MATERIALIZED (
    SELECT ref_id FROM refs WHERE kind IN ('conv', 'draft')
),
kw_cand AS MATERIALIZED (
    SELECT c2.chunk_id
      FROM chunks c2
     WHERE c2.chunk_kind NOT IN ('card_authors', 'card_combined',
           'card_description', 'card_title', 'equation', 'figure',
           'paper_note', 'references', 'scan_geometry', 'table')
       AND length(c2.text) >= 150
       AND (c2.meta->>'no_index') IS DISTINCT FROM 'true'
       AND EXISTS (
           SELECT 1 FROM chunk_embeddings ce2
            WHERE ce2.chunk_id = c2.chunk_id
              AND ce2.embedder = 'bge-m3' AND ce2.status = 'ok'
              AND ce2.vector IS NOT NULL
              AND ce2.content_sha IS NOT DISTINCT FROM c2.content_sha)
       AND c2.ref_id NOT IN (SELECT ref_id FROM kw_refs)
       AND c2.keywords IS NULL
     ORDER BY c2.chunk_id DESC
     LIMIT 2000
)
SELECT c.chunk_id
  FROM kw_cand q
  JOIN chunks c ON c.chunk_id = q.chunk_id
  LEFT JOIN chunk_embeddings ce
    ON ce.chunk_id = c.chunk_id AND ce.embedder = 'bge-m3' AND ce.status = 'ok'
 WHERE ce.vector IS NOT NULL
   AND ce.content_sha IS NOT DISTINCT FROM c.content_sha
 ORDER BY c.chunk_id DESC
 LIMIT 500
   FOR UPDATE OF c SKIP LOCKED;
```

If either plan shows a multi-million-row sequential or backward pkey
walk inside `kw_cand`, keep the lane off and re-open the 0266 review.

Delete this file when the migration ships.
