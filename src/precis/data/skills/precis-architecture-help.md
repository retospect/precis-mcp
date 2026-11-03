---
id: precis-architecture-help
family: core
title: precis — retention, decay, recall and lifecycle in quick answers
summary: architecture Q/A — hard caps and pruning, the pinned flag, decay windows, dreams, recall ranking, SPACE scoping, append-only chunks, gripe↔todo wiring, operator overrides
answers:
  - is there a hard cap or forced pruning on memories?
  - what keeps a memory from being pruned or decayed?
  - how does memory decay work — does a decayed memory get deleted?
  - how do dreams and dream consolidation work?
  - how does recall rank memories — and what is +recall?
  - how does SPACE: scoping work on memories?
  - what is append-only in precis — what can never be lost?
  - how do gripes and todos relate?
  - where do operator overrides live — settings, cadences, dark switches?
applies-to: get/search/tag (kind='memory'), the gripe/todo lifecycle, cache-backed kinds
tags: [orientation]
status: active
---

# precis-architecture-help — the memory and retention architecture, fast

Quick answers to the recurring "how does precis hold, rank and retire
knowledge" questions — each names the exact tag, verb or view, then the
skill that owns the detail. Verbs and kinds: [[precis-overview]].

## Is there a hard cap or forced pruning on memories?

No — there is no count cap and no forced pruning. A `memory` ref never
expires and `delete` is a soft-delete tombstone (`retire_ref` stamps
`retired_at`; recoverable at the SQL layer), not a removal. The one
armed automatic retirement is the daily `section:threads` sweep — a
`Left: none` first line, a live `supersedes` edge, or 14 days
untouched (open work on the `Left:` line becomes a linked todo
first). A second sweep exists but stays dark: `dream_consolidate`
retires stale `DREAM:`-tagged memories behind `supersedes` summaries
once lit (the cadence fires but the pass no-ops until an operator
lights it and disarms its `dry_run`; ruled do-not-light). →
[[precis-memory-help]]

## What keeps a memory from being pruned or decayed?

Nothing needs to — memories are not pruned (see above). `pinned` on a
memory is a plain open tag: writable and searchable
(`tags=['pinned']`), but no mechanism reads it — the threads sweep
never checks it and it suppresses nothing:

```python
tag(kind="memory", id=42, add=["pinned"])
```

`CACHE:pinned` is real only on cache-backed kinds, and it is a
*rendered state*, not a flag you set: a cache row written without a
TTL (`fresh_until` NULL) never goes stale and its footer reports
`CACHE:pinned`. On a cache kind the bare `pinned` spelling is refused
(it collides with the closed `CACHE:` value). `sticky:thread` /
`sticky:global` pin a memory into the per-turn preamble — that is
view-state, not retention; unpinning drops it from the preamble but
the memory stays in the corpus.
→ [[precis-tags]] · [[precis-cache]] · [[precis-memory-help]]

## How does memory decay work — does a decayed memory get deleted?

The decay columns are write-only today. `put` accepts
`args={'auto_refresh_days': N}` and stamps `refreshed_at` at insert;
DREAM-tagged writes get a default window (60 days, 90 for thread
dreams) when the writer set none. But nothing ranks or expires on
them: the 1.0 → 0 weight slide exists only as a code comment,
`touch_ref` — the refresh writer — has no callers, and re-tagging
does not reset `refreshed_at` (tag writes never touch the `refs`
row). `refreshed_at` is read for display only (`view='raw'`, the web
ref page) — the preamble's recent-thoughts list orders by
`updated_at`, not `refreshed_at`. Nothing is deleted either way. →
[[precis-inner-life-help]]

## How do dreams and dream consolidation work?

The `dream_agent` cadence (~15 min, dark-switched) runs a dreaming
pass that writes `DREAM:speculative` memories — fenced out of default
`search` unless you name the tag:

```python
search(kind="memory", tags=["DREAM:speculative"], q="<topic>")
```

`DREAM:consolidated`/`acquire` are system-set (agent `tag`/`put` of
them is refused; `speculative` is writable). The sweep that would
retire stale dreams behind `supersedes` summaries is dark by operator
ruling — dreams stay; their links are the point. Manual
episodic→semantic consolidation: [[precis-self-consolidation-help]].
→ [[precis-inner-life-help]]

## How does recall rank memories?

Hybrid lexical + semantic, fused, then three soft re-ranks: the
semantically nearest hit keeps a slot even with no word match
(rescue), recently touched nodes rank a little higher (band prior —
`last_viewed`/`last_recalled`/`updated` stamps, not `refreshed_at`),
and the best `section:gotchas` node is appended when none made the
page (quota). `view='index'` hard-drops semantic-only hits past the
fold distance — an *embedding-distance* cut (the fixed floor for
memory is 0.42; the default policy is a query-relative gap cut capped
at the 0.65 corpus floor), not a check of which words the title or
`meta.hook` carries. `+recall` on a fisheye lists up to 8 nearest
neighbours by embedding distance — candidate kinds are the focus's
own kind plus `finding`, each inside a 0.6 distance cut and not
already linked to the focus.
→ [[precis-fisheye-help]] · [[precis-memory-help]]

## How does SPACE: scoping work on memories?

`SPACE:` is a closed axis a `memory` accepts: `research` (the
default), `repo-dev` (dev-harness knowledge), `personal` (reserved,
nothing writes it), `design-conventions` (the design knowledge
graph's corpus — `precis conventions` imports write it on
`finding`).
Recall stays inside the focus's own space — a `repo-dev` node never
recalls a `research` one, and `+recall` and ring narrowing
(`args={'ring': {'space': 'repo-dev'}}`) honour it too. Per-space
counts: `search(kind='memory', view='count', args={'group_by':
'tag:SPACE'})`. → [[precis-tags]] · [[precis-memory-help]]

## What is append-only in precis?

Body chunks are append-only — an `edit` rewrite deletes and reinserts
its chunks rather than updating in place, the old body stays readable
in `view='log'`, and `retired_at` tombstones keep every soft-deleted
ref recoverable. The things that do expire have their own clocks —
`CACHE:*` TTLs, the agentlog GC — and even those reap links and
rows, never the chunks. →
[[precis-delete-help]] · [[precis-cache]]

## How do gripes and todos relate?

A `gripe` is intake — friction, bugs, annoyances, filed in one line; a
`todo` is the task substrate work actually runs against. The gripe
`STATUS:` ladder (`triaged` → `fixing` → `auto_verify`/`user_verify` →
`done`) tracks fix state; the fix lands as a `job` linked `fixes` →
the gripe, and builder waves drive it with `edit(kind='gripe',
mode='fix')`. Open work that outlives a retired thread node becomes a
todo minted from its `Left:` line. → [[precis-gripe-help]] ·
[[precis-gripe-loop-help]] · [[precis-todo-tree-help]]

## Where do operator overrides live?

In the DB and the host, not the corpus: `service_config` rows light a
dark cadence (`prio > 0`), `precis settings` keys hold per-service
switches (a sweep's dry-run lever), env vars sit underneath both.
Read the live set with `get(kind='job', id='/settings')` — there is no
agent write verb, so changing one means asking the operator.
Per-ref values an agent *can* write: `auto_refresh_days` (a decay
marker nothing reads yet), `sticky:*`, `meta.until` on a parked
gripe.
→ [[precis-settings-help]]

## See also

- [[precis-overview]] — the seven verbs and kind table
- [[precis-memory-help]] — the full memory verb surface
- [[precis-inner-life-help]] — decay windows and the dream tag protocol
- [[precis-self-consolidation-help]] — the manual consolidation pass
- [[precis-tags]] — every closed axis and bare flag
- [[precis-fisheye-help]] — the extent ladder and `+recall`
- [[precis-cache]] — cache TTLs and `CACHE:*` freshness
- [[precis-settings-help]] — DB-resident settings and the `/settings` view

<!--
Fact-check sources (gr553220; archdocs3 pass, symbol anchors).
Paths are under src/precis/ except precis_web (src/precis_web).

- No hard cap / soft-delete only: store._refs_ops RefsMixin.retire_ref
  stamps retired_at; workers/memory_thread_sweep.py
  plan_thread_sweep/run_memory_thread_sweep_pass — candidate = live
  memory + section:threads, rules a/b/c;
  utils/memory_threads.py STALE_DAYS = 14. Second sweep, dark:
  workers/dream_consolidate.py module docstring + run_dream_consolidate_pass
  (supersedes summary, members retired); dream_consolidate_enabled
  gate + dream_consolidate.dry_run setting; do-not-light ruling
  Reto 2026-10-24.
- pinned inert on memory: sweep queries never read it; bare flag =
  open tag on memory (no CACHE axis in its _KIND_ALLOWED_AXES set),
  refused on cache kinds — store/types.py Tag.parse_strict
  kind-scoped collision + _KIND_ALLOWED_AXES. CACHE:pinned tag row
  inert: freshness reads cache_state.fresh_until only —
  handlers/_cache_base.py CacheBackedHandler._is_fresh +
  _format_cache_footer. fresh_until written from ttl_seconds:
  store/_cache_ops.py put_cache_entry/update_cache_entry.
- ttl_days=0 → pinned: _cache_base.py CacheBackedHandler.get
  (ttl_days is not None → ttl_override_active; >0 → seconds, else
  stays None) → put_cache_entry/_refetch_in_place write
  ttl_seconds=None → fresh_until NULL. Pinned kinds:
  math.py MathHandler.ttl_seconds=None; perplexity.py
  ThinkHandler/ResearchHandler TIER ttl_seconds=None (websearch 7d);
  put with mode='import' writes ttl_seconds=None.
- decay columns write-only: auto_refresh_days/refreshed_at set at
  insert in _refs_ops.py RefsMixin.insert_ref (1.0 → 0 slide is a
  comment there); DREAM defaults handlers/memory.py
  _dream_stamp_defaults + _DREAM_REFRESH_DAYS/_DREAM_THREAD_REFRESH_DAYS
  applied in _create_with_id; touch_ref has no callers; tag writes
  never touch refs — _tags_ops.py add_tag/remove_tag. refreshed_at
  readers are display-only: _numeric_ref.py view='raw' render,
  precis_web/routes/refs.py _row; the tags-only browse orders by
  updated_at — _numeric_ref.py browse_default_sort 'recency' →
  _list_by_tags 'updated_desc'.
- dreams: workers/dream_agent.py docstring (gated cadence);
  store/_tag_filter.py SPECULATIVE_TAG/speculative_fence
  (DREAM:speculative only); memory.py _DREAM_SYSTEM_VALUES
  (consolidated/acquire refused).
- recall ranking: memory.py MemoryHandler._rerank_fused →
  utils/memory_fusion.py rerank (rescue/band prior/gotcha quota);
  band stamps utils/memory_hubs.py recency_stamps = GREATEST(
  last_viewed_at, last_recalled_at, updated_at) + BAND_OLD_DAYS —
  refreshed_at unused. view='index' hard cut on embedding distance:
  memory.py search view='index' wiring → _chunks_ops.py
  search_chunks_fused semantic_only_cut threshold →
  _search_cut.py semantic_fold_distance/gap_cut_distance; fixed
  floor _mappers.py SEMANTIC_ONLY_FOLD_DISTANCE['memory']=0.42,
  default gap capped at SEMANTIC_DISTANCE_FLOOR=0.65. +recall:
  utils/eye_render.py _recall — kinds {kind,'finding'},
  _RECALL_K=8, _linked_ref_ids excludes linked+self,
  RECALL_MAX_DISTANCE=0.6, SPACE partition unless ring overrides.
- SPACE: types.py _CLOSED_VOCAB['SPACE'] — research / repo-dev /
  personal / design-conventions.
- append-only / gripe↔todo / operator overrides: unchanged —
  precis-agentlog-help, docs/conventions/invariants.md,
  precis-gripe-help, precis-relations, precis-settings-help.
-->
