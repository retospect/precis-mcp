"""``type='child_job_succeeded'`` — wait for any child job to succeed.

Slice-5 of ``docs/design/todo-tree-plan.md``: a todo with
``meta.executor`` is the *intent*; the dispatch worker mints a
``kind='job'`` ref under it; when that job finishes successfully
the todo's auto_check resolves and the leaf flips to
``STATUS:done``.

Resolves to ``True`` when any non-deleted child of the leaf has
``kind='job'`` AND ``STATUS:succeeded`` — *unless* one of two
guards fires first (see below). Failed siblings are ignored — the
failure-bubble tag (``child-failed:N``) is what surfaces a stuck
parent, not this evaluator.

Guards (why a succeeded child job is NOT always "done")
=======================================================

This evaluator is the right completion signal for a *deterministic*
parent whose entire work is one offline job (``fix_gripe`` and the
like): the job succeeds, the parent is done. It is the WRONG signal
for two cases, which this module refuses to resolve:

1. **Planner coroutines.** An ``LLM:*``-tagged parent runs the
   ``plan_tick`` coroutine — each tick is one ``kind='job'`` that
   ``STATUS:succeeded`` on any clean run, including ticks that merely
   *minted children* (``verdict: continue``) or *yielded*
   (``ask-user:``). Treating "a child job succeeded" as "the goal is
   done" closed an in-progress paper cascade on its FIRST successful
   planning tick. The dispatcher already declines to *inject* this
   auto_check on ``plan_tick`` parents (``_SELF_RESOLVING_JOB_TYPES``),
   but a stale / hand-authored / legacy spec can still be attached —
   so we also refuse to *honour* it here. A planner drives its own
   ``STATUS`` (guarded ``STATUS:done`` / ``ask-user:`` / ``halt:``).

2. **Live child todos.** Even for a deterministic parent, a succeeded
   child job does not mean the work is finished while sibling child
   todos are still open. This mirrors the manual ``STATUS:done``
   guardrail (``handlers/_todo_guards.check_status_done_artifact``),
   which the auto-resolver bypasses by writing the tag directly.

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
    from psycopg import Connection

    from precis.store import Store


def _parent_is_planner_coroutine(conn: Connection, ref_id: int) -> bool:
    """True when the parent carries an ``LLM:<model>`` tag.

    Such a todo is dispatched as a ``plan_tick`` coroutine that drives
    its own terminal state; ``child_job_succeeded`` must never close it
    (see guard 1 in the module docstring).
    """
    row = conn.execute(
        """
        SELECT 1 FROM ref_tags rt
          JOIN tags t ON t.tag_id = rt.tag_id
         WHERE rt.ref_id = %s
           AND t.namespace = 'LLM'
         LIMIT 1
        """,
        (ref_id,),
    ).fetchone()
    return row is not None


def _has_live_child_todo(conn: Connection, ref_id: int) -> bool:
    """True when ``ref_id`` has a child todo that is not yet finished.

    "Finished" matches the dispatcher's notion (``done`` / ``won't-do``);
    anything else (open / doing / blocked / paused) counts as live work
    remaining (see guard 2 in the module docstring).
    """
    row = conn.execute(
        """
        SELECT 1 FROM refs c
         WHERE c.parent_id = %s
           AND c.kind = 'todo'
           AND c.deleted_at IS NULL
           AND COALESCE(
                 (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                   WHERE rt.ref_id = c.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                 'open'
               ) NOT IN ('done', 'won''t-do')
         LIMIT 1
        """,
        (ref_id,),
    ).fetchone()
    return row is not None


def evaluate(store: Store, spec: dict[str, Any], *, ref_id: int) -> bool | None:
    """Check for ≥1 succeeded child job under ``ref_id``.

    The auto_check worker passes ``ref_id`` as a kwarg so evaluators
    that need to know "who is asking" can look up tree-relative state
    without a parameter on the spec. The other evaluators don't use
    ``ref_id`` (they answer global questions like "is paper:X
    ingested?"); this one needs to scope to the calling leaf.

    Returns ``None`` (= not yet, leave the leaf open) when a guard
    fires; ``True`` only when a child job has actually succeeded and
    neither guard applies.
    """
    with store.pool.connection() as conn:
        # Guard 1: planner coroutines self-resolve — never close them.
        if _parent_is_planner_coroutine(conn, ref_id):
            return None
        # Guard 2: don't close while a child todo is still live.
        if _has_live_child_todo(conn, ref_id):
            return None
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
