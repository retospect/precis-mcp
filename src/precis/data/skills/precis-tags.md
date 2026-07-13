---
id: precis-tags
title: precis — set, filter by, and discover tags
summary: tag taxonomy — UPPERCASE axes, lowercase open axes, bare flags, filter and discover
applies-to: tag (add=, remove=), search (tags=), put (tags= on create), get/search (kind='tag')
status: active
---

# precis-tags — set, filter by, and discover tags

Three tag shapes by case. Pick by what you're doing:

| Want to... | Use | Example |
|---|---|---|
| Set state on a fixed axis | `UPPERCASE:value` | `PRIO:high`, `STATUS:done` |
| Categorise by an open axis | `lowercase:value` | `topic:co2-capture`, `project:precis-v2` |
| Mark a boolean flag | bare | `star`, `wip`, `pinned` |

UPPERCASE replaces within its prefix. Lowercase and bare accumulate.

## How do I set a tag?
## Add a tag to an existing ref
## How do I mark a todo as high priority?

```python
tag(kind='todo', id=48, add=[
    'PRIO:high',           # replaces any other PRIO:* on this ref
    'topic:co2-capture',   # adds (lowercase tags accumulate)
    'star',                # bare flag set
])
```

Closed prefixes are **kind-gated** — `PRIO:` and `STATUS:` only apply
to workflow kinds (`todo`, `gripe`); `memory` and other
free-form kinds reject them. See the per-kind axis matrix below.

## How do I remove a tag?
## Drop a tag from a ref
## How do I clear STATUS:done on a todo?

```python
tag(kind='todo', id=48, remove=[
    'topic:co2-capture',  # remove this lowercase tag
    'star',               # clear the flag
    'STATUS:done',        # remove only if STATUS is currently 'done'
])
```

`remove=` is value-matched for closed prefixes — `remove=['STATUS:open']`
against a `STATUS:done` ref is a silent no-op. To switch axes, prefer
the overwrite form `tag(add=['STATUS:open'])` — atomic, no separate
remove needed. `remove=` runs the same canonical-form validation as
`add=` (so `remove=['urgent']` raises the bare-flag-collision error).

## How do I filter search results by tag?
## Restrict search to refs that carry tag X
## Combine search with a tag filter

`search(tags=...)` narrows results to refs that carry **all** the
listed tags (AND semantics). Combine with `q=` for ranked search
inside the filtered set:

```python
search(kind='paper', q='photocatalysis', tags=['topic:co2-capture'])
```

```python
search(kind='todo', q='write', tags=['STATUS:open', 'PRIO:high'])
```

```python
search(kind='memory', q='kwargs vs modes', tags=['confidence-strong'])
```

`tags=` runs the same canonical-form validation as `tag(add=)` — an
`urgent` filter raises the bare-flag-collision error.

## List every tag in use
## Browse the tag vocabulary
## What tags exist across the corpus?

```python
get(kind='tag')                            # most-used first, paginated
get(kind='tag', page=2)                    # next page (default 50 per page)
get(kind='tag', page_size=20)              # smaller page
get(kind='tag', scope='paper')             # tags used on papers only
```

Output is a TOON table with `tag / count / axis`. Each row is
addressable — paste the `tag` slug back as `id=` to drill in.

## Search tags semantically
## Find tags related to a topic
## Is there already a tag for "sustainability"?

```python
search(kind='tag', q='carbon capture')     # hybrid lexical + semantic
search(kind='tag', q='sustainability')
search(kind='tag', q='photocatalysis', page=2)
```

**Before coining a new tag, search for an existing one.** Fragmentation
(`topic:co2-capture` alongside `topic:carbon-capture` alongside
`topic:CO2`) is the chief failure mode of an open-axis tag system; the
discovery surface exists to prevent it.

Lexical substring matches come first; semantically-similar tags
follow in a "related" section. Pick the closest existing tag rather
than inventing a new neighbour.

## See what's tagged with X
## Get metadata for a tag
## Who uses topic:co2-capture?

```python
get(kind='tag', id='topic:co2-capture')    # count, first/last seen, sample refs
get(kind='tag', id='STATUS:done')          # closed axis — also shows sibling values
get(kind='tag', id='pinned')               # bare flag (probes FLAG and OPEN)
```

Shows usage count, when the tag was first/last attached, up to 5
sample refs carrying it, and (for closed axes) the sibling values
in the same prefix. To enumerate **every** ref carrying a tag, use
`search(q='', tags=['<tag>'])` with the appropriate `kind=`.

## What are the closed UPPERCASE axes?
## Which UPPERCASE prefixes does the runtime know?
## Where do STATUS, PRIO, SRC, CACHE come from?

The runtime rejects unknown values inside a registered closed prefix
and rejects bare flags that collide with a closed value. Pick from
the canonical list:

| Prefix | Values | Writer |
|---|---|---|
| `STATUS:` | see table below — value subset depends on kind | agent |
| `PRIO:` | `low` / `normal` / `high` / `urgent` | agent |
| `SRC:` | `primary` / `secondary` | agent |
| `CACHE:` | `fresh` / `stale` / `pinned` | system |
| `WATCH:` | `hourly` / `daily` / `weekly` / `monthly` | agent (cache-backed refs) |
| `DREAM:` | `consolidated` / `speculative` / `acquire` | dreaming worker |
| `DENSITY:` | `dense` / `medium` / `sparse` | chunk pipeline (chunk-level — not applied to refs) |
| `AUDIT:` | `missing-citation` / `empty-stub` / `unsupported-claim` / `citation-drift` / `missing-data` | content-QA audit (on the anchored change-request `todo`/`finding`) |

Any UPPERCASE prefix outside that table is rejected — coin concepts
as lowercase tags (`density:dense`, `confidence:strong`) instead.

### `STATUS:` value subsets per lifecycle

`STATUS:` is the one axis that hosts multiple lifecycles on the same
prefix. The runtime accepts the union (20 values); each handler enforces
a sane subset for its kind. Pick the row that matches the ref you're
tagging:

| Lifecycle | Kinds | Values |
|---|---|---|
| Workflow (original) | `todo`, `gripe` | `open`, `doing`, `blocked`, `done`, `won't-do` |
| Gripe-specific | `gripe` | also: `triaged`, `ready_for_fix`, `in_review`, `wontfix` |
| Citation chase | `finding` | `tracing`, `established`, `multi_candidate`, `dead_chain` |
| Job queue | `job` | `queued`, `submitted`, `running`, `succeeded`, `failed`, `cancelled`, `cancel_requested` |

The runtime rejects unknown values at write time with the full options
list. To see the live set, `get(kind='skill', id='precis-status-help')`.

## Which closed axes apply to which kind?
## Per-kind axis matrix
## I tried PRIO:high on a memory and it was rejected — why?

Each kind opts in to a subset of the closed prefixes. A tag outside
the kind's allowed set raises `BadInput`; the error names the allowed
axes and suggests the lowercase rewrite.

| Kind | Allowed closed axes |
|---|---|
| `todo` | `STATUS`, `PRIO`, `LLM` (dispatch tier), `AUDIT` (content-QA category) |
| `gripe` | `STATUS`, `PRIO` |
| `finding` | `STATUS` (lifecycle subsets — see table above); also `AUDIT` (content-QA category) |
| `job` | `STATUS` (lifecycle subsets — see table above) |
| `paper`, `patent` | `SRC`, `CACHE` |
| `perplexity-research`, `perplexity-reasoning`, `websearch`, `web`, `youtube` | `CACHE`, `WATCH` |
| `memory` | `DREAM` (dreaming-worker provenance) |
| `anki`, `conv`, `oracle`, `skill` | _none_ — use lowercase open tags or bare flags |

Free-form kinds (`memory` etc.) express the same semantics with open
tags:

```python
tag(kind='memory', id=48, add=['prio:high'])      # lowercase = OK
```

(`PRIO:high` on memory would be rejected — the runtime error names the
allowed axes and suggests the lowercase form.)

## What do validation errors look like?
## I got a BadInput on tag — what's the recovery?

```text
put(kind='todo', text='...', tags=['urgent'])
[error:BadInput] bare flag 'urgent' collides with closed value 'PRIO:urgent'
  next: use tags=['PRIO:urgent'] instead of tags=['urgent']

tag(kind='todo', id=40, add=['STATUS:bogus'])
[error:BadInput] invalid STATUS value: 'bogus'
  options: ['blocked', 'cancel_requested', 'cancelled', 'dead_chain',
            'doing', 'done', 'established', 'failed', 'in_review',
            'multi_candidate', 'open', 'queued', 'ready_for_fix',
            'running', 'submitted', 'succeeded', 'tracing', 'triaged',
            'won't-do', 'wontfix']
```

## Tag a ref at creation time
## Add tags in the same put call (no second round-trip)

`put` accepts `tags=[...]` on creation:

```python
put(kind='memory', text='...', tags=['topic:co2-capture', 'confidence-strong'])
```

```python
put(kind='todo', text='...', tags=['PRIO:high', 'project:precis-v2'])
```

After creation, use `tag(...)` to mutate.

## Common lowercase prefixes and bare flags

Lowercase prefixes (coin new ones freely):

- `topic:` — subject matter (`topic:co2-capture`, `topic:noxrr`)
- `project:` — which initiative (`project:giri`, `project:precis-v2`)
- `confidence-` — bare-style confidence levels (`confidence-tentative`,
  `confidence-moderate`, `confidence-strong`, `confidence-certain`)

Bare flags (coin freely as long as they don't collide with a closed
value on the same kind): `wip`, `star`, `draft`, `private`, `pinned`.

The collision check is kind-scoped: `tag(kind='memory', add=['pinned'])`
works (memory has no `CACHE:` axis), but `tag(kind='paper', add=['pinned'])`
is rejected (paper allows `CACHE:` and the bare flag would shadow the
closed form).

## See also

```python
get(kind='skill', id='precis-overview')        # verbs and kinds
get(kind='skill', id='precis-tag-help')        # the tag verb mechanics
get(kind='skill', id='precis-search-help')     # tags= filter inside search
get(kind='skill', id='precis-cache')           # CACHE:* and the pinned flag
get(kind='skill', id='precis-todo-help')       # STATUS:/PRIO: lifecycle
get(kind='skill', id='precis-memory-help')     # open-tag categorisation
```
