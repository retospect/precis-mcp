# Dreaming — memory consolidation worker

Status: **proposed** (plan-first artifact; no code landed yet)
Author: (fill in)
Date: 2026-06-05

## Problem

Memories (`kind='memory'`) accumulate as a flat, append-only stream of
short notes. Over time the stream grows redundant: the same fact gets
re-noted in slightly different words, a decision gets refined across
three separate entries, a question and its later answer live as two
disconnected memories. Nothing today merges them.

We want a **dreaming** pass: a background job that periodically picks a
memory, finds its semantic neighbours, asks a strong supervising model
whether any subset should be *consolidated*, and — when the model says
yes — replaces that subset with a single better memory while
**migrating all links** and retiring the originals so they no longer
surface.

This is the memory analogue of the existing finding-chase worker
(`src/precis/workers/chase.py`): a ref-level pass, one ref per
transaction, LLM-gated, audited through `ref_events`.

## Decisions (from discussion)

- **Neighbour search is semantic (option A).** Memories get a
  `card_combined` chunk + embedding so "find adjacent memories" uses
  real cosine similarity, catching paraphrased duplicates that a
  lexical title match would miss.
- **Autonomous, but supervised by a strong model.** There is no
  separate human approval step. The *supervisor is the LLM* — an
  opus-class model with extended thinking — gated default-off behind
  `PRECIS_DREAM_LLM`. The `ref_events` log is the audit trail; soft
  delete + a `supersedes` link chain make every merge reversible at the
  SQL layer.

## What exists today (grounding)

- `MemoryHandler` is a `note_like` numeric-ref kind.
  `put(text=...)` → `insert_ref(kind='memory', title=text, meta={})`
  in `_numeric_ref.py::_create`. **No chunk is created**, so memories
  have no embeddings today. Memory `search` is lexical over
  `refs.title` (`search_refs_lexical`).
- Semantic search exists only over chunks:
  `Store.search_blocks_semantic(query_vec, kind=..., scope_ref_id=...,
  max_distance=...)` (cosine via `chunk_embeddings`, excludes `ord<0`).
- Links live in `links (src_ref_id, src_chunk_id, dst_ref_id,
  dst_chunk_id, relation)` with a `UNIQUE ... NULLS NOT DISTINCT`
  index and a self-loop CHECK. Ops: `add_link` / `remove_link` /
  `links_for` in `store/_links_ops.py`.
- `soft_delete_ref(ref_id)` sets `deleted_at`; every read path filters
  `deleted_at IS NULL`. Tombstones still render in link views with a
  "(deleted)" marker (intentional — provenance stays visible).
- The `Relation` enum (`store/types.py`) + the `relations` seed in
  `migrations/0001_initial.sql` are the source of truth.
  **`supersedes` does not yet exist.**
- LLM calls go through `call_claude_p(prompt, model=...)`
  (`utils/claude_p.py`); model is per-call overridable. Chase defaults
  to Haiku; dreaming will pass an opus-class model.
- Precedent worker shape: `run_finding_chase_pass` — claim
  `FOR UPDATE ... SKIP LOCKED`, process one ref per tx, default-off LLM
  (`PRECIS_CHASE_LLM`), write `ref_events`, mutate `meta`, re-emit
  `card_combined` via DELETE+INSERT, flip a STATUS tag. The CLI
  registers it as a ref-pass in `cli/worker.py`.

## Design

### 1. Schema migration `0003_dreaming.sql`

Additive only (no destructive ops; no threshold tripped):

- Seed two new rows in `relations`: `supersedes` (inverse
  `superseded-by`) and `superseded-by` (inverse `supersedes`).
- Mirror them in the `Relation` Literal and `_INVERSE_RELATIONS` in
  `store/types.py`.

Rationale for a new relation rather than reusing `retracts`:
retraction carries a "this was wrong" connotation; consolidation is
"this was absorbed into a better phrasing". Keeping them distinct
keeps the provenance graph honest and queryable.

### 2. Memories become embeddable

Give each memory a synthetic `card_combined` chunk (`ord=-1`, same
convention as papers/findings) holding the memory text, so the
existing embed worker vectorizes it and `search_blocks_semantic`
can find neighbours.

- **On create**: `MemoryHandler` (or the shared note-like create path)
  emits the card chunk in the same tx as the ref insert.
- **On edit/text change**: DELETE+INSERT the `ord=-1` row so the
  embedding cascade re-runs (never in-place UPDATE of `chunks.text` —
  see AGENTS.md "Don't mutate body chunks").
- **Backfill**: a one-shot pass creates the missing card chunk for
  pre-existing live memories; the embed worker drains them.

Open sub-question for review: do we scope this to `memory` only, or to
all `note_like` numeric kinds? Recommendation: **memory only** for
now; widen later if other kinds want dreaming.

### 3. The dreaming worker `precis.workers.dream`

Mirrors `chase`'s structure. Two files:
`workers/dream.py` (deterministic orchestration) and
`workers/_dream_llm.py` (the default-off opus hook + prompt).

**Claim.** Select live memories that are due for a dream pass and not
already retired. "Due" = not visited within a cooldown window. Track
visitation with a `DREAM:` namespaced tag and/or `meta.dreamed_at`, so
the claim query is
`memory refs WHERE deleted_at IS NULL AND not-recently-dreamed
ORDER BY ref_id LIMIT n FOR UPDATE ... SKIP LOCKED` (release locks like
chase does, then touch each in its own tx).

**Neighbour gather.** For the claimed memory, embed-lookup its card and
run `search_blocks_semantic(kind='memory', scope excludes self,
max_distance=<tight floor>, limit=K)`. Build a candidate cluster:
the seed + its near neighbours, with guards:
- min cluster size 2 (a lone memory has nothing to merge),
- max cluster size capped (e.g. 6) to bound prompt cost,
- a distance floor (`PRECIS_DREAM_MAX_DIST`) so only genuinely close
  memories enter the cluster.

If no cluster forms, mark the seed dreamed and return a no-op outcome.

**LLM supervision (default-off, opus-class).** `_dream_llm.py` shows
the cluster (id + text + tags + existing links summary) and asks for a
strict-JSON decision:

```json
{
  "merge": true | false,
  "merge_ids": [<int>, ...],       // subset of the shown ids, >= 2
  "new_text": "<consolidated memory>",
  "new_tags": ["..."],             // optional; union of survivors by default
  "reason": "<one sentence>"
}
```

Conservative contract: `merge=false` unless the model is confident the
subset says the same thing / refines one another. Distinct facts stay
distinct. On any LLM error or unparseable output → no-op (fall back to
"mark dreamed, move on"), exactly like chase tolerates `None`.

**Consolidate (one transaction).** When `merge=true`:
1. Create the new memory: `insert_ref(kind='memory', title=new_text)`
   + its `card_combined` chunk + merged tags.
2. **Migrate links.** For every link touching any `merge_id`
   (`links_for(old, direction='both')`): re-point the old endpoint to
   the new ref, preserving relation and the *other* endpoint.
   - Dedup against the unique index (re-pointing may collide with an
     existing edge → drop the duplicate).
   - Drop any edge that would become a self-loop (e.g. a link that was
     between two members of the merge set).
   - Implement as a dedicated `Store.migrate_links(old_ref_id,
     new_ref_id)` helper (INSERT ... ON CONFLICT DO NOTHING the
     re-pointed rows, then DELETE the old rows) so the handler/store
     layering stays clean.
3. Add provenance edges: `new --supersedes--> old` for each old id
   (auto-mirrors to `superseded-by`).
4. Stamp `meta.superseded_by = new_id` (+ `meta.dreamed_at`) on each
   old ref, then `soft_delete_ref(old)`. Retired memories stop
   surfacing immediately; the link view still shows them as "(deleted)"
   so the trail is visible from the survivor.

**Audit.** Write one `ref_events` row per pass (source `'dream'`) with
the decision, cluster ids, distances, merged-into id, link-migration
counts, and LLM cost — same pattern as chase's `_flush_event`.

### 4. CLI wiring

Register the pass in `cli/worker.py` as a ref-pass behind
`--only dream` (default-off in the normal worker rotation, like
`fetch_oa`). Knobs via env: `PRECIS_DREAM_LLM` (gate),
`PRECIS_DREAM_MODEL` (default opus-class), `PRECIS_DREAM_MAX_DIST`,
`PRECIS_DREAM_COOLDOWN`, cluster-size caps.

### 5. Idempotency & safety

- Re-running dream on a corpus is safe: retired memories are
  soft-deleted and tagged `dreamed`, so they aren't re-claimed; the
  survivor is freshly created and enters the cooldown window.
- A merge is fully reversible at the SQL layer: `deleted_at=NULL` on
  the old rows + the `supersedes` edges document what happened. (No MCP
  undo, consistent with existing soft-delete semantics.)
- Cost is bounded by `claude_p`'s `--max-budget-usd` per call plus the
  cluster-size cap; the gate keeps it off by default.

## Test plan

- `Store.migrate_links`: re-point, dedup-on-collision, self-loop drop,
  inbound+outbound, chunk-pos links.
- Card-chunk emission on memory create/edit; backfill pass.
- Neighbour gather: distance floor, min/max cluster guards.
- `advance` / consolidate path with a mocked LLM (`PRECIS_CLAUDE_BIN`
  stub emitting canned JSON): merge=false no-op, merge=true full
  consolidation, link migration counts, survivor surfaces / originals
  don't, `supersedes` edges present.
- Worker pass aggregation + `ref_events` shape.

## Definition of done (per AGENTS.md)

- Plan reviewed (this doc).
- ADR in `docs/decisions/` for: the new `supersedes` relation, and the
  autonomous-but-LLM-supervised consolidation policy.
- `0003_dreaming.sql` applies cleanly to a fresh DB; only the new file
  pending under `precis migrate --dry-run`.
- Memory create/edit + backfill + worker implemented; `--only dream`
  has `--help` and an integration test; README line added.
- Full check green (`ruff check`, `ruff format --check`, `mypy`,
  `pytest`); version bump + `CHANGELOG` entry.

## Open questions for the reviewer

1. Card chunk for `memory` only, or all `note_like` numeric kinds?
2. Should `new_tags` default to the union of survivors' tags, or only
   tags the LLM explicitly keeps? (Recommendation: union, minus
   transient `DREAM:`/`STATUS:` control tags.)
3. Cooldown policy: time-based (`meta.dreamed_at` + window) vs
   tag-based (`DREAM:seen`, cleared when the memory is edited)?
4. Distance floor + cluster-size defaults — pick conservative starting
   values and tune from `ref_events` telemetry.
