"""Failure-bubble: tag the parent todo when a job fails.

Slice-5 of ``docs/backlog/todo-tree-plan.md``: a child job hitting
``STATUS:failed`` flips a flag on its parent todo so the parent
shows up in the nursery digest's "stuck-doable" / "stale-claim"
detectors. The parent's owner (asa or human) then decides what to
do — re-dispatch (clear the flag, the dispatch worker re-mints),
switch executor, ask the user, give up.

The bubble is a single open tag ``child-failed:<job_id>`` so:

* the operator can see *which* child failed without reading meta;
* the nursery detection is a simple ``WHERE t.value LIKE
  'child-failed:%'``;
* clearing the flag is an ordinary ``tag(remove=…)`` call.

Idempotent: re-applying the same tag is a no-op.

**Infra-class bounded auto-retry** (2026-07-30, the 07-26→30 agent-lane
stall — 11 planner parents latched on a lease-expiry sweep and needed
manual tag removal). ``child-failed:<job_id>`` is written for two
indistinguishable causes: a genuine content-class task error, and an
infra-class lease-expiry orphan sweep (``sweeper.py``'s
``_transition_to_failed``, which stamps ``swept:claim-orphaned`` on the
child *before* calling this function). The former needs a human/planner
decision; the latter almost always self-heals on a fresh attempt — the
worker that died mid-task, not the task, was the problem. So: an
infra-class failure is *not* latched immediately. Instead a bounded,
windowed per-parent counter (:func:`_bump_orphan_retry_count`, modelled
on ``workers/planner_guardrails.py``'s ``bump_tick_count``) tracks how
many infra failures have landed in the trailing window; under the cap
the parent is left unlatched (no ``child-failed:`` tag at all) so it
falls straight back into ``_candidate_parent_ids`` and the dispatcher
mints a fresh child next sweep. At/over the cap it latches exactly like
a content failure, plus ``halt:orphan-retry-cap`` for visibility — a
persistently-orphaned coordinator (dead executor, not a transient sweep)
stops spinning instead of retrying forever. The content path is
byte-identical to before: every consumer of ``child-failed:<job_id>``
(the exclusion registry, ``_detect_child_failed_parked``, …) is
untouched.
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
INFRA_FAILURE_TAGS = frozenset({"swept:claim-orphaned"})

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

    # Two lanes (ADR 0044). Intent lane: the parent IS a todo — tag it,
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
    """Live todo ids that ``requested`` this job (ADR 0044 compute lane).

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


__all__ = ["bubble_job_failure"]
