# Reparenting todos via a reserved `link(rel='parent')` relation

- **Status**: implemented
- **Builds on**: `docs/design/todo-tree-plan.md` (the tree + guards),
  ADR 0026 (precis-web surface; deferred "Move…")

## Problem

The todo tree stores hierarchy in the `refs.parent_id` column. At
create time `put(kind='todo', parent_id=N)` sets it, and the guards
(cycle, depth, level-gradient/owner) run. But there was **no verb to
move an existing todo** — the one tree mutation without an MCP
surface (ADR 0026 §Consequences explicitly deferred it). The web
Tasks tab therefore shipped without drag/move.

We need a move surface that:

1. reuses the existing guards (no second copy of the cycle/depth/owner
   logic);
2. doesn't add a new MCP verb or a new parameter to an existing verb;
3. round-trips on read so a client sees what it set.

## Decision

Expose reparenting as a **reserved virtual relation** `parent` on the
existing `link` verb:

```
link(kind='todo', id=child, target='todo:newparent', rel='parent')   # move
link(kind='todo', id=child, rel='parent', mode='remove')             # detach to root
```

`TodoHandler.link()` intercepts `rel='parent'` before the relation
vocabulary is validated and routes to `_reparent()`, which runs
`check_owner_only_ref`, `check_parent_exists`, `check_no_cycle`, and a
new subtree-aware `check_reparent_depth`, then calls
`Store.set_parent()`. Every other relation falls through to the
stored-link path unchanged.

`parent` is a **façade over the column**, not a `links` row:

- It is **not** added to the `Relation` Literal or the `relations`
  seed table — a vocabulary entry that never owns a row would be a
  lie, and it would pollute `link` for kinds where "parent" is
  meaningless.
- It is intercepted **per-kind** (todo only).
- On read, `get(kind='todo', id=N, view='links')` synthesizes a
  `## parent` section from `parent_id` so the edge round-trips.

`parent_id` stays the single source of truth: tree views, the
`ON DELETE SET NULL` cascade, and all guards key off the column.

### Subtree-aware depth guard

`check_depth_under` measures the *parent* depth — correct when
creating a leaf. A re-parent moves a whole subtree, so the deepest
resulting node is `depth(new_parent) + 1 + height(subtree under
child)`. `check_reparent_depth` computes the subtree height with a
recursive CTE and rejects on the same `>= MAX_DEPTH` boundary as
create, so a leaf move and a leaf create behave identically.

### Detach semantics

`mode='remove'` sets `parent_id = NULL` (the todo becomes a root). An
optional `target=` on remove must name the *current* parent, so a
stale "remove parent X" can't silently detach from a different parent.

## Web surface

`POST /tasks/{id}/move` (form `new_parent_id`, empty = detach)
dispatches the `link(rel='parent')` call through the in-process
runtime — no `parent_id` write in the web layer, guards stay
single-sourced (the no-surface-drift principle from ADR 0026). The
dashboard adds native HTML5 drag-and-drop: drag a task onto another to
reparent, or onto a top bar to promote to a root; a numeric "move
under #__" input is the keyboard fallback.

## Processing indicators (related, same change)

The Tasks tree now surfaces **both** processing signals per node:

- **Live lock** — `Store.locked_ref_ids()` probes `pg_locks` via a
  `FOR UPDATE SKIP LOCKED` diff (rolled back immediately) and flags
  rows a worker currently holds.
- **Lease** — `STATUS:running` + `meta.lease_until` (the durable
  marker a worker writes), rendered as a "running" badge with the
  lease timestamp.

Child `kind='job'` rows are surfaced under their parent todo so the
badges have a node to attach to (jobs are where processing happens).

## Alternatives considered

1. **A new `reparent` verb / a `parent_id=` kwarg on `edit`.**
   Rejected: adds surface area an agent must learn; the constraint was
   "no new verb." `link` already carries "connect A to B."
2. **Migrate `parent` into the `links` table (real edge).** Rejected:
   tree views, cascade, and guards all depend on the column; a links
   row would duplicate state and need a sync trigger.
3. **SortableJS for the DnD.** Rejected: it reorders siblings, not
   nesting; native HTML5 drag-onto-node matches the `parent_id` model
   and adds no dependency.

## Tests

`tests/test_todo_tree.py` — move, detach, cycle reject, subtree-depth
reject, owner-only reject, wrong-current-parent reject, links-view
round-trip, and a guard that a normal relation still stores a link
(doesn't leak into the column). `tests/precis_web/test_routes.py` —
the move route dispatches the correct `link` args for both move and
detach.
