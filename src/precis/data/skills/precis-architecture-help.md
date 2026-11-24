---
id: precis-architecture-help
family: core
title: precis — retention, decay, recall and lifecycle in quick answers
summary: architecture Q/A — hard caps and pruning, the pinned flag, decay windows, dreams, recall ranking, SPACE scoping, append-only chunks, gripe↔todo wiring, reading operator switches
answers:
  - is there a hard cap or forced pruning on memories?
  - what happens when a thread note expires?
  - what keeps a memory from being pruned or decayed?
  - how does memory decay work — does a decayed memory get deleted?
  - how do dreams and dream consolidation work?
  - how does recall rank memories — and what is +recall?
  - how does SPACE: scoping work on memories?
  - what is append-only in precis — what can never be lost?
  - how do gripes and todos relate?
  - where can I read operator switches — settings, cadences, dark switches?
applies-to: get/search/tag (kind='memory'), the gripe/todo lifecycle, cache-backed kinds
tags: [orientation]
status: active
---

# precis-architecture-help — the memory and retention architecture, fast

Quick answers to the recurring "how does precis hold, rank and retire
knowledge" questions — each names the exact tag, verb or view, then the
skill that owns the detail. Verbs and kinds: [[precis-overview]].

## Is there a hard cap or forced pruning on memories?

No — there is no count cap and no forced pruning of the corpus. The
only automatic retirements are the daily `section:threads` sweep
retiring a finished or empty thread (a `Left: none` first line, an empty body or a
live `supersedes` edge) and a `SPACE:scratch` node self-retiring past
its TTL — plus, at 14 days untouched, a thread gets a decision ask, not
a retirement (gr746642: Reto's no-silent-removal ruling). Every
retirement is a soft-delete tombstone, recoverable at the SQL layer —
`delete` included, never a removal. What each expiry date triggers:
the next answer. → [[precis-memory-help]]

## What happens when a thread note expires?

The footer on `get` and `put` names the scheduled action. The daily
`memory_thread_sweep` cadence makes the following decisions.
Which rule fired decides what happens:

- **rule a** — a `Left: none` first line or an empty body. The thread
  retires on the next sweep without a question or a 14-day wait.
- **rule b** — another *live* memory links `supersedes` to it: the
  replacement is on record. Retires automatically, like rule a.
- **rule c** — untouched 14 days (the same clock `precis memory
  index` calls "live"): **no retirement**. The sweep mints one
  `ask-user` todo — options `extend` / `retire` / `transfer`, titled
  with the node's title and age and quoting its `Left:` line — and the
  node stays in recall. The daily review shows it in both the `asks`
  and `reto-queue` views of todo search. No answer means the item stays in
  the review and the node stays in recall — an unanswered ask is never
  a retire reason, and while an ask is open no second one is minted.
  Answering (`comment answer: …`, then `STATUS:done` + drop
  `ask-user`) lets a later sweep fire enact the choice: `extend`
  restarts the 14-day clock, `retire` soft-retires the node, `transfer`
  requires a live memory or spec linked `derived-from` the thread.
  An ask the sweep can no longer act on is *closed out*, not left to
  rot in the review: the thread edited back into currency (or turned
  currency suspect) closes its ask `thread-no-longer-stale`; a closed
  ask whose `answer:` names no valid choice closes
  `answer-unrecognised`; `transfer` with no `derived-from` successor
  closes `transfer-no-successor`. The last two re-raise the decision
  the same fire, quoting the reason; closing an ask with no `answer:`
  comment at all is a deliberate human close — it stays as a record
  and is never re-raised.
  A dated section inside the stale window makes the node
  a *currency suspect* instead — listed (`precis memory sweep`), never
  acted on. A thread with no `Left:` line is the trap: rule c still
  asks, but the review line shows no open work — the create ack warns
  at `put` time.
- **rule s** — `SPACE:scratch`: self-retires past its TTL (7-day
  default from `updated_at`, `meta.expires` overrides, a
  `scratch-for:<task>` tag gives a three-day grace after the last
  owner closes, capped at 30 days from the last edit). Scratch
  is the opt-in throwaway space — the one age-out that removes a node
  without asking.

"Retires" means the soft delete throughout: the node leaves recall
(`search` stops listing it) but the tombstone stays — `get` still
answers, the body chunks remain, and the row is recoverable at the SQL
layer. No clock ever hard-deletes. **Exempt a thread** by keeping it
edited (the clock runs from `updated_at`), by answering `extend` when
asked, or by retagging it out of `section:threads`. On a non-thread
node the same date is when the garden flags it for a review/compact
pass (rulings and specs are shielded) — still nothing deleted. →
[[precis-memory-help]] · [[precis-delete-help]]

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
TTL never goes stale and its footer reports
`CACHE:pinned`. On a cache kind the bare `pinned` spelling is refused
(it collides with the closed `CACHE:` value). `sticky:thread` /
`sticky:global` pin a memory into the per-turn preamble — that is
view-state, not retention; unpinning drops it from the preamble but
the memory stays in the corpus.
→ [[precis-tags]] · [[precis-cache]] · [[precis-memory-help]]

## How does memory decay work — does a decayed memory get deleted?

The decay fields are write-only today. `put` accepts
`args={'auto_refresh_days': N}` and stamps `refreshed_at` at insert;
DREAM-tagged writes get a default window (60 days, 90 for thread
dreams) when the writer set none. But nothing ranks or expires on
these fields — that sentence is about `refreshed_at` alone, not the
retirements that do exist: re-tagging does not reset `refreshed_at`,
and `refreshed_at` is display only (`view='raw'`, the web ref page) —
the preamble's recent-thoughts list orders by `updated_at`, not
`refreshed_at`. The memory retirements are the threads sweep and the
scratch TTL (see "What happens when a thread note expires?" above) —
they key off `updated_at`/tags, never off these decay fields, and they
soft-delete, never remove. →
[[precis-inner-life-help]]

## How do dreams and dream consolidation work?

A background dreaming pass writes `DREAM:speculative` memories — fenced out of default
`search` unless you name the tag:

```python
search(kind="memory", tags=["DREAM:speculative"], q="<topic>")
```

`DREAM:consolidated`/`acquire` are system-set (agent `tag`/`put` of
them is refused; `speculative` is writable). Dreams are not retired
automatically — they stay; their links are the point. Manual
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
graph's corpus, held on `finding`), `scratch` (opt-in throwaway space —
its nodes self-retire past a 7-day TTL, `meta.expires` overrides).
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

## Where can I read operator switches — settings, cadences, dark switches?

In the deployment, not the corpus: per-service switches (a cadence lit or
dark, a sweep's dry-run lever) are set by the operator. Read the live set
with `get(kind='job', id='/settings')` ([[precis-job-observe-help]]
§"Is a live switch set? — /settings") — there is no agent write verb.
Per-ref values an agent *can* write: `auto_refresh_days` (a decay
marker nothing reads yet), `sticky:*`, `meta.until` on a parked
gripe.

## See also

- [[precis-overview]] — the seven verbs and kind table
- [[precis-memory-help]] — the full memory verb surface
- [[precis-inner-life-help]] — decay windows and the dream tag protocol
- [[precis-self-consolidation-help]] — the manual consolidation pass
- [[precis-tags]] — every closed axis and bare flag
- [[precis-fisheye-help]] — the extent ladder and `+recall`
- [[precis-cache]] — cache TTLs and `CACHE:*` freshness
- [[precis-job-observe-help]] — the `/settings` view and the other read-only job views
