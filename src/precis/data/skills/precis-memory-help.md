---
id: precis-memory-help
title: precis — capture notes, decisions, ideas, questions
applies-to: get/search (kind='memory'), put (kind='memory')
status: active
---

# precis-memory-help — capture notes, decisions, ideas, questions

Memory is a numeric-ref scratchpad for thoughts that stand alone:
notes, decisions, ideas, open questions, distilled summaries.
Categorise with open tags (`topic:`, `project:`, `confidence-*`)
and bare flags (`pinned`, `wip`). There is no enforced sub-kind.

Server assigns an integer id on create. Both `id=47` and
`id='memory:47'` are accepted (the link-target form).

## Save a thought
## Capture a note
## Jot something down before I forget

```python
put(kind='memory',
    text='Wang2020 chunk 38 has the cleanest Z-scheme diagram.',
    tags=['topic:noxrr'],
    link='paper:wang2020state~38')
# → returns integer id (e.g. 73)
```

`text=` is the only required arg. `tags=` and `link=` on create
save a round-trip vs. a follow-up `tag()` / `link()`.

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
    link='paper:wang2020state~38', rel='cites')
```

After the fact, use `link()`:

```python
link(kind='memory', id=73,
     target='paper:wang2020state', rel='related-to')

link(kind='memory', id=73,
     target='paper:chen2021critique', rel='contradicts')
```

Targets always carry the `kind:` prefix. Relation vocabulary
(`cites`, `contradicts`, `supports`, `derived-from`, …) lives in
`precis-relations`.

## Promote a research cache to a durable memory
## Distil a Sonar deep-research answer into a note
## Save the gist of an expensive cache call

`get(kind='research', ...)` returns a long, expensive answer. The
durable distillation is a memory linked back to the cache:

```python
get(kind='research', q='mechanism of NOxRR')   # populates cache

put(kind='memory',
    text='Distilled mechanism: three-electron pathway via *NO → *N₂O₂ → N₂.',
    tags=['topic:noxrr', 'confidence-moderate'],
    link='research:mechanism-of-noxrr', rel='derived-from')
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

## Tag axes available on memory

Closed UPPERCASE axes (`STATUS:`, `PRIO:`, `SRC:`, `CACHE:`) are
**rejected** on memory. Express the same intent with open tags:

| Want | Use |
|---|---|
| Priority | `prio:high` (lowercase) |
| Status | `wip`, `done` (bare flags) or `status:open` (lowercase) |
| Confidence | `confidence-tentative` / `-moderate` / `-strong` / `-certain` |
| Topic | `topic:<slug>` |
| Project | `project:<slug>` |
| Boolean | `pinned`, `star`, `private`, `draft` |

See `precis-tags` for the full axis vocabulary.

## See also

```python
get(kind='skill', id='precis-overview')       # verbs and kinds
get(kind='skill', id='precis-tags')           # open-tag axes, bare flags
get(kind='skill', id='precis-relations')      # rel= vocabulary
get(kind='skill', id='precis-link-help')      # link verb mechanics
get(kind='skill', id='precis-cache')          # research/think/web TTLs
get(kind='skill', id='precis-search-help')    # hybrid search mechanics
get(kind='skill', id='precis-put-help')       # put-verb arg shapes
```
