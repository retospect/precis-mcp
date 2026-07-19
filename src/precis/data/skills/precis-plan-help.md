---
id: precis-plan-help
title: precis — the plan kind (a thread's reasoning outline)
summary: a hierarchical todo-list + notes on the draft chunk-tree; create vs. add-node, whole-tree render, status/belief markers, the ▸ cursor, pe<id> node addressing
applies-to: put / get / edit / delete / link (kind='plan')
status: active
---

# precis-plan-help — the reasoning-outline kind (ADR 0051 §2b)

A `plan` is the **forward** facet of a thread's logbook: a hierarchical
**todo-list + reasoning notes** you keep *while* working a project. It
rides the **same** chunk-tree substrate as `draft` (nodes reorder /
reparent / edit in place) but is a **distinct kind that is NEVER
exported** (`corpus_role='none'`) — a plan is scaffolding, not a
deliverable.

- **One plan per project**, joined by a `plan-of` link to the owning
  `kind='todo'` project. `project=` on create is that todo's id.
- **Nodes are addressed `pe<chunk_id>`** (the ADR 0036 universal handle),
  with relative nav `pe<id>^` (parent) / `+N` (step) / `-lo..hi` (span).
- **Rendered whole every turn** with per-node markers: `[open]` / `[wip]`
  / `done:` status, a `?` / `⚠` belief prefix, and a model-owned `▸`
  you-are-here cursor.

It is deliberately **lean** vs. `draft`: no figures / tables / authors /
styles / export. A node carries only its text plus two `meta` markers.

## put — create a plan, or add a node (read this first)

The single trap in this kind is **create vs. add-node**. `put` routes by
your arguments, not by whether the slug exists yet:

| You pass… | `put` does |
|---|---|
| `project=<todo-id>` (with `title=`, optional `text=`) | **creates** the plan |
| `mode='create'` (explicit signal) | **creates** the plan |
| `chunk_kind=` / `at=` / `text=` **without** `project=`/`mode='create'` | **adds a node** to an existing plan |

`project=` is the create-only param and `mode='create'` is the explicit
signal — **either one means "create"**, even alongside `text=` (the
natural "make me a plan and start it with this thought" call). A node
placement *without* those adds to an existing plan.

**Create:**

```python
# Minimal create — title + owning project todo.
put(kind='plan', id='nanotrans-plan', title='Nanotrans build', project=412)
#   → created plan 'nanotrans-plan' (root pe88); linked plan-of project 412

# Create AND seed the first node in one call (text= becomes node 1).
put(kind='plan', id='nanotrans-plan', title='Nanotrans build', project=412,
    text='survey the prior art', status='open')
#   → created plan 'nanotrans-plan' (root pe88); added node pe89
```

**Add a node** to a plan that already exists (needs `text=`; `at=` places
it, default `last`):

```python
put(kind='plan', id='nanotrans-plan', text='draft the intro',
    at={'last': True}, status='open')
#   → added 1 node to nanotrans-plan: pe90
```

`at=` anchors (mirror `draft`): `{'first': True}` · `{'last': True}` ·
`{'into': 'pe89'}` (as a child) · `{'before': 'pe90'}` · `{'after':
'pe89'}`.

**The chicken-and-egg, gone.** Adding a node to a plan that *doesn't
exist yet* no longer misfires — you get an actionable error, not a
misleading "slug not found":

```python
put(kind='plan', id='ghost', text='a node', at={'last': True})
#   → BadInput: plan 'ghost' doesn't exist yet — create it before adding
#     nodes (a create needs project=, the owning project todo id)
```

So: **create with `project=` first, then add nodes.**

## get — list, render the tree, read one node

```python
get(kind='plan')                       # list every plan
get(kind='plan', id='nanotrans-plan')  # render the WHOLE marked outline
get(kind='plan', id='pe89')            # one node verbatim + a small window
get(kind='plan', id='pe89-2..3')       # relative window around pe89
```

The whole-tree render is one line per node —
`{indent}{marker} pe<id> {gloss}` — where the marker is the `▸` cursor if
this is the you-are-here node, else the status marker (`[open]` default /
`[wip]` / `done:`) with a `?` / `⚠` belief prefix when set. The root
title heading is the plan's *name*, not a todo, so it renders bare (`#`).
(`view='toc'` is accepted and equals the default whole-tree render; any
other `view=` errors.)

## edit — text, move, markers, cursor

```python
edit(kind='plan', id='pe90', text='draft the intro (2 paras)')  # rewrite a node
edit(kind='plan', id='pe90', move={'after': 'pe91'})            # reorder / reparent
edit(kind='plan', id='pe90', status='wip')                      # set the todo state
edit(kind='plan', id='pe90', belief='⚠')                        # flag caution (or '?')
edit(kind='plan', id='pe90', status='')                         # '' clears the marker
edit(kind='plan', id='nanotrans-plan', cursor='pe90')           # set the ▸ you-are-here
edit(kind='plan', id='nanotrans-plan', cursor='')               # clear it
```

- `status` ∈ `open | wip | done`; `belief` ∈ `? | ⚠`. On `edit` an empty
  string **clears** the marker.
- `cursor=` is a **plan-level** op — a model-owned pointer stored on the
  plan *ref* (not on a chunk), so `id=` may be the plan slug or any node
  in it. The target must resolve to a live node in *this* plan; `''`
  clears it (a cleared cursor falls back to the first `open` node at
  render time). The cursor is yours to move as the thread advances — it's
  how the render shows "here".

## delete — soft-retire a node

```python
delete(kind='plan', id='pe90')                   # defaults per the store
delete(kind='plan', id='pe90', mode='cascade')   # retire the whole subtree
delete(kind='plan', id='pe90', mode='promote')   # retire the node, splice children up
```

Deletes target a **node** (`pe<id>`), never the plan slug, and are
soft-retires (recoverable). `mode='cascade'` retires the node and its
descendants; `mode='promote'` retires just the node and lifts its
children into its slot.

## link — folder placement only

```python
link(kind='plan', id='nanotrans-plan', target='folder:7', rel='parent')
```

The only accepted relation is `rel='parent'` (a folder placement, ADR
0045). The project join (`plan-of`) is made for you at create time from
`project=`.

## Toolpath — start a plan, work it, walk away

```python
# 1) Create the plan under its project todo, seeding the first thought.
p = put(kind='plan', id='nanotrans-plan', title='Nanotrans build',
        project=412, text='survey the prior art', status='open')
# 2) Add the next steps as nodes.
put(kind='plan', id='nanotrans-plan', text='draft the intro',   at={'last': True})
put(kind='plan', id='nanotrans-plan', text='pick the baseline', at={'last': True})
# 3) As you work: flip status, park the cursor where you are.
edit(kind='plan', id='pe89', status='done')      # prior-art survey done
edit(kind='plan', id='nanotrans-plan', cursor='pe90')  # now on 'draft the intro'
# 4) Read it whole any time — markers + ▸ show the state at a glance.
get(kind='plan', id='nanotrans-plan')
```

See `precis-overview` for the master kinds table, `precis-tasks-help` for
the todo tree the plan hangs off, and ADR 0051 §2b for the design.
