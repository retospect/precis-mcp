"""Job → quest resolution for the work board (spec me573285 §W5, td573299).

Every live job on the board resolves to a quest, one of two ways:

* the todo chain — the job's ``refs.parent_id`` chain walks up through
  live todos until an ancestor carries a ``serves`` edge to a live
  quest (the same edge ``work_view._todo_servers`` reads). The nearest
  serving ancestor wins; several quests at one depth take the lowest
  quest id. A retired ancestor ends the walk, and a retired quest's
  edges count for nothing — an ``abandoned`` quest still counts.
* the job's own meta — :func:`precis.workers.job_describe.quest_id_from_meta`
  (``params.quest_id``, ``coordinator_state.quest_id``, or a structure's
  ``quest:<id>`` tag). The fallback fires when the chain serves nothing
  or the parent is not a todo at all (``struct_relax`` jobs parent on a
  structure). A meta quest id that is not a live quest counts as
  ``None``.

:func:`live_jobs` lists the board's live job rows (STATUS running,
queued or yielded); :func:`resolve_job_quests` answers the map in a
constant number of queries for any number of jobs; and
:func:`classify_live_jobs` splits the rows three ways — resolved, the
``platform`` bucket (no todo parent, a :data:`PLATFORM_UPKEEP` job
type, filed under ``qu540835``) and the ``unassigned`` health signal
(everything else unresolved — a parentless ``paper_pull`` is
unassigned, not hidden under platform). The work model and the
/status health row both call ``classify_live_jobs`` so the two counts
cannot drift.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, NamedTuple

from precis.workers.job_describe import (
    PLATFORM_UPKEEP,
    quest_id_from_meta,
    quest_ids_for_structures,
    structure_ref_id_from_meta,
)

if TYPE_CHECKING:
    from precis.store import Store

#: The platform-upkeep quest's numeric id — the numeric twin of
#: :data:`UPKEEP_QUEST_HANDLE` (``qu<id>``).
PLATFORM_QUEST_ID = 540835

#: Job STATUS values that put a row on the board — mirror
#: ``work_view._BOARD_JOB``; a local constant so this module stays out
#: of the executor import chain.
_LIVE_STATUS = ("running", "queued", "yielded")

#: How far up the parent chain the resolver walks — the same cap
#: ``views._fetch_subtree`` uses on the way down.
_MAX_ANCESTOR_DEPTH = 10


@contextmanager
def _connect(store: Store, conn: Any | None) -> Iterator[Any]:
    """Yield the caller's connection when parked, else a pooled one —
    the ``_request_conn`` shape ``status.py`` keeps for its pages."""
    if conn is not None:
        yield conn
    else:
        with store.pool.connection() as c:
            yield c


def live_jobs(store: Store, *, conn: Any | None = None) -> list[dict[str, Any]]:
    """Every live job row on the board, one query.

    Returns ``[{id, parent_id, title, status, meta}]`` for each
    non-retired ``kind='job'`` whose STATUS is running, queued or
    yielded, ordered by ref_id.
    """
    with _connect(store, conn) as c:
        rows = c.execute(
            """
            SELECT r.ref_id, r.parent_id, r.title, t.value, r.meta
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'job' AND r.retired_at IS NULL
               AND t.namespace = 'STATUS'
               AND t.value = ANY(%s)
             ORDER BY r.ref_id
            """,
            (list(_LIVE_STATUS),),
        ).fetchall()
    out: dict[int, dict[str, Any]] = {}
    for rid, parent_id, title, status, meta in rows:
        out[int(rid)] = {
            "id": int(rid),
            "parent_id": parent_id,
            "title": title or "",
            "status": str(status),
            "meta": meta or {},
        }
    return list(out.values())


def _resolve(
    conn: Any, job_ids: list[int]
) -> tuple[dict[int, int | None], dict[int, int | None]]:
    """``({job_id: quest id | None}, {job_id: live todo parent | None})``.

    A constant number of queries for any number of jobs: (a) the jobs'
    parents and metas; (b) one recursive CTE walking each job's
    ``parent_id`` chain up through live todos (``kind='todo' AND
    retired_at IS NULL``, depth cap :data:`_MAX_ANCESTOR_DEPTH`); (c)
    one read of the ancestors' ``serves`` edges into live quests; (d)
    the bulk structure-tag read plus one live-quest confirmation for
    the ids ``quest_id_from_meta`` produces.
    """
    quests: dict[int, int | None] = {int(i): None for i in job_ids}
    todo_parent: dict[int, int | None] = {}
    if not job_ids:
        return quests, todo_parent
    ids = [int(i) for i in job_ids]
    jobs = {
        int(r[0]): {"parent_id": r[1], "meta": r[2] or {}}
        for r in conn.execute(
            "SELECT ref_id, parent_id, meta FROM refs WHERE ref_id = ANY(%s)",
            (ids,),
        ).fetchall()
    }
    ancestors = conn.execute(
        """
        WITH RECURSIVE up(job_id, ref_id, depth) AS (
            SELECT j.ref_id, p.ref_id, 1
              FROM refs j
              JOIN refs p ON p.ref_id = j.parent_id
             WHERE j.ref_id = ANY(%s)
               AND p.kind = 'todo' AND p.retired_at IS NULL
            UNION ALL
            SELECT u.job_id, p.ref_id, u.depth + 1
              FROM up u
              JOIN refs cur ON cur.ref_id = u.ref_id
              JOIN refs p ON p.ref_id = cur.parent_id
             WHERE p.kind = 'todo' AND p.retired_at IS NULL
               AND u.depth < %s
        )
        SELECT job_id, ref_id, depth FROM up
        """,
        (ids, _MAX_ANCESTOR_DEPTH),
    ).fetchall()
    chain: dict[int, list[tuple[int, int]]] = {}
    for jid, anc, depth in ancestors:
        jid, anc, depth = int(jid), int(anc), int(depth)
        chain.setdefault(jid, []).append((depth, anc))
        if depth == 1:
            todo_parent[jid] = anc
    ancestor_ids = sorted({anc for pairs in chain.values() for _d, anc in pairs})
    serves_by_ancestor: dict[int, list[int]] = {}
    if ancestor_ids:
        for src, dst in conn.execute(
            "SELECT l.src_ref_id, l.dst_ref_id FROM links l "
            "JOIN refs q ON q.ref_id = l.dst_ref_id "
            "WHERE l.src_ref_id = ANY(%s) AND l.relation = 'serves' "
            "AND q.kind = 'quest' AND q.retired_at IS NULL",
            (ancestor_ids,),
        ).fetchall():
            serves_by_ancestor.setdefault(int(src), []).append(int(dst))
    structure_quests = quest_ids_for_structures(
        conn,
        [
            sid
            for j in jobs.values()
            if (sid := structure_ref_id_from_meta(j["meta"])) is not None
        ],
    )
    meta_qids = {
        qid
        for j in jobs.values()
        if (qid := quest_id_from_meta(j["meta"], structure_quests)) is not None
    }
    live_meta_quests = (
        {
            int(r[0])
            for r in conn.execute(
                "SELECT ref_id FROM refs WHERE ref_id = ANY(%s) "
                "AND kind = 'quest' AND retired_at IS NULL",
                (sorted(meta_qids),),
            ).fetchall()
        }
        if meta_qids
        else set()
    )
    for jid, job in jobs.items():
        resolved: int | None = None
        for _depth, anc in sorted(chain.get(jid, [])):
            served = serves_by_ancestor.get(anc)
            if served:
                resolved = min(served)
                break
        if resolved is None:
            qid = quest_id_from_meta(job["meta"], structure_quests)
            if qid in live_meta_quests:
                resolved = qid
        quests[jid] = resolved
    return quests, todo_parent


def resolve_job_quests(
    store: Store, job_ids: list[int], *, conn: Any | None = None
) -> dict[int, int | None]:
    """``{job_id: quest ref_id | None}`` — the chain first (nearest
    serving ancestor, lowest quest id on a tie), the job's meta as the
    fallback; ``None`` when neither names a live quest."""
    with _connect(store, conn) as c:
        quests, _todo_parent = _resolve(c, job_ids)
    return quests


class JobQuestSplit(NamedTuple):
    """:func:`classify_live_jobs`'s three buckets: ``by_quest`` groups
    resolved rows by quest ref_id; ``platform`` the unresolved,
    todo-parent-less :data:`PLATFORM_UPKEEP` rows; ``unassigned`` the
    rest — the health signal. Every row gains a ``todo_parent`` key
    (the job's live todo parent ref_id, else ``None``)."""

    by_quest: dict[int, list[dict[str, Any]]]
    unassigned: list[dict[str, Any]]
    platform: list[dict[str, Any]]


def classify_live_jobs(store: Store, *, conn: Any | None = None) -> JobQuestSplit:
    """Split :func:`live_jobs` by :func:`resolve_job_quests`'s verdict.

    ``platform`` = unresolved AND no live todo parent AND
    ``meta.job_type`` in :data:`PLATFORM_UPKEEP`; ``unassigned`` =
    unresolved and not platform. The work model and the /status health
    row share this one classifier so their counts cannot drift.
    """
    with _connect(store, conn) as c:
        rows = live_jobs(store, conn=c)
        quests, todo_parent = _resolve(c, [r["id"] for r in rows])
    by_quest: dict[int, list[dict[str, Any]]] = {}
    unassigned: list[dict[str, Any]] = []
    platform: list[dict[str, Any]] = []
    for r in rows:
        r["todo_parent"] = todo_parent.get(r["id"])
        qid = quests.get(r["id"])
        if qid is not None:
            by_quest.setdefault(qid, []).append(r)
        elif (
            r["todo_parent"] is None
            and (r["meta"] or {}).get("job_type") in PLATFORM_UPKEEP
        ):
            platform.append(r)
        else:
            unassigned.append(r)
    return JobQuestSplit(by_quest=by_quest, unassigned=unassigned, platform=platform)


__all__ = [
    "PLATFORM_QUEST_ID",
    "JobQuestSplit",
    "classify_live_jobs",
    "live_jobs",
    "resolve_job_quests",
]
