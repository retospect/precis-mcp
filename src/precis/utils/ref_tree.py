"""Ancestry predicates over the ``refs.parent_id`` tree.

``refs.retired_at`` is **not transitive**: soft-deleting a project todo
leaves every descendant's own ``retired_at`` NULL. So ``retired_at IS
NULL`` on a single row says nothing about whether the *tree* that row
lives in is still alive — you have to walk up.

Two independent bugs came out of that one gap, which is why the walk
lives here rather than inside either caller:

* :mod:`precis.workers.dispatch` kept **dispatching** an orphaned
  subtree — a deleted project ran planner ticks for four days.
* :mod:`precis.quest.weave_review` kept **minting into** one — 258 fresh
  review-todos landed under a parent deleted two days earlier, because
  the code-minting path bypasses ``TodoHandler.put``'s
  ``check_parent_exists`` (see that module's docstring on the
  trusted-code-path convention).

Stopping a dead tree from dispatching does not stop it from growing;
both directions need the same predicate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from precis.store import Store

#: Max ancestor hops the walk follows — a guard against a cyclic
#: ``parent_id``, not a real depth limit.
MAX_ANCESTOR_DEPTH = 64


def deleted_in_ancestry(
    store: Store, ids: list[int], *, include_self: bool = False
) -> set[int]:
    """Return the subset of ``ids`` living under a soft-deleted ancestor.

    ``include_self=False`` (the default) asks only about *strict*
    ancestors — the caller has already established the rows themselves
    are live, as ``dispatch``'s candidate query has. ``include_self=True``
    also counts a row that is itself soft-deleted, which is what a
    "is this parent safe to mint under" check wants: minting under a
    directly-deleted parent and minting under a deleted grandparent are
    the same mistake.
    """
    if not ids:
        return set()
    min_depth = 0 if include_self else 1
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            WITH RECURSIVE anc (cand_id, parent_id, retired_at, depth) AS (
                SELECT r.ref_id, r.parent_id, r.retired_at, 0
                  FROM refs r WHERE r.ref_id = ANY(%(ids)s)
                UNION ALL
                SELECT a.cand_id, p.parent_id, p.retired_at, a.depth + 1
                  FROM anc a JOIN refs p ON p.ref_id = a.parent_id
                 WHERE a.depth < %(max_depth)s
            )
            SELECT DISTINCT cand_id FROM anc
             WHERE retired_at IS NOT NULL AND depth >= %(min_depth)s
            """,
            {
                "ids": ids,
                "max_depth": MAX_ANCESTOR_DEPTH,
                "min_depth": min_depth,
            },
        ).fetchall()
    return {int(r[0]) for r in rows}


def is_orphaned(store: Store, ref_id: int) -> bool:
    """True if ``ref_id`` is soft-deleted or has a soft-deleted ancestor."""
    return bool(deleted_in_ancestry(store, [ref_id], include_self=True))
