---
id: precis-link-help
title: precis — the link verb (typed edges between refs)
status: active
tier: 1
floor: any
applies-to: link (every kind that supports it)
last-updated: 2026-05-24
---

# precis-link-help — typed edges between refs

`link` creates and removes typed edges between two refs. Edges are
directional and carry a relation slug (`cites`, `blocks`,
`contradicts`, `derived-from`, `supports`, …). For the full
relation vocabulary, see `precis-relations`.

```python
# Add: this memory cites that paper.
link(kind='memory', id=42,
     target='paper:wang2020state', rel='cites')

# Remove: drop that specific (target, relation) pair.
link(kind='memory', id=42,
     target='paper:wang2020state', rel='cites',
     mode='remove')

# Remove every link from this memory to that paper, regardless of relation.
link(kind='memory', id=42,
     target='paper:wang2020state',
     mode='remove')
```

## Arguments

| Arg | Type | Default | Meaning |
|---|---|---|---|
| `kind` | str | required | Kind owning the *source* ref. |
| `id` | str / int | required | Source ref id. |
| `target` | str | required | Canonical link target — `kind:identifier[~selector]`. |
| `mode` | str | `add` | `add` creates the edge. `remove` deletes it. |
| `rel` | str | None | Relation slug. Defaults to `related-to` on add. |

## Canonical target form

Every link target uses the same shape:

```
kind:identifier[~selector]
```

Examples:

```
paper:wang2020state                # ref-level link
paper:wang2020state~38             # block-level link (block 38)
patent:ep4123456a1                 # patent ref
todo:158                           # numeric-id ref
markdown:notes/foo.md              # file ref
markdown:notes/foo.md~intro        # block in a file
```

The `kind:` prefix is **required** — the parser doesn't infer kind
from id shape because slug shapes overlap (e.g. `wang2020state`
could be a memory id elsewhere).

## Relations

Common relation slugs:

- `cites` — citation (paper, patent, memory)
- `blocks` — workflow blocker (todo, quest, gripe)
- `contradicts` — refutes / opposes
- `derived-from` — content provenance
- `supports` — corroborates
- `annotates` — note about a ref
- `references` — generic mention
- `related-to` — fallback default; use a specific relation when one
  fits

See `precis-relations` for the full vocabulary and per-kind
constraints.

## Mode semantics

- **`mode='add'`** (default): create the edge. Idempotent —
  re-adding the same (source, target, rel) is a no-op.
- **`mode='remove'`**:
  - With `rel=` set: removes the specific (target, relation) pair.
  - Without `rel=`: removes **every** link from source to target,
    regardless of relation.

## Where else can I link?

`link` is the dedicated verb for retroactive edge management.
Outbound links can also be attached **at creation time** via
`put(... link=..., rel=...)` when a fresh ref ships with an
outbound edge:

```python
put(kind='memory',
    text='Counter-evidence to Wang.',
    link='paper:wang2020state', rel='contradicts')
```

For link removal — or for adding to an existing ref — use this
verb directly.

## Worked examples

### Citation graph

```python
link(kind='memory', id=42,
     target='paper:wang2020state', rel='cites')
link(kind='memory', id=42,
     target='paper:kim2024electro', rel='cites')
```

### Block-level link to a paper paragraph

```python
link(kind='memory', id=42,
     target='paper:wang2020state~38', rel='annotates')
```

### Workflow blocker

```python
link(kind='todo', id=158,
     target='gripe:7', rel='blocks')
```

### Bulk-remove every link to a target

```python
link(kind='memory', id=42,
     target='paper:wang2020state',
     mode='remove')
```

## See also

- `precis-relations` — full relation vocabulary
- `precis-put-help` — link-during-create flow
- `precis-tag-help` — tags (cross-cutting metadata, distinct from
  links)
