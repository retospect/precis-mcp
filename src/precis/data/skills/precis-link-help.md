---
id: precis-link-help
title: precis — the link verb (typed edges between refs)
summary: the link verb — typed directional edges between refs, target addressing, idempotency
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

A target is a universal handle, or the legacy `kind:identifier[~selector]`.

```text
pa5                                # a paper by handle (= paper:wang2020state)
pc38                               # block 38 of that paper by handle
td158                              # a todo by handle
paper:wang2020state                # legacy ref-level form, still resolves
paper:wang2020state~38             # legacy block form
patent:ep4123456a1
todo:158                           # numeric-id ref
markdown:notes/foo.md
markdown:notes/foo.md~intro        # block in a file
```

A handle (`pa5`, `pc38`) self-identifies its kind. On the legacy form the
`kind:` prefix is required — slug shapes overlap across kinds and the parser
won't guess.

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

## Reparent a todo (`rel='parent'`)

Todos form a tree. `rel='parent'` places one todo under another;
`mode='remove'` lifts it back out to a top-level root. The `parent`
relation applies to `kind='todo'`.

```python
link(kind='todo', id=141, target='todo:158', rel='parent')   # move 141 under 158
link(kind='todo', id=141, rel='parent', mode='remove')        # detach 141 to a root
```

A move that would form a cycle or nest deeper than the tree's depth
cap is rejected. The current parent shows under `## parent` in
`get(kind='todo', id=141, view='links')`. See `precis-todo-help` for
the full tree workflow.

## See also

```python
get(kind='skill', id='precis-relations')     # relation vocabulary, per-kind constraints
get(kind='skill', id='precis-put-help')      # link= on creation
get(kind='skill', id='precis-tags')          # tags vs links — when to reach for which
get(kind='skill', id='precis-overview')      # verbs and address grammar
```
