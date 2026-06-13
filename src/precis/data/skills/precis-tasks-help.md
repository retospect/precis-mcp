---
id: precis-tasks-help
title: precis — hierarchical task tree (strategic / tactical / subtask)
applies-to: get/search/put/delete/tag/link (kind='todo'; tree views)
status: active
---

# precis-tasks-help — the hierarchical task tree

Built on top of `kind='todo'`. Every todo is a node; an optional
`parent_id` wires it under another todo to form a tree:

```
strategic root  (Reto-owned, level:strategic)
  └─ tactical    (Reto-owned, level:tactical)
      └─ subtask (worker-owned by default)
         └─ subtask
            └─ ...   (depth-10 wall)
```

Branches (todos with children) are **outcomes** — the first line
reads "what does done look like." Leaves (no children) are **next
physical actions** — first line is an imperative ("Draft setup
paragraph using found papers"). Promotion (a leaf grows children)
and demotion (children deleted) are the same operation in either
direction; no kind change, no migration.

If you are working a leaf, see `precis-decomposition-help` for the
GTD interrogation and the split-vs-block-vs-wait rule. The skill
also covers the depth-10 wall and how to recover from it.

## Add a strategic root (owner only)

```python
put(kind='todo', text='Build the nanocube AI compute platform.',
    tags=['level:strategic'])
```

Strategic and tactical tiers are gated: workers (`PRECIS_SOURCE`
starting with `asa-`) cannot create or mutate them. CLI sessions,
interactive Python, and the precis-web UI (`web:reto`) pass through.

Workers may **propose** a tactical via the open
`level:proposed-tactical` tag for owner triage:

```python
put(kind='todo', text='Write the boxel paper',
    parent_id=42, tags=['level:proposed-tactical'])
```

## Add a child under an existing todo

```python
put(kind='todo', text='Draft setup paragraph (4-6 sentences).',
    parent_id=98)
```

`parent_id` validates: the parent must exist, live (not soft-
deleted), and be a `todo`. Cycles are rejected at write time
(self-reference via repeated re-parent paths). The chain may not
exceed 10 deep — if you hit the wall, attach a `waiting-for:*` tag
or a `blocked-by` link instead of splitting further.

## Dashboard: strategic roots and 7d accounting

```python
search(kind='todo', view='roots')         # one row per strategic
search(kind='todo', view='strategic')     # strategic + tactical layer
```

`view='roots'` shows past-tense accounting only: how many leaves
got marked done in the last 7 days per strategic, and which
strategic is up next (lowest picks among active strategics). No
forecasts, no ETAs.

## Drill into a subtree

```python
get(kind='todo', id=42, view='tree')      # ASCII subtree under #42
```

Tree icons: `○` doable · `▶` doing · `◀ claimed-by:<x>` claimed ·
`⏸` waiting / paused / asking-reto · `✓` done · `✗` won't-do.

## Doable leaves — what to pull next

```python
search(kind='todo', view='doable')
search(kind='todo', view='doable', args={'under': 67})  # within a subtree
```

"Doable" = leaf with no live children, status open / doing, no
open blocked-by links, no `waiting-for:*` tag, no `asking-reto`
tag, and no `paused` ancestor. Ordering: least-picked strategic
first, then `PRIO:` value, then ref_id (sibling order).

## Pausing a subtree

```python
tag(kind='todo', id=98, add=['STATUS:paused'])
tag(kind='todo', id=98, add=["STATUS:open"])   # unpause
```

Pause propagates at query time — every doable / strategic / picks
query skips refs whose ancestor chain contains a `paused` branch.
Nothing in the subtree gets touched; counts and decay continue.

## Waiting, blocked, and asks

```python
search(kind='todo', view='waiting')     # any waiting-for:* tagged leaf
search(kind='todo', view='blocked')     # any open blocked-by link
search(kind='todo', view='asking-reto') # parked-on-Reto-reply leaves
```

- `waiting-for:reviewer-x` — generic external wait. Open tag; any
  value lower-cased is fine.
- `blocked-by` link — the wait target is another ref in the tree
  (`link(rel='blocked-by', target='todo:104')`).
- `asking-reto` — chatter renders these in her preamble so Reto
  sees pending asks at a glance (Slice 2).

## Walk-on-read ancestry

Every `get(kind='todo', id=N)` reply includes the full chain from
the strategic root down to the leaf. Cheap (depth ≤ 10) and saves
the agent a follow-up call to figure out why this leaf exists.

## Tag vocabulary specific to the tree

| Tag | Purpose | Who writes |
|---|---|---|
| `level:strategic` | Top-tier outcome | owner only |
| `level:tactical` | Sub-strategic outcome | owner only |
| `level:subtask` (default if omitted) | Worker-level | anyone |
| `level:proposed-tactical` | Worker's tactical pitch | anyone |
| `claimed-by:<handle>` | Atomic claim marker | the claimer |
| `waiting-for:<target>` | External wait | anyone |
| `asking-reto` / `asking-reto:<msg_id>` | Parked on Reto's Discord reply | anyone |

The flat list surface (`/recent`, `/open`, `/done`, …) keeps
working — see `precis-todo-help`. This skill adds the tree
discipline on top.

## Identity routing — who counts as worker

The level gradient is gated by `PRECIS_SOURCE` (an env var set
once per process):

- unset / `cli` / `user` → **owner** (interactive Reto)
- starts with `web:` → **owner** (precis-web UI passes `web:reto`)
- starts with `asa-` → **worker** (`asa-chatter`, `asa-worker`,
  `asa-dreamer`)
- anything else → **owner** (forward-compatible default)

Worker authority is the constraint; the rest is unconstrained.

## See also

```python
get(kind='skill', id='precis-todo-help')         # flat todo surface
get(kind='skill', id='precis-decomposition-help')# GTD interrogation, split rule (Slice 2)
get(kind='skill', id='precis-tags')              # STATUS / PRIO vocabulary
get(kind='skill', id='precis-relations')         # blocked-by / blocks / note-for
```
