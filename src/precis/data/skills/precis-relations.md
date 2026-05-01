---
id: precis-relations
title: precis — link two refs
status: phase-7
tier: 1
floor: any
applies-to: link (target=, mode=, rel=), get (view='links'), put (link=, rel= on create)
last-updated: 2026-04-28
---

# precis-relations — link two refs

Links connect any two refs (or specific blocks within them) across
kinds. The link table is **kind-agnostic** — paper → todo,
memory → markdown-block, todo → todo are all legal.

## Canonical syntax

The link target always carries an explicit kind prefix:

```
kind:identifier[~selector]
```

- `kind:` — registered kind (`paper`, `memory`, `todo`, `markdown`, …).
- `identifier` — slug for slug kinds, integer id for numeric kinds.
- `~selector` — *optional* block selector: numeric pos (`~38`) or
  block slug (`~agenda`).

The relation goes in a separate `rel=` kwarg (default `related-to`).
There is **no** colon-suffix shortcut — earlier docs that showed
`link='158:blocked-by'` were inconsistent and have been retired.

## Relations vocabulary

| `rel=` | Inverse | Use when |
|---|---|---|
| `related-to` (default) | self | Symmetric "see also" |
| `blocks` / `blocked-by` | each other | Workflow dependency |
| `cites` / `cited-by` | each other | Citation graph |
| `derived-from` / `derived-into` | each other | Provenance |
| `supports` / `supported-by` | each other | Evidential support |
| `contradicts` / `contradicted-by` | each other | Disagreement |
| `generalises` / `specialises` | each other | Abstraction level |
| `see-also` | (none) | Asymmetric pointer for context |

## Link a memory to a paper

```python
link(kind='memory', id=47, target='paper:wang2020state')
# default rel='related-to', mode='add'
```

## Cite a specific block

```python
link(kind='memory', id=89,
     target='paper:wang2020state~38',
     rel='cites')
```

Block selector `~38` pins the link to paper block 38 rather than
the paper as a whole.

## Block one todo on another

```python
link(kind='todo', id=141,
     target='todo:158',
     rel='blocked-by')
```

## Record a contradiction across kinds

```python
link(kind='memory', id=89,
     target='paper:chen2021critique',
     rel='contradicts')
```

## See what's linked

```python
get(kind='todo', id=141, view='links')
#  outbound
# → todo:158  (blocked-by)
#  inbound
# (none)
```

The renderer prints both directions: `→` for outbound, `←` for
inbound.

## Remove a link

```python
# Remove a specific (target, relation) pair
link(kind='todo', id=141, target='todo:158', mode='remove', rel='blocked-by')

# Remove ALL links to a target (any relation)
link(kind='todo', id=141, target='todo:158', mode='remove')
```

`mode='add'` and `mode='remove'` are mutually exclusive — issue
two `link()` calls if you want to swap a relation atomically:
remove the old (target, rel) pair, then add the new one.

## Validation errors

```python
link(kind='memory', id=47, target='wang2020state')
# [error:BadInput] link target 'wang2020state' missing required 'kind:' prefix
#   next: use canonical 'kind:identifier' form
#         (e.g. 'paper:wang2020state' or 'todo:158')

link(kind='memory', id=47, target='paper:nope')
# [error:NotFound] link target 'paper:nope' resolves to no live paper ref
#   next: check it exists: get(kind='paper', id='nope')

link(kind='memory', id=47, target='paper:wang2020state', rel='references')
# [error:BadInput] unknown relation: 'references'
#   options: ['blocked-by', 'blocks', 'cited-by', 'cites', ...]
#   next: pick from the registered relations or omit rel=
#         for the default 'related-to'
```

## Notes

- **Kind-agnostic:** any ref can link to any ref (live or
  soft-deleted; deleted targets render with a `(deleted)` marker).
- **Position-aware:** block-level links work on either end, e.g.
  `link='paper:slug~5'` or sourcing from a memory block via
  future Phase 7.5 work.
- **Idempotent:** re-issuing the same `(src, src_pos, dst, dst_pos,
  relation)` insert is a no-op (UNIQUE constraint).
- **Self-loops blocked:** linking a ref to itself at the same
  position raises `BadInput`. Same-ref different-pos links are
  allowed (e.g. `memory:42~3 → memory:42~7` for "see block 7").
- **Inverse_slug is documentation, not auto-mirroring** — adding
  a `cites` link does *not* create a `cited-by` row. The renderer
  shows the inverse direction by querying both sides.
- **Bare slug, mode-suffix syntax (older docs):** retired. The
  runtime now requires `kind:identifier` and a separate `rel=`.

## See also

- `precis-overview` — verbs and kinds
- `precis-tags` — for axes the relations vocabulary can't express
- `precis-todo-help` — `blocks`/`blocked-by` workflow filter
