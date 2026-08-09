"""claude_inproc executor — claim a job and dispatch to its job_type.

Sibling-worker shape: no ``WorkerHandler`` subclass,
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
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from psycopg import Connection

from precis.quest import review_guard
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
    TERMINAL as _TERMINAL,
)
from precis.workers.executors._common import (
    append_chunk as _append_chunk,
)
from precis.workers.executors._common import (
    claim_executor_jobs,
)
from precis.workers.executors._common import (
    current_status as _current_status,
)
from precis.workers.executors._common import (
    is_cancel_requested as _is_cancel_requested,
)
from precis.workers.executors._common import (
    poison_guard as _poison_guard,
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
    """Lock up to ``limit`` claimable claude_inproc jobs.

    ``reclaim_stale_running=True`` (the lease-epoch reclaim arm): the
    dispatch runs entirely in-process (a ``claude -p`` subprocess this
    worker owns), so a worker restart mid-tick means the compute is
    genuinely dead — same "worker death = compute death" assumption
    ``ssh_node`` was written under. Before this, a bounced worker's
    plan_tick/fix_gripe job sat ``STATUS:running`` for the full 90-min
    lease before anything reclaimed it (the "bounce wedge", gr187627's
    generalization). The epoch arm now reclaims it on the FIRST post-
    restart claim pass, no matter which host restarts; the expiry arm
    still covers a same-generation hang.
    """
    return claim_executor_jobs(
        conn, executor=_EXECUTOR_NAME, limit=limit, reclaim_stale_running=True
    )


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


def _inproc_concurrency() -> int:
    """How many claimed jobs to run **in parallel** within one pass.

    Each tick is a blocking ``claude -p`` subprocess (releases the GIL),
    so a small thread pool gives real parallelism and drains the queue
    faster. Default 1 (sequential — the historical behaviour); raise via
    ``PRECIS_INPROC_CONCURRENCY`` on a beefy worker. Clamped to [1, 16].
    Spend scales with concurrency: each tick is still bounded by the
    per-tick cost cap in ``call_claude_agent`` / the daily ceiling, so
    this knob trades parallelism for burn-rate, not for cost safety."""
    try:
        n = int(os.environ.get("PRECIS_INPROC_CONCURRENCY", "1"))
    except ValueError:
        return 1
    return max(1, min(16, n))


def _run_job_safe(store: Any, ref_id: int, title: str, meta: dict[str, Any]) -> bool:
    """Run one claimed job; record + swallow any failure. Returns ok."""
    try:
        _run_one(store, ref_id, title, meta)
        return True
    except Exception as exc:  # pragma: no cover — defensive
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
        return False


def run_claude_inproc_pass(store: Any, *, limit: int = 4) -> dict[str, int]:
    """Process up to ``limit`` claude_inproc jobs.

    Returns ``{claimed, ok, failed}`` for runner aggregation.
    Smaller default ``limit`` than chunk-level workers because each
    job runs a multi-minute LLM subprocess; we want the loop to
    yield often.

    With ``PRECIS_INPROC_CONCURRENCY>1`` the claimed batch runs in a
    thread pool (each tick is a blocking subprocess) so several ticks
    drain at once; the claim count widens to cover the pool.
    """
    concurrency = _inproc_concurrency()
    claim_n = max(limit, concurrency)
    # Stage 1: claim under a short tx. Lease must be written
    # before we release the FOR UPDATE lock so concurrent runners
    # don't double-claim.
    poisoned = 0
    to_run: list[tuple[int, str, dict[str, Any]]] = []
    with store.pool.connection() as conn:
        rows = _claim_jobs(conn, limit=claim_n)
        if not rows:
            conn.commit()
            return {"claimed": 0, "ok": 0, "failed": 0}
        for ref_id, title, meta in rows:
            # Crash-loop guard (§H piece 3): the bump (expiry-only) + the
            # reclaim classification already happened inside
            # claim_executor_jobs; this just applies the cap the way
            # ssh_node's original guard did. Was a known interim gap
            # ("ZERO attempt cap of its own") since claude_inproc's
            # reclaim_stale_running landed — now closed.
            if _poison_guard(store, conn, ref_id, meta):
                poisoned += 1
                continue
            # Lease must outlive the longest possible job. A plan_tick
            # tick can request up to ``timeout_s=3600`` (60 min, per
            # plan_tick.PARAMS_SCHEMA), and the executor does extra work
            # (writing summary / result chunks) after the subprocess
            # returns. A 30-min lease was SHORTER than a long tick, so
            # the lease could expire mid-run and a second worker could
            # re-claim and double-run the job. 90 min covers the max
            # tick + post-processing with margin; the only cost is that
            # a genuinely crashed worker's job stays `running` a bit
            # longer before another worker rescues it (latency, not
            # correctness — crashes are rare).
            conn.execute(
                "UPDATE refs SET meta = meta || "
                "jsonb_build_object("
                "  'lease_until', (now() + interval '90 minutes')::text"
                ") "
                "WHERE ref_id = %s",
                (ref_id,),
            )
            _set_status(store, ref_id, _RUNNING, conn=conn)
            to_run.append((ref_id, title, meta))
        conn.commit()

    if not to_run:
        return {"claimed": len(rows), "ok": 0, "failed": poisoned}

    # Stage 2: run the claimed jobs. Sequential by default; a thread pool
    # when concurrency>1 (each _run_one blocks on a subprocess that
    # releases the GIL, so threads parallelise the wall-clock).
    pool_size = min(concurrency, len(to_run))
    if pool_size <= 1:
        results = [
            _run_job_safe(store, rid, title, meta) for rid, title, meta in to_run
        ]
    else:
        with ThreadPoolExecutor(max_workers=pool_size) as ex:
            results = list(
                ex.map(
                    lambda r: _run_job_safe(store, r[0], r[1], r[2]),
                    to_run,
                )
            )
    ok = sum(1 for r in results if r)
    return {
        "claimed": len(rows),
        "ok": ok,
        "failed": (len(results) - ok) + poisoned,
    }


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

    # Honor the job's per-todo permission envelope (slice 8) for the whole
    # dispatch: any ``call_claude_agent`` reached inside picks the box up via
    # the executor-scoped active envelope, so its tier-1 deny list applies
    # "regardless of host". Absent/permissive envelope → today's behavior.
    # (fix_gripe, §H cycle a, is the one exception: its shape — full FS
    # access, no DB at all — doesn't fit the write axis's "no-write ⇒ no FS
    # writes either" coupling, so it builds and passes its OWN explicit
    # envelope to ``call_claude_agent`` rather than picking up this
    # job-scoped one; see ``fix_gripe._spawn_claude``.)
    from precis.workers.envelope import envelope_scope, parse_envelope

    with envelope_scope(parse_envelope(meta)):
        # Plugin job_types declare their own ``dispatch`` callable.
        # Built-ins (fix_gripe, plan_tick) leave ``spec.dispatch`` as
        # ``None`` and fall through to the in-tree switch below.
        if spec.dispatch is not None:
            ctx = _build_dispatch_context(store, ref_id, title, meta)
            spec.dispatch(ctx, spec)
            # Plugin dispatchers signal *failure* explicitly
            # (``ctx.record_failure`` → ``STATUS:failed``) and *cancellation*
            # via ``ctx.set_status``, but they leave the happy-path transition
            # to the executor: they do their work, append a summary, and return
            # still ``RUNNING``. Mark the job SUCCEEDED here unless the
            # dispatcher already drove it terminal. Without this the job lingers
            # ``running`` until the 1h stuck-job sweeper reaps it as
            # ``claim-orphaned`` → ``failed`` — which, for a recurring
            # ``news_poll`` / ``briefing``, bubbles onto the spawned child and
            # wedges the schedule spawner ("previous still open").
            _finalize_plugin_dispatch(store, ref_id)
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

    def _ctx_record_failure(reason: str, *, failure_class: str | None = None) -> None:
        # ``gripe_rollback=None`` — plugin dispatchers don't have
        # the fix_gripe gripe-link convention. Plugins that DO
        # need a side-effect rollback can do it explicitly via
        # set_status against the linked ref.
        _record_failure(
            store, ref_id, reason, gripe_rollback=None, failure_class=failure_class
        )

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


def _finalize_plugin_dispatch(store: Any, ref_id: int) -> None:
    """Drive a plugin-dispatched job to ``SUCCEEDED`` after a clean run.

    Only transitions a job still in a non-terminal state — a dispatcher
    that already recorded a failure (``STATUS:failed``) or cancellation
    is left untouched. Idempotent and race-free: ``dispatch`` guarantees
    one runner per job, so the read-then-write needs no extra locking.
    """
    with store.pool.connection() as conn:
        if _current_status(conn, ref_id) not in _TERMINAL:
            _set_status(store, ref_id, _SUCCEEDED, conn=conn)
            conn.commit()


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

    # Review-ledger writeback (rung 3b) snapshot — captured BEFORE the
    # LLM runs so a clean completion can tell "reviewer approved" from
    # "reviewer edited the chunk itself" (a future authoring reviewer
    # must not self-approve prose it just wrote). One cheap meta read for
    # every tick; the anchor resolve + sha snapshot only run when this
    # parent IS a review-mode todo.
    review_pass: tuple[str, int, str | None] | None = None
    review_meta = _review_meta(store, parent_id)
    if review_meta is not None:
        lens, anchor = review_meta
        snap = _anchor_chunk_snapshot(store, anchor, lens)
        if snap is not None:
            review_pass = (lens, snap[0], snap[1])

    # ``meta.params`` carries the model (synthesized from the parent's
    # ``meta.llm_tier`` at dispatch time). Pull it from the job ref.
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

    from precis.utils.claude_agent import stream_final_text
    from precis.utils.tick_conclusion import parse as parse_tick_conclusion

    # ``outcome.stdout`` is now the full stream-json message stream (every
    # turn + tool call/result). The final assistant text is lifted from the
    # trailing result event (falls back to raw stdout on text/stub output),
    # and the WHOLE stream is stored as ``meta.transcript`` for debugging
    # (capped; GC'd by age — see workers/sweeper). The conclusion parser
    # and job_summary continue to see the final text, as before.
    raw_stream = outcome.stdout or ""
    final_text = stream_final_text(raw_stream)
    conclusion = parse_tick_conclusion(final_text)
    # A tick cut off mid-flight by an *exhaustion* — the --max-turns
    # ceiling or the wall-clock timeout — is *resumable*, not failed: a
    # fresh tick continues with a new budget. Don't bubble it as a hard
    # failure (which parks the parent out of the rotation); mark it
    # terminal-but-non-blocking so dispatch re-mints a fresh tick next
    # sweep — bounded by a per-parent streak cap so a tick that *always*
    # runs out can't loop forever burning spend. See CHANGELOG.
    resume_reason = _resume_reason(outcome, raw_stream)

    with store.pool.connection() as conn:
        _append_chunk(
            store,
            ref_id,
            _JOB_SUMMARY_KIND,
            final_text or "(no output)",
            conn=conn,
        )
        # Full LLM transcript for the Tasks-view debugger. Capped at 1 MiB
        # so a runaway tick can't bloat refs.meta; stored on the job ref
        # (not a chunk → never embedded, no migration).
        _TRANSCRIPT_CAP = 1_000_000
        transcript = raw_stream[:_TRANSCRIPT_CAP]
        if len(raw_stream) > _TRANSCRIPT_CAP:
            transcript += "\n…(truncated)"
        if transcript:
            _set_meta(conn, ref_id, transcript=transcript)
        # Structured per-tick audit chunk — slim, grepable summary of
        # what the LLM did. Replaces dumping raw stdout into the
        # parent's re-tick prompt. Builds from the worker_logs query
        # over MCP tool calls correlated by parent_todo. When the LLM
        # included a structured tick-conclusion block at the tail of
        # stdout, its verdict + one-paragraph summary go at the top of
        # this chunk so the parent re-tick reads the synth first.
        # Resolve the resume verdict before rendering the audit chunk so
        # the job_result reads honestly ("resumed (timeout)" vs "failed").
        # The streak read+write happens in this same tx.
        cap = _resume_streak_cap()
        if resume_reason is not None:
            streak = _bump_resume_streak(conn, parent_id)
            resume = streak <= cap
        else:
            _reset_resume_streak(conn, parent_id)
            streak = 0
            resume = False
        result_text = _build_job_result_text(
            store=store,
            job_ref_id=ref_id,
            parent_ref_id=parent_id,
            model=spec.name,  # actually plan_tick; model is in meta.params.model
            exit_code=outcome.exit_code,
            duration_s=outcome.duration_s,
            conclusion=conclusion,
            resume=(resume_reason, streak, cap) if resume_reason else None,
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
        if outcome.exit_code == 0 or resume:
            # Clean tick, or a resumable exhaustion (max-turns / timeout)
            # under the cap: mark succeeded (terminal + non-blocking) so
            # dispatch re-mints a fresh tick. child_job_succeeded is
            # guarded for meta.llm_tier-set parents, so this never
            # auto-closes the parent.
            if resume:
                _set_meta(
                    conn,
                    ref_id,
                    resumed_reason=resume_reason,
                    resume_streak=streak,
                )
                _append_chunk(
                    store,
                    ref_id,
                    _JOB_EVENT_KIND,
                    f"runner: tick hit {resume_reason} (resumable, streak "
                    f"{streak}/{cap}); not bubbling — a fresh tick will "
                    f"continue next dispatch sweep.",
                    conn=conn,
                )
            _set_status(store, ref_id, _SUCCEEDED, conn=conn)
        else:
            # A real failure, or a resumable exhaustion past the cap. The
            # latter (resume_reason is not None) is a *structured,
            # self-diagnosed* signal — "this leaf is too wide for one
            # tick" — distinct from a hard crash/tool error, so it gets one
            # shot at auto-recovery before parking (gripe 168886 tier 2):
            # mint a single narrowly-scoped decompose tick instead of
            # bubbling. A hard failure (resume_reason is None) always
            # bubbles immediately, same as before; a *repeat*
            # streak-exhaustion after the decompose attempt also bubbles
            # (the guardrail is one attempt per parent, not a loop).
            if resume_reason is not None and not _decompose_already_attempted(
                conn, parent_id
            ):
                child_id = _mint_auto_decompose(
                    store,
                    conn,
                    parent_id,
                    model=str(params.get("model") or "sonnet"),
                    streak=streak,
                    cap=cap,
                )
                _append_chunk(
                    store,
                    ref_id,
                    _JOB_EVENT_KIND,
                    f"runner: tick hit {resume_reason} {streak} consecutive "
                    f"times (cap {cap}) — not bubbling yet; auto-minted a "
                    f"narrowly-scoped decompose tick (job #{child_id}) to "
                    f"split this leaf into smaller subtasks instead. One "
                    f"auto-decompose attempt per parent — a repeat failure "
                    f"escalates to a real park.",
                    conn=conn,
                )
                _set_status(store, ref_id, _FAILED, conn=conn)
                conn.commit()
                return
            if resume_reason is not None:
                _append_chunk(
                    store,
                    ref_id,
                    _JOB_EVENT_KIND,
                    f"runner: tick hit {resume_reason} {streak} consecutive "
                    f"times (cap {cap}) — bubbling as a real failure "
                    f"(auto-decompose already attempted for this parent). "
                    f"The task likely needs splitting into smaller subtasks.",
                    conn=conn,
                )
            _set_status(store, ref_id, _FAILED, conn=conn)
            conn.commit()
            from precis.handlers._job_bubble import bubble_job_failure

            bubble_job_failure(store, ref_id)
            return
        # Rung 3b writeback fires ONLY for a genuinely-finished review pass:
        # a clean, non-resumed tick that concluded ``verdict: done``. A
        # resumed exhaustion (``resume`` — max-turns / timeout under the cap)
        # or a ``continue``/``yield``/``halt`` verdict means the reviewer did
        # NOT finish the section, so recording an "approved" row would falsely
        # mark an unchecked chunk clean — the one thing the memoized ledger
        # must never do (a *missing* row is cheap: the chunk is simply
        # re-reviewed next fanout; a *false* approval hides an unreviewed
        # section behind a green ✓).
        if (
            review_pass is not None
            and not resume
            and conclusion is not None
            and conclusion.verdict == "done"
        ):
            lens, chunk_id, sha_before = review_pass
            _maybe_record_review_pass(
                store,
                conn,
                review_todo_id=parent_id,
                lens=lens,
                chunk_id=chunk_id,
                sha_before=sha_before,
            )
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
    resume: tuple[str, int, int] | None = None,
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
    elif resume is not None:
        reason, streak, cap = resume
        if streak <= cap:
            lines.append(
                f"verdict (runner): resumed (hit {reason}; streak "
                f"{streak}/{cap}) — a fresh tick continues next sweep"
            )
        else:
            lines.append(
                f"verdict (runner): failed (hit {reason} {streak} times, "
                f"cap {cap}) — split this into smaller subtasks"
            )
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


# ── Review-ledger writeback (rung 3b — pass-only, "no record = not
# passed" per docs/backlog/paper-writing-pipeline.md §"Review — the
# memoized approval ledger") ───────────────────────────────────────────
#
# A review-mode plan_tick's parent todo carries meta.review=<lens> +
# meta.anchor='dc<id>' (the same shape predicates.has_review/has_anchor
# read). On a clean SUCCEEDED completion — zero filed findings AND the
# anchor chunk's content_sha unchanged since the tick started — record
# store.record_review(chunk_id, lens, verdict='approved'). Any findings,
# or a sha that moved (the reviewer edited the chunk itself — future
# authoring reviewers must not self-approve prose they just wrote),
# records nothing: the chunk correctly stays "requires review".
#
# The `toc` lens (item 10, document-altitude review) pins its approval to
# the draft's TOC digest instead of the anchor chunk's content_sha — a
# heading add/remove/rename/reorder moves the digest (no self-approval,
# same mechanism as any other lens's sha check), but a paragraph body edit
# leaves the digest untouched (approval still records — deliberate: the
# toc lens judges outline shape, not prose).


def _review_meta(store: Any, parent_id: int) -> tuple[str, str] | None:
    """``(lens, anchor)`` when ``parent_id`` is a review-mode todo (its
    ``refs.meta`` carries both ``review`` and ``anchor``), else ``None``.

    One cheap read, run for *every* plan_tick — this is the entire
    "is this tick reviewer mode?" cost a non-review tick pays."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->>'review', meta->>'anchor' FROM refs WHERE ref_id = %s",
            (parent_id,),
        ).fetchone()
    if row is None or not row[0] or not row[1]:
        return None
    return (row[0], row[1])


def _anchor_chunk_snapshot(
    store: Any, anchor: str, lens: str
) -> tuple[int, str | None] | None:
    """Resolve a ``dc<id>`` anchor to ``(chunk_id, sha_before)`` via the
    shared handle resolver (``store.get_draft_chunk`` — same lookup
    ``predicates.py``'s anchor handling relies on), or ``None`` when the
    anchor doesn't resolve to a live chunk.

    For every lens but ``toc``, ``sha_before`` is the anchor chunk's own
    ``content_sha``. For ``toc`` (item 10 — document-altitude review) the
    approval isn't pinned to any one chunk's text: it's pinned to the
    draft's :meth:`~precis.store._draft_ops.DraftOps.toc_digest`, captured
    here at tick start so the writeback can tell a heading
    add/remove/rename/reorder (digest moved — no self-approval) from a
    paragraph body edit (digest unchanged — approval still stands)."""
    chunk = store.get_draft_chunk(anchor)
    if chunk is None:
        return None
    if lens == "toc":
        return (chunk.chunk_id, store.toc_digest(chunk.ref_id))
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT content_sha FROM chunks WHERE chunk_id = %s", (chunk.chunk_id,)
        ).fetchone()
    return (chunk.chunk_id, row[0] if row is not None else None)


def _maybe_record_review_pass(
    store: Any,
    conn: Connection,
    *,
    review_todo_id: int,
    lens: str,
    chunk_id: int,
    sha_before: str | None,
) -> None:
    """Record a ``chunk_review`` "approved" verdict for a clean review
    tick — the reviewer filed nothing AND the anchor's watermark
    (the chunk's ``content_sha`` for every lens but ``toc``; the draft's
    TOC digest for ``toc``, item 10) is unchanged since the tick started.
    Called from ``_run_plan_tick``'s success path (gated on a non-resumed
    ``verdict: done`` tick), after ``_set_status(..., _SUCCEEDED, ...)``
    and before ``conn.commit()``.

    "Filed nothing" spans BOTH representations a reviewer uses: a
    ``kind='finding'`` child of the review-todo (the lens skills'
    ``put(kind='finding')`` shape) AND an anchored change-request
    ``kind='todo'`` on the chunk (``meta.anchor=dc<id>`` — the shape the
    ``precis-draft-reviewer`` persona files, which is NOT a child of the
    review-todo). Either an open finding of either kind → no approval, so
    a reviewer that raised an issue never also stamps the chunk clean.

    CRITICAL: this runs in the hot executor path for every SUCCEEDED
    plan_tick that is in review mode. Any failure here must never fail
    the job or abort the commit — swallow and log."""
    try:
        finding_row = conn.execute(
            "SELECT count(*) FROM refs WHERE parent_id = %s AND kind = 'finding' "
            "AND deleted_at IS NULL",
            (review_todo_id,),
        ).fetchone()
        if finding_row is not None and int(finding_row[0]) > 0:
            return  # findings filed as kind='finding' children — requires review
        # The draft-reviewer persona files each finding as an anchored
        # change-request kind='todo' (meta.anchor=dc<id>), NOT a
        # kind='finding' child — so also skip approval when this chunk carries
        # any OPEN (not done / won't-do) anchored change-request, matched
        # across the dc<id> / base58-handle / legacy ¶handle anchor forms.
        # Conservative by design: an open request from any lens or a human
        # blocks the auto-approval, erring toward "requires review". Shared
        # with the incremental fanout's skip-unsettled check —
        # ``quest.review_guard``.
        if review_guard.has_open_change_request(conn, chunk_id):
            return  # an open anchored change-request — requires review
        if lens == "toc":
            # The toc lens's watermark is the draft's TOC digest, not this
            # (or any single) chunk's content_sha — resolve the owning ref
            # via the anchored chunk, recompute the digest now, and compare
            # against the one captured at tick start. A heading rename/
            # reorder/add/remove moves the digest (no self-approval); a
            # paragraph body edit does not (approval still records — the
            # deliberate item-10 semantic difference from chunk lenses).
            ref_row = conn.execute(
                "SELECT ref_id FROM chunks WHERE chunk_id = %s", (chunk_id,)
            ).fetchone()
            if ref_row is None:
                return
            digest_now = store.toc_digest(int(ref_row[0]))
            if digest_now != sha_before:
                return  # a heading moved — no self-approval
            conn.execute(
                """INSERT INTO chunk_review (chunk_id, checker, approved_sha, verdict)
                        VALUES (%s, %s, %s, %s)
                   ON CONFLICT (chunk_id, checker) DO UPDATE
                          SET approved_sha = EXCLUDED.approved_sha,
                              verdict = EXCLUDED.verdict,
                              at = now()""",
                (chunk_id, lens, digest_now, "approved"),
            )
            return
        sha_row = conn.execute(
            "SELECT content_sha FROM chunks WHERE chunk_id = %s", (chunk_id,)
        ).fetchone()
        sha_now = sha_row[0] if sha_row is not None else None
        if sha_now != sha_before:
            return  # the reviewer edited the chunk itself — no self-approval
        store.record_review(chunk_id, lens, verdict="approved")
    except Exception:
        log.exception(
            "review writeback failed (review_todo=%s chunk=%s lens=%s) — "
            "swallowing; job status/commit is unaffected",
            review_todo_id,
            chunk_id,
            lens,
        )


#: plan_tick's wall-clock timeout sentinel exit code (set by
#: ``PlanTickOutcome`` on ``subprocess.TimeoutExpired``).
_TIMEOUT_EXIT_CODE = 124


def _resume_reason(outcome: Any, raw_stream: str) -> str | None:
    """Classify a non-zero tick outcome as a *resumable exhaustion* — or
    ``None`` for a clean tick / real error.

    Three exhaustions are resumable (the coroutine was cut off mid-flight,
    a fresh tick continues): the ``--max-turns`` ceiling (a trailing
    ``error_max_turns`` result event), the ``--max-budget-usd`` cap (a
    trailing budget result event — plan_tick sets this cap), and
    the wall-clock timeout (the process was killed, so there's no result
    event — detected by the ``PlanTickOutcome`` timeout sentinel exit code).
    Each is bounded by the same per-parent streak cap so a tick that *always*
    runs out escalates (and splits) instead of looping forever.

    A non-claude transport (the in-process OSS ``tools=`` tick, the LLM routing seam
    unit-4b) emits no stream-json for the parse below, so it sets an explicit
    ``outcome.resume_reason`` instead — honored verbatim here. The claude path
    leaves it ``None`` (the field defaults so), so its classification is the
    stream + exit-code logic below, unchanged/byte-identical."""
    from precis.utils.claude_agent import stream_terminal_reason

    explicit = getattr(outcome, "resume_reason", None)
    if explicit is not None:
        return explicit
    if outcome.exit_code == 0:
        return None
    reason = stream_terminal_reason(raw_stream)
    if reason == "max_turns":
        return "max_turns"
    if reason is not None and "budget" in reason:
        return "budget"
    if outcome.exit_code == _TIMEOUT_EXIT_CODE:
        return "timeout"
    return None


def _resume_streak_cap() -> int:
    """How many *consecutive* resumable-exhaustion ticks (max-turns or
    timeout) to auto-resume before bubbling as a real failure. Default
    3 — enough to ride out a tick that was simply mid-stride, low enough
    that a tick which can never fit in one budget gets escalated (and
    split) instead of looping and burning spend. Clamped to [1, 20].
    ``PRECIS_PLAN_TICK_MAX_TURNS_RESUMES`` is honoured as a back-compat
    alias for the cap."""
    raw = os.environ.get("PRECIS_PLAN_TICK_RESUME_CAP") or os.environ.get(
        "PRECIS_PLAN_TICK_MAX_TURNS_RESUMES", "3"
    )
    try:
        n = int(raw)
    except ValueError:
        n = 3
    return max(1, min(20, n))


def _bump_resume_streak(conn: Connection, parent_ref_id: int) -> int:
    """Increment + return the parent's consecutive resume streak
    (``meta.plan_tick_resume_streak``). One job runs per parent at a
    time (dispatch guarantees no live sibling job), so the read-modify-
    write needs no extra locking."""
    row = conn.execute(
        "SELECT COALESCE((meta->>'plan_tick_resume_streak')::int, 0) "
        "FROM refs WHERE ref_id = %s",
        (parent_ref_id,),
    ).fetchone()
    streak = (int(row[0]) if row and row[0] is not None else 0) + 1
    conn.execute(
        "UPDATE refs SET meta = jsonb_set("
        "COALESCE(meta, '{}'::jsonb), '{plan_tick_resume_streak}', "
        "to_jsonb(%s::int)) WHERE ref_id = %s",
        (streak, parent_ref_id),
    )
    return streak


def _reset_resume_streak(conn: Connection, parent_ref_id: int) -> None:
    """Drop the parent's resume streak — any non-exhaustion tick (clean
    exit or a real error) ends the run."""
    conn.execute(
        "UPDATE refs SET meta = COALESCE(meta, '{}'::jsonb) - "
        "'plan_tick_resume_streak' WHERE ref_id = %s "
        "AND meta ? 'plan_tick_resume_streak'",
        (parent_ref_id,),
    )


#: ``refs.meta`` key on a plan_tick parent marking that one auto-decompose
#: attempt (gripe 168886 tier 2) has already been minted for it. Read
#: before minting a second one — the guardrail is ONE attempt per parent,
#: ever, so a leaf that stalls again after decomposing escalates straight
#: to a real ``child-failed`` park instead of looping on the same "split
#: it up" prompt.
_DECOMPOSE_ATTEMPTED_KEY = "plan_tick_decompose_attempted"


def _decompose_already_attempted(conn: Connection, parent_ref_id: int) -> bool:
    """True once ``parent_ref_id`` has already had an auto-decompose tick
    minted for it (see :data:`_DECOMPOSE_ATTEMPTED_KEY`).

    Unlocked read-then-write, same as :func:`_bump_resume_streak` just above
    it: safe only because dispatch guarantees exactly one live job per
    parent at a time. A future change that lets multiple ``claude_inproc``
    workers claim ticks for the same parent concurrently would need to add
    locking here too.
    """
    row = conn.execute(
        "SELECT COALESCE((meta->>%s)::boolean, false) FROM refs WHERE ref_id = %s",
        (_DECOMPOSE_ATTEMPTED_KEY, parent_ref_id),
    ).fetchone()
    return bool(row[0]) if row else False


def _mint_auto_decompose(
    store: Any,
    conn: Connection,
    parent_ref_id: int,
    *,
    model: str,
    streak: int,
    cap: int,
) -> int:
    """Mint one narrowly-scoped ``plan_tick`` follow-up tick under
    ``parent_ref_id`` with ``params.decompose=True``, instead of bubbling a
    streak-exhaustion failure (gripe 168886 tier 2).

    Reuses the ``plan_tick`` job_type as-is — no new registry entry, no
    executor wiring — the decompose tick is just a normal plan_tick job the
    ``claude_inproc`` executor claims and runs like any other; the only
    difference is ``plan_tick.run`` prepending a forcing instruction to the
    user prompt (see :data:`~precis.workers.job_types.plan_tick._DECOMPOSE_INSTRUCTION`)
    that tells the planner to read the brief + the prior failed attempts
    (already visible in the normal prompt's "Children status" block — every
    earlier plan_tick job under this parent carries a ``job_result`` chunk)
    and mint 2-5 child subtasks instead of attempting the task itself.

    Stamps :data:`_DECOMPOSE_ATTEMPTED_KEY` on the parent (the guardrail)
    and resets the resume streak so the decompose tick starts clean.
    Shares ``conn`` with the caller's transaction so the mint, the flag,
    and the streak reset commit atomically with the failed job's own
    status flip.
    """
    from precis.store.types import Tag

    # Propagate the parent's prio onto the minted job, same as the normal
    # dispatch.py mint path (``_claim_and_dispatch``) — otherwise an urgent
    # (low-number) parent's decompose tick lands at the claim-order default
    # (COALESCE(prio, 5), _common.py::claim_executor_jobs, ASC = lower-first)
    # and queues behind every more-urgent prio<5 job, defeating the point of
    # an urgent auto-recovery.
    parent_prio_row = conn.execute(
        "SELECT prio FROM refs WHERE ref_id = %s", (parent_ref_id,)
    ).fetchone()
    parent_prio = (
        int(parent_prio_row[0])
        if parent_prio_row and parent_prio_row[0] is not None
        else None
    )

    child = store.insert_ref(
        kind="job",
        slug=None,
        title=f"plan_tick decompose (auto-mint under todo:{parent_ref_id})",
        meta={
            "job_type": "plan_tick",
            "executor": _EXECUTOR_NAME,
            "params": {"model": model, "decompose": True},
            "dispatched_from_todo": parent_ref_id,
        },
        parent_id=parent_ref_id,
        prio=parent_prio,
        conn=conn,
    )
    store.add_tag(
        child.id,
        Tag.closed("STATUS", "queued"),
        set_by="system",
        replace_prefix=True,
        conn=conn,
    )
    _set_meta(conn, parent_ref_id, **{_DECOMPOSE_ATTEMPTED_KEY: True})
    _reset_resume_streak(conn, parent_ref_id)
    store.append_event(
        parent_ref_id,
        source="claude_inproc",
        event="auto-decompose-minted",
        payload={"job_id": int(child.id), "streak": streak, "cap": cap},
        conn=conn,
    )
    log.info(
        "claude_inproc: parent #%d hit resume-streak exhaustion %d/%d — "
        "auto-minted decompose job #%d instead of bubbling",
        parent_ref_id,
        streak,
        cap,
        child.id,
    )
    return int(child.id)


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
        elif outcome.status == "skipped":
            # GLM/OpenRouter fleet-flip safety gate (backend=openai) — a
            # clean no-op, not a failure: no bubble, gripe just stays open
            # for a re-attempt once the backend reverts. Mirrors the
            # cooperative-cancel treatment above (STATUS:cancelled, no
            # failure bubble).
            _set_status(store, ref_id, _CANCELLED, conn=conn)
            _set_status(store, gripe_id, "open", conn=conn)
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
