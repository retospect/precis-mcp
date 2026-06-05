---
id: precis-tags
title: precis — set and filter by tags
applies-to: tag (add=, remove=), search (tags=), put (tags= on create)
status: active
---

# precis-tags — set and filter by tags

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
to workflow kinds (`todo`, `gripe`, `quest`); `memory` and other
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
search(kind='todo', q='write', tags=['STATUS:open', 'PRIO:high'])
search(kind='memory', q='kwargs vs modes', tags=['confidence-strong'])
```

`tags=` runs the same canonical-form validation as `tag(add=)` — an
`urgent` filter raises the bare-flag-collision error.

## What are the closed UPPERCASE axes?
## Which UPPERCASE prefixes does the runtime know?
## Where do STATUS, PRIO, SRC, CACHE come from?

The runtime rejects unknown values inside a registered closed prefix
and rejects bare flags that collide with a closed value. Pick from
the canonical list:

| Prefix | Values | Writer |
|---|---|---|
| `STATUS:` | `open` / `doing` / `blocked` / `done` / `won't-do` | agent |
| `PRIO:` | `low` / `normal` / `high` / `urgent` | agent |
| `SRC:` | `primary` / `secondary` | agent |
| `CACHE:` | `fresh` / `stale` / `pinned` | system |

Any UPPERCASE prefix outside that table is rejected — coin concepts
as lowercase tags (`density:dense`, `confidence:strong`) instead.

## Which closed axes apply to which kind?
## Per-kind axis matrix
## I tried PRIO:high on a memory and it was rejected — why?

Each kind opts in to a subset of the closed prefixes. A tag outside
the kind's allowed set raises `BadInput`; the error names the allowed
axes and suggests the lowercase rewrite.

| Kind | Allowed closed axes |
|---|---|
| `todo`, `gripe`, `quest` | `STATUS`, `PRIO` |
| `paper`, `patent` | `SRC`, `CACHE` |
| `research`, `think`, `websearch`, `web`, `youtube` | `CACHE` |
| `memory`, `fc`, `conv`, `oracle`, `skill` | _none_ — use lowercase open tags or bare flags |

Free-form kinds (`memory` etc.) express the same semantics with open
tags:

```python
tag(kind='memory', id=48, add=['prio:high'])      # OK (lowercase)
tag(kind='memory', id=48, add=['PRIO:high'])      # rejected
```

## What do validation errors look like?
## I got a BadInput on tag — what's the recovery?

```text
put(kind='todo', text='...', tags=['urgent'])
[error:BadInput] bare flag 'urgent' collides with closed value 'PRIO:urgent'
  next: use tags=['PRIO:urgent'] instead of tags=['urgent']

tag(kind='todo', id=40, add=['STATUS:bogus'])
[error:BadInput] invalid STATUS value: 'bogus'
  options: ['blocked', 'doing', 'done', 'open', "won't-do"]
```

## Tag a ref at creation time
## Add tags in the same put call (no second round-trip)

`put` accepts `tags=[...]` on creation:

```python
put(kind='memory', text='...', tags=['topic:co2-capture', 'confidence-strong'])
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
