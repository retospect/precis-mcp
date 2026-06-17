# Phase 0 PR 2 — substrate hardening

## Motivation

Three small, surgical changes the `precis-dft` spec
(`~/.claude/plans/we-have-a-cluster-hidden-bird.md`) depends on,
none big enough to warrant its own PR:

1. **Race-safe job idempotency.** Today's `_lookup_idem` is a
   runtime SELECT with no SQL constraint behind it; two
   concurrent `put(kind='job', link='...')` calls can both miss
   the duplicate and both insert.
2. **MCP frame-size handling.** Large `kind='material'` renders
   (with nested `derived.reactions[*]` + `reaction_eval` chunks)
   will exceed the stdio frame limit; today's path returns the
   full body as one string.
3. **`meta.no_index` chunk filter.** `structure_draft` view
   chunks are for navigation, not search; they shouldn't be
   picked up by `chunk_keywords` or `chunk_embeddings`. A
   per-chunk opt-out keeps the indexer workload sane and the
   keyword set clean.

## 2.1 Race-safe idempotency

### Today

`src/precis/handlers/job.py:357-385` — `_lookup_idem(idem)`:

```python
SELECT r.ref_id FROM refs r
 WHERE r.kind = 'job' AND r.deleted_at IS NULL
   AND r.meta->>'idem_key' = %s
   AND NOT EXISTS (
         SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
          WHERE rt.ref_id = r.ref_id
            AND t.namespace = 'STATUS'
            AND t.value = ANY(%s)
       )
 ORDER BY r.ref_id DESC LIMIT 1
```

Called from `JobHandler.put` (`handlers/job.py:184-194`). The
SELECT runs in the dispatch transaction. If two callers race,
both can read "no existing active job" and both insert one.

### Change

Wrap the SELECT + INSERT in a transaction-scoped advisory lock
keyed on a hash of the idem string:

```python
if resolved_idem is not None:
    with self.store.pool.connection() as conn:
        conn.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (_idem_lock_key(resolved_idem),),
        )
        existing = self._lookup_idem(resolved_idem, conn=conn)
        if existing is not None:
            return Response(body=...)
        # ... proceed to insert in the same conn / tx ...
```

`_idem_lock_key(s: str) -> int` returns a 64-bit signed integer
derived from `hashlib.blake2b(s.encode(), digest_size=8)`. Two
concurrent puts with the same idem key serialize on the
`pg_advisory_xact_lock` and only one creates a row; the other
sees the just-inserted job on its second SELECT.

`_lookup_idem` gains an optional `conn=` parameter so the lookup
and insert share a transaction.

### Why advisory lock, not partial unique index

Postgres partial indexes can't reference subqueries — the obvious
predicate `WHERE … AND NOT EXISTS (SELECT … FROM ref_tags …)`
isn't allowed. Alternatives:

- Trigger maintaining a denormalized `is_terminal` boolean on
  `refs`, partial index on `WHERE is_terminal = FALSE`.
  Correct but expensive: every `STATUS:` tag write fires the
  trigger; every job ref pays the cost.
- Single advisory lock keyed on the idem hash: zero schema
  change, costs one round-trip per concurrent submit. v1 traffic
  pattern (a handful of concurrent puts) doesn't justify the
  trigger.

Pick advisory lock now; revisit if profiling shows contention.

## 2.2 MCP frame-size textual chunking

### Today

`src/precis/runtime.py:144-187` — `dispatch_with_status` returns
`(body, is_error)` where `body` is the full rendered string. The
MCP tool wrappers (`server.py:51`,
`_TOOL_KW={"structured_output": False}`) hand the string to
FastMCP, which wraps as `TextContent`. No size cap.

The stdio frame limit is client-dependent but commonly ~32KB.
A `get(kind='material', id='material:Pt3Ni_L12')` after PR 1 of
the precis-dft spec lands could carry the bulk material header
plus a dozen `reaction_eval` chunks (~3-5KB each) plus normal
chunks.

### Change

Add a configurable body-size cap (default 24KB to stay under
typical stdio limits) checked at the end of `dispatch_with_status`:

```python
MAX_BODY_BYTES = int(os.environ.get("PRECIS_MAX_BODY_BYTES", 24576))

def dispatch_with_status(self, verb, args):
    body, is_error = self._do(verb, args)
    if len(body.encode("utf-8")) <= MAX_BODY_BYTES:
        return body, is_error
    head, cursor = self._chunk_body(body)
    return head, is_error
```

`_chunk_body(body)`:

1. Split on `\n## ` boundaries (the rendered bodies are
   Markdown with H2 section headers).
2. Greedily fill the first chunk up to `MAX_BODY_BYTES - 256`
   (leave room for the `Next:` footer).
3. Cache the remainder string in a process-local LRU keyed by a
   new opaque cursor (uuid4 stem) with a 5-minute TTL.
4. Return `head + "\n\nNext: more(cursor='" + cursor + "')"`.

### New `more` verb

A new MCP tool `more(cursor)` reads from the cache, returns the
next chunk (also bounded), and either re-cursors or returns
plain. The cache is per-process; lost on restart.

Implemented as a thin module `src/precis/more.py` with a
`PaginationCache` class. Registered alongside the seven verbs in
`server.py`. Not a kind verb — it's a transport-layer
affordance, like `status`.

### What if a single section is itself > 24KB?

Fallback: split on `\n\n` (paragraph) boundaries. Fallback²:
hard-cut on a `MAX_BODY_BYTES - 256` byte boundary at a UTF-8
char boundary. Handlers that routinely produce big single
sections (we should not let `material.reaction_eval` chunks
become one giant section per network) get a warning.

### Why not a structured `next_cursor` field on Response

`Response` (`response.py:1-17`) is `{body: str, cost: str|None}`.
Widening to `{body, next_cursor, cost}` is a protocol change
that every handler, every test, and every render path would
have to absorb. Textual `Next:` cues are already the convention
(see `_numeric_ref.py:331+` list views). Stay in that pattern.

### Material handler hint

`material.reaction_eval` chunks must each be their own
`\n## ` section, not concatenated into one section per material.
This is a documentation note for the precis-dft handler authors,
not a precis-mcp change.

## 2.3 `meta.no_index` chunk filter

### Today

`src/precis/workers/chunk_keywords.py:94-...` —
`claim_chunks_without_keywords` claims any chunk where
`keywords IS NULL OR keywords_meta->>'version' != current_version`.

`src/precis/workers/embed.py` — the embedder claim has a similar
shape: it pulls chunks where `embedding IS NULL` and
non-blocking `chunk_kind`.

Neither today honours a per-chunk opt-out flag. `structure_draft`
view chunks (which we'll write at ~7 per edit, churning rapidly)
would burn the indexer for no agent benefit.

### Change

Add `meta->>'no_index' IS DISTINCT FROM 'true'` to both claim
queries:

```sql
WHERE ... existing predicates ...
  AND (c.meta->>'no_index') IS DISTINCT FROM 'true'
```

`IS DISTINCT FROM` is NULL-safe: chunks without the flag (the
vast majority) still match. Only chunks where `meta.no_index =
'true'` are skipped.

### Convention

A chunk writer that wants to opt out passes
`BlockInsert(meta={"no_index": True, "chunk_kind": "view:toc"})`.
Documented in the storage spec (`docs/design/storage-v2.md`) as
"opt-out for ephemeral / navigation-only chunks; skipped by
`chunk_keywords` and `chunk_embeddings`; still readable by `get`."

### Index implication

A partial expression index on `chunks((meta->>'no_index'))` would
help if a substantial fraction of chunks were flagged. v1's
expected fraction is small (only view chunks); a seq-scan
predicate is fine. Add the index later if profiling shows it.

## What does not change

- The 7-verb dispatch surface, `Response` shape, MCP tool
  registration, FastMCP-side wiring.
- Existing handlers' rendering — they all stay under 24KB for
  typical refs.
- `_lookup_idem`'s SELECT shape; only the call site adds the
  advisory lock.
- Chunk inserts that don't carry `no_index` — they index as
  before.

## Risk and rollback

- **Idempotency change is local to one method**; existing
  callers see no API change.
- **Frame chunking is opt-in via `MAX_BODY_BYTES` env**.
  Defaults to a value that's effectively a no-op on every
  existing rendering path (all are well under 24KB today).
- **`no_index` filter is additive**: nothing today writes the
  flag, so today's claim behaviour is unchanged.
- All three changes are localized; rollback is a revert.

## Tests

- `tests/handlers/test_job_idempotency_race.py` (new): launch
  two threads racing identical `put(kind='job', ...,
  idem_key='X')`; assert exactly one row created and both calls
  return the same ref_id.
- `tests/test_runtime_chunking.py` (new): hand-build a body
  > 24KB, dispatch, assert the head ends with `Next: more(...)`
  and that `more(cursor)` returns the tail.
- `tests/workers/test_chunk_keywords_no_index.py` (new): insert
  three chunks, one with `meta.no_index=True`; run claim;
  assert only the two unflagged chunks are claimed.
- `tests/workers/test_embed_no_index.py` (new): analogous for
  the embedder.

## Files touched

| File | Change |
|---|---|
| `src/precis/handlers/job.py` | Advisory lock around `_lookup_idem` + insert in `put`. New `_idem_lock_key` helper. |
| `src/precis/runtime.py` | `MAX_BODY_BYTES` env var; `_chunk_body` split-on-`\n## ` with pagination cache. |
| `src/precis/more.py` | New: `PaginationCache` + `more(cursor)` tool. |
| `src/precis/server.py` | Register `more` MCP tool alongside the 7 verbs. |
| `src/precis/workers/chunk_keywords.py` | Add `(c.meta->>'no_index') IS DISTINCT FROM 'true'` to claim. |
| `src/precis/workers/embed.py` | Same predicate on its claim. |
| `docs/design/storage-v2.md` | Note: `meta.no_index` convention. |
| `tests/handlers/test_job_idempotency_race.py` | New. |
| `tests/test_runtime_chunking.py` | New. |
| `tests/workers/test_chunk_keywords_no_index.py` | New. |
| `tests/workers/test_embed_no_index.py` | New. |
| `CHANGELOG.md` | Entry under `## Unreleased`. |
| `pyproject.toml` | Version bump. |

## Out of scope (separate PRs)

- Plugin registries for job_types and migrations (PR 1).
- `coordinator` executor + `wake_runner` (PR 3).
- `precis.ref_passes` entry-point group (PR 4).

## Open questions

- Should the pagination cache be per-process (in-memory) or
  shared (Redis-backed)? Per-process is enough for v1 and avoids
  a new dependency. Cursors die on worker restart; the agent
  retries the original call.
- Default `MAX_BODY_BYTES = 24576` — confirm via the FastMCP
  stdio frame profiling before PR. Some clients tolerate more.
