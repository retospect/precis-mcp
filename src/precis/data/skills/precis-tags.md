---
id: precis-tags
title: precis — set and filter by tags
status: draft
tier: 1
floor: any
applies-to: put (tags=/untags= kwargs), search/get (tags= filter)
last-updated: 2026-04-26
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
put(kind='memory', id='48', tags=[
    'PRIO:high',           # replaces any other PRIO:* on this ref
    'topic:co2-capture',   # adds (lowercase tags accumulate)
    'star',                # bare flag set
])
```

UPPERCASE replaces within its prefix.  Lowercase and bare add.

## Filter by tags

```python
search(kind='paper', q='photocatalysis', tags=['SRC:primary', 'CACHE:fresh'])
# ANDs across the list
```

## Remove tags

```python
put(kind='memory', id='48', untags=['star', 'topic:co2-capture'])
```

## Compose in one call

```python
put(kind='todo', id='141', tags=['STATUS:done'], untags=['wip', 'star'])
```

## The six closed UPPERCASE prefixes

| Prefix | Values | Writer |
|---|---|---|
| `SRC:` | `primary` / `secondary` / `rumor` / `generated` | system, read-only |
| `CACHE:` | `fresh` / `stale` / `expired` | system, read-only |
| `DENSITY:` | `sparse` / `medium` / `dense` | system, read-only |
| `STATUS:` | `active` / `done` / `blocked` / `archived` / `cancelled` | agent |
| `PRIO:` | `low` / `med` / `high` / `urgent` | agent |
| `CONFIDENCE:` | `tentative` / `moderate` / `strong` / `certain` | agent |

`STATUS:done` also stamps `completed_at` atomically.

## Common lowercase prefixes

- `topic:` — subject matter (`topic:co2-capture`, `topic:noxrr`)
- `project:` — what initiative (`project:giri`, `project:precis-v2`)
- `kind:` — sub-kind on memories (`kind:decision`, `kind:idea`)

Coin new prefixes freely.

## Common bare flags

`wip`, `star`, `draft`, `private`, `pinned` (suppresses `CACHE:*` decay).
Coin new ones freely.

## Notes

- Bare flag names cannot collide with closed-vocab values.
  `tags=['urgent']` errors — use `tags=['PRIO:urgent']`.
- Invalid UPPERCASE values error with the valid list.

## See also

- `precis-overview` — verbs and kinds
- `precis-cache` — `CACHE:*` and the `pinned` flag
- `precis-density` — `DENSITY:*` and novelty-finding
- `precis-todo-help` — `STATUS:` lifecycle
- `precis-memory-help` — `CONFIDENCE:` and the `kind:` discriminator
