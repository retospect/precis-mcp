"""Failure bubble: tag the parent todo when a job fails.

Slice-5 of ``docs/backlog/todo-tree-plan.md``: a child job hitting
``STATUS:failed`` flips a flag on its parent todo so the parent shows up
in the nursery digest's "stuck-doable"/"stale-claim" detectors. The
parent's owner (asa or human) decides what to do — re-dispatch (clear the
flag, the dispatch worker re-mints), switch executor, ask the user, give
up.

The bubble is a single open tag ``child-failed:<job_id>``: the operator
sees which child failed without reading meta, nursery detection is a flat
``WHERE t.value LIKE 'child-failed:%'``, clearing is an ordinary
``tag(remove=…)``. Idempotent: re-applying the same tag is a no-op.

**Infra-class bounded auto-retry.** ``child-failed:<job_id>`` covers two
indistinguishable causes: a genuine content-class task error, and an
infra-class lease-expiry orphan sweep (``sweeper.py``'s
``_transition_to_failed``, which stamps ``swept:claim-orphaned`` on the
child first). The former needs a human/planner decision; the latter
almost always self-heals on a fresh attempt (the worker died mid-task,
not the task), so it is *not* latched immediately: a bounded, windowed
per-parent counter (:func:`_bump_orphan_retry_count`, modelled on
``workers/planner_guardrails.py``'s ``bump_tick_count``) tracks infra
failures in the trailing window; under the cap the parent stays unlatched
(no ``child-failed:`` tag) and falls back into ``_candidate_parent_ids``
for a fresh child next sweep. At/over the cap it latches like a content
failure, plus ``halt:orphan-retry-cap`` for visibility, so a
persistently-orphaned coordinator stops spinning instead of retrying
forever. Every consumer of ``child-failed:<job_id>`` (the exclusion
registry, ``_detect_child_failed_parked``, …) is unaffected either way.

**Subprocess-death misclassification** (``docs/backlog/
parked-leaf-recovery.md``): a child process dying by signal (SIGKILL/OOM)
or exiting with no result file used to land as a bare content-class
failure — the compute never ran, but the bubble couldn't tell that apart
from a genuine task error. The generic subprocess-exit layer
(``executors/_common.record_failure``'s ``open_tag=`` parameter, wired
from ``precis_pathway.seed_job``) now stamps ``infra:child-killed`` on
the job before it's marked failed; that value joins
:data:`INFRA_FAILURE_TAGS`, so a subprocess death gets the same bounded
auto-retry as a lease-expiry orphan instead of latching immediately. See
:mod:`precis.workers.sweeper`'s ``unpark`` phase for the complementary
autonomous recovery once a bubble does latch (bounded, cool-down-gated,
terminal past ``UNPARK_CAP``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from precis.store.types import Tag

if TYPE_CHECKING:
    from psycopg import Connection

    from precis.store import Store

log = logging.getLogger(__name__)

#: Open-tag values on the *child* job that mark a failure as infra-class
#: (lease-expiry / orphan-sweep) rather than content-class (a genuine task
#: error). Only the sweeper writes these (``workers/sweeper.py``'s
#: ``_transition_to_failed``), and only once the job's lease has already
#: expired — so at bubble time no live worker can still hold the job. A
#: bounded auto-retry is safe: the classification is provably "the worker
#: died", never "the worker is still running and about to finish".
#: Extensible — a future infra-detectable failure mode appends here.
#: ``infra:child-killed`` (2026-08-10) is written by the generic
#: subprocess-exit layer (``executors/_common.record_failure``'s
#: ``open_tag=``) — synchronously, by the worker that owned the job,
#: before it marks the job failed — so the same "no live worker can
#: still be running it" safety argument applies.
INFRA_FAILURE_TAGS = frozenset({"swept:claim-orphaned", "infra:child-killed"})

#: Bounded-retry knobs for the infra-class path. A parent may re-arm this
#: many infra failures inside the trailing window before the bubble gives
#: up and latches ``child-failed:`` + ``halt:orphan-retry-cap`` — bounds a
#: coordinator whose infra keeps dying (not just one bad sweep) from
#: spinning forever on free retries.
ORPHAN_RETRY_CAP = 3
ORPHAN_RETRY_WINDOW_HOURS = 6


def bubble_job_failure(
    store: Store,
    job_id: int,
    *,
    conn: Connection | None = None,
) -> None:
    """Tag the parent todo of ``job_id`` with ``child-failed:<job_id>``.

    No-op when the job has no parent (a legacy orphan job from
    pre-Slice-5 — kept working for backwards compatibility), or when
    the parent isn't a todo (shouldn't happen given the parent-kind
    guard, but defensive).

    ``conn`` lets the caller share an in-flight transaction so the
    parent-tag write commits with the job-status write. When ``None``
    the helper opens its own short-lived tx via ``store.tx()``.
    """
    parent_id, parent_kind = _lookup_parent(store, job_id, conn=conn)
    if parent_id is None:
        log.info(
            "bubble: job #%d has no parent_id — orphan job, no bubble",
            job_id,
        )
        return

    # Two lanes. Intent lane: the parent IS a todo — tag it,
    # exactly as before. Compute lane: the parent is a build subject
    # (structure / cad / draft), which has no rotation to enter, so the
    # bubble targets the requesting todo(s) reached via the ``requested``
    # link instead. A pure direct-manipulation build with no requester
    # (a human clicking "relax") has nowhere to bubble — the failure is
    # visible on the artifact's own ``view='runs'``.
    if parent_kind == "todo":
        targets = [parent_id]
    else:
        targets = _requester_todos(store, job_id, conn=conn)
        if not targets:
            log.info(
                "bubble: job #%d parents on %s #%d with no requester todo — "
                "no bubble (failure surfaces on the artifact)",
                job_id,
                parent_kind,
                parent_id,
            )
            return

    is_infra = _is_infra_failure(store, job_id, conn=conn)
    tag = Tag.open(f"child-failed:{job_id}")

    if is_infra:
        for target in targets:
            n = _bump_orphan_retry_count(
                store, target, window_hours=ORPHAN_RETRY_WINDOW_HOURS, conn=conn
            )
            if n < ORPHAN_RETRY_CAP:
                log.info(
                    "bubble: job #%d failed (infra: swept:claim-orphaned) — "
                    "todo #%d infra retry %d/%d, re-armed, not latched",
                    job_id,
                    target,
                    n,
                    ORPHAN_RETRY_CAP,
                )
                continue
            log.warning(
                "bubble: job #%d failed (infra: swept:claim-orphaned) — "
                "todo #%d hit infra retry cap %d/%d, latching %s",
                job_id,
                target,
                n,
                ORPHAN_RETRY_CAP,
                tag,
            )
            _apply_tags(
                store, target, [tag, Tag.open("halt:orphan-retry-cap")], conn=conn
            )
        return

    for target in targets:
        _apply_tags(store, target, [tag], conn=conn)
    log.info(
        "bubble: job #%d failed → tagged todo(s) %s with %s",
        job_id,
        targets,
        tag,
    )


def remove_child_failed_tags(
    store: Store, ref_id: int, *, conn: Connection | None = None
) -> int:
    """Remove every open ``child-failed:<job_id>`` tag on ``ref_id``.

    A todo can accumulate more than one such tag over time (one per
    failed child — see the module docstring), so this is a bulk
    ``LIKE``-scoped delete rather than a single :meth:`Store.remove_tag`
    call. Shared by two consumers (parked-leaf-recovery,
    docs/backlog/parked-leaf-recovery.md):

    * the ``child_job_succeeded`` auto_check evaluator, when a fresh
      child success flips the parent — clears stale bubbles left by
      earlier failed siblings instead of leaving a done-but-parked-
      looking row;
    * :mod:`precis.workers.sweeper`'s ``unpark`` phase, when a bounded
      autonomous retry re-arms a parked leaf.

    Returns the number of tags removed (0 = none were present, a
    harmless no-op either way). ``conn`` threads exactly like
    :func:`_apply_tags` — shares the caller's transaction when given,
    else opens (and commits) its own.
    """
    sql = (
        "DELETE FROM ref_tags rt "
        "USING tags t "
        "WHERE rt.tag_id = t.tag_id "
        "  AND rt.ref_id = %s "
        "  AND t.namespace = 'OPEN' "
        "  AND t.value LIKE 'child-failed:%%'"
    )
    if conn is not None:
        cur = conn.execute(sql, (ref_id,))
        return cur.rowcount or 0
    with store.pool.connection() as c:
        cur = c.execute(sql, (ref_id,))
        c.commit()
        return cur.rowcount or 0


def _apply_tags(
    store: Store, target: int, tags: list[Tag], *, conn: Connection | None
) -> None:
    """Write ``tags`` onto ``target``, sharing ``conn`` if given, else a
    short-lived tx of its own (mirrors the prior inline write)."""
    if conn is not None:
        for t in tags:
            store.add_tag(target, t, set_by="system", conn=conn)
    else:
        with store.tx() as tx_conn:
            for t in tags:
                store.add_tag(target, t, set_by="system", conn=tx_conn)


def _is_infra_failure(store: Store, job_id: int, *, conn: Connection | None) -> bool:
    """Does the *child* job carry one of :data:`INFRA_FAILURE_TAGS`?

    Only the sweeper writes those (see the module docstring) — and it
    commits the tag before calling :func:`bubble_job_failure`, so this
    read (whether via a fresh connection or the caller's shared ``conn``)
    always sees it.
    """
    sql = (
        "SELECT 1 FROM ref_tags rt "
        "  JOIN tags t ON t.tag_id = rt.tag_id "
        " WHERE rt.ref_id = %s "
        "   AND t.namespace = 'OPEN' "
        "   AND t.value = ANY(%s) "
        " LIMIT 1"
    )
    params = (job_id, sorted(INFRA_FAILURE_TAGS))
    if conn is not None:
        row = conn.execute(sql, params).fetchone()
    else:
        with store.pool.connection() as c:
            row = c.execute(sql, params).fetchone()
    return row is not None


_BUMP_ORPHAN_RETRY_SQL = """
    UPDATE refs
       SET meta = jsonb_set(
             jsonb_set(
               COALESCE(meta, '{}'::jsonb),
               '{orphan_retry_count}',
               to_jsonb(
                 CASE
                   WHEN (meta->>'orphan_retry_window_start') IS NULL
                     OR (meta->>'orphan_retry_window_start')::timestamptz
                        < now() - %(window)s::interval
                   THEN 1
                   ELSE COALESCE((meta->>'orphan_retry_count')::int, 0) + 1
                 END
               ),
               true
             ),
             '{orphan_retry_window_start}',
             to_jsonb(
               CASE
                 WHEN (meta->>'orphan_retry_window_start') IS NULL
                   OR (meta->>'orphan_retry_window_start')::timestamptz
                      < now() - %(window)s::interval
                 THEN now()
                 ELSE (meta->>'orphan_retry_window_start')::timestamptz
               END
             ),
             true
           )
     WHERE ref_id = %(ref_id)s
 RETURNING (meta->>'orphan_retry_count')::int
"""


def _bump_orphan_retry_count(
    store: Store, ref_id: int, *, window_hours: int, conn: Connection | None
) -> int:
    """Increment ``meta.orphan_retry_count`` on ``ref_id``, windowed.

    Modelled on ``workers/planner_guardrails.py``'s ``bump_tick_count``:
    a single atomic ``UPDATE … RETURNING`` (Postgres serialises concurrent
    writers on the row, so no separate lock is needed). The window resets
    — count starts back at 1 — once ``orphan_retry_window_start`` is
    missing or older than ``window_hours``; otherwise the count keeps
    climbing. Returns the *new* count (the value including this bump).

    ``conn`` is threaded exactly like :func:`_apply_tags`: when the caller
    shares a live transaction, the bump runs on it (no independent
    ``.commit()``) so a caller-side rollback also rolls the counter back —
    a failure that "didn't happen" must not leave a surviving retry-count
    increment. Only opens (and commits) its own connection when ``conn is
    None``.
    """
    params = {"window": f"{window_hours} hours", "ref_id": ref_id}
    if conn is not None:
        row = conn.execute(_BUMP_ORPHAN_RETRY_SQL, params).fetchone()
    else:
        with store.pool.connection() as c:
            row = c.execute(_BUMP_ORPHAN_RETRY_SQL, params).fetchone()
            c.commit()
    return int(row[0]) if row else 0


def _lookup_parent(
    store: Store, job_id: int, *, conn: Connection | None
) -> tuple[int | None, str | None]:
    """Read ``(parent_id, parent_kind)`` for ``job_id``. ``(None, None)``
    when the job has no parent."""
    sql = (
        "SELECT p.ref_id, p.kind "
        "  FROM refs j "
        "  LEFT JOIN refs p ON p.ref_id = j.parent_id "
        " WHERE j.ref_id = %s"
    )
    if conn is not None:
        row = conn.execute(sql, (job_id,)).fetchone()
    else:
        with store.pool.connection() as c:
            row = c.execute(sql, (job_id,)).fetchone()
    if row is None or row[0] is None:
        return (None, None)
    return (int(row[0]), row[1])


def _requester_todos(
    store: Store, job_id: int, *, conn: Connection | None
) -> list[int]:
    """Live todo ids that ``requested`` this job (compute lane).

    The edge is stored ``requester todo --requested--> job``, so the
    requesters are the ``src`` of ``requested`` rows landing on this job.
    Soft-deleted requesters are skipped."""
    sql = (
        "SELECT r.ref_id "
        "  FROM links l "
        "  JOIN refs r ON r.ref_id = l.src_ref_id "
        " WHERE l.dst_ref_id = %s "
        "   AND l.relation = 'requested' "
        "   AND r.kind = 'todo' "
        "   AND r.deleted_at IS NULL"
    )
    if conn is not None:
        rows = conn.execute(sql, (job_id,)).fetchall()
    else:
        with store.pool.connection() as c:
            rows = c.execute(sql, (job_id,)).fetchall()
    return [int(r[0]) for r in rows]


__all__ = ["bubble_job_failure", "remove_child_failed_tags"]
