---
id: precis-folder-help
title: precis — the folder kind (placement for authored artifacts)
summary: kind='folder' — single-parent containers for what you MAKE (draft, structure, cad, todo roots); place via link(rel='parent'); search(folder=...) scopes to a subtree; papers/memories stay out
applies-to: kind='folder'; link(rel='parent') on draft/structure/cad/todo; search(folder=...)
status: active
---

# precis-folder-help — the `folder` kind

A **folder** is an organizational container (ADR 0045): a plain
numeric ref whose children are the live refs pointing at it via
`parent_id`. Placement is the *extrinsic* "where do I keep this?"
axis — deliberately dumb and uniform, and **orthogonal to
derivation**: a structure's `derived-from` lineage, a todo's
scheduling tree, and a draft's workspace are intrinsic per-kind
relations that folders never touch.

**Folders organize what you MAKE; search organizes what you
COLLECT.** Papers, cfps, and patents keep their own discovery layer
(search / clusters / TOC / tags). Stream kinds (memory, alert,
agentlog, job, news) arrive at machine rate and have their own
reviewers — they never go in folders. To keep a memory's insight in
a folder, *promote* it: distill into a draft / note and place that.

**Tags find; folders account.** A tag query has uncertain recall; a
folder listing is bounded and complete — the right unit for "have I
seen everything under X?" and for orienting at the start of a task.

## Verbs

```
put(kind='folder', text='Hardware')                    # create
get(kind='folder')                                     # the whole folder tree
get(kind='folder', id=12)                              # path + contents
get(kind='folder', id=12, view='tree')                 # full subtree (all kinds)
delete(kind='folder', id=12)                           # refused while non-empty
```

## Placing artifacts — the reserved `parent` relation

`parent` is a *virtual* relation (ADR 0027, generalized): it writes
`refs.parent_id`, never a `links` row. Placeable kinds are those with
`role='artifact'`: **draft, structure, cad, todo (strategic roots),
folder**.

```
link(kind='draft',     id='<slug>', target='folder:12', rel='parent')
link(kind='structure', id='<slug>', target='folder:12', rel='parent')
link(kind='cad',       id='<slug>', target='folder:12', rel='parent')
link(kind='todo',      id=N,        target='folder:12', rel='parent')  # strategic roots only
link(kind='folder',    id=13,       target='folder:12', rel='parent')  # nest
link(kind='<any>',     id=...,      rel='parent', mode='remove')       # unfile
```

Rules:

- **Single parent.** A ref sits in at most one folder; moving is one
  call, no copy semantics.
- **Todo roots only.** A todo placed in a folder must carry
  `level:strategic` — folder = *where*, project = *what/why/when*.
  The scheduling machinery (rotation, doable, picks, reviews) treats
  a folder-parented strategic exactly like a bare root, and folder
  levels never consume the tree's depth budget.
- **Unfiled is fine.** There is no forced inbox; an unplaced artifact
  simply lists as unfiled. Detaching (`mode='remove'`) returns it
  there.
- **Delete refuses non-empty.** Move the contents out first (the
  error lists them).

## Scoped search

`folder=` on `search` filters any query to the folder's live subtree
(recursive walk over `parent_id`). Accepts the id, `folder:N`, the
`fo<N>` handle, or the folder's unique name.

```
search(q='relaxation cache', folder=12)          # everything about X, in here
search(kind='draft', q='intro', folder='Hardware')
search(tags=['throwaway'], folder=12)            # tags-only sweep, scoped
```

A folder-scoped search always runs through the cross-kind fan-out
(so hits can be membership-filtered); kinds that don't support
cross-kind search are rejected with the eligible list.

## Discipline

Keep folders **cheap and shallow** — 1–2 levels, artifact kinds
only. If a folder wants a third level, it probably wants to be a
project (a strategic todo with `meta.workspace`). No auto-foldering
pass exists on purpose; placement is an authored act.

Cross-refs: `precis-tasks-help` (projects), `precis-structure-help`,
`precis-cad-help`, `precis-draft-help`, `precis-search-help`.
