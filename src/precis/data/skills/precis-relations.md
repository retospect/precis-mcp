---
id: precis-relations
title: precis — link two refs
status: draft
tier: 1
floor: any
applies-to: put (link=/unlink= kwargs), get (view='links')
last-updated: 2026-04-26
---

# precis-relations — link two refs

Three relations.  `related-to` is the default.

| `rel=` | Use when |
|---|---|
| `related-to` (default) | Any connection.  "See also." |
| `blocked-by` / `blocks` | Workflow dependency.  Target won't progress until source completes. |
| `contradicts` / `contradicted-by` | Source disagrees with target's claim. |

## Link a memory to a paper

```python
put(kind='memory', id='47', link='wang2020state')
# default rel='related-to'
```

## Block one todo on another

```python
put(kind='todo', id='141', link='158', rel='blocked-by')
```

Now `get(kind='todo', view='blocked')` includes `141` until `158` is
`STATUS:done`.

## Record a contradiction

```python
put(kind='paper', id='wang2020state', link='chen2021critique', rel='contradicts')
```

## See what's linked

```python
get(kind='todo', id='141', view='links')
# → outbound: blocked-by 158
# → inbound:  (nothing)
```

## Remove a link

```python
put(kind='todo', id='141', unlink='158', rel='blocked-by')
# omit rel= to remove all links to that target
```

## Notes

- Mixed kinds are fine: `memory → paper`, `todo → todo`, `paper → paper-chunk`.
- Deleting a ref removes its links.
- For nuance the three relations don't cover ("extends," "quotes," "echoes"),
  use `related-to` and put the nuance in the link's text note.

## See also

- `precis-overview` — verbs and kinds
- `precis-tags` — for axes the 3 relations can't express
- `precis-todo-help` — `blocks`/`blocked-by` and the `view='blocked'` filter
