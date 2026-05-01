---
id: precis-tags
title: precis — set and filter by tags
status: phase-7
tier: 1
floor: any
applies-to: tag (add=, remove=), search (tags=), put (tags= on create)
last-updated: 2026-04-28
---

# precis-tags — set and filter by tags

Three tag shapes by case.  Pick by what you're doing:

| Want to... | Use | Example |
|---|---|---|
| Set state on a fixed axis | `UPPERCASE:value` | `PRIO:high`, `STATUS:done` |
| Categorise by an open axis | `lowercase:value` | `topic:co2-capture`, `project:precis-v2` |
| Mark a boolean flag | bare | `star`, `wip`, `pinned` |

## Set tags

```python
tag(kind='memory', id=48, add=[
    'PRIO:high',           # replaces any other PRIO:* on this ref
    'topic:co2-capture',   # adds (lowercase tags accumulate)
    'star',                # bare flag set
])
```

UPPERCASE replaces within its prefix.  Lowercase and bare add.

## Remove tags

```python
tag(kind='memory', id=48, remove=[
    'topic:co2-capture',  # remove this lowercase tag
    'star',               # clear the flag
    'STATUS:done',        # remove only if STATUS is currently 'done'
])
```

`tag(remove=...)` is **value-matched** for closed prefixes:
`remove=['STATUS:open']` against a `STATUS:done` ref is a silent
no-op (no error, no row touched). To switch axes, prefer the
overwrite form via `tag(add=['STATUS:open'])` — it's atomic.

`tag()` is rejected on a non-existent ref, and `remove=` goes
through the same canonical-form validation as `add=` —
`remove=['urgent']` raises the bare-flag-collision error.

## Filter by tags

`search(tags=...)` narrows results to refs that carry **all** the
listed tags (AND semantics). Combine with `q=` for ranked search
inside the filtered set.

```python
search(kind='paper', q='photocatalysis', tags=['topic:co2-capture'])
# only blocks belonging to papers tagged with that topic

search(kind='todo', q='write', tags=['STATUS:open', 'PRIO:high'])
# only refs that carry BOTH tags

search(kind='memory', q='', tags=['star'])     # not currently legal
# search requires q=; use a list view instead:
get(kind='memory', id='/recent')                # then post-filter, OR
list_refs(kind='memory', tags=['star'])         # store-level (when
                                                # exposed by a handler)
```

The filter is applied at the SQL layer via the unified `ref_tags`
view, so it cuts the rows the lexical/semantic ranker has to score —
two orders of magnitude fewer rows for the typical "STATUS:open todo"
pattern. Same canonical-form validation as `tag(add=...)`: an
`urgent` filter raises the bare-flag-collision error.

## The closed UPPERCASE vocabularies

The runtime **rejects** unknown values inside a registered closed
prefix and **rejects** bare flags that collide with a closed value.
Pick from the canonical list:

| Prefix | Values | Writer |
|---|---|---|
| `STATUS:` | `open` / `doing` / `blocked` / `done` / `won't-do` | agent |
| `PRIO:` | `low` / `normal` / `high` / `urgent` | agent |
| `SRC:` | `primary` / `secondary` | agent |
| `CACHE:` | `fresh` / `stale` / `pinned` | system |

Other prefixes referenced in older docs (`DENSITY:`, `CONFIDENCE:`,
`STATUS:archived` / `STATUS:cancelled`) are **not** in the closed
vocabulary today and would be rejected; coin them as lowercase tags
(`density:dense`) until they're formally registered.

## Validation errors

```python
put(kind='memory', text='...', tags=['urgent'])
# [error:BadInput] bare flag 'urgent' collides with closed value 'PRIO:urgent'
#   next: use tags=['PRIO:urgent'] instead of tags=['urgent']

tag(kind='todo', id=40, add=['STATUS:bogus'])
# [error:BadInput] invalid STATUS value: 'bogus'
#   options: ['blocked', 'doing', 'done', 'open', "won't-do"]
```

## Create-with-tags shortcut

`put` accepts `tags=[...]` on creation as a shortcut so you don't
need two calls for a fresh ref:

```python
put(kind='memory', text='...', tags=['kind:decision', 'topic-co2'])
```

After creation, use `tag(...)` to mutate.

## Common lowercase prefixes

- `topic:` — subject matter (`topic:co2-capture`, `topic:noxrr`)
- `project:` — what initiative (`project:giri`, `project:precis-v2`)
- `kind:` — sub-kind on memories (`kind:decision`, `kind:idea`)

Coin new prefixes freely. Lowercase prefixes are open-ended; the
runtime never rejects them.

## Common bare flags

`wip`, `star`, `draft`, `private`, `pinned`. Coin new ones freely as
long as they don't collide with the closed-vocab values above.

## Not yet implemented

- `tags=` filter on `get(kind=K)` list views — surfaced at the
  store level (`Store.list_refs(tags=...)`) but not piped through
  the agent-facing list-view path. Use `search(...)` with `q=`
  for now.
- Block-level (positional) tag filtering — the schema supports
  `pos=N` tags on a specific block, but no handler currently
  writes them and the search filter only matches ref-level tags.

## See also

- `precis-overview` — verbs and kinds
- `precis-cache` — `CACHE:*` and the `pinned` flag
- `precis-todo-help` — `STATUS:` lifecycle
- `precis-memory-help` — `kind:` discriminator
