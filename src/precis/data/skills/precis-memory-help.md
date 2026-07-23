---
id: precis-memory-help
title: precis — capture notes, decisions, ideas, questions
summary: scratchpad for notes, decisions, ideas, questions — open tags, no enforced sub-kind
applies-to: get/search (kind='memory'), put (kind='memory')
status: active
---

# precis-memory-help — capture notes, decisions, ideas, questions

Memory is a numeric-ref scratchpad for thoughts that stand alone:
notes, decisions, ideas, open questions, distilled summaries.
Categorise with open tags (`topic:`, `project:`, `confidence-*`)
and bare flags (`pinned`, `wip`). There is no enforced sub-kind.

Server assigns an integer id on create. Its handle is `me<id>`
(e.g. `me47`); `id=47` and `id='memory:47'` (link-target form) still
resolve on input.

**There is no slug/filename lookup — `id=` must be an integer (or a
handle/link-target form that decodes to one).** This is a different
system from the harness's own local `~/.claude/.../memory/*.md` files,
which *are* addressed by filename-stem slug (e.g. `backlog_foo`) — that
slug means nothing here. Passing one to `get`/`delete`/`tag`/`link`
raises `[error:BadInput] memory id must be an integer, got '<slug>'`.
If you only have a topic, not the id: `search(kind='memory',
q='<topic>')` first, then use the integer id from the hit.

## Save a thought
## Capture a note
## Jot something down before I forget

```python
put(kind='memory',
    text='Wang2020 chunk 38 has the cleanest Z-scheme diagram.',
    title='Wang2020 has the cleanest Z-scheme diagram',   # short header
    tags=['topic:noxrr'],
    link='pc38')                       # chunk handle (legacy paper:wang2020state~38 still resolves)
# → returns integer id (e.g. 73)
```

`text=` is the only required arg — it is the memory's **body prose**,
stored in a chunk (embedded + keyworded, so search finds it). `title=`
is the short **header** shown in listings, search hits, and the grid;
write the body first, then a title once its point is clear. Omit `title=`
and one is derived from the body's first line (capped at 80 chars), but an
explicit title reads better. `tags=` and `link=` on create save a
round-trip vs. a follow-up `tag()` / `link()`.

Rewrite a memory in place with `edit(kind='memory', id=N, mode='replace',
text='new body'[, title='new header'])` — same id, links stay attached,
old body kept in `view='log'`.

## Record a decision I just made
## Log a design choice with rationale
## Pin a decision so I can find it later

```python
put(kind='memory',
    text='Dropped mode-driven tag/link in favour of typed kwargs.',
    tags=['confidence-strong', 'project:precis-v2', 'topic:api-design'])
```

Decisions are conventionally tagged with `confidence-*` and the
relevant `project:`. Add `pinned` to suppress any future archival.

## Flag an open question to come back to
## Park an unresolved problem
## Note something I'm unsure about

```python
put(kind='memory',
    text='Does CACHE: pinning play well with re-ingest?',
    tags=['confidence-tentative', 'topic:caching', 'wip'])
```

`confidence-tentative` + `wip` is the convention for unresolved
questions. Drop `wip` and bump confidence when you settle it.

## Find memories I wrote earlier
## Search my notes by topic
## Look up what I decided about X

```python
search(kind='memory', q='kwargs vs modes')
search(kind='memory', q='kwargs vs modes', tags=['topic:api-design'])
search(kind='memory', tags=['project:precis-v2', 'confidence-strong'])
```

`q=` is hybrid lexical + semantic over memory text. `tags=` narrows
to refs carrying every listed tag (AND). Omit `q=` to browse a
tag slice.

## Read a memory I have the id for
## Open a memory by id

```python
get(id='me73')                       # handle — prefix infers kind=
get(kind='memory', id=73)
get(kind='memory', id='memory:73')   # link-target form also works
```

## Link a memory to the paper or patent it came from
## Attach a note to a specific paper section
## Cross-reference a memory with another ref

Set the link at creation time when the memory exists *because of*
another ref:

```python
put(kind='memory',
    text='Three-electron pathway — see §2.',
    tags=['topic:noxrr'],
    link='pc38', rel='cites')          # chunk handle (legacy paper:wang2020state~38 still resolves)
```

After the fact, use `link()`:

```python
link(kind='memory', id=73,
     target='pa<id>', rel='related-to')    # ref handle (legacy paper:wang2020state still resolves)

link(kind='memory', id=73,
     target='pa<id>', rel='contradicts')   # the chen2021critique handle
```

Targets lead with the ref/chunk **handle** (`pa<id>`, `pc38`); the
legacy `kind:slug` form (`paper:wang2020state`) still resolves.
Relation vocabulary (`cites`, `contradicts`, `supports`,
`derived-from`, …) lives in `precis-relations`.

## Name another ref in the text — it auto-links
## Cite a paper / patent / memory inline so the connection is traceable

Naming another ref **inside the memory body** by its `[handle]` makes the
memory a node in the graph: every `[handle]` you write resolves to a live
`related-to` backlink, so the memory is discoverable from the *target's*
side too — not just by its own text. A handle is a ref to *something*, so
one rule covers every kind:

```python
put(kind='memory',
    text="[pa812] free-energy bound mirrors [pt913] clamp circuit.",
    tags=['topic:thermo'])
# → related-to links to pa812 and pt913, materialised from the text
```

Write the `[handle]` exactly as a `search` / `get` result printed it
(`[me5]` a memory, `[pa5]` / `[pc10]` a paper, `[pt6]` a patent,
`[or3]` an oracle, …). Editing a mention out drops its link on the next
write; adding one adds the link. This is the lightweight alternative to
an explicit `link()` — reach for it when the reference lives naturally in
the prose, and `link()` when it doesn't. See `precis-addressing-help` for
the handle form.

## Promote a research cache to a durable memory
## Distil a Sonar deep-research answer into a note
## Save the gist of an expensive cache call

`get(kind='perplexity-research', ...)` returns a long, expensive answer. The
durable distillation is a memory linked back to the cache:

```python
get(kind='perplexity-research', q='mechanism of NOxRR')   # populates cache

put(kind='memory',
    text='Distilled mechanism: three-electron pathway via *NO → *N₂O₂ → N₂.',
    tags=['topic:noxrr', 'confidence-moderate'],
    link='perplexity-research:mechanism-of-noxrr', rel='derived-from')
```

The memory survives the cache's TTL and carries your own framing.

## Bump confidence as evidence accumulates
## Upgrade a tentative note to strong
## Change confidence-moderate to confidence-certain

`confidence-*` is an open-tag axis — open tags accumulate rather
than replace, so untag the old value and add the new in one call:

```python
tag(kind='memory', id=73,
    add=['confidence-certain'],
    remove=['confidence-moderate'])
```

Levels: `confidence-tentative` → `confidence-moderate` →
`confidence-strong` → `confidence-certain`.

## Sticky memories — show up every turn until they decay

Some memories matter so much you want them in front of you on every
turn until they don't. Tag them sticky:

```python
# Pin to this thread for 30 days (the default TTL)
tag(kind='memory', id=42, add=['sticky:thread'])

# Pin globally — visible in every conv — for 90 days (default)
tag(kind='memory', id=42, add=['sticky:global'])

# Pin for a specific TTL — re-tag bumps it back to that window
tag(kind='memory', id=42, add=['sticky:thread'], ttl_days=7)
tag(kind='memory', id=42, add=['sticky:global'], ttl_days=180)

# Refresh — re-tagging resets the expiry to a fresh window
tag(kind='memory', id=42, add=['sticky:thread'])    # TTL → 30d again

# Actively unpin (before expiry)
tag(kind='memory', id=42, remove=['sticky:thread'])
```

**Memory survives forever.** The sticky tag is a view-state — when
it expires (or you remove it), the memory itself stays in the
corpus and remains searchable; only the per-turn preamble injection
stops. asa_bot's preamble shows a `[expires in Nd]` warning when a
sticky tag is within 3 days of decay, so you can decide whether to
refresh or let it go.

Use sparingly — every sticky memory eats prompt budget every turn.
~5 thread-scoped + ~5 global is the soft cap.

## Tag axes available on memory

The only closed UPPERCASE axis accepted on memory is `DREAM:`
(`consolidated` / `speculative` / `acquire`) — written by the
dreaming worker, not by agent code. Every other closed axis
(`STATUS:`, `PRIO:`, `SRC:`, `CACHE:`, `WATCH:`) is rejected.
Express the same intent with open tags:

| Want | Use |
|---|---|
| Priority | `prio:high` (lowercase) |
| Status | `wip`, `done` (bare flags) or `status:open` (lowercase) |
| Confidence | `confidence-tentative` / `-moderate` / `-strong` / `-certain` |
| Topic | `topic:<slug>` |
| Project | `project:<slug>` |
| Boolean | `pinned`, `star`, `private`, `draft` |

See `precis-tags` for the full axis vocabulary and per-kind matrix.

## See also

```python
get(kind='skill', id='precis-overview')       # verbs and kinds
get(kind='skill', id='precis-tags')           # open-tag axes, bare flags
get(kind='skill', id='precis-relations')      # rel= vocabulary
get(kind='skill', id='precis-link-help')      # link verb mechanics
get(kind='skill', id='precis-cache')          # perplexity-research/perplexity-reasoning/web TTLs
get(kind='skill', id='precis-search-help')    # hybrid search mechanics
get(kind='skill', id='precis-put-help')       # put-verb arg shapes
```
