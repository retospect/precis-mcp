# ADR 0007 — Derived queue (no `block_jobs` table)

- **Status**: accepted (2026-05-21)
- **Deciders**: Reto + agent
- **Supersedes**: nothing — this updates `docs/design/storage-v2.md`
  (a plan, not an ADR), which originally proposed a `block_jobs`
  table. The plan is revised in the same commit that introduces
  this ADR.

## Context

`storage-v2.md` originally specified a `block_jobs` table to drive
the lazy worker:

```sql
CREATE TABLE block_jobs (
    id BIGSERIAL PRIMARY KEY,
    chunk_id BIGINT REFERENCES chunks(id) ON DELETE CASCADE,
    job_kind TEXT NOT NULL,         -- 'embed:bge-m3', 'summarize:rake'
    status TEXT NOT NULL,           -- 'pending', 'running', 'done', 'failed'
    attempts INT NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);
```

The worker would `SELECT ... WHERE status='pending' FOR UPDATE
SKIP LOCKED`, claim a job, run it, update status. This is the
textbook job-queue pattern.

User pointed out: the data already encodes the queue. Whatever
chunk is missing an embedding *needs* an embedding. We don't need
a separate table tracking that fact.

## Decision

No `block_jobs` table. The worker's "queue" is derived by
`LEFT JOIN`-ing chunks against output tables and selecting rows
that are missing.

```sql
-- chunks missing the bge-m3 embedding
SELECT c.id, c.text
  FROM chunks c
  LEFT JOIN chunk_embeddings ce
    ON ce.chunk_id = c.id AND ce.embedder = 'bge-m3'
 WHERE ce.chunk_id IS NULL
 ORDER BY c.id
 LIMIT 100
   FOR UPDATE OF c SKIP LOCKED;
```

Same query shape for `chunk_summaries`. Workers loop these queries
until they return zero rows, then sleep and re-poll.

### Failure handling — failure marker rows

The wrinkle in a derived queue is poison pills: if chunk #42
genuinely cannot be embedded (oversized, normalized to empty,
model OOM), the worker re-picks it forever.

Solution: failures get a row in the *same* output table, with
`vector=NULL` and `status='failed'`. The "missing" predicate
becomes "no row at all", so failed chunks are skipped naturally.

```sql
CREATE TABLE chunk_embeddings (
    chunk_id BIGINT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    embedder TEXT NOT NULL REFERENCES embedders(name),
    vector vector(1024),                          -- NULL on failure
    status TEXT NOT NULL DEFAULT 'ok'
        CHECK (status IN ('ok', 'failed')),
    attempts INT NOT NULL DEFAULT 1,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chunk_id, embedder)
);

CREATE INDEX chunk_embeddings_status_failed
    ON chunk_embeddings (chunk_id, embedder)
    WHERE status = 'failed';
```

Manual retry of a failed chunk:
```sql
DELETE FROM chunk_embeddings
 WHERE chunk_id = $1 AND embedder = $2 AND status = 'failed';
```
The next worker pass picks it up.

The same shape applies to `chunk_summaries`. The status enum is
the only generic part; each derived artifact has its own table.

### Concurrent workers — `FOR UPDATE OF c SKIP LOCKED`

Locking the chunk row (not a job row) prevents two workers from
embedding the same chunk simultaneously. The lock is released as
soon as the worker writes its result row and commits, which is
fast (one INSERT per chunk).

For long-running operations (large summaries, slow embedders), the
chunk-row lock blocks other workers for the whole duration. If
this becomes a problem, switch to advisory locks
(`pg_advisory_xact_lock(chunk_id)`) which release on transaction
end without holding row locks. Defer until measured pain.

### Observability

`SELECT count(*) WHERE status='running'` is no longer available.
Replacement: a status query per derived artifact.

```sql
-- Worker progress
SELECT
    e.name AS embedder,
    (SELECT count(*) FROM chunks) AS total,
    count(ce.chunk_id) FILTER (WHERE ce.status = 'ok') AS done,
    count(ce.chunk_id) FILTER (WHERE ce.status = 'failed') AS failed,
    (SELECT count(*) FROM chunks)
        - count(ce.chunk_id) AS pending
  FROM embedders e
  LEFT JOIN chunk_embeddings ce ON ce.embedder = e.name
 GROUP BY e.name;
```

`precis health` and `precis worker --status` will surface the
above.

## Consequences

### Positive

- One fewer table in the schema. Lower cognitive load for new
  contributors and migration authors.
- Ingest path is shorter: insert chunk → done. No "also enqueue
  N jobs per chunk" step.
- Adding a new embedder is just `INSERT INTO embedders(...)` and
  starting the worker. The worker picks up every chunk that
  doesn't yet have a row for that embedder.
- Stateless workers. Restart is free.
- The "is it done?" query is a `LEFT JOIN`, exactly the same
  shape as the worker's claim query. One mental model.

### Negative

- No built-in attempt cap. A chunk that fails for a transient
  reason (network blip, GPU eviction) is retried by manual deletes
  of the failure marker row. We accept this; transient failures
  are rare in our pipeline.
- No priority. Order is `chunk_id ASC` (FIFO by ingest order). If
  priority surfaces (e.g., backfill an old paper before a new
  one), add `ORDER BY refs.priority DESC, c.id` and a `priority`
  column on `refs`. Not now.
- `chunk-row` locking ties up the chunk for the duration of the
  worker pass. Move to advisory locks if it bites.

### Migration

Already covered: `block_jobs` was never created. The greenfield
schema in B1 simply omits it. `chunk_embeddings` and
`chunk_summaries` ship with `status` / `attempts` / `last_error`
columns from the start.

## Open follow-ups

- Decide a backoff policy. Today: instant retry on insert (worker
  re-picks on next poll). Tomorrow: exponential backoff via a
  `next_attempt_at` column? Defer until needed.
- Decide whether the worker writes a `pending` row before starting
  to embed (so a crashed worker leaves an `attempts=N` row). Today:
  no — the row is only written on success or final failure. The
  chunk-level lock prevents double work; a crashed worker just
  releases the lock and the chunk is re-claimed.
