"""Agent-run log — the write side of ``kind='agentlog'``.

A small, store-only surface that records *who wrote what*. Peer to the
agent-facing :class:`precis.handlers.agentlog.AgentLogHandler` (the
read / triage side), and a structural twin of :mod:`precis.alerts`: a
numeric-id, NOT-embedded ref produced by machinery, not hand-authored.

Where an *alert* is a machine-detected condition, an *agentlog* is a
*run* — one record per agentic pass (a ``plan_tick`` coroutine, an
operator-requested change, a chat follow-up) that touches the corpus.
It captures:

* the full **assembled prompt** the run was handed (``meta.prompt``) —
  the thing the transcript view always wanted but the job ref never
  stored separately;
* the **model + source** (``meta.model`` / ``meta.source``) and the
  owning todo / job (``meta.parent_ref_id`` / ``meta.job_ref_id``);
* a ``touched`` **link to every chunk the run wrote or moved**, attached
  lazily by the editing handlers via :func:`touch_from_env` reading
  ``PRECIS_CURRENT_AGENTLOG`` off the subprocess env (same back-door
  pattern as ``PRECIS_CURRENT_TODO``).

So a chunk that "looks wrong" walks back through ``chunk_connections``
to the exact run that produced it, and the run's prompt + transcript
become the debugging surface.

Lifecycle: there is none to speak of — an agentlog is opened at run
start, finalized at run end, and reaped by the sweeper past a retention
window (:func:`gc_stale_logs`), which drops the ``touched`` links but
never the chunks. Like alerts, agentlogs are intentionally NOT embedded
(body lives in ``title`` + ``meta``, no ``card_combined``), so the
embed / chunk_keywords workers skip them and they never reach semantic
search.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from typing import Any

from precis.store import Store
from precis.store.types import Tag

log = logging.getLogger(__name__)

#: Env var the runner sets on the ``claude -p`` subprocess so the MCP
#: server inside it can attribute every draft write to this run. Parallel
#: to ``PRECIS_CURRENT_TODO``.
ENV_VAR = "PRECIS_CURRENT_AGENTLOG"

#: Default retention window (days) before the sweeper reaps an agentlog
#: and its ``touched`` links. Overridable via env in the sweeper.
RETENTION_DAYS = 30


def open_log(
    store: Store,
    *,
    source: str,
    title: str,
    model: str | None = None,
    prompt: str | None = None,
    parent_ref_id: int | None = None,
    job_ref_id: int | None = None,
    meta_extra: dict[str, Any] | None = None,
) -> int:
    """Open an agentlog at the start of a run. Returns the ref id.

    ``source`` is the coarse producer (``"plan_tick"``, ``"operator"``,
    ``"chat"``) and becomes both an ``agentlog-source:<source>`` open tag
    and ``meta.source``. ``prompt`` is the full assembled text the run
    was handed — stored verbatim in ``meta.prompt`` so the transcript
    view can show exactly what the agent saw. The id is meant to be
    threaded to the worker subprocess via :data:`ENV_VAR`.
    """
    meta: dict[str, Any] = {
        "source": source,
        "started_at": _now_iso(store),
    }
    if model is not None:
        meta["model"] = model
    if prompt is not None:
        meta["prompt"] = prompt
    if parent_ref_id is not None:
        meta["parent_ref_id"] = int(parent_ref_id)
    if job_ref_id is not None:
        meta["job_ref_id"] = int(job_ref_id)
    if meta_extra:
        meta.update(meta_extra)
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="agentlog", slug=None, title=title, meta=meta, conn=conn
        )
        store.add_tag(
            ref.id, Tag.open(f"agentlog-source:{source}"), set_by="system", conn=conn
        )
        return int(ref.id)


def attach_touch(
    store: Store,
    *,
    log_id: int,
    chunk_ids: Iterable[int],
) -> int:
    """Link an agentlog to each chunk it wrote/moved (by ``chunk_id``).

    One ``touched`` link per (run, chunk) edge; idempotent (the link
    unique constraint dedups a re-touch within the same run). Returns the
    number of edges attached. We translate each ``chunk_id`` to its
    owning ref + ``ord`` and reuse :meth:`Store.add_link` (which resolves
    ``(ref, ord)`` back to a stable ``chunk_id`` at insert time) rather
    than reimplementing the links insert — DraftChunk exposes
    ``chunk_id`` but not ``ord``, so the round-trip is the DRY bridge.
    """
    ids = [int(c) for c in chunk_ids]
    if not ids:
        return 0
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, ord FROM chunks WHERE chunk_id = ANY(%s)",
            (ids,),
        ).fetchall()
    n = 0
    for ref_id, ord_ in rows:
        store.add_link(
            src_ref_id=log_id,
            dst_ref_id=int(ref_id),
            dst_pos=int(ord_),
            relation="touched",
            set_by="agent",
        )
        n += 1
    return n


def touch_from_env(store: Store, *, chunk_ids: Iterable[int]) -> None:
    """Best-effort: attribute the given chunks to the current run.

    Reads :data:`ENV_VAR` off the environment (set by the runner on the
    ``claude -p`` subprocess); a no-op when unset / unparseable so an
    operator console edit or a test that didn't open a log just skips
    attribution. Swallows its own errors — attribution must never fail
    an edit (same contract as the abbrev / citation hints).
    """
    log_id = current_from_env()
    if log_id is None:
        return
    try:
        attach_touch(store, log_id=log_id, chunk_ids=chunk_ids)
    except Exception:
        log.warning(
            "agentlog: touch attribution failed for log %d", log_id, exc_info=True
        )


def finalize_log(
    store: Store,
    *,
    log_id: int,
    status: str | None = None,
    result: str | None = None,
    meta_extra: dict[str, Any] | None = None,
) -> None:
    """Stamp run-end state onto an open agentlog.

    Patches ``meta`` with ``ended_at`` plus any caller-supplied
    ``status`` / ``result`` / extras. The bulky transcript stays on the
    owning ``kind='job'`` ref (one hop via ``meta.job_ref_id``); the
    agentlog keeps only the prompt + small result so its GC is cheap.
    """
    patch: dict[str, Any] = {"ended_at": _now_iso(store)}
    if status is not None:
        patch["status"] = status
    if result is not None:
        patch["result"] = result
    if meta_extra:
        patch.update(meta_extra)
    with store.tx() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || %s::jsonb, updated_at = now() "
            "WHERE ref_id = %s AND kind = 'agentlog'",
            (json.dumps(patch), int(log_id)),
        )


def gc_stale_logs(store: Store, *, older_than_days: int = RETENTION_DAYS) -> int:
    """Reap agentlogs (and their ``touched`` links) past the retention
    window. Returns the number of agentlogs soft-deleted.

    Deletes the ``touched`` link rows outright — they're pure
    attribution, worthless once the run record is gone — but NEVER the
    chunks they point at (links carry no FK / cascade to ``refs``; the
    body chunks are append-only and survive). The agentlog ref is
    soft-deleted (``deleted_at``) like any other ref, keeping the row
    for forensics while dropping it from every live view.

    Cheap: one DELETE on links + one UPDATE on refs per pass.
    """
    with store.tx() as conn:
        ids = [
            int(r[0])
            for r in conn.execute(
                "SELECT ref_id FROM refs "
                "WHERE kind = 'agentlog' AND deleted_at IS NULL "
                "  AND created_at < now() - %s::interval",
                (f"{int(older_than_days)} days",),
            ).fetchall()
        ]
        if not ids:
            return 0
        # Drop the attribution edges (either endpoint — 'touched' is
        # symmetric and could in principle be inserted from either side).
        conn.execute(
            "DELETE FROM links "
            "WHERE relation = 'touched' "
            "  AND (src_ref_id = ANY(%s) OR dst_ref_id = ANY(%s))",
            (ids, ids),
        )
        conn.execute(
            "UPDATE refs SET deleted_at = now(), updated_at = now() "
            "WHERE ref_id = ANY(%s)",
            (ids,),
        )
        return len(ids)


def list_recent(store: Store, *, limit: int = 200) -> list[dict[str, Any]]:
    """Recent agentlogs, newest-first, with source / model / counters.

    Shared read used by the ``/agentlogs`` web tab. Pure SQL — no
    embedder, no handler. ``touched`` is the count of chunks the run
    wrote/moved (its blast radius)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id,
                   r.title,
                   r.meta->>'source'        AS source,
                   r.meta->>'model'         AS model,
                   r.meta->>'status'        AS status,
                   r.meta->>'parent_ref_id' AS parent_ref_id,
                   r.meta->>'job_ref_id'    AS job_ref_id,
                   r.created_at,
                   (SELECT count(*) FROM links l
                     WHERE l.relation = 'touched' AND l.src_ref_id = r.ref_id)
                     AS touched
              FROM refs r
             WHERE r.kind = 'agentlog'
               AND r.deleted_at IS NULL
             ORDER BY r.created_at DESC
             LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "ref_id": int(r[0]),
            "title": r[1],
            "source": r[2],
            "model": r[3],
            "status": r[4],
            "parent_ref_id": int(r[5]) if r[5] is not None else None,
            "job_ref_id": int(r[6]) if r[6] is not None else None,
            "created_at": r[7],
            "touched": int(r[8]),
        }
        for r in rows
    ]


def current_from_env() -> int | None:
    """Return the current run's agentlog id from :data:`ENV_VAR`, or None.

    Parallel to :func:`precis.utils.workspace.current_todo_from_env`:
    the runner sets it on the ``claude -p`` subprocess env; the MCP
    server inside that subprocess reads it here when attributing draft
    writes. ``None`` on unset / non-integer / non-positive — attribution
    silently skipped rather than erroring."""
    raw = os.environ.get(ENV_VAR)
    if not raw or not raw.strip():
        return None
    try:
        val = int(raw.strip())
    except ValueError:
        log.warning("%s rejected (must be int): %r", ENV_VAR, raw)
        return None
    return val if val > 0 else None


def _now_iso(store: Store) -> str:
    """ISO8601 'now' from the DB clock (no host-clock skew across nodes)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT to_char(now(), 'YYYY-MM-DD\"T\"HH24:MI:SSOF')"
        ).fetchone()
    return str(row[0]) if row else ""


__all__ = [
    "ENV_VAR",
    "RETENTION_DAYS",
    "attach_touch",
    "current_from_env",
    "finalize_log",
    "gc_stale_logs",
    "list_recent",
    "open_log",
    "touch_from_env",
]
