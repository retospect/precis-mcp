"""The cause-based breaker: a lane or host whose newest builds all die of
one engine cause stops taking new builds (Reto 2026-11-16: parked work
never holds a seat and no count of parked chains blocks the factory — the
cause does).

**Trip.** The newest ``K`` ended ``run_slice`` jobs of one lane (or of one
host, ``params.target_node``) — ``K`` is ``factory.gripe_dispatch.
breaker_parks``, default 5 — all failed, all for the same engine cause
(:func:`job_cause`), across at least two build nodes (one node burning its
own ladder is the build's failure, not the engine's). A succeeded build in
the window breaks the streak. Jobs at or below the floor a closed breaker
ask recorded do not count (:func:`lane_floors`).

**Cause.** No new taxonomy; the classifiers the factory already has, in
this order: the lane's host refusal (``meta.host_refusal``, gr571395); the
lane-auth allowlist (:func:`precis.utils.llm.failure.lane_auth_key` —
logged out, invalid key, authentication error); an HTTP 401/403 in the
failure text (the MCP preflight's "HTTP 401 from …"); the router's failure
classes (:func:`precis.utils.llm.failure.classify_text` — quota, rate,
budget, transport); and ``meta.failure_class == 'infra'`` — the engine
class of :data:`precis.workers.factory_rescue.BOUNDS` (a missing sandbox
image or bwrap failure surfaces here when the executor stamps it infra).
Anything else — a red test, a no-commit exit — is a task cause and never
trips.

**Effect.** One ask-user todo names the scope, the cause and the job ids
(:func:`open_trip`); while it is open :func:`tripped_lanes` names the
lanes it holds out and :func:`precis.plans.lanes.lane_backoffs` reports
them backed off — every picker (``pick_lane``, the retry ladder, the
rescue pass, ``run_slice``'s submit) already skips a backed-off lane — and
``gripe_dispatch`` drops their cap from the capacity it mints against.
Every other lane and host keeps dispatching. Closing the ask resets the
breaker: only jobs newer than the trip's last job count again.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from precis import settings as psettings
from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
from precis.todo import TERMINAL_TODO_STATUSES

log = logging.getLogger(__name__)

#: Registry key and default of ``K`` (:func:`breaker_parks`).
BREAKER_PARKS_KEY = "factory.gripe_dispatch.breaker_parks"
DEFAULT_BREAKER_PARKS = 5
#: ``meta`` key on the trip's ask todo: ``{scope, name, cause, jobs, lanes,
#: last_job, k}``.
BREAKER_KEY = "breaker"
#: Ended ``run_slice`` job STATUS values that count toward a streak.
_ENDED = ("failed", "timed-out", "succeeded")
#: Newest ended jobs read per pass — a bound, far above ``K`` x lanes.
_WINDOW = 600

_HTTP_AUTH = re.compile(r"\bHTTP(?: Error)? (401|403)\b")
_ENGINE_CLASSES = frozenset({"quota", "rate", "budget", "transport"})


def breaker_parks(store: Any) -> int:
    """``factory.gripe_dispatch.breaker_parks``; unset or below 1 reads
    :data:`DEFAULT_BREAKER_PARKS`."""
    raw = psettings.get_int(BREAKER_PARKS_KEY, store=store)
    return raw if raw is not None and raw >= 1 else DEFAULT_BREAKER_PARKS


def job_cause(meta: dict[str, Any], text: str) -> str | None:
    """The engine cause of one failed ``run_slice`` job, or ``None`` when
    its failure is a task cause (see the module docstring for the order)."""
    from precis.utils.llm import failure as _failure

    if meta.get("host_refusal"):
        return "host-refusal"
    auth = _failure.lane_auth_key(text)
    if auth is not None:
        return f"auth:{auth}"
    http = _HTTP_AUTH.search(text)
    if http is not None:
        return f"http-{http.group(1)}"
    cls = _failure.classify_text(text).reason_class
    if cls in _ENGINE_CLASSES:
        return str(cls)
    if meta.get("failure_class") == "infra":
        return "infra"
    return None


@dataclass(frozen=True, slots=True)
class Trip:
    """One tripped scope: ``scope`` is ``lane`` or ``host``, ``lanes`` the
    lanes it holds out, ``jobs`` the ``K`` job ids that tripped it."""

    scope: str
    name: str
    cause: str
    jobs: tuple[int, ...]
    todos: tuple[int, ...]
    lanes: tuple[str, ...]


def _open_breaker_asks(store: Any) -> list[tuple[int, dict[str, Any]]]:
    """``(ask id, meta.breaker)`` of every breaker ask still open."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.meta->'breaker' FROM refs r
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND jsonb_typeof(r.meta->'breaker') = 'object'
               AND NOT EXISTS (SELECT 1 FROM ref_tags st
                                 JOIN tags s ON s.tag_id = st.tag_id
                                WHERE st.ref_id = r.ref_id
                                  AND s.namespace = 'STATUS'
                                  AND s.value = ANY(%s))
            """,
            (sorted(TERMINAL_TODO_STATUSES),),
        ).fetchall()
    return [(int(r[0]), dict(r[1])) for r in rows]


def tripped_lanes(store: Any) -> set[str]:
    """Lanes an open breaker ask holds out of dispatch."""
    return {
        str(lane)
        for _ask, b in _open_breaker_asks(store)
        for lane in (b.get("lanes") or ())
    }


def lane_floors(store: Any) -> dict[str, int]:
    """``{lane: newest job id}`` a CLOSED breaker ask covered — closing the
    ask resets the breaker for every lane it held out (a host ask covers
    each lane it listed, so the same old jobs never re-trip it under
    another scope); only newer jobs count again."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT lane.value, max((r.meta->'breaker'->>'last_job')::bigint)
              FROM refs r,
                   jsonb_array_elements_text(r.meta->'breaker'->'lanes') AS lane(value)
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND jsonb_typeof(r.meta->'breaker') = 'object'
               AND EXISTS (SELECT 1 FROM ref_tags st
                             JOIN tags s ON s.tag_id = st.tag_id
                            WHERE st.ref_id = r.ref_id
                              AND s.namespace = 'STATUS' AND s.value = ANY(%s))
             GROUP BY lane.value
            """,
            (sorted(TERMINAL_TODO_STATUSES),),
        ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows if r[1] is not None}


def _failure_text(store: Any, job_id: int) -> str:
    """The job's newest ``job_event`` chunk — the failure reason."""
    try:
        chunks = [
            c
            for c in store.chunks.list_chunks_for_ref(job_id)
            if c.chunk_kind == "job_event" and c.text
        ]
    except Exception:
        return ""
    return str(max(chunks, key=lambda c: (c.ord, c.id)).text) if chunks else ""


def _ended_jobs(store: Any) -> list[tuple[int, int | None, str, str | None, str, dict]]:
    """``(job id, todo id, lane, host, status, meta)`` of the newest ended
    ``run_slice`` jobs, newest first."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.parent_id, r.meta->>'lane',
                   r.meta->'params'->>'target_node', s.value, r.meta
              FROM refs r
              JOIN ref_tags st ON st.ref_id = r.ref_id
              JOIN tags s ON s.tag_id = st.tag_id
                         AND s.namespace = 'STATUS' AND s.value = ANY(%s)
             WHERE r.kind = 'job' AND r.retired_at IS NULL
               AND r.meta->>'job_type' = 'run_slice'
               AND r.meta ? 'lane'
             ORDER BY r.ref_id DESC
             LIMIT %s
            """,
            (list(_ENDED), _WINDOW),
        ).fetchall()
    return [
        (
            int(r[0]),
            int(r[1]) if r[1] is not None else None,
            str(r[2]),
            str(r[3]) if r[3] else None,
            str(r[4]),
            dict(r[5] or {}),
        )
        for r in rows
    ]


def detect(store: Any, k: int | None = None) -> list[Trip]:
    """Every scope whose newest ``k`` ended builds share one engine cause
    and has no open breaker ask yet. Lane scopes first; a host scope whose
    lanes a lane trip (or an open ask) already holds out is not reported
    again."""
    k = breaker_parks(store) if k is None else k
    jobs = _ended_jobs(store)
    held = tripped_lanes(store)
    floors = lane_floors(store)
    open_scopes = {
        (str(b.get("scope")), str(b.get("name")))
        for _ask, b in _open_breaker_asks(store)
    }
    trips: list[Trip] = []
    for scope, pick in (("lane", lambda j: j[2]), ("host", lambda j: j[3])):
        groups: dict[str, list[tuple[Any, ...]]] = {}
        for j in jobs:
            name = pick(j)
            if name:
                groups.setdefault(name, []).append(j)
        for name, rows in sorted(groups.items()):
            if (scope, name) in open_scopes:
                continue
            window = [j for j in rows if j[0] > floors.get(j[2], 0)][:k]
            if len(window) < k or any(j[4] == "succeeded" for j in window):
                continue
            causes = {job_cause(j[5], _failure_text(store, j[0])) for j in window}
            if len(causes) != 1 or None in causes:
                continue
            todos = tuple(sorted({j[1] for j in window if j[1] is not None}))
            if len(todos) < 2:
                continue
            lanes = tuple(sorted({j[2] for j in window}))
            if scope == "host" and set(lanes) <= held:
                continue
            trips.append(
                Trip(
                    scope,
                    name,
                    str(next(iter(causes))),
                    tuple(sorted(j[0] for j in window)),
                    todos,
                    lanes,
                )
            )
            held |= set(lanes)
    return trips


def open_trip(store: Any, trip: Trip, k: int) -> int:
    """File the ONE ask-user todo for ``trip`` and return its id."""
    handles = ", ".join(f"jo{j}" for j in trip.jobs)
    where = f"{trip.scope} {trip.name}"
    title = (
        f"{where}: the newest {k} builds all died of {trip.cause} — new builds "
        "stopped there"
    )
    body = (
        f"The newest {k} ended `run_slice` builds on {where} all failed for one "
        f"engine cause, `{trip.cause}`, across {len(trip.todos)} build nodes "
        f"({', '.join(f'td{t}' for t in trip.todos)}): {handles}. That is the "
        f"machinery, not the work, so the factory stopped dispatching new builds "
        f"onto {'lane(s) ' + ', '.join(trip.lanes) if trip.scope == 'host' else 'that lane'} "
        "— every other lane and host keeps dispatching, and parked work stays "
        "saved on its branch. Fix the cause (credential, image, sandbox, "
        "host), then close this ask: closing it resets the breaker and "
        "dispatch resumes there; only builds newer than "
        f"jo{max(trip.jobs)} count toward a new trip."
    )
    meta: dict[str, Any] = {
        "ask": {
            "kind": "ruling",
            "options": ["fixed — resume dispatch there"],
            "blocks": "nothing",
            "context": f"{where}: {trip.cause} x{k} ({handles})"[:600],
        },
        BREAKER_KEY: {
            "scope": trip.scope,
            "name": trip.name,
            "cause": trip.cause,
            "jobs": list(trip.jobs),
            "lanes": list(trip.lanes),
            "last_job": max(trip.jobs),
            "k": k,
        },
    }
    from precis.todo.guards import check_ask_meta

    check_ask_meta(meta)
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="todo", slug=None, title=title, meta=meta, conn=conn
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        for value in ("ask-user", "showstopper"):
            store.add_tag(ref.id, Tag.open(value), set_by="system", conn=conn)
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": TODO_BODY_KIND})],
            conn=conn,
        )
    log.warning("lane_breaker: %s tripped on %s (%s)", where, trip.cause, handles)
    return int(ref.id)
