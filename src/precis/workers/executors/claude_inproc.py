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
from typing import TYPE_CHECKING, Any

from psycopg import Connection

from precis.workers.executors import EXECUTOR_PROVIDES
from precis.workers.executors._common import (
    CANCELLED as _CANCELLED,
)
from precis.workers.executors._common import (
    FAILED as _FAILED,
)
from precis.workers.executors._common import (
    JOB_EVENT_KIND as _JOB_EVENT_KIND,
)
from precis.workers.executors._common import (
    JOB_SUMMARY_KIND as _JOB_SUMMARY_KIND,
)
from precis.workers.executors._common import (
    RUNNING as _RUNNING,
)
from precis.workers.executors._common import (
    SUCCEEDED as _SUCCEEDED,
)
from precis.workers.executors._common import (
    append_chunk as _append_chunk,
)
from precis.workers.executors._common import (
    claim_executor_jobs,
)
from precis.workers.executors._common import (
    is_cancel_requested as _is_cancel_requested,
)
from precis.workers.executors._common import (
    record_failure as _record_failure,
)
from precis.workers.executors._common import (
    set_meta as _set_meta,
)
from precis.workers.executors._common import (
    set_status as _set_status,
)
from precis.workers.job_types import get_job_type, known_job_types

if TYPE_CHECKING:
    from precis.workers.executors._context import DispatchContext

log = logging.getLogger(__name__)


_EXECUTOR_NAME = "claude_inproc"

# Chunk kind specific to this executor's gripe-comment timeline.
_GRIPE_COMMENT_KIND = "gripe_comment"


# ── Claim ─────────────────────────────────────────────────────────


def _claim_jobs(
    conn: Connection, *, limit: int
) -> list[tuple[int, str, dict[str, Any]]]:
    """Lock up to ``limit`` claimable claude_inproc jobs."""
    return claim_executor_jobs(conn, executor=_EXECUTOR_NAME, limit=limit)


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

    # Plugin job_types declare their own ``dispatch`` callable.
    # Built-ins (fix_gripe, plan_tick) leave ``spec.dispatch`` as
    # ``None`` and fall through to the in-tree switch below.
    if spec.dispatch is not None:
        ctx = _build_dispatch_context(store, ref_id, title, meta)
        spec.dispatch(ctx, spec)
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


def _build_dispatch_context(
    store: Any, ref_id: int, title: str, meta: dict[str, Any]
) -> DispatchContext:
    """Construct a DispatchContext closing over executor helpers.

    Each closure opens its own short-lived DB connection so the
    plugin dispatcher doesn't have to thread a transaction handle
    through its logic. The cost is one connection round-trip per
    call, which matches what the in-tree built-in dispatchers
    (``_run_fix_gripe`` / ``_run_plan_tick``) already pay.
    """
    from precis.workers.executors._context import DispatchContext

    def _ctx_set_status(value: str) -> None:
        with store.pool.connection() as conn:
            _set_status(store, ref_id, value, conn=conn)
            conn.commit()

    def _ctx_append_chunk(kind: str, text: str) -> None:
        with store.pool.connection() as conn:
            _append_chunk(store, ref_id, kind, text, conn=conn)
            conn.commit()

    def _ctx_set_meta(**fields: Any) -> None:
        with store.pool.connection() as conn:
            _set_meta(conn, ref_id, **fields)
            conn.commit()

    def _ctx_record_failure(reason: str) -> None:
        # ``gripe_rollback=None`` — plugin dispatchers don't have
        # the fix_gripe gripe-link convention. Plugins that DO
        # need a side-effect rollback can do it explicitly via
        # set_status against the linked ref.
        _record_failure(store, ref_id, reason, gripe_rollback=None)

    def _ctx_is_cancel_requested() -> bool:
        with store.pool.connection() as conn:
            return _is_cancel_requested(conn, ref_id)

    return DispatchContext(
        store=store,
        ref_id=ref_id,
        title=title,
        meta=meta,
        set_status=_ctx_set_status,
        append_chunk=_ctx_append_chunk,
        set_meta=_ctx_set_meta,
        record_failure=_ctx_record_failure,
        is_cancel_requested=_ctx_is_cancel_requested,
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
            store,
            ref_id,
            _JOB_SUMMARY_KIND,
            outcome.stdout or "(no output)",
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
                store,
                ref_id,
                _JOB_EVENT_KIND,
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
            project_tag = "project:" + ws_path.rstrip("/").split("/")[-1]
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


__all__ = ["EXECUTOR_PROVIDES", "run_claude_inproc_pass"]
