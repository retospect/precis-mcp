"""Stuck-job sweeper — recovers cascades after orphaned claims.

A ``kind='job'`` ref that carries ``STATUS:running`` for longer than the
configured threshold without any subsequent status change is treated as
an orphan: its claimer (worker subprocess) is presumed dead and the
parent todo's ``child_job_succeeded`` auto_check is silently stuck.

**Lease guard.** A running job may still carry a live ``meta.lease_until``
— the executor writes one at claim time to cover the longest legitimate
run (``claude_inproc`` sets 90 min for a ``plan_tick``, which can request
a 60-min tick plus post-processing). A job whose lease has *not* yet
expired is, by contract, still owned by a live worker: sweeping it is a
false claim-orphaned that mints a spurious ``child-failed`` bubble and
(under the ``claude_inproc`` executor) races the still-running subprocess.
So the sweeper only fires when the lease is **absent or expired**, the
same predicate the reclaim path uses (:func:`claim_executor_jobs` in
``executors/_common.py``). The hours threshold then backstops lease-less
legacy jobs and adds margin past a just-expired lease.

The sweeper:

1. Selects rows where the *current* ``STATUS:`` value is ``running``, the
   ``ref_tags`` row that wrote that tag is older than
   :data:`STUCK_JOB_HOURS`, **and** ``meta.lease_until`` is null or past.
2. Replaces ``STATUS:running`` with ``STATUS:failed`` (via
   ``replace_prefix=True`` on the STATUS namespace).
3. Adds an ``OPEN:swept:claim-orphaned`` tag so the failure isn't
   mis-attributed to the executor.
4. Calls ``bubble_job_failure`` to tag the parent todo
   ``child-failed:<job_id>``. The bubble is normally fired from
   ``JobHandler.tag(STATUS:failed)``; the sweeper writes the tag at
   the store level (the handler isn't in scope here), so the bubble
   is called explicitly.
5. Appends a ``job-swept`` event so the audit trail is intact.

The transition is what wakes the cascade — the operator sees the
stuck parent in the nursery's "child-failed" surfacing and can
re-tick.

Configuration:

* ``PRECIS_STUCK_JOB_HOURS`` — float, default ``1.0``. Set higher for
  legitimately long opus passes; the planner-coroutine guardrails
  already cap per-tick wall-clock and cost.

Pass shape:

* SQL-only, idempotent (already-failed jobs never re-claim).
* Runs in the ``system`` worker profile alongside ``nursery`` and
  ``dispatch`` so every cluster node contributes; per-row
  ``FOR UPDATE OF r SKIP LOCKED`` dedups racing sweepers.
* Cheap (one SELECT + N UPDATEs per pass); the default rotation can
  run it every cycle without budget concern.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from precis.alerts import raise_alert
from precis.handlers._job_bubble import bubble_job_failure
from precis.store import Store
from precis.store.types import Tag
from precis.workers.executors._common import (
    effective_requires,
    release_job_reservation,
)
from precis.workers.runner import BatchResult

#: Cap the unschedulable scan so a huge queue can't make the per-minute
#: sweep expensive; a genuine capability outage trips the alert on the
#: first few jobs regardless.
_UNSCHEDULABLE_SCAN_CAP = 500

log = logging.getLogger(__name__)


def _stuck_job_hours() -> float:
    """Read the threshold from env, default 1.0h, floor 0.1h."""
    raw = os.environ.get("PRECIS_STUCK_JOB_HOURS")
    if raw is None:
        return 1.0
    try:
        val = float(raw)
    except ValueError:
        return 1.0
    return max(0.1, val)


STUCK_JOB_HOURS = _stuck_job_hours()

#: The chunk-embedding model whose transient failures the sweeper re-opens.
_EMBED_MODEL = "bge-m3"

#: ``last_error`` fragments (SQL ILIKE) that mark an embed ``status='failed'``
#: row as a *transient backend outage* — the embedder was down / overloaded /
#: hit a memory spike — rather than a per-chunk fault. Such a row is safe to
#: re-open for a fresh attempt. The mid-2026 embedder wedge/crash-loop outages
#: stamped ~92k rows ``all embedder endpoints failed`` this way; the root cause
#: is since fixed (``EmbedderUnavailable`` now *defers* the batch instead of
#: stranding rows), so these are legacy debris that will never re-fail
#: transiently again. A stranded embedding also blocks that chunk's KeyBERT
#: keywords (they score against the vector), so re-opening cascades to
#: un-stick the downstream ``chunk_keywords`` backlog for free.
_EMBED_TRANSIENT_ERROR_PATTERNS = (
    "%all embedder endpoints failed%",
    "%out of memory%",
    "%timeout%",
    "%unavailable%",
    "%connection%",
)

#: The llm-v1 gloss summarizer whose transient failures the sweeper re-opens —
#: parallel to the embed case.
_SUMMARIZE_LLM_MODEL = "llm-v1"

#: ``last_error`` fragment marking an llm-v1 ``chunk_summaries`` failure as a
#: *transient backend blank*: the shared 80B returned "" under contention and it
#: was recorded as ``empty summary``. Root cause since fixed (llm_summarize
#: retries in-process + a looser cross-pass cap, 2cd78cc7); the ~5k rows
#: stranded before that landed are legacy debris safe to re-summarize.
_SUMMARIZE_TRANSIENT_ERROR_PATTERNS = ("%empty summary%",)

#: Only re-open a failed artifact row while its ``attempts`` stay under this — a
#: genuinely-poison chunk (real dim-mismatch / always-blank) must still
#: terminate rather than loop forever. Transient outages no longer re-strand
#: under current code, so this only backstops the rare genuine per-row fault.
_REOPEN_MAX_ATTEMPTS = 6


def _reopen_limit() -> int:
    """Per-pass cap on transient-failed artifact rows re-opened, per table
    (``PRECIS_EMBED_REOPEN_LIMIT``, default 1000, floor 0; 0 disables the whole
    re-open step). Bounded so each sweep stays cheap — the backlog drains over
    successive passes and the load-gated worker pass, not this re-open, is the
    real throughput throttle.
    """
    raw = os.environ.get("PRECIS_EMBED_REOPEN_LIMIT")
    if raw is None:
        return 1000
    try:
        return max(0, int(raw))
    except ValueError:
        return 1000


def _reopen_transient_failed_artifacts(
    store: Store,
    *,
    table: str,
    artifact_col: str,
    artifact: str,
    patterns: tuple[str, ...],
    limit: int,
) -> int:
    """Re-open a bounded batch of transient-classified ``status='failed'`` rows
    in a chunk-artifact table (``chunk_embeddings`` / ``chunk_summaries``) so the
    owning worker pass re-claims and re-derives them.

    Deletes the failed row (and any lingering ``chunk_claims`` lease): the claim
    treats a ``failed`` row as terminal ("done until a manual DELETE" —
    ``base.py`` / ``llm_summarize``), so removing it makes the chunk claimable
    again. ``table`` / ``artifact_col`` are internal constants (never user
    input), safe to interpolate; the rest is parameterized. Idempotent — once
    the backlog is drained the SELECT finds nothing; ``FOR UPDATE SKIP LOCKED``
    dedups racing sweepers across nodes. Returns the count re-opened.
    """
    if limit <= 0:
        return 0
    sql = f"""
        WITH doomed AS (
            SELECT chunk_id
              FROM {table}
             WHERE {artifact_col} = %(artifact)s
               AND status = 'failed'
               AND attempts < %(max_attempts)s
               AND last_error ILIKE ANY(%(patterns)s)
             ORDER BY chunk_id
             LIMIT %(limit)s
               FOR UPDATE SKIP LOCKED
        ),
        drop_claims AS (
            DELETE FROM chunk_claims cl
             USING doomed d
             WHERE cl.chunk_id = d.chunk_id AND cl.artifact = %(artifact)s
        )
        DELETE FROM {table} t
         USING doomed d
         WHERE t.chunk_id = d.chunk_id AND t.{artifact_col} = %(artifact)s
        RETURNING t.chunk_id
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            sql,
            {
                "artifact": artifact,
                "max_attempts": _REOPEN_MAX_ATTEMPTS,
                "patterns": list(patterns),
                "limit": limit,
            },
        ).fetchall()
        conn.commit()
        return len(rows)


def _reopen_transient_failed_embeds(store: Store, *, limit: int) -> int:
    """Embed re-open (see :func:`_reopen_transient_failed_artifacts`). This is
    the remediation ``EmbedHandler.write_ok`` prescribes. A stranded embedding
    also blocks that chunk's KeyBERT keywords, so re-opening cascades to
    un-stick the downstream ``chunk_keywords`` backlog for free."""
    return _reopen_transient_failed_artifacts(
        store,
        table="chunk_embeddings",
        artifact_col="embedder",
        artifact=_EMBED_MODEL,
        patterns=_EMBED_TRANSIENT_ERROR_PATTERNS,
        limit=limit,
    )


def _reopen_transient_failed_summaries(store: Store, *, limit: int) -> int:
    """llm-v1 gloss re-open (see :func:`_reopen_transient_failed_artifacts`).
    Recovers the ~5k ``chunk_summaries`` rows stranded ``empty summary`` before
    the llm_summarize in-process retry + looser cap (2cd78cc7) landed, so the
    now-retry-capable pass re-summarizes them."""
    return _reopen_transient_failed_artifacts(
        store,
        table="chunk_summaries",
        artifact_col="summarizer",
        artifact=_SUMMARIZE_LLM_MODEL,
        patterns=_SUMMARIZE_TRANSIENT_ERROR_PATTERNS,
        limit=limit,
    )


@dataclass(frozen=True, slots=True)
class _Orphan:
    """One stuck-job candidate identified before the locked transition."""

    ref_id: int
    title: str | None
    running_since: datetime


def _transcript_retention_days() -> int:
    """Days to keep a job's full LLM ``meta.transcript`` before GC.

    Transcripts (the full stream-json of a plan_tick) are large; we keep
    a debugging window then drop them. ``PRECIS_TRANSCRIPT_RETENTION_DAYS``
    (default 30); the chunk-level job_summary/job_result stay regardless."""
    raw = os.environ.get("PRECIS_TRANSCRIPT_RETENTION_DAYS")
    if not raw:
        return 30
    try:
        return max(1, int(raw))
    except ValueError:
        return 30


def _gc_transcripts(store: Store) -> int:
    """Strip ``meta.transcript`` from job refs older than the retention
    window. Cheap single UPDATE; returns the number reaped."""
    days = _transcript_retention_days()
    with store.pool.connection() as conn:
        cur = conn.execute(
            "UPDATE refs SET meta = meta - 'transcript' "
            "WHERE kind = 'job' AND meta ? 'transcript' "
            "  AND created_at < now() - %s::interval",
            (f"{days} days",),
        )
        conn.commit()
        return cur.rowcount or 0


def _agentlog_retention_days() -> int:
    """Days to keep an agentlog (and its ``touched`` links) before GC.

    Run-attribution records accumulate one per tick; we keep a debugging
    window then reap them. ``PRECIS_AGENTLOG_RETENTION_DAYS`` (default
    falls back to :data:`precis.agentlog.RETENTION_DAYS`). The GC drops
    the ``touched`` links but never the chunks they point at."""
    from precis.agentlog import RETENTION_DAYS

    raw = os.environ.get("PRECIS_AGENTLOG_RETENTION_DAYS")
    if not raw:
        return RETENTION_DAYS
    try:
        return max(1, int(raw))
    except ValueError:
        return RETENTION_DAYS


def _route_log_retention_days() -> int:
    """Days to keep ``llm_call_log`` rows (+ orphaned blobs) before GC.

    ``PRECIS_LLM_LOG_RETENTION_DAYS`` (default
    :data:`precis.route_log.DEFAULT_RETENTION_DAYS`). Lite metadata rows are
    cheap, but a corpus-scale batch pass now writes one per chunk, so the log
    needs an actual pruner — this wires :func:`route_log.gc` into the sweep."""
    from precis import route_log

    raw = os.environ.get(route_log.RETENTION_DAYS_ENV)
    if not raw:
        return route_log.DEFAULT_RETENTION_DAYS
    try:
        return max(1, int(raw))
    except ValueError:
        return route_log.DEFAULT_RETENTION_DAYS


#: app_state marker + refresh window for the heading-intent prune piggy-back
#: (source-backfill slice 8b.4). Throttled to once per this window; between runs
#: the sweeper does one cheap ``app_state`` read.
_INTENT_PRUNE_STATE_KEY = "heading_intent_prune:last_run"


def _intent_prune_refresh_hours() -> float:
    raw = os.environ.get("PRECIS_HEADING_INTENT_PRUNE_HOURS")
    try:
        return max(0.1, float(raw)) if raw else 6.0
    except ValueError:
        return 6.0


def _prune_dangling_intents(store: Store) -> int:
    """Throttled piggy-back: retire heading-intent notes whose anchored heading no
    longer resolves (the rename/delete orphan case — a heading edit is DELETE+INSERT,
    so its ``dc<id>`` anchor goes dead). The deterministic hygiene heal for slice 8b,
    same shape as ``paper_hygiene`` repointing links off soft-deleted refs.

    Gated to once per ``PRECIS_HEADING_INTENT_PRUNE_HOURS`` (default 6) via an
    ``app_state`` marker so the per-minute, cluster-wide sweep doesn't rescan every
    intent each cycle. The shared marker plus the idempotent soft-delete serialise it
    across nodes without a lock — a rare double-run just reaps the same orphans
    twice, harmlessly. Returns the number retired (0 when throttled)."""
    last = store.get_setting(_INTENT_PRUNE_STATE_KEY)
    if last:
        try:
            if datetime.now(UTC) - datetime.fromisoformat(last) < timedelta(
                hours=_intent_prune_refresh_hours()
            ):
                return 0
        except ValueError:
            pass  # unparseable marker → treat as due
    from precis.backfill.heading_intent import prune_dangling

    retired = prune_dangling(store)
    store.set_setting(_INTENT_PRUNE_STATE_KEY, datetime.now(UTC).isoformat())
    return len(retired)


def _alert_unschedulable_jobs(store: Store) -> int:
    """Alert on queued jobs no host can place (slice 6d).

    A job that requires (declares or derives) a resource, has no
    ``target_node`` pin to fall back on, and whose capability is advertised
    by NO host in ``resource_slots`` can never be reserved anywhere — it
    would sit queued forever. Raise a ``warn`` alert per such job (deduped
    by ref) so the gap is visible instead of a silent park. Pinned jobs are
    skipped: self-gating declines to reserve but the node gate still runs
    them, so they aren't stuck. Returns the alert count.
    """
    with store.pool.connection() as conn:
        advertised = {
            str(r[0])
            for r in conn.execute(
                "SELECT DISTINCT resource FROM resource_slots"
            ).fetchall()
        }
        rows = conn.execute(
            """
            SELECT r.ref_id, r.meta
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'job'
               AND r.deleted_at IS NULL
               AND t.namespace = 'STATUS'
               AND t.value = 'queued'
             ORDER BY r.ref_id
             LIMIT %s
            """,
            (_UNSCHEDULABLE_SCAN_CAP,),
        ).fetchall()
    n = 0
    for raw_id, raw_meta in rows:
        ref_id = int(raw_id)
        meta = dict(raw_meta or {})
        requires = effective_requires(meta)
        if not requires:
            continue
        params = meta.get("params") or {}
        if params.get("target_node"):
            continue  # pinned → the node gate still runs it; not stuck
        unmet = sorted(res for res in requires if res not in advertised)
        if not unmet:
            continue
        raise_alert(
            store,
            source="scheduler",
            fingerprint=f"unschedulable:{ref_id}",
            title=f"Job #{ref_id} needs {', '.join(unmet)} — no host advertises it",
            detail=(
                f"requires={requires}; unmet={unmet}; no target_node pin. "
                "Provision the capability (a host must advertise it via the "
                "heartbeat probe) or the job waits forever."
            ),
            severity="warn",
            subject_ref_id=ref_id,
        )
        n += 1
    return n


def run_sweeper_pass(store: Store, *, limit: int = 50) -> BatchResult:
    """Detect orphans, lock-and-transition each, return BatchResult.

    Also GCs stale LLM transcripts (``meta.transcript`` older than the
    retention window) and stale agentlogs (run-attribution records +
    their ``touched`` links, never the chunks) — cheap piggy-backs on
    the per-minute sweep.

    Counters:

    * ``claimed`` = candidate orphans the SELECT surfaced
    * ``ok`` = orphans actually transitioned to ``STATUS:failed``
    * ``failed`` = orphans skipped due to a lost race (another worker
      held the row, or its status changed between enumeration and lock)
    """
    reaped = _gc_transcripts(store)
    if reaped:
        log.info("sweeper: GC'd %d stale job transcript(s)", reaped)
    from precis import agentlog

    reaped_logs = agentlog.gc_stale_logs(
        store, older_than_days=_agentlog_retention_days()
    )
    if reaped_logs:
        log.info("sweeper: GC'd %d stale agentlog(s)", reaped_logs)
    from precis import route_log

    reaped_calls = route_log.gc(store, retention_days=_route_log_retention_days())
    if reaped_calls:
        log.info("sweeper: GC'd %d stale llm_call_log row(s)", reaped_calls)
    reopen_limit = _reopen_limit()
    reopened = _reopen_transient_failed_embeds(store, limit=reopen_limit)
    if reopened:
        log.info(
            "sweeper: re-opened %d transient-failed embed row(s) for re-embedding "
            "(also un-blocks their chunk_keywords)",
            reopened,
        )
    reopened_sum = _reopen_transient_failed_summaries(store, limit=reopen_limit)
    if reopened_sum:
        log.info(
            "sweeper: re-opened %d transient-failed llm-v1 summary row(s) "
            "for re-summarization",
            reopened_sum,
        )
    pruned_intents = _prune_dangling_intents(store)
    if pruned_intents:
        log.info(
            "sweeper: retired %d dangling heading-intent note(s) (anchor heading gone)",
            pruned_intents,
        )
    unschedulable = _alert_unschedulable_jobs(store)
    if unschedulable:
        log.warning(
            "sweeper: %d queued job(s) require a capability no host advertises",
            unschedulable,
        )
    threshold_hours = _stuck_job_hours()
    candidates = _enumerate_orphans(store, threshold_hours, limit=limit)
    if not candidates:
        return BatchResult(handler="sweeper", claimed=0, ok=0, failed=0)
    n_ok = 0
    n_failed = 0
    for orphan in candidates:
        if _transition_to_failed(store, orphan, threshold_hours):
            n_ok += 1
            log.warning(
                "sweeper: job #%d swept (running since %s, > %.1fh)",
                orphan.ref_id,
                orphan.running_since.isoformat(),
                threshold_hours,
            )
        else:
            n_failed += 1
    return BatchResult(
        handler="sweeper",
        claimed=len(candidates),
        ok=n_ok,
        failed=n_failed,
    )


def _enumerate_orphans(
    store: Store, threshold_hours: float, *, limit: int
) -> list[_Orphan]:
    """Find ``kind='job'`` refs whose current STATUS:running tag is stale.

    "Current STATUS" is the most-recently-applied ``STATUS:`` tag (the
    handler writes with ``replace_prefix=True``, so only one
    ``STATUS:`` row per ref ever exists at a given time). Its
    ``ref_tags.created_at`` is the claim timestamp.

    ``ssh_node``-executor jobs are excluded: that executor reclaims its own
    expired-lease running jobs (lease-steal + attempt cap in
    ``executors/ssh_node.py``), so a swept→failed here would race — and win —
    the steal, stranding the barrier instead of retrying it. The executor
    owns the crash-recovery story for its jobs; the sweeper must not fail
    them out from under it.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.title, rt.created_at
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'job'
               AND r.deleted_at IS NULL
               AND t.namespace = 'STATUS'
               AND t.value = 'running'
               AND (r.meta->>'executor') IS DISTINCT FROM 'ssh_node'
               AND rt.created_at < now() - %s::interval
               AND (
                    (r.meta->>'lease_until') IS NULL
                 OR (r.meta->>'lease_until')::timestamptz < now()
               )
             ORDER BY r.ref_id
             LIMIT %s
            """,
            (f"{threshold_hours} hours", limit),
        ).fetchall()
    return [
        _Orphan(
            ref_id=int(r[0]),
            title=r[1],
            running_since=r[2],
        )
        for r in rows
    ]


def _transition_to_failed(
    store: Store, orphan: _Orphan, threshold_hours: float
) -> bool:
    """Lock the job ref, re-verify state, write STATUS:failed + swept tag.

    Returns ``True`` on successful transition, ``False`` on race
    (someone else held the row, or the status changed between
    enumeration and lock).
    """
    with store.tx() as conn:
        row = conn.execute(
            """
            SELECT r.ref_id
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.ref_id = %s
               AND r.kind = 'job'
               AND r.deleted_at IS NULL
               AND t.namespace = 'STATUS'
               AND t.value = 'running'
               AND (r.meta->>'executor') IS DISTINCT FROM 'ssh_node'
               AND rt.created_at < now() - %s::interval
               AND (
                    (r.meta->>'lease_until') IS NULL
                 OR (r.meta->>'lease_until')::timestamptz < now()
               )
             FOR UPDATE OF r SKIP LOCKED
            """,
            (orphan.ref_id, f"{threshold_hours} hours"),
        ).fetchone()
        if row is None:
            return False
        # Replace STATUS:running with STATUS:failed in one shot.
        # replace_prefix=True nukes any other STATUS:* on this ref
        # (there should only be one, but defensively cover races).
        store.add_tag(
            orphan.ref_id,
            Tag.closed("STATUS", "failed"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        # Mark *why* it failed so the operator / downstream consumers
        # can distinguish a sweeper transition from an executor
        # failure. Open tag (non-closed) keeps it searchable as a
        # filter.
        store.add_tag(
            orphan.ref_id,
            Tag.open("swept:claim-orphaned"),
            set_by="system",
            conn=conn,
        )
        store.append_event(
            orphan.ref_id,
            source="sweeper",
            event="job-swept",
            payload={
                "running_since": orphan.running_since.isoformat(),
                "swept_at": datetime.now(UTC).isoformat(),
                "threshold_hours": threshold_hours,
                "cause": "claim-orphaned",
            },
            conn=conn,
        )
        # Refund the crashed job's resource reservation (slice 6c) — the
        # sweeper writes STATUS:failed directly rather than via
        # ``set_status``, so it releases the slots itself. Idempotent: a
        # no-op if the job reserved nothing.
        release_job_reservation(conn, orphan.ref_id)
    # Bubble runs in its own transaction so the parent's tag write
    # is durable even if the caller's loop crashes mid-rotation. The
    # bubble helper is idempotent (re-applying the same
    # ``child-failed:<job>`` tag is a no-op), so the explicit call
    # doesn't race with anything the JobHandler.tag path may do
    # later if the operator re-tags by hand.
    bubble_job_failure(store, orphan.ref_id)
    return True


__all__ = ["STUCK_JOB_HOURS", "run_sweeper_pass"]
