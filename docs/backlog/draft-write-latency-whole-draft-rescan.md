---
status: draft
pillar: memory-graph
title: put(kind='draft') p95 is 182s — sync_draft_links rescans the whole draft on every write
prio: high
---

# Draft/finding writes are O(whole draft) per call

`DraftHandler.sync_draft_links` (`src/precis/handlers/draft.py::DraftHandler.sync_draft_links`)
runs synchronously on every `put`/`edit`, and its own docstring says it plainly:

> Recomputed over the whole draft on each write

It calls `store.drafts.reading_order(ref_id)` for **every chunk in the
draft**, not just the one being written, then resolves link targets per chunk
through `draft_markup.resolve_draft_link_targets` → `_resolve_reference` →
`resolve_universal_handle`, which is a DB round-trip per reference found in
the text (`src/precis/utils/draft_markup.py`). Cost is
O(existing-chunk-count × refs-per-chunk) on every write, and it grows with
the draft.

## Measured (surface-review pass #1, 2026-09-29)

- `put` mean latency **18.9 s**, `edit` mean **38.8 s** (5-day window).
- Per-kind, from the prod ledger: `put(kind='draft')` p95 **181.9 s**,
  `put(kind='finding')` p95 **174.6 s** — the two prose-heavy kinds, i.e.
  exactly the ones that accumulate chunks.
- Corroborating: a burst of 30+ sequential
  `put(kind='draft', chunk_kind='term', …)` calls against one draft
  (`norr-her-survey`) hit a `Connection closed` mid-burst — consistent with
  cumulative full-draft rescans compounding until a transport timeout.

## This is NOT the embedding cascade

`draft.py` already documents that re-embed is async/worker-side and "does NOT
fan out embed requests on the request path" — gripe 244419 diagnosed and
fixed that specific wedge. Do not re-attribute this latency to embeddings;
the whole-draft link rescan is a separate synchronous cost on the same path.

## Why it matters beyond the seconds

A verb whose p95 is three minutes is one agents learn to route around —
batching fewer calls, avoiding the verb, or forking to raw SQL.
`mcp-surface-economy.md` already makes this argument about a `search` that
could hang ("a verb that may hang is one agents learn to route around, which
undercuts every gap fixed above"); this is the same dynamic on the write
path, now measured.

It also compounds the missing batch form (see `mcp-surface-economy.md`, the
eighth gap): clearing a draft's 508 undefined abbreviations is ~500
singleton `put`s, each paying a full-draft rescan that gets more expensive as
the preceding 499 land.

## Direction

Scope the rescan to the edited chunk(s) plus their previously-resolved
targets rather than the whole draft, or move it off the request path the way
embed already is. The docstring explains why it was made whole-draft
(chunk-grounded `src_pos`, so a citation resolves to the originating
paragraph) — that property must survive whichever route is taken, so this
needs a design pass, not a one-line change.

## Acceptance criteria

- `put`/`edit` latency on a large draft no longer scales with total chunk
  count; a benchmark on a synthetic large draft pins it.
- Edges stay chunk-grounded on the source side (`dc<id>`-granular `src_pos`)
  — the regression the current design exists to prevent.
- A moved chunk still gets its edges restamped.
