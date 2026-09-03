---
id: precis-todo-tree-help
title: precis — hierarchical todo tree (strategic / tactical / subtask)
summary: the todo tree — strategic/tactical/subtask levels, doable rotation, halt/ask-user yielding
answers:
  - how do I add a strategic root or a project?
  - how do I split a todo into subtasks?
  - what's the dashboard view for my project's tree?
  - I'm stuck on a leaf — how do I decompose it?
  - how do I attach a longer spec or acceptance criteria to a todo?
applies-to: get/search/put/delete/tag/link (kind='todo'; tree views)
status: active
---

# precis-todo-tree-help — the hierarchical todo tree

Built on top of `kind='todo'`. Every todo is a node; an optional
`parent_id` wires it under another todo to form a tree. The tier is two
boolean `meta` fields, not a tag — `rotation_root` marks a strategic
root, `worker_mintable=false` marks tactical, and the default (both
unset) is an ordinary worker-mintable subtask:

```
strategic root  (owner-only, meta.rotation_root=true)
  └─ tactical    (owner-only, meta.worker_mintable=false)
      └─ subtask (worker-owned by default — no facet fields set)
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
put(
    kind="todo",
    text="Build the nanocube AI compute platform.",
    meta={"rotation_root": True},
)
```

Strategic and tactical tiers are gated: workers (`PRECIS_SOURCE`
starting with `asa-`) cannot create or mutate them. CLI sessions,
interactive Python, and the precis-web UI (`web:owner`) pass through.
Promoting/demoting an *existing* todo's facets after creation goes
through `tag(meta={...})` (`tag()` already carries non-tag state via
`prio=`, so this follows the same precedent), same owner-only gate.

Workers may **propose** a tactical via the open `proposed-tactical`
tag for owner triage:

```python
put(
    kind="todo",
    text="Write the boxel paper",
    parent_id=42,
    tags=["proposed-tactical"],
)
```

## Add a child under an existing todo

```python
put(kind="todo", text="Draft setup paragraph (4-6 sentences).", parent_id=98)
```

`parent_id` validates: the parent must exist, live (not soft-
deleted), and be a `todo`. Cycles are rejected at write time
(self-reference via repeated re-parent paths). The chain may not
exceed 10 deep — if you hit the wall, attach a `waiting-for:*` tag
or a `blocked-by` link instead of splitting further.

## Attach a details body to a todo

The `text=` title is the header — keep it a short imperative. For
longer context (a spec, acceptance criteria, a plan), pass an optional
`body=`; it lands in a `todo_body` chunk (embedded + keyworded, so it's
searchable) and renders under the title on `get(kind='todo', id=N)`.
The tree / doable / attention views still show only the title.

```python
put(
    kind="todo",
    text="Wire the export endpoint.",
    body="Accept ?format=csv|json. 404 on unknown format. "
    "Stream for >10k rows. See the design note in me812.",
)
# rewrite it later:
edit(kind="todo", id=42, mode="replace", body="updated acceptance criteria")
# or rewrite both at once:
edit(kind="todo", id=42, mode="replace", text="new title", body="new body")
```

## Dashboard: strategic roots and 7d accounting

```python
search(kind="todo", view="roots")  # one row per strategic
search(kind="todo", view="strategic")  # strategic + tactical layer
search(kind="todo", view="projects")  # strategics that own a workspace
```

`view='roots'` shows past-tense accounting only: how many leaves
got marked done in the last 7 days per strategic, and which
strategic is up next (lowest picks among active strategics). No
forecasts, no ETAs.

## Projects (a strategic root that owns a workspace)

A *project* is not a separate kind — it's a strategic root carrying
`meta.workspace`. The workspace gives the subtree a home directory
(under `PRECIS_ROOT`), a file format, and a standing **brief** ("project
thoughts": voice, scope, constraints). All three cascade to every
descendant via the put-time inheritance, so you set them once at the
root. A root with `meta.workspace` set is auto-stamped
`rotation_root=true` if you don't set the facet fields yourself.

```python
put(
    kind="todo",
    text="Write the nanotrans review.",
    meta={
        "llm_tier": "opus",
        "workspace": {
            "path": "projects/nanotrans_auto",  # relative to PRECIS_ROOT
            "format": "tex",  # 'tex' | 'md'
            "entrypoint": "main.tex",
            "brief": "Terse, IEEE voice. Cite primary sources only. "
            "Do NOT speculate beyond the corpus.",
        },
    },
)
```

What you get for free:

* Every ref minted under the project (todos, citations, findings,
  files) is auto-tagged `project:<slug>` — so
  `search(tags=['project:nanotrans_auto'])` returns the whole project
  surface across kinds. The slug is the basename of the workspace path.
  Stamped on the owner path too, not just inside a planner tick.
* The `brief` rides into every descendant's planner context as a
  `## Project context` block, so a deep leaf works to the project frame
  without you repeating it in each child body.
* `view='projects'` lists every project with its open-todo count, file
  count, and the first line of its brief.

Edit the brief later with `edit`/`put` on the root's `meta.workspace`;
new descendants inherit the updated block.

## Drill into a subtree

```python
get(kind="todo", id=42, view="tree")  # ASCII subtree under td42
```

Tree icons: `○` doable · `▶` doing · `◀ claimed-by:<x>` claimed ·
`⏸` waiting / paused / ask-user · `✓` done · `✗` won't-do ·
`⚙` job (execution attempt under a todo parent).

## Doable leaves — what to pull next

```python
search(kind="todo", view="doable")
search(kind="todo", view="doable", args={"under": 67})  # within a subtree
```

"Doable" = leaf with no live children, status open / doing, no
open blocked-by links, no `waiting-for:*` tag, no `ask-user`
tag, no `child-failed:*` tag
(a child job failed and is awaiting
the owner's decision), no `paused` ancestor, and not a recurring
(`meta.schedule` set) umbrella row. Ordering: `prio` int column ASC,
then least-picked strategic, then ref_id (sibling order). PRIO 1
preempts the 1/N rotation; cron-spawned subtasks default to PRIO 2.

## Pausing a subtree

```python
tag(kind="todo", id=98, add=["STATUS:paused"])
tag(kind="todo", id=98, add=["STATUS:open"])  # unpause
```

Pause propagates at query time — every doable / strategic / picks
query skips refs whose ancestor chain contains a `paused` branch.
Nothing in the subtree gets touched; counts and decay continue.

## Waiting, blocked, and asks

```python
search(kind="todo", view="waiting")  # any waiting-for:* tagged leaf
search(kind="todo", view="blocked")  # any open blocked-by link
search(kind="todo", view="ask-user")  # parked-on-owner-reply leaves
```

- `waiting-for:reviewer-x` — generic external wait. Open tag; any
  value lower-cased is fine.
- `blocked-by` link — the wait target is another ref in the tree
  (`link(rel='blocked-by', target='todo:104')`).
- `ask-user` — chatter renders these in her
  preamble so the owner sees pending asks at a glance.

## Walk-on-read ancestry

Every `get(kind='todo', id=N)` reply includes the full chain from
the strategic root down to the leaf. Cheap (depth ≤ 10) and saves
the agent a follow-up call to figure out why this leaf exists.

## Facet fields + tag vocabulary specific to the tree

The level tier is `meta`, not a tag:

| `meta` field | Purpose | Who writes |
|---|---|---|
| `rotation_root=true` | Strategic root — the picks-7d rotation unit | owner only |
| `worker_mintable=false` | Tactical (sub-strategic, non-root) tier | owner only |
| *(both unset)* | Subtask — the worker-mintable default | anyone |
| `schedule={...}` | Scheduled root — presence IS "recurring" | owner only |

| Tag | Purpose | Who writes |
|---|---|---|
| `proposed-tactical` | Worker's tactical pitch | anyone |
| `claimed-by:<handle>` | Atomic claim marker | the claimer |
| `waiting-for:<target>` | External wait | anyone |
| `ask-user` / `ask-user:<question>` | Parked on a human's reply; bare = "any human", `ask-user:<text>` carries the question inline. Add `user:<who>` to address a specific person | anyone |
| `child-failed:<job_id>` | A child `kind='job'` failed; the parent's owner must decide next move (retry / switch / give up). Doable view skips parents with this tag | written by the executor / `JobHandler.tag` on STATUS:failed |
| `halt` | Explicit "robot stay away" marker. Pulls the leaf out of `view='doable'` AND out of the dispatch worker's candidate query. Workers MAY add it (escalation: "I think this needs human eyes / I don't know how to proceed") but only the owner may remove it (the resume edge). Surfaces under `view='attention'` so halted leaves don't vanish. | anyone may add; owner only removes |

Priority is set with the `prio=N` kwarg (1..10, lower = hotter).
`PRIO:urgent|high|normal|low` remains a **searchable tag** but does not
set priority — use `prio=N` for that.

The flat list surface (`/recent`, `/open`, `/done`, …) keeps
working — see `precis-todo-help`. This skill adds the tree
discipline on top.

## See also

```python
get(kind="skill", id="precis-todo-help")  # flat todo surface
get(
    kind="skill", id="precis-decomposition-help"
)  # GTD interrogation, split rule
get(kind="skill", id="precis-auto-todo-help")  # meta.auto_check leaves
get(
    kind="skill", id="precis-recurring-help"
)  # meta.schedule + Watches umbrella
get(
    kind="skill", id="precis-minter-help"
)  # meta.executor + minter worker
get(kind="skill", id="precis-job-help")  # the kind='job' substrate
get(kind="skill", id="precis-nursery-help")  # hourly review digest tier
get(kind="skill", id="precis-tags")  # STATUS / PRIO vocabulary
get(kind="skill", id="precis-relations")  # blocked-by / blocks / note-for
search(kind="skill", q="your goal")  # if none of the above fit
```
