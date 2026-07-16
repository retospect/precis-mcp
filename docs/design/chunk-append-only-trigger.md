# Enforce chunk append-only discipline at runtime

**Status**: implemented · **Area**: `store/` schema (migration + trigger)
**Backlog**: OPEN-ITEMS.md / "Architecture review / P2 — quality" /
"Enforce chunk append-only discipline at runtime".

## Problem

The rule "body chunks are append-only; never in-place `UPDATE chunks.text`"
is convention-only (AGENTS.md "Don't mutate body chunks"). Nothing stops new
code from doing `UPDATE chunks SET text = …` on a body row. When that happens
the derived side-tables go stale by construction:

- `chunk_embeddings` keeps the vector of the *old* text.
- `chunk_summaries` keeps the RAKE/LLM summary of the *old* text.
- `chunks.keywords` / `keywords_meta` keep the old discovery keywords.

For row-identity-cascade chunks there is no signal that re-triggers the embed
/ summarize / keyword workers, so search silently serves the pre-edit text.

## Which chunks are actually append-only

The `chunks` table holds two invalidation models in one table:

| Family | `ord` | `content_sha` | Invalidation | In-place text UPDATE |
| --- | --- | --- | --- | --- |
| body (paper, plaintext, memory_body, …) | `>= 0` | `NULL` | row identity | **forbidden** |
| cards (`card_*`) | `< 0` | `NULL` | manual (`rewrite_cards` drops embeddings/keywords) | allowed |
| draft-family (draft, plan, figure) | `>= 0` | **NOT NULL** | `content_sha` diff (`edit_text` bumps sha) | allowed |

So the *only* dangerous case is a text change on a **body row**: `ord >= 0`
**and** `content_sha IS NULL`. Draft chunks are excluded by `content_sha` (the
embed/summary workers compare `chunk_embeddings.content_sha`), and cards are
excluded by `ord < 0` (`precis.ingest.cards.rewrite_cards` rewrites their text
in place but deletes the matching `chunk_embeddings` and nulls `keywords` in
the same transaction).

## Decision

Add a `BEFORE UPDATE` row trigger on `chunks` that raises when a text change
targets a body row:

```sql
WHEN (NEW.text IS DISTINCT FROM OLD.text
      AND OLD.ord >= 0
      AND OLD.content_sha IS NULL)
```

The `WHEN` guard keeps the trigger free for the common non-text UPDATEs
(`meta`, `pos`, `parent_chunk_id`, `chunk_kind`, `retired_at`) — it only runs
the raise function when the text of a body row actually changes.

The sanctioned way to change a body chunk's text stays: `DELETE` the row and
`INSERT` a fresh one (the `DELETE`→`chunk_embeddings`/`chunk_summaries`/
`chunk_tags` FK cascade tears down the derived rows, and the fresh `INSERT`
re-enters the worker queues). `DELETE`+`INSERT` never fires this trigger.

### Why a trigger and not a `ChunkOps` Python guard

The bug is "some code path issues a raw text UPDATE". A Python guard only
covers callers that go through it; a DB trigger covers every path (store
mixins, migrations, ad-hoc psql) — which is the point of "enforce at runtime".

## Compatibility

- `edit_text` (draft, `content_sha` set): excluded — allowed.
- `rewrite_cards` (cards, `ord < 0`): excluded — allowed.
- migration `0050` (memory card→body repurpose): changes `ord`/`chunk_kind`,
  not `text` → `WHEN` guard false → never fires.
- No `INSERT … ON CONFLICT DO UPDATE SET text` exists on `chunks`.

## Migration / baseline

Forward-only `0068_chunks_forbid_body_text_update.sql`, idempotent
(`CREATE OR REPLACE FUNCTION`, `DROP TRIGGER IF EXISTS` then `CREATE TRIGGER`).
The baseline snapshot is left behind head (allowed mid-cycle, ADR 0031);
`test_schema_convergence` still passes because both replay and baseline+tail
apply the new migration. Regenerate the baseline at release time.

## Test

`tests/test_chunk_append_only.py` (db-marked):

- body row text UPDATE raises;
- body row non-text UPDATE (meta) succeeds;
- card (`ord < 0`) text UPDATE succeeds;
- draft chunk text UPDATE (with `content_sha` bump) succeeds;
- DELETE+INSERT of a body row succeeds.
