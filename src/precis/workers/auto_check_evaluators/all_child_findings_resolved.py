"""``type='all_child_findings_resolved'`` — close lit-hunt todos.

The planner-coroutine cascade's literature-hunt pattern is:

1. LLM identifies missing primary sources for the section it's
   writing.
2. Mints a child todo carrying ``meta.auto_check = {'type':
   'all_child_findings_resolved'}`` plus a body like "find these
   N papers".
3. The child todo's own tick mints one ``kind='finding'`` per
   missing paper. Each finding starts at ``STATUS:tracing``; the
   ``finding_chase`` worker (system-profile pass) walks
   Unpaywall → arXiv → S2 → EPO OPS and either resolves it
   (``STATUS:established``, paper minted) or gives up
   (``STATUS:dead_chain``).
4. This evaluator watches the lit-hunt todo's children. When every
   finding has reached a terminal state, the auto_check worker
   flips the todo to ``STATUS:done`` — no LLM re-tick needed for
   the closing edge.

The cascade can then proceed: the parent's next tick reads the
workspace status block, sees the new papers in the corpus, and
``\\cite{}`` them properly.

Spec
====

```json
{ "type": "all_child_findings_resolved" }
```

No arguments. ``ref_id`` is the lit-hunt todo's id; the evaluator
walks its direct ``kind='finding'`` children. Resolved =
``STATUS:established`` OR ``STATUS:dead_chain`` OR
``STATUS:multi_candidate``. Tracing children block the close.

Returns:

* ``True`` when at least one finding exists AND all findings are
  resolved.
* ``False`` when at least one finding is still ``tracing``.
* ``None`` (the "not yet" return) when the todo has no findings
  yet — the LLM hasn't minted any. The auto_check worker will
  re-poll on its next sweep.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store import Store


_TERMINAL_FINDING_STATUSES: frozenset[str] = frozenset(
    {"established", "dead_chain", "multi_candidate"}
)


def evaluate(store: Store, spec: dict[str, Any], *, ref_id: int) -> bool | None:
    """Check if all child findings have reached terminal state."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT COALESCE(
                     (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                       WHERE rt.ref_id = c.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                     'tracing'
                   ) AS status,
                   count(*) AS n
              FROM refs c
             WHERE c.parent_id = %s
               AND c.kind = 'finding'
               AND c.deleted_at IS NULL
             GROUP BY 1
            """,
            (ref_id,),
        ).fetchall()
    if not rows:
        # No findings yet — the LLM may not have minted them, or this
        # todo isn't actually a lit-hunt. Return None so the worker
        # polls again next sweep without flipping the todo.
        return None
    total = 0
    unresolved = 0
    for status, n in rows:
        total += int(n)
        if str(status) not in _TERMINAL_FINDING_STATUSES:
            unresolved += int(n)
    if total == 0:
        return None
    return unresolved == 0
