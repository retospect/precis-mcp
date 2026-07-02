"""``type='derived_job_succeeded'`` — wait for a requested build to land.

The compute-lane twin of ``child_job_succeeded`` (ADR 0044). A derived
job (DFT relax / route / mesh / compile) parents on its *subject
artifact*, not on the requesting todo — so the requester cannot find it
by walking children. It finds it by the ``requested`` link instead
(``requester todo --requested--> job``).

Resolves to ``True`` when any job this todo ``requested`` has reached
``STATUS:succeeded``. Failure is *not* reported here — the
failure-bubble (``handlers/_job_bubble.py``) tags the requester
``child-failed:<job_id>`` on a failed build, which the doable view
excludes, exactly mirroring the intent-lane contract. A cache *hit*
never reaches this evaluator: the structure handler returns the relaxed
geometry synchronously and mints no job, so nothing links ``requested``.

Spec
====

```json
{ "type": "derived_job_succeeded" }
```

No arguments. The leaf's own ``ref_id`` is the requester; the evaluator
follows its outgoing ``requested`` links. The structure dispatch injects
this spec onto the requester todo when it wires the link (when the todo
carries no auto_check of its own), so most callers never write it by
hand.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store import Store


def evaluate(store: Store, spec: dict[str, Any], *, ref_id: int) -> bool | None:
    """True when a job ``requested`` by ``ref_id`` has succeeded.

    Returns ``None`` (leave the leaf open) until then. Unlike
    ``child_job_succeeded`` there is no planner-coroutine guard: a todo
    that requested a derived build is a plain leaf whose work *is* that
    build, and a planner drives its own STATUS rather than requesting
    a build to auto-close on."""
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT 1
              FROM links l
              JOIN refs j ON j.ref_id = l.dst_ref_id
              JOIN ref_tags rt ON rt.ref_id = j.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE l.src_ref_id = %s
               AND l.relation = 'requested'
               AND j.kind = 'job'
               AND j.deleted_at IS NULL
               AND t.namespace = 'STATUS'
               AND t.value = 'succeeded'
             LIMIT 1
            """,
            (ref_id,),
        ).fetchone()
    return row is not None
