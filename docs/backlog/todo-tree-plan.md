# Todo-Tree Plan — remaining fold candidates

Shipped portion: see the `precis.handlers.todo` and `precis.workers`
docstrings; full five-slice plan in git history. The tree
(`refs.parent_id`, level gradient, `meta.auto_check`,
`level:recurring` + Watches umbrella, PRIO column, review tiers,
jobs-as-children) is live — migrations `0013_todo_tree.sql`,
`0014_refs_prio.sql`; ADRs 0030, 0061.

Owner anchors: `src/precis/handlers/todo.py`,
`src/precis/workers/auto_check.py`, `src/precis/workers/schedule/`.

## Open scope — kind folds (Slice 6 candidates)

The audit rule: anything with a `STATUS:` lifecycle + a worker
substrate folds into the tree; pure content / tool-output / metadata
kinds do not.

- **`finding` (citation chase) fold** — once the dispatch worker has
  a `chase` executor, `kind='finding'` becomes a soft-cutover the
  same way `kind='job'` did in Slice 5. Do NOT fold naively: a
  finding's deterministic `pub_id` content-dedup, its own STATUS axis
  (`tracing`/`established`/`dead_chain`/`multi_candidate`/`cycle`),
  and the mutable `meta.chain` journal have no todo equivalent
  (ADR 0030 records the rejection of a direct collapse).
- **`gripe` fold re-evaluation** — a gripe could be a
  `level:tactical` todo with `meta.gripe_state`. Deferred because
  the comment-timeline UI is established and migrating live gripes
  outweighs the tidiness win; re-evaluate when the gripe count is
  small.
- **Do NOT fold `job`** (ADR 0030: lease semantics + auto_check
  failure-bubble are load-bearing) or `message` (side-effect output,
  not a workspace item).

## Explicitly NOT in scope

- DAG support — strict tree only; multi-parent waits for a concrete
  consumer.
- Rich due-date semantics — a `due:<iso-date>` tag suffices until a
  real consumer asks for server-side filtering.
