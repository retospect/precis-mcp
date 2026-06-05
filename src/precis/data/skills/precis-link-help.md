---
id: precis-link-help
title: precis — the link verb (typed edges between refs)
applies-to: link (every kind that supports it)
status: active
---

# precis-link-help — typed edges between refs

`link` creates or removes a directional, typed edge from one ref to
another. The relation vocabulary (`cites`, `blocks`, `contradicts`,
…) lives in `precis-relations`; this skill documents the verb.

## Connect one ref to another
## Add a typed edge between two refs
## How do I record that ref A cites ref B?

```python
link(kind='memory', id=42,
     target='paper:wang2020state', rel='cites')
```

Source is `(kind, id)`. Target is a canonical address. `rel=`
defaults to `related-to` — name a specific relation when one fits.
Re-adding the same `(source, target, rel)` is a no-op.

## Address the other end of the edge
## What does target= take?
## How do I point at a block of a paper, not the whole paper?

Targets use one shape: `kind:identifier[~selector]`.

```text
paper:wang2020state                # ref-level
paper:wang2020state~38             # block 38 of that paper
patent:ep4123456a1
todo:158                           # numeric-id ref
markdown:notes/foo.md
markdown:notes/foo.md~intro        # block in a file
```

The `kind:` prefix is required — slug shapes overlap across kinds
and the parser won't guess.

## Remove an edge I added earlier
## Drop a link between two refs
## Undo a link

```python
link(kind='memory', id=42,
     target='paper:wang2020state', rel='cites',
     mode='remove')                                  # one specific (target, rel)

link(kind='memory', id=42,
     target='paper:wang2020state',
     mode='remove')                                  # every edge to this target
```

With `rel=` set, `mode='remove'` deletes that exact pair. Omit
`rel=` to drop every link from source to target regardless of
relation.

## Arguments

| Arg | Type | Default | Meaning |
|---|---|---|---|
| `kind` | str | required | Kind of the source ref. |
| `id` | str / int | required | Source ref id. |
| `target` | str | required | `kind:identifier[~selector]`. |
| `rel` | str | `related-to` | Relation slug (see `precis-relations`). |
| `mode` | str | `add` | `add` or `remove`. |

## Link at creation time instead

When a fresh ref ships with one outbound edge, attach it on the
`put` call rather than a follow-up `link`:

```python
put(kind='memory',
    text='Counter-evidence to Wang.',
    link='paper:wang2020state', rel='contradicts')
```

Use `link` directly for removal, for second edges, or when adding
to a ref that already exists.

## Block-level link to a paper paragraph

```python
link(kind='memory', id=42,
     target='paper:wang2020state~38', rel='annotates')
```

## Workflow blocker between tasks

```python
link(kind='todo', id=158,
     target='gripe:7', rel='blocks')
```

## See also

```python
get(kind='skill', id='precis-relations')     # relation vocabulary, per-kind constraints
get(kind='skill', id='precis-put-help')      # link= on creation
get(kind='skill', id='precis-tags')          # tags vs links — when to reach for which
get(kind='skill', id='precis-overview')      # verbs and address grammar
```
