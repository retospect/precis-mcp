---
id: precis-todo-help
title: precis — create, prioritise, complete todos
summary: basic todo CRUD — create, prioritise, complete; status workflow, project and topic tagging
applies-to: get/search/put/delete/tag/link (kind='todo')
status: active
---

# precis-todo-help — create, prioritise, complete todos

Todos are work items in the store. Address by numeric id — both
`id=122` and `id='todo:122'` are accepted.

## Create a todo
## Add a new task to my queue
## How do I file something I need to do?

```python
put(kind='todo', text='Review section 3 of abazari2024design.')
put(kind='todo', text='Draft the abstract.', tags=['PRIO:high'])
put(kind='todo', text='Wait on reviewer feedback.',
    tags=['PRIO:normal', 'project:precis-v2'])
```

Server assigns the integer id and defaults to `STATUS:open`. Pass
`tags=` on `put` to set priority or project in one round-trip.

## See what's on my plate
## List my todos
## Show me the queue

```python
get(kind='todo')                  # alias for /recent
get(kind='todo', id='/recent')    # most recent 20, any status
get(kind='todo', id='/open')      # open + doing + blocked (the queue)
get(kind='todo', id='/queue')     # alias for /open
get(kind='todo', id='/doing')     # in-progress only
get(kind='todo', id='/blocked')   # waiting on something
get(kind='todo', id='/done')      # completed
```

## Start work on a todo
## Mark a todo as in-progress
## How do I move a todo to doing?

```python
tag(kind='todo', id=122, add=['STATUS:doing'])
```

`STATUS:` is closed-prefix and replaces atomically — setting a new
value removes the previous one. No separate remove needed.

## Mark a todo done
## Complete a todo
## How do I close out a finished task?

```python
tag(kind='todo', id=122, add=['STATUS:done'])
tag(kind='todo', id=122, add=["STATUS:won't-do"])   # decided not to do it
```

## Change priority
## Re-prioritise a todo
## Bump a todo to urgent

```python
tag(kind='todo', id=141, add=['PRIO:urgent'])
tag(kind='todo', id=141, add=['PRIO:low'])
```

Values: `low` / `normal` / `high` / `urgent`. Overwrite is atomic
within the `PRIO:` prefix.

## Block a todo on another ref
## Mark a todo as waiting on something else
## How do I say this is blocked by another todo?

```python
tag(kind='todo', id=141, add=['STATUS:blocked'])
link(kind='todo', id=141, target='todo:158', rel='blocked-by')
link(kind='todo', id=141, target='paper:wang2020state', rel='blocked-by')
```

`STATUS:blocked` marks the state; `link(... rel='blocked-by')`
records what it's waiting on. Targets carry an explicit kind prefix
(`todo:158`, `paper:<slug>`, `markdown:notes/x.md`).

## Move a todo under another (reparent)
## Change a todo's parent in the tree
## How do I nest one task under another?

Todos form a tree. Move an existing todo with the `parent` relation
on the `link` verb:

```python
link(kind='todo', id=141, target='todo:158', rel='parent')   # 141 becomes a child of 158
link(kind='todo', id=141, rel='parent', mode='remove')        # detach 141 to a top-level root
```

A move that would form a cycle, nest deeper than the tree's depth
cap, or touch a strategic / tactical node from a worker source is
rejected. To set the parent when *creating* a todo, pass `parent_id=`
on `put` instead. The current parent shows under `## parent` in
`get(kind='todo', id=141, view='links')`.

## Rewrite a todo's text
## Edit / fix the wording of a todo
## How do I change what a todo says without losing its place?

```python
edit(kind='todo', id=122, mode='replace', text='Review section 3 of abazari2024design (focus on the kinetics).')
```

In-place rewrite: the id, parent, links, and tags all stay attached —
the old body is preserved in `ref_events` (read it back via
`get(kind='todo', id=122, view='log')`). Only `mode='replace'` is
supported. Prefer this over delete + re-`put`, which would break every
inbound edge and the tree position. Owner-only on strategic / tactical
nodes (same authority as delete / reparent).

## Schedule a todo for a date
## Add a due date to a todo
## How do I track when something is due?

There is no built-in due-date field. Use a lowercase tag:

```python
tag(kind='todo', id=122, add=['due:2026-06-15'])
search(kind='todo', tags=['due:2026-06-15', 'STATUS:open'])
```

Date-driven views (`/today`, `/overdue`, `/due`) are not exposed —
filter via `search(tags=['due:<date>'])`.

## Search todos by content
## Find a todo by keyword
## Look up todos matching a phrase

```python
search(kind='todo', q='abstract draft')
search(kind='todo', q='precis-v2 review', tags=['STATUS:open'])
search(kind='todo', q='reviewer', tags=['STATUS:open', 'PRIO:high'])
```

`tags=` filters with AND semantics. Combine with `q=` to rank
inside the filtered set.

## Inspect a todo's full record (debug `meta`)
## Why isn't this todo dispatching / firing / unblocking?
## See the executor / schedule / auto_check / workspace

```python
get(kind='todo', id=40266, view='raw')
```

The default `get(kind='todo', id=N)` shows the curated summary —
`parent`, `prio`, `title`, `tags`. A todo's **behaviour**, though, is
driven by `meta`, which that view hides. `view='raw'` dumps the
verbatim record: every set column **plus the full `meta` JSON** plus
tags and links. Reach for it when a todo isn't behaving and the default
render looks fine — two todos can render identically yet behave
completely differently. The meta keys that matter:

| meta key | drives | symptom when wrong |
|---|---|---|
| `executor` | `dispatch` minting a `kind='job'` | todo never dispatches |
| `schedule` | the `level:recurring` umbrella's cadence (cron / `every:`) | watch never fires |
| `auto_check` | the wait-for condition on a leaf | leaf stuck "waiting" |
| `workspace` | project `path` / `format` / `brief` | wrong output dir / no brief |
| `anchor` | the draft `¶handle` a change-request targets | edit lands on the wrong chunk |

`raw` is **universal** — it works on every numeric-ref kind (`memory`,
`gripe`, `job`, `finding`, …), not just `todo`, so `meta` is always
inspectable.

## Delete a todo
## Remove a todo I no longer want

```python
delete(kind='todo', id=122)
```

Prefer `STATUS:won't-do` over delete when the decision itself is
worth keeping a record of.

## See also

```python
get(kind='skill', id='precis-overview')      # verbs and kinds
get(kind='skill', id='precis-tags')          # STATUS:/PRIO: vocabulary, validation
get(kind='skill', id='precis-relations')     # blocked-by / blocks and other rels
get(kind='skill', id='precis-search-help')   # tags= filter, q= ranking
```
