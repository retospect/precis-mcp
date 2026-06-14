"""Dispatch worker — Slice 5 of ``docs/design/todo-tree-plan.md``.

The bridge between the todo tree (intent) and the job substrate
(execution). Walks open todos with ``meta.executor`` set, picks
the next one that has no successful child job yet, and mints a
``kind='job'`` ref under it. The existing ``job_claude_inproc``
worker (or any future executor) claims the job and runs it.

When the job succeeds, the parent todo's ``meta.auto_check`` of
type ``child_job_succeeded`` resolves the leaf to ``STATUS:done``
on the next ``auto_check`` pass. When the job fails, the
failure-bubble path (see Slice-5 task #6) tags the parent
``child-failed:<job_id>`` so the operator / asa-bot sees a stuck
parent in the nursery digest.

Idempotency
-----------

A todo with ``meta.executor`` already has a child job: skip. The
dispatcher does **not** auto-re-dispatch after a failure (per the
"bubble back up" rule — the parent decides). Once any child job is
queued / running / succeeded / failed, no new job mints until the
parent's owner intervenes (remove the ``child-failed:N`` tag,
delete the failed child job, or change the executor).

Multi-host concurrency
----------------------

Same row-lock pattern as the schedule worker: per-todo
``SELECT … FOR UPDATE OF r SKIP LOCKED`` inside ``store.tx()``
spans the claim → child-job-insert. Two dispatch workers racing on
the same todo serialise on the refs row's tx lock; the loser walks
past via SKIP LOCKED.

Auto-injection of ``auto_check``
--------------------------------

If a writer set ``meta.executor`` but forgot ``meta.auto_check``,
the dispatcher silently injects
``{"type": "child_job_succeeded"}``. Without it the todo would
never resolve on job success — the spawned job would finish, the
parent would sit open forever. The default-on behaviour matches
the user's stated discipline ("true unless false") and is harmless
when explicit auto_check was already set (we only inject when the
key is missing).
"""

from __future__ import annotations

import logging
from typing import Any

from precis.handlers._todo_views import _doable_exclusion_clause
from precis.store import Store
from precis.store.types import Tag
from precis.workers.executors import (
    EXECUTOR_PROVIDES,
    is_known_executor,
)
from precis.workers.job_types import get_job_type
from precis.workers.runner import BatchResult

log = logging.getLogger(__name__)


# Statuses on the parent that are eligible for dispatch. A paused /
# done / blocked parent doesn't dispatch.
_OPEN_PARENT_STATUSES: frozenset[str] = frozenset({"open", "doing"})


def run_dispatch_pass(store: Store, *, limit: int = 50) -> BatchResult:
    """Drain up to ``limit`` dispatchable todos. Returns BatchResult.

    Counters:

    * ``claimed`` = number of parent todos we successfully locked
    * ``ok`` = number of child jobs minted
    * ``failed`` = number of parents we couldn't dispatch for
      (bad executor / job_type, validation failure)
    """
    candidate_ids = _candidate_parent_ids(store, limit=limit)
    if not candidate_ids:
        return BatchResult(handler="dispatch", claimed=0, ok=0, failed=0)
    n_claimed = 0
    n_ok = 0
    n_failed = 0
    for parent_id in candidate_ids:
        try:
            claimed, minted = _claim_and_dispatch(store, parent_id)
        except Exception:
            log.exception(
                "dispatch: failed to process parent todo id=%d", parent_id
            )
            n_failed += 1
            continue
        n_claimed += claimed
        if minted:
            n_ok += 1
        elif claimed:
            n_failed += 1
    return BatchResult(
        handler="dispatch", claimed=n_claimed, ok=n_ok, failed=n_failed
    )


# ── candidate enumeration (unlocked) ──────────────────────────────


def _candidate_parent_ids(store: Store, *, limit: int) -> list[int]:
    """Return ref ids of dispatchable parent todos.

    Eligibility:

    * ``kind='todo'`` and not deleted
    * ``meta.executor`` is set
    * STATUS in ``open|doing`` (paused / done / blocked skip)
    * No existing non-deleted child of ``kind='job'`` (no
      previous attempt; the bubble-up discipline says "one
      attempt per parent until the owner intervenes")
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id
              FROM refs r
             WHERE r.kind = 'todo' AND r.deleted_at IS NULL
               AND r.meta ? 'executor'
               AND COALESCE(
                     (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                       WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                     'open'
                   ) = ANY(%s)
               AND NOT EXISTS (
                   SELECT 1 FROM refs c
                    WHERE c.parent_id = r.ref_id
                      AND c.kind = 'job'
                      AND c.deleted_at IS NULL
               )
               AND NOT EXISTS (
                   -- Shared ``_DOABLE_EXCLUSION_TAGS`` registry: keep
                   -- the dispatch candidate filter in lock-step with
                   -- ``view='doable'``. ``halt`` (owner-applied) plus
                   -- the existing waiting-for / asking-reto / child-
                   -- failed forms all block dispatch the same way.
                   SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                    WHERE rt.ref_id = r.ref_id
                      AND t.namespace = 'OPEN'
                      AND """ + _doable_exclusion_clause() + """
               )
             ORDER BY r.ref_id
             LIMIT %s
            """,
            (sorted(_OPEN_PARENT_STATUSES), limit),
        ).fetchall()
    return [int(r[0]) for r in rows]


# ── per-parent locked mint ────────────────────────────────────────


def _claim_and_dispatch(
    store: Store, parent_id: int
) -> tuple[int, bool]:
    """Lock one parent todo and mint its child job.

    Returns ``(claimed, minted)``:

    * ``claimed`` = 1 if we locked the parent, 0 if another worker
      held the row.
    * ``minted`` = True if we wrote the child job, False if we
      rejected (bad executor / job_type / etc).
    """
    with store.tx() as conn:
        row = conn.execute(
            """
            SELECT r.ref_id,
                   r.meta->>'executor' AS executor,
                   r.meta->>'job_type' AS job_type,
                   r.meta->'params' AS params,
                   r.meta ? 'auto_check' AS has_auto_check
              FROM refs r
             WHERE r.ref_id = %s
               AND r.kind = 'todo'
               AND r.deleted_at IS NULL
               AND r.meta ? 'executor'
               AND COALESCE(
                     (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                       WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                     'open'
                   ) = ANY(%s)
               AND NOT EXISTS (
                   SELECT 1 FROM refs c
                    WHERE c.parent_id = r.ref_id
                      AND c.kind = 'job'
                      AND c.deleted_at IS NULL
               )
               AND NOT EXISTS (
                   -- Re-check the exclusion registry inside the
                   -- FOR UPDATE — guards against a halt tag landing
                   -- between candidate enumeration and the lock.
                   SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                    WHERE rt.ref_id = r.ref_id
                      AND t.namespace = 'OPEN'
                      AND """ + _doable_exclusion_clause() + """
               )
             FOR UPDATE OF r SKIP LOCKED
            """,
            (parent_id, sorted(_OPEN_PARENT_STATUSES)),
        ).fetchone()
        if row is None:
            # Another worker holds it, or the parent's state changed
            # between enumeration and lock (status flipped, child
            # job was just minted by a race). Either way, no-op.
            return (0, False)
        ref_id = int(row[0])
        executor = row[1]
        job_type = row[2]
        params = dict(row[3] or {})
        has_auto_check = bool(row[4])

        # Validate executor + job_type at dispatch time. The TodoHandler
        # doesn't validate ``meta.executor`` / ``meta.job_type`` on
        # ``put`` (it's just a meta key from the handler's perspective);
        # the dispatcher is the boundary that rejects mis-spelled or
        # incompatible combinations. Logs + skips on failure so the
        # operator sees the broken parent in logs without crashing
        # the pass.
        if not isinstance(executor, str) or not is_known_executor(executor):
            log.warning(
                "dispatch: parent #%d has unknown meta.executor=%r; "
                "skipping",
                ref_id,
                executor,
            )
            return (1, False)
        if not isinstance(job_type, str):
            log.warning(
                "dispatch: parent #%d has missing meta.job_type; skipping",
                ref_id,
            )
            return (1, False)
        spec = get_job_type(job_type)
        if spec is None:
            log.warning(
                "dispatch: parent #%d has unknown meta.job_type=%r; "
                "skipping",
                ref_id,
                job_type,
            )
            return (1, False)
        if executor not in spec.compatible_executors:
            log.warning(
                "dispatch: parent #%d job_type=%r incompatible with "
                "executor=%r; skipping",
                ref_id,
                job_type,
                executor,
            )
            return (1, False)
        missing_caps = spec.requires - EXECUTOR_PROVIDES[executor]
        if missing_caps:
            log.warning(
                "dispatch: parent #%d executor=%r missing caps for %r: %s",
                ref_id,
                executor,
                job_type,
                sorted(missing_caps),
            )
            return (1, False)

        # Auto-inject ``auto_check`` if the writer didn't set one,
        # so the parent resolves on the child's success.
        if not has_auto_check:
            conn.execute(
                """
                UPDATE refs
                   SET meta = meta || jsonb_build_object(
                                'auto_check',
                                jsonb_build_object('type', 'child_job_succeeded')
                              )
                 WHERE ref_id = %s
                """,
                (ref_id,),
            )

        # Mint the child job ref. Stay inside the tx so the row
        # lock spans claim + mint + status tag + event append.
        child_meta: dict[str, Any] = {
            "job_type": job_type,
            "executor": executor,
            "params": params,
            "dispatched_from_todo": ref_id,
        }
        title = f"{job_type} (dispatched from todo:{ref_id})"
        child = store.insert_ref(
            kind="job",
            slug=None,
            title=title,
            meta=child_meta,
            parent_id=ref_id,
            conn=conn,
        )
        store.add_tag(
            child.id,
            Tag.closed("STATUS", "queued"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        store.append_event(
            ref_id,
            source="dispatch",
            event="job-minted",
            payload={
                "job_id": int(child.id),
                "job_type": job_type,
                "executor": executor,
            },
            conn=conn,
        )
        log.info(
            "dispatch: parent #%d → minted job #%d (job_type=%s, "
            "executor=%s)",
            ref_id,
            child.id,
            job_type,
            executor,
        )
        return (1, True)


__all__ = ["run_dispatch_pass"]
