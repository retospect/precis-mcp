"""claude_inproc executor — claim a job and dispatch to its job_type.

Sibling-worker shape (per ADR 0017): no ``WorkerHandler`` subclass,
just a ``run_claude_inproc_pass`` function the CLI registers as a
``RefPass``. Each pass:

1. Claim up to ``limit`` ``kind='job'`` rows whose ``meta.executor``
   is ``'claude_inproc'``, tagged ``STATUS:queued`` (or whose
   lease has expired), not yet terminal.
2. For each claimed job: tag ``STATUS:running``, look up the
   ``job_type`` in the registry, invoke ``run(...)``, write the
   resulting summary + gripe comment chunks, transition status.
3. Failures are recorded as ``STATUS:failed`` + a ``job_event``
   chunk; the linked gripe (if any) rolls back to ``STATUS:open``.

Concurrency: ``FOR UPDATE OF r SKIP LOCKED`` on the claim so
multiple workers don't double-process. v1 ships only one runner
on one host, but the lock keeps us honest.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from precis.store.types import BlockInsert
from precis.workers.executors import EXECUTOR_PROVIDES
from precis.workers.job_types import get_job_type, known_job_types

log = logging.getLogger(__name__)


_EXECUTOR_NAME = "claude_inproc"

# Status tag values.
_STATUS_NAMESPACE = "STATUS"
_QUEUED = "queued"
_RUNNING = "running"
_SUCCEEDED = "succeeded"
_FAILED = "failed"
_CANCEL_REQUESTED = "cancel_requested"
_CANCELLED = "cancelled"

# Terminal STATUS values — a row carrying any of these is not
# claimable.
_TERMINAL = (_SUCCEEDED, _FAILED, _CANCELLED)

# Chunk kinds the executor writes.
_JOB_EVENT_KIND = "job_event"
_JOB_SUMMARY_KIND = "job_summary"
_GRIPE_COMMENT_KIND = "gripe_comment"


# ── Claim ─────────────────────────────────────────────────────────


def _claim_jobs(
    conn: Connection, *, limit: int
) -> list[tuple[int, str, dict[str, Any]]]:
    """Lock up to ``limit`` claimable claude_inproc jobs.

    Claimable = ``kind='job'``, executor matches, ``STATUS:queued``,
    not terminal, lease expired or absent.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")

    rows = conn.execute(
        """
        SELECT r.ref_id, r.title, r.meta
          FROM refs r
         WHERE r.kind = 'job'
           AND r.deleted_at IS NULL
           AND r.meta->>'executor' = %s
           AND EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = %s
                    AND t.value = %s
               )
           AND NOT EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = %s
                    AND t.value = ANY(%s)
               )
           AND (
                (r.meta->>'lease_until') IS NULL
             OR (r.meta->>'lease_until')::timestamptz < now()
           )
         ORDER BY r.ref_id
         LIMIT %s
           FOR UPDATE OF r SKIP LOCKED
        """,
        (
            _EXECUTOR_NAME,
            _STATUS_NAMESPACE,
            _QUEUED,
            _STATUS_NAMESPACE,
            list(_TERMINAL),
            limit,
        ),
    ).fetchall()
    return [(int(r[0]), str(r[1]), dict(r[2] or {})) for r in rows]


def _linked_gripe_id(store: Any, job_ref_id: int) -> int | None:
    """Find the gripe this job links to via ``rel='fixes'``."""
    links = store.links_for(job_ref_id, direction="out")
    fixes = [l for l in links if l.relation == "fixes"]
    if not fixes:
        return None
    endpoints = store.fetch_refs_by_ids({l.dst_ref_id for l in fixes})
    for link in fixes:
        target = endpoints.get(link.dst_ref_id)
        if target is not None and target.kind == "gripe":
            return int(target.id)
    return None


# ── Status helpers ────────────────────────────────────────────────


def _set_status(
    store: Any, ref_id: int, value: str, *, conn: Connection | None = None
) -> None:
    """Replace the current ``STATUS:`` tag with ``value`` on ``ref_id``."""
    from precis.store import Tag

    tag = Tag.parse_strict(f"STATUS:{value}")
    store.add_tag(
        ref_id,
        tag,
        set_by="agent",
        replace_prefix=True,
        conn=conn,
    )


def _is_cancel_requested(conn: Connection, ref_id: int) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
         WHERE rt.ref_id = %s
           AND t.namespace = %s
           AND t.value = %s
         LIMIT 1
        """,
        (ref_id, _STATUS_NAMESPACE, _CANCEL_REQUESTED),
    ).fetchone()
    return row is not None


def _append_chunk(
    store: Any,
    ref_id: int,
    chunk_kind: str,
    text: str,
    *,
    conn: Connection | None = None,
) -> None:
    """Append a chunk at the next ``ord`` for the ref.

    When ``conn`` is provided we count via that connection so back-to-
    back appends inside the same tx see each other's INSERTs. The
    previous implementation called ``store.list_blocks_for_ref`` which
    opens its own pool connection — uncommitted INSERTs in ``conn``
    were invisible, leading to two calls computing the same
    ``next_pos`` and a unique-constraint violation on
    ``(ref_id, ord)``.
    """
    if conn is not None:
        row = conn.execute(
            "SELECT COALESCE(MAX(ord) + 1, 0) FROM chunks "
            "WHERE ref_id = %s AND ord >= 0",
            (ref_id,),
        ).fetchone()
        next_pos = int(row[0]) if row and row[0] is not None else 0
    else:
        blocks = store.list_blocks_for_ref(ref_id)
        next_pos = len(blocks)
    store.insert_blocks(
        ref_id,
        [BlockInsert(pos=next_pos, text=text, meta={"chunk_kind": chunk_kind})],
        conn=conn,
    )


def _set_meta(conn: Connection, ref_id: int, **fields: Any) -> None:
    """Merge ``fields`` into ``refs.meta``."""
    conn.execute(
        "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
        (Jsonb(fields), ref_id),
    )


# ── Pass entry point ──────────────────────────────────────────────


def run_claude_inproc_pass(store: Any, *, limit: int = 4) -> dict[str, int]:
    """Process up to ``limit`` claude_inproc jobs.

    Returns ``{claimed, ok, failed}`` for runner aggregation.
    Smaller default ``limit`` than chunk-level workers because each
    job runs a multi-minute LLM subprocess; we want the loop to
    yield often.
    """
    # Stage 1: claim under a short tx. Lease must be written
    # before we release the FOR UPDATE lock so concurrent runners
    # don't double-claim.
    with store.pool.connection() as conn:
        rows = _claim_jobs(conn, limit=limit)
        if not rows:
            conn.commit()
            return {"claimed": 0, "ok": 0, "failed": 0}
        for ref_id, _title, _meta in rows:
            conn.execute(
                "UPDATE refs SET meta = meta || "
                "jsonb_build_object("
                "  'lease_until', (now() + interval '30 minutes')::text"
                ") "
                "WHERE ref_id = %s",
                (ref_id,),
            )
            _set_status(store, ref_id, _RUNNING, conn=conn)
        conn.commit()

    ok = 0
    failed = 0
    for ref_id, title, meta in rows:
        try:
            _run_one(store, ref_id, title, meta)
            ok += 1
        except Exception as exc:  # pragma: no cover — defensive
            failed += 1
            log.warning("claude_inproc: job %d raised: %s", ref_id, exc, exc_info=True)
            try:
                with store.pool.connection() as conn:
                    _append_chunk(
                        store,
                        ref_id,
                        _JOB_EVENT_KIND,
                        f"runner: uncaught exception: {exc!r}",
                        conn=conn,
                    )
                    _set_status(store, ref_id, _FAILED, conn=conn)
                    conn.commit()
            except Exception:  # pragma: no cover
                log.warning("claude_inproc: failed to record failure", exc_info=True)
    return {"claimed": len(rows), "ok": ok, "failed": failed}


# ── Per-job dispatch ──────────────────────────────────────────────


def _run_one(store: Any, ref_id: int, title: str, meta: dict[str, Any]) -> None:
    """Dispatch a single claimed job to its job_type handler."""
    job_type_name = meta.get("job_type")
    if not job_type_name:
        _record_failure(
            store,
            ref_id,
            "missing meta.job_type",
            gripe_rollback=None,
        )
        return
    spec = get_job_type(str(job_type_name))
    if spec is None:
        _record_failure(
            store,
            ref_id,
            f"unknown job_type {job_type_name!r}; known: {known_job_types()}",
            gripe_rollback=None,
        )
        return

    # Cooperative cancel check before doing real work.
    with store.pool.connection() as conn:
        if _is_cancel_requested(conn, ref_id):
            _append_chunk(
                store,
                ref_id,
                _JOB_EVENT_KIND,
                "runner: cancel requested before run",
                conn=conn,
            )
            _set_status(store, ref_id, _CANCELLED, conn=conn)
            conn.commit()
            return

    if spec.name == "fix_gripe":
        _run_fix_gripe(store, ref_id, spec)
    elif spec.name == "plan_tick":
        _run_plan_tick(store, ref_id, spec)
    else:  # pragma: no cover
        _record_failure(
            store,
            ref_id,
            f"no dispatcher for job_type {spec.name!r}",
            gripe_rollback=None,
        )


def _run_plan_tick(store: Any, ref_id: int, spec: Any) -> None:
    """plan_tick dispatch: invoke the planner LLM under a parent todo.

    The job's ``parent_id`` points at the todo being worked on; the
    planner reads body + ancestry + completed child summaries and
    decides on subtasks / yield / done. Status writes the job row;
    no side effects on a hypothetical "linked gripe" — plan_tick
    parents are todos, not gripes.
    """
    parent_id = _parent_todo_id(store, ref_id)
    if parent_id is None:
        _record_failure(
            store,
            ref_id,
            "plan_tick job has no parent todo",
            gripe_rollback=None,
        )
        return

    # ``meta.params`` carries the model (synthesized from the parent's
    # ``LLM:<value>`` tag at dispatch time). Pull it from the job ref.
    params = _job_params(store, ref_id)

    t0 = time.perf_counter()
    try:
        outcome = spec.run(
            store=store,
            job_ref_id=ref_id,
            parent_ref_id=parent_id,
            params=params,
        )
    except Exception as exc:
        wall = time.perf_counter() - t0
        with store.pool.connection() as conn:
            _append_chunk(
                store,
                ref_id,
                _JOB_EVENT_KIND,
                f"runner: plan_tick raised after {wall:.1f}s: {exc!r}",
                conn=conn,
            )
            _set_status(store, ref_id, _FAILED, conn=conn)
            _set_meta(conn, ref_id, wall_seconds=wall)
            conn.commit()
            from precis.handlers._job_bubble import bubble_job_failure

            bubble_job_failure(store, ref_id)
        return

    from precis.utils.tick_conclusion import parse as parse_tick_conclusion

    conclusion = parse_tick_conclusion(outcome.stdout or "")

    with store.pool.connection() as conn:
        _append_chunk(
            store, ref_id, _JOB_SUMMARY_KIND, outcome.stdout or "(no output)",
            conn=conn,
        )
        # Structured per-tick audit chunk — slim, grepable summary of
        # what the LLM did. Replaces dumping raw stdout into the
        # parent's re-tick prompt. Builds from the worker_logs query
        # over MCP tool calls correlated by parent_todo. When the LLM
        # included a structured tick-conclusion block at the tail of
        # stdout, its verdict + one-paragraph summary go at the top of
        # this chunk so the parent re-tick reads the synth first.
        result_text = _build_job_result_text(
            store=store,
            job_ref_id=ref_id,
            parent_ref_id=parent_id,
            model=spec.name,  # actually plan_tick; model is in meta.params.model
            exit_code=outcome.exit_code,
            duration_s=outcome.duration_s,
            conclusion=conclusion,
        )
        _append_chunk(store, ref_id, "job_result", result_text, conn=conn)
        if outcome.stderr:
            _append_chunk(
                store, ref_id, _JOB_EVENT_KIND,
                f"stderr ({len(outcome.stderr)} chars):\n{outcome.stderr[:4000]}",
                conn=conn,
            )
        _set_meta(conn, ref_id, wall_seconds=outcome.duration_s)
        if outcome.exit_code == 0:
            _set_status(store, ref_id, _SUCCEEDED, conn=conn)
        else:
            _set_status(store, ref_id, _FAILED, conn=conn)
            conn.commit()
            from precis.handlers._job_bubble import bubble_job_failure

            bubble_job_failure(store, ref_id)
            return
        conn.commit()


def _build_job_result_text(
    *,
    store: Any,
    job_ref_id: int,
    parent_ref_id: int,
    model: str,
    exit_code: int,
    duration_s: float,
    conclusion: Any = None,
) -> str:
    """Render the structured ``chunk_kind='job_result'`` audit text.

    Pulls counts from the DB: files written under the parent's
    workspace during this tick (via ref_events / put-time tagging),
    citations + findings + child todos minted with the project tag.
    Cheap query, runs in the worker's connection.
    """
    # Job timing
    with store.pool.connection() as conn:
        cur = conn.execute(
            "SELECT created_at, updated_at FROM refs WHERE ref_id = %s",
            (job_ref_id,),
        ).fetchone()
        if cur is None:
            ts_started, ts_finished = "?", "?"
        else:
            ts_started, ts_finished = str(cur[0]), str(cur[1])
        # Workspace path & project tag from parent
        meta_cur = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s",
            (parent_ref_id,),
        ).fetchone()
        ws_path = ""
        if meta_cur and meta_cur[0]:
            ws_block = meta_cur[0].get("workspace")
            if isinstance(ws_block, dict):
                ws_path = ws_block.get("path") or ""
        project_tag = ""
        if ws_path:
            project_tag = (
                "project:" + ws_path.rstrip("/").split("/")[-1]
            )
        # Counts under the parent during this tick window
        cit_count = 0
        finding_count = 0
        child_count = 0
        if project_tag:
            cit_count = int(
                conn.execute(
                    """
                    SELECT count(*) FROM refs r
                      JOIN ref_tags rt ON rt.ref_id = r.ref_id
                      JOIN tags t ON t.tag_id = rt.tag_id
                     WHERE r.kind = 'citation' AND r.deleted_at IS NULL
                       AND t.namespace = 'OPEN' AND t.value = %s
                       AND r.created_at >= %s
                    """,
                    (project_tag, ts_started),
                ).fetchone()[0]
            )
            finding_count = int(
                conn.execute(
                    """
                    SELECT count(*) FROM refs r
                      JOIN ref_tags rt ON rt.ref_id = r.ref_id
                      JOIN tags t ON t.tag_id = rt.tag_id
                     WHERE r.kind = 'finding' AND r.deleted_at IS NULL
                       AND t.namespace = 'OPEN' AND t.value = %s
                       AND r.created_at >= %s
                    """,
                    (project_tag, ts_started),
                ).fetchone()[0]
            )
        child_count = int(
            conn.execute(
                "SELECT count(*) FROM refs WHERE parent_id = %s AND kind = 'todo' "
                "AND deleted_at IS NULL AND created_at >= %s",
                (parent_ref_id, ts_started),
            ).fetchone()[0]
        )
    # Build the text — terse, structured. When the LLM emitted a
    # tick-conclusion block, its synth lives at the top so the parent
    # re-tick reads it before the counts.
    lines: list[str] = []
    if conclusion is not None:
        if conclusion.verdict:
            lines.append(f"verdict (LLM): {conclusion.verdict}")
        if conclusion.summary:
            lines.append("summary (LLM):")
            for ln in conclusion.summary.splitlines():
                lines.append(f"  {ln}")
        if conclusion.files:
            lines.append("files (LLM-claimed): " + ", ".join(conclusion.files))
        if lines:
            lines.append("")
    lines.extend(
        [
            f"ts: {ts_started} → {ts_finished}",
            f"job: #{job_ref_id}  parent: #{parent_ref_id}  model: {model}",
            f"duration: {duration_s:.1f}s  exit: {exit_code}",
            "",
            "Produced this tick:",
            f"  - subtasks minted: {child_count}",
            f"  - citations minted: {cit_count}",
            f"  - findings minted: {finding_count}",
        ]
    )
    if exit_code == 0:
        lines.append("verdict (runner): succeeded")
    else:
        lines.append("verdict (runner): failed")
    return "\n".join(lines)


def _parent_todo_id(store: Any, job_ref_id: int) -> int | None:
    """Return the parent todo id of a job ref, or None when orphaned."""
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT p.ref_id
              FROM refs j
              JOIN refs p ON p.ref_id = j.parent_id
             WHERE j.ref_id = %s
               AND p.kind = 'todo'
               AND p.deleted_at IS NULL
            """,
            (job_ref_id,),
        ).fetchone()
    return int(row[0]) if row else None


def _job_params(store: Any, job_ref_id: int) -> dict[str, Any]:
    """Pull ``meta.params`` from a job ref as a plain dict."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->'params' FROM refs WHERE ref_id = %s",
            (job_ref_id,),
        ).fetchone()
    if row is None or row[0] is None:
        return {}
    return dict(row[0])


def _run_fix_gripe(store: Any, ref_id: int, spec: Any) -> None:
    """fix_gripe dispatch: find the linked gripe, invoke, transition."""
    gripe_id = _linked_gripe_id(store, ref_id)
    if gripe_id is None:
        _record_failure(
            store,
            ref_id,
            "fix_gripe job has no link='gripe:<id>' rel='fixes'",
            gripe_rollback=None,
        )
        return

    t0 = time.perf_counter()
    try:
        outcome = spec.run(store=store, job_id=ref_id, gripe_id=gripe_id)
    except Exception as exc:
        wall = time.perf_counter() - t0
        with store.pool.connection() as conn:
            _append_chunk(
                store,
                ref_id,
                _JOB_EVENT_KIND,
                f"runner: job_type raised after {wall:.1f}s: {exc!r}",
                conn=conn,
            )
            _set_status(store, ref_id, _FAILED, conn=conn)
            _set_meta(conn, ref_id, wall_seconds=wall)
            # Roll gripe back to open per failure-rollback policy.
            _set_status(store, gripe_id, "open", conn=conn)
            _append_chunk(
                store,
                gripe_id,
                _GRIPE_COMMENT_KIND,
                f"[worker:job:{ref_id}] fix attempt crashed: {exc!r}",
                conn=conn,
            )
            conn.commit()
        return

    with store.pool.connection() as conn:
        _append_chunk(store, ref_id, _JOB_SUMMARY_KIND, outcome.summary_text, conn=conn)
        _append_chunk(
            store, gripe_id, _GRIPE_COMMENT_KIND, outcome.gripe_comment_text, conn=conn
        )
        _set_meta(
            conn,
            ref_id,
            wall_seconds=outcome.wall_seconds,
            branch=outcome.branch,
            sha=outcome.sha,
        )
        if outcome.status == "succeeded":
            _set_status(store, ref_id, _SUCCEEDED, conn=conn)
            _set_status(store, gripe_id, "in_review", conn=conn)
        else:
            _set_status(store, ref_id, _FAILED, conn=conn)
            _set_status(store, gripe_id, "open", conn=conn)
            # Slice-5 failure-bubble: tag the parent todo if any.
            # Inside the same tx so the status + bubble commit
            # together; orphan jobs (legacy, no parent_id) just no-op.
            from precis.handlers._job_bubble import bubble_job_failure

            bubble_job_failure(store, ref_id, conn=conn)
        conn.commit()


def _record_failure(
    store: Any,
    ref_id: int,
    reason: str,
    *,
    gripe_rollback: int | None,
) -> None:
    """Tag a job ``STATUS:failed`` with a reason event chunk."""
    with store.pool.connection() as conn:
        _append_chunk(store, ref_id, _JOB_EVENT_KIND, reason, conn=conn)
        _set_status(store, ref_id, _FAILED, conn=conn)
        if gripe_rollback is not None:
            _set_status(store, gripe_rollback, "open", conn=conn)
        # Slice-5 failure-bubble — see _finalise comment above.
        from precis.handlers._job_bubble import bubble_job_failure

        bubble_job_failure(store, ref_id, conn=conn)
        conn.commit()


__all__ = ["EXECUTOR_PROVIDES", "run_claude_inproc_pass"]
