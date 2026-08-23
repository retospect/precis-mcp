---
status: draft
title: Scope-aware three-state "is it embedded yet" hint, so a caller can poll instead of guessing
prio: normal
---

# Scope-aware three-state embed-status hint

From gripe 244419's fix plan (item C). Separable — depends on nothing and
blocks nothing.

## Motivation / why

After touching a chunk the caller may want to know when the embedding lands.
The chunk row is available immediately; only the derived rows lag. Today
there is no way to ask, so an agent either guesses or re-searches blindly.

No new bookkeeping is needed — the state already exists.
`workers/embed.py::unembedded_chunk_count` decides staleness by comparing
`chunk_embeddings.content_sha` against `chunks.content_sha` (the same
predicate `EmbedHandler`'s derived-queue claim uses). So the scope you just
edited *is* the receipt: generalize that helper to take a scope and let the
caller poll it. Self-correcting, nothing to leak or garbage-collect, still
correct across worker restarts and concurrent sibling edits.

Bonus: the same hint makes the existing ~1M-chunk backlog legible per-scope
for the first time, instead of one global number.

## In scope

- A scope parameter (ref_id / draft slug / chunk-id set) on the existing
  `unembedded_chunk_count` predicate, exposed to the caller.
- **Three** states, not two: `current` (sha matches) / `pending` (no row, or
  row with a different sha) / `failed` (row with `status = 'failed'`).

## Explicitly NOT in scope

- **A server-side `wait=True`.** Blocking the MCP call until embeddings land
  holds an anyio worker thread for the duration — that is exactly the
  gr244419 wedge reintroduced through a friendlier door. Waiting must be
  client-side polling on a cheap count.
- Changing `unembedded_chunk_count`'s corpus-wide contract. `materialize`'s
  backlog high-water threshold and `embed_batch`'s `queue_remaining` share it
  precisely so the two can never disagree about what "backlog" means (§F
  cycle a) — the scoped form must not perturb the unscoped one.

## Acceptance criteria

- The existing `unembedded_chunk_count(conn)` call sites
  (`workers/materialize.py`, `workers/job_types/embed_batch.py`) return
  byte-identical numbers.
- A chunk whose only `chunk_embeddings` row has `status = 'failed'` reports
  `failed`, NOT `current`. This is the load-bearing case: today's
  `NOT EXISTS` clause treats `status = 'failed'` as satisfied (i.e. "don't
  retry"), so a permanently-failed chunk reads as not-stale and a naive wait
  loop would return success on exactly the case you'd most want to know
  about.
- Polling the hint over a scope costs one indexed query, not a per-chunk fan-out.

## Target + blast radius

`workers/embed.py::unembedded_chunk_count` and its two existing call sites;
whichever verb surfaces the hint. Read-only — no write path changes.

## Open questions / decisions log

- Which verb carries it? A field on the `sub=`/edit response (alongside the
  `{touched: N, embed_stale: N}` fan-out report), a `get`-side status, or
  both. The edit-response field is the one the polling loop actually needs a
  starting number from.
