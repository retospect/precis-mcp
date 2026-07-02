"""Failure-bubble: tag the parent todo when a job fails.

Slice-5 of ``docs/design/todo-tree-plan.md``: a child job hitting
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
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from precis.store.types import Tag

if TYPE_CHECKING:
    from psycopg import Connection

    from precis.store import Store

log = logging.getLogger(__name__)


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

    tag = Tag.open(f"child-failed:{job_id}")
    if conn is not None:
        for target in targets:
            store.add_tag(target, tag, set_by="system", conn=conn)
    else:
        with store.tx() as tx_conn:
            for target in targets:
                store.add_tag(target, tag, set_by="system", conn=tx_conn)
    log.info(
        "bubble: job #%d failed → tagged todo(s) %s with %s",
        job_id,
        targets,
        tag,
    )


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
