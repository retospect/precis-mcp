# ADR 0027 — reparent todos via a reserved `parent` link relation

- **Status**: accepted (2026-06-14)
- **Deciders**: Reto + agent
- **Builds on**:
  - ADR 0026 — precis-web surface (deferred "Move…")
  - `docs/design/todo-tree-plan.md` — the tree + write-time guards
  - `docs/design/todo-reparent-via-link.md` — this change's plan

## Context

The todo tree stores hierarchy in `refs.parent_id`. `put(parent_id=N)`
sets it at create time and runs the cycle / depth / level-gradient
guards, but there was no way to **move an existing todo** — the one
tree mutation without an MCP surface. ADR 0026 §Consequences shipped
the web Tasks tab without drag/move for exactly this reason.

Constraints: reuse the existing guards (no duplicated cycle/depth/owner
logic), add no new verb and no new parameter on an existing verb, and
round-trip on read.

## Decision

Reparenting is exposed as a **reserved relation** `parent` on the
existing `link` verb:

```
link(kind='todo', id=child, target='todo:newparent', rel='parent')
link(kind='todo', id=child, rel='parent', mode='remove')   # detach to root
```

`TodoHandler.link()` intercepts `rel='parent'` before relation-
vocabulary validation and routes to `_reparent()`, which runs the
guards and calls `Store.set_parent()`. Every other relation falls
through to the stored-link path unchanged.

Implementation note (maintainer-facing only — **agents see `parent`
as an ordinary todo relation**): `parent` is a façade over the
`parent_id` column, not a `links` row.

- It is **not** added to the `Relation` Literal or the `relations`
  seed table. A vocabulary entry that never owns a row would be a lie,
  and it would pollute `link` for kinds where "parent" is meaningless.
- It is intercepted **per-kind** (todo only).
- On read, the todo links view synthesizes the `## parent` section
  from the column so the edge round-trips.

`parent_id` remains the single source of truth: tree views, the
`ON DELETE SET NULL` cascade, and every guard key off the column.

### Subtree-aware depth guard

`check_depth_under` measures the parent's depth (correct for a leaf
create). A re-parent moves a whole subtree, so the new
`check_reparent_depth` rejects when
`depth(new_parent) + 1 + height(child_subtree) >= MAX_DEPTH`, the same
boundary as create.

### Detach

`mode='remove'` sets `parent_id = NULL`. An optional `target=` on
remove must name the current parent, preventing a stale request from
detaching from a different parent.

## Consequences

- Supersedes the "re-parenting deferred" consequence of ADR 0026; the
  web Tasks tab gains drag-to-reparent (`POST /tasks/{id}/move`
  dispatching `link(rel='parent')`).
- The web layer never writes `parent_id` directly — guards stay
  single-sourced (no-surface-drift, per ADR 0026).
- `Store.locked_ref_ids()` (a `FOR UPDATE SKIP LOCKED` probe) is added
  so the Tasks tab can flag live-locked nodes alongside the lease.

## Alternatives considered

1. **A new `reparent` verb or `parent_id=` on `edit`.** Rejected:
   more surface for an agent to learn; `link` already means "connect A
   to B."
2. **Promote `parent` into the `links` table (a real edge).**
   Rejected: tree views, cascade, and guards depend on the column; a
   links row would duplicate state and need a sync trigger.
3. **Add `parent` to the closed `Relation` vocabulary.** Rejected: it
   would advertise a relation that's meaningless for non-todo kinds
   and never stores a row.
