"""``type='child_job_succeeded'`` — wait for any child job to succeed.

Slice-5 of ``docs/design/todo-tree-plan.md``: a todo with
``meta.executor`` is the *intent*; the dispatch worker mints a
``kind='job'`` ref under it; when that job finishes successfully
the todo's auto_check resolves and the leaf flips to
``STATUS:done``.

Resolves to ``True`` when any non-deleted child of the leaf has
``kind='job'`` AND ``STATUS:succeeded``. Failed siblings are
ignored — the failure-bubble tag (``child-failed:N``) is what
surfaces a stuck parent, not this evaluator.

Spec
====

```json
{ "type": "child_job_succeeded" }
```

No arguments. The leaf's own ``ref_id`` is the parent; the
evaluator looks up its children directly. The dispatch worker
auto-injects this spec when it mints a job under a todo with
``meta.executor`` set, so most operators never write it by hand.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store import Store


def evaluate(store: Store, spec: dict[str, Any], *, ref_id: int) -> bool | None:
    """Check for ≥1 succeeded child job under ``ref_id``.

    The auto_check worker passes ``ref_id`` as a kwarg so evaluators
    that need to know "who is asking" can look up tree-relative state
    without a parameter on the spec. The other evaluators don't use
    ``ref_id`` (they answer global questions like "is paper:X
    ingested?"); this one needs to scope to the calling leaf.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.parent_id = %s
               AND r.kind = 'job'
               AND r.deleted_at IS NULL
               AND t.namespace = 'STATUS'
               AND t.value = 'succeeded'
             LIMIT 1
            """,
            (ref_id,),
        ).fetchone()
    return row is not None
