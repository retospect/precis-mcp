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
from precis.workers import planner_guardrails
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


# Job types whose parent manages its OWN terminal state and must NOT
# get a ``child_job_succeeded`` auto_check injected. ``plan_tick`` is
# the LLM planner coroutine: a tick exits ``STATUS:succeeded`` whenever
# the claude -p subprocess runs cleanly — including when the planner
# *yielded* (``ask-user:``) or *minted children* (``continue``).
# Injecting ``child_job_succeeded`` would then auto-close the parent on
# the first clean tick, before any work landed. The planner instead
# closes itself with its own ``STATUS:done`` tag (guarded), or parks on
# ``ask-user:`` / ``halt`` — so a coroutine parent needs no auto_check.
_SELF_RESOLVING_JOB_TYPES: frozenset[str] = frozenset({"plan_tick"})


def _halt_bad_dispatch(
    store: Store, conn: Any, ref_id: int, detail: str
) -> tuple[int, bool]:
    """Self-halt a mis-configured parent so it stops re-warning forever.

    A parent whose ``executor`` / ``job_type`` is invalid can never mint
    a child, so it stays a dispatch candidate and re-warns on *every*
    sweep. Left unhalted, a handful of such todos flood ``worker_logs``
    indefinitely — a real incident was six todos carrying a bogus
    ``meta.executor='plan_tick'`` (``plan_tick`` is a job_type, never an
    executor) warning ~40k times/day/host.

    Tagging ``halt:bad-dispatch`` (an exclusion-registry tag, see
    ``handlers/_todo_views._DOABLE_EXCLUSION_TAGS``) drops the parent
    from candidate enumeration: warn once, surface in the halt /
    attention view, and resume by removing the tag once the meta is
    fixed. The tag is written on the dispatch tx's own ``conn`` while it
    holds ``FOR UPDATE OF r`` on the parent — atomic with the claim.
    """
    store.add_tag(ref_id, Tag.open("halt:bad-dispatch"), set_by="system", conn=conn)
    log.warning("dispatch: parent #%d %s; halted (halt:bad-dispatch)", ref_id, detail)
    return (1, False)


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
        # Planner-coroutine guardrails: tick cap, per-todo cost cap,
        # global daily ceiling. The first two halt the parent
        # in-place (tag halt:tick-cap / halt:cost-cap so attention
        # view surfaces it); the third skips the whole dispatch
        # round. See workers/planner_guardrails.py.
        verdict = planner_guardrails.check_parent(store, parent_ref_id=parent_id)
        if not verdict.allow:
            if verdict.halt_tag is None:
                # Global ceiling — stop dispatching anything until
                # the rolling window clears. Other candidates would
                # hit the same gate.
                log.info(
                    "dispatch: aborting round, daily ceiling: %s",
                    verdict.reason,
                )
                break
            # Per-todo halt — counted as a skip, not a failure.
            continue
        try:
            claimed, minted = _claim_and_dispatch(store, parent_id)
        except Exception:
            log.exception("dispatch: failed to process parent todo id=%d", parent_id)
            n_failed += 1
            continue
        n_claimed += claimed
        if minted:
            n_ok += 1
            # Bump tick count so the next sweep sees the increment;
            # caps land on the next candidate enumeration.
            planner_guardrails.bump_tick_count(store, parent_id)
        elif claimed:
            n_failed += 1
    return BatchResult(handler="dispatch", claimed=n_claimed, ok=n_ok, failed=n_failed)


# ── candidate enumeration (unlocked) ──────────────────────────────


def _candidate_parent_ids(store: Store, *, limit: int) -> list[int]:
    """Return ref ids of dispatchable parent todos.

    Eligibility (planner-coroutine slice):

    * ``kind='todo'`` and not deleted.
    * Auto-run signal: either a closed-vocab ``LLM:<model>`` tag
      (opus / sonnet / haiku — runs the LLM planner) OR an
      ``executor:<runner>`` tag (code-path runner) OR — legacy —
      ``meta.executor`` set (back-compat with the v1 ``fix_gripe``
      shape; new code uses the tag forms).
    * STATUS in ``open|doing`` (paused / done / blocked skip).
    * No **live** child job — a child of ``kind='job'`` whose own
      STATUS is anything other than ``done`` / ``succeeded`` /
      ``won't-do`` / ``failed``. Completed jobs from prior ticks are
      fine; only an in-flight (``queued`` / ``running``) job blocks.
      ``succeeded`` is the executor's terminal value for a clean run
      and MUST be in this set, else a ``plan_tick`` coroutine could
      never re-tick after its first successful tick. (Failed jobs
      bubble ``child-failed:N`` to the parent which the exclusion
      registry handles.)
    * No **live** child todo — a child of ``kind='todo'`` whose own
      STATUS is open / doing (the planner spawned children and they
      are still working). This is the coroutine yield: a parent that
      minted children sits silent until they all resolve, then
      re-becomes a candidate so the planner can read the
      ``job_summary`` chunks and continue.
    * No exclusion tag (registry: halt / halt:* / ask-user* /
      waiting-for:* / child-failed:*).
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id
              FROM refs r
             WHERE r.kind = 'todo' AND r.deleted_at IS NULL
               AND (
                   r.meta ? 'executor'
                   OR EXISTS (
                       SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                        WHERE rt.ref_id = r.ref_id
                          AND (
                              t.namespace = 'LLM'
                              OR (t.namespace = 'OPEN' AND t.value LIKE 'executor:%%')
                          )
                   )
               )
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
                      AND COALESCE(
                            (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                              WHERE rt.ref_id = c.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                            'open'
                          ) NOT IN ('done', 'failed', 'succeeded', 'won''t-do')
               )
               AND NOT EXISTS (
                   SELECT 1 FROM refs c
                    WHERE c.parent_id = r.ref_id
                      AND c.kind = 'todo'
                      AND c.deleted_at IS NULL
                      AND COALESCE(
                            (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                              WHERE rt.ref_id = c.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                            'open'
                          ) NOT IN ('done', 'won''t-do')
               )
               AND NOT EXISTS (
                   SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                    WHERE rt.ref_id = r.ref_id
                      AND t.namespace = 'OPEN'
                      AND """
            + _doable_exclusion_clause()
            + """
               )
             ORDER BY r.ref_id
             LIMIT %s
            """,
            (sorted(_OPEN_PARENT_STATUSES), limit),
        ).fetchall()
    return [int(r[0]) for r in rows]


# ── per-parent locked mint ────────────────────────────────────────


def _claim_and_dispatch(store: Store, parent_id: int) -> tuple[int, bool]:
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
                   r.meta ? 'auto_check' AS has_auto_check,
                   (SELECT 'LLM:' || t.value FROM ref_tags rt
                      JOIN tags t ON t.tag_id = rt.tag_id
                     WHERE rt.ref_id = r.ref_id
                       AND t.namespace = 'LLM'
                     LIMIT 1) AS llm_tag,
                   (SELECT t.value FROM ref_tags rt
                      JOIN tags t ON t.tag_id = rt.tag_id
                     WHERE rt.ref_id = r.ref_id
                       AND t.namespace = 'OPEN'
                       AND t.value LIKE 'executor:%%'
                     LIMIT 1) AS executor_tag
              FROM refs r
             WHERE r.ref_id = %s
               AND r.kind = 'todo'
               AND r.deleted_at IS NULL
               AND (
                   r.meta ? 'executor'
                   OR EXISTS (
                       SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                        WHERE rt.ref_id = r.ref_id
                          AND (
                              t.namespace = 'LLM'
                              OR (t.namespace = 'OPEN' AND t.value LIKE 'executor:%%')
                          )
                   )
               )
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
                      AND COALESCE(
                            (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                              WHERE rt.ref_id = c.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                            'open'
                          ) NOT IN ('done', 'failed', 'succeeded', 'won''t-do')
               )
               AND NOT EXISTS (
                   SELECT 1 FROM refs c
                    WHERE c.parent_id = r.ref_id
                      AND c.kind = 'todo'
                      AND c.deleted_at IS NULL
                      AND COALESCE(
                            (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                              WHERE rt.ref_id = c.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                            'open'
                          ) NOT IN ('done', 'won''t-do')
               )
               AND NOT EXISTS (
                   -- Re-check the exclusion registry inside the
                   -- FOR UPDATE — guards against a halt / ask-user
                   -- tag landing between candidate enumeration and
                   -- the lock.
                   SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                    WHERE rt.ref_id = r.ref_id
                      AND t.namespace = 'OPEN'
                      AND """
            + _doable_exclusion_clause()
            + """
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
        llm_tag = row[5]
        executor_tag = row[6]

        # Planner-coroutine path: when a todo is LLM:*-tagged but lacks
        # ``meta.executor``, synthesize the dispatch parameters from the
        # tag. The model picker IS the tag value (``LLM:opus`` →
        # ``model=opus``); the job_type is the generic planner tick
        # (``plan_tick``) which knows how to read the parent's body,
        # ancestry, and prior child summaries into a single prompt.
        # ``executor:<runner>`` tags route to code-path runners with a
        # parallel synthesis (job_type = ``executor:<runner>``).
        if not isinstance(executor, str) and llm_tag:
            model = str(llm_tag).removeprefix("LLM:")
            executor = "claude_inproc"
            job_type = job_type or "plan_tick"
            params.setdefault("model", model)
        elif not isinstance(executor, str) and executor_tag:
            runner = str(executor_tag).removeprefix("executor:")
            # Reserved; v1 has no registered executor:* values, so
            # this branch only fires if the closed-vocab guard is
            # widened in a future slice. The runner name is the
            # job_type by convention.
            executor = runner
            job_type = job_type or runner

        # Validate executor + job_type at dispatch time. The TodoHandler
        # doesn't validate ``meta.executor`` / ``meta.job_type`` on
        # ``put`` (it's just a meta key from the handler's perspective);
        # the dispatcher is the boundary that rejects mis-spelled or
        # incompatible combinations. Logs + skips on failure so the
        # operator sees the broken parent in logs without crashing
        # the pass.
        # Validation failures self-halt the parent (see
        # ``_halt_bad_dispatch``) rather than warn-and-skip: an
        # un-dispatchable parent that merely skips stays a candidate and
        # re-warns on every sweep, forever.
        if not isinstance(executor, str) or not is_known_executor(executor):
            return _halt_bad_dispatch(
                store, conn, ref_id, f"has unknown meta.executor={executor!r}"
            )
        if not isinstance(job_type, str):
            return _halt_bad_dispatch(store, conn, ref_id, "has missing meta.job_type")
        spec = get_job_type(job_type)
        if spec is None:
            return _halt_bad_dispatch(
                store, conn, ref_id, f"has unknown meta.job_type={job_type!r}"
            )
        if executor not in spec.compatible_executors:
            return _halt_bad_dispatch(
                store,
                conn,
                ref_id,
                f"job_type={job_type!r} incompatible with executor={executor!r}",
            )
        missing_caps = spec.requires - EXECUTOR_PROVIDES[executor]
        if missing_caps:
            return _halt_bad_dispatch(
                store,
                conn,
                ref_id,
                f"executor={executor!r} missing caps for {job_type!r}: "
                f"{sorted(missing_caps)}",
            )

        # Auto-inject ``auto_check`` if the writer didn't set one, so a
        # deterministic job's parent resolves on the child's success.
        # Skip it for self-resolving job types (the ``plan_tick``
        # coroutine drives its own STATUS — see
        # ``_SELF_RESOLVING_JOB_TYPES``); injecting there would close the
        # parent on its first clean tick.
        if job_type in _SELF_RESOLVING_JOB_TYPES:
            # Belt-and-suspenders: declining to *inject* isn't enough —
            # a stale / hand-authored / legacy ``child_job_succeeded``
            # spec can already be attached (this is exactly what
            # auto-closed an in-progress paper cascade on its first clean
            # planning tick). Strip it so the auto_check worker can't fire
            # it. Only the footgun type is removed; a deliberate
            # ``time_past`` / ``ask-user`` spec on a planner is left alone.
            conn.execute(
                """
                UPDATE refs
                   SET meta = meta - 'auto_check'
                 WHERE ref_id = %s
                   AND meta->'auto_check'->>'type' = 'child_job_succeeded'
                """,
                (ref_id,),
            )
        elif not has_auto_check:
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
            "dispatch: parent #%d → minted job #%d (job_type=%s, executor=%s)",
            ref_id,
            child.id,
            job_type,
            executor,
        )
        return (1, True)


__all__ = ["run_dispatch_pass"]
