---
id: precis-todo-help
title: precis — create, prioritise, schedule, complete todos
status: draft
tier: 1
floor: any
applies-to: get/search (kind='todo'), put (kind='todo')
last-updated: 2026-04-26
---

# precis-todo-help — create, prioritise, schedule, complete todos

## Create

```python
put(kind='todo', text='Review section 3 of wang2020state.',
    tags=['PRIO:high', 'project:precis-v2'],
    due='friday')
# server assigns integer id (e.g. 122); STATUS:active default
```

`due=` accepts `'2026-05-01'`, `'friday'`, `'+3d'`, `'tomorrow 5pm'`.
Server normalises to ISO.

## See what's on

```python
get(kind='todo', view='today')        # due today, active
get(kind='todo', view='overdue')      # past due, active
get(kind='todo', view='due')          # all dated active, sorted
get(kind='todo', view='unscheduled')  # active, no due date
get(kind='todo', view='blocked')      # active with blocked-by links
get(kind='todo', view='done')         # recently completed
```

## Mark done

```python
put(kind='todo', id='122', tags=['STATUS:done'])
# stamps completed_at atomically
```

## Block on another todo

```python
put(kind='todo', id='141', link='158', rel='blocked-by')
# 141 enters view='blocked' until 158 hits STATUS:done
```

## Re-prioritise

```python
put(kind='todo', id='141', tags=['PRIO:urgent'], untags=['wip'])
# PRIO:urgent replaces any other PRIO:*
```

## Sweep by project

```python
search(kind='todo', tags=['project:precis-v2', 'STATUS:active'])
```

## Notes

- `STATUS:` values: `active` (default), `done`, `blocked`, `archived`, `cancelled`.
- `PRIO:` values: `low`, `med`, `high`, `urgent`.
- `view='today'` shows everything due today, sorted by priority.
  Use `tags=['PRIO:urgent']` to filter.

## See also

- `precis-overview` — verbs and kinds
- `precis-tags` — `STATUS:`, `PRIO:`, `project:`
- `precis-relations` — `blocks` / `blocked-by`
