---
id: precis-todo-help
title: precis — create, prioritise, complete todos
status: phase-7
tier: 1
floor: any
applies-to: get/search (kind='todo'), put (kind='todo')
last-updated: 2026-04-28
---

# precis-todo-help — create, prioritise, complete todos

## Create

```python
put(kind='todo', text='Review section 3 of abazari2024design.',
    tags=['PRIO:high'])
# server assigns integer id (e.g. 122); STATUS:open default
```

## See what's on

```python
get(kind='todo')                      # alias for /recent
get(kind='todo', id='/recent')        # most recent 20 (any status)
get(kind='todo', id='/open')          # open + doing + blocked (the queue)
get(kind='todo', id='/doing')         # in-progress only
get(kind='todo', id='/blocked')       # waiting on something
get(kind='todo', id='/done')          # completed
```

`id='/queue'` is an alias for `/open`. **Date-driven views (`/today`,
`/overdue`, `/due`, `/unscheduled`) are not implemented** — todos
have no built-in due-date field today; track scheduling client-side
or via lowercase tags (`due:2026-05-01`).

## Mark done / move through statuses

```python
put(kind='todo', id=122, tags=['STATUS:done'])
put(kind='todo', id=122, tags=['STATUS:doing'])
put(kind='todo', id=122, tags=['STATUS:blocked'])
```

`STATUS:` is closed-prefix and replaces atomically — setting one
removes the previous value.

## Block on another todo

```python
put(kind='todo', id=141, link='todo:158', rel='blocked-by')
# id=141 declares it's blocked-by id=158
```

The link target always carries an explicit kind prefix
(`todo:158`, `paper:wang2020state`, `markdown:notes/x.md`); the
relation goes in a separate `rel=` kwarg. See `precis-relations`
for the full vocabulary and inverse map.

## Re-prioritise

```python
put(kind='todo', id=141, tags=['PRIO:urgent'])
# replaces any other PRIO:* on this ref atomically
```

For closed-prefix axes, just set the new value — overwrite is
atomic. For open or flag tags, use `untags=['topic-x']` (see
`precis-tags` for the full removal semantics).

## Search by content

```python
search(kind='todo', q='precis-v2 review', tags=['STATUS:open'])
# narrow to open todos only
```

`tags=` accepts the same canonical forms as `put`. Multiple tags
combine with AND (`tags=['STATUS:open', 'PRIO:high']` returns
only refs that carry both).

## Vocabulary

- `STATUS:` values: `open` (default on create), `doing`, `blocked`,
  `done`, `won't-do`. Unknown values are rejected with the valid
  list at write time.
- `PRIO:` values: `low`, `normal`, `high`, `urgent`.

## See also

- `precis-overview` — verbs and kinds
- `precis-tags` — `STATUS:`, `PRIO:`, validation rules
- `precis-relations` — `blocks` / `blocked-by`
