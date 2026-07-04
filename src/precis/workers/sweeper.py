"""Stuck-job sweeper — recovers cascades after orphaned claims.

A ``kind='job'`` ref that carries ``STATUS:running`` for longer than the
configured threshold without any subsequent status change is treated as
an orphan: its claimer (worker subprocess) is presumed dead and the
parent todo's ``child_job_succeeded`` auto_check is silently stuck.

The sweeper:

1. Selects rows where the *current* ``STATUS:`` value is ``running`` and
   the ``ref_tags`` row that wrote that tag is older than
   :data:`STUCK_JOB_HOURS`.
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
from datetime import UTC, datetime

from precis.handlers._job_bubble import bubble_job_failure
from precis.store import Store
from precis.store.types import Tag
from precis.workers.runner import BatchResult

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

#: Only re-open a failed embed row while its ``attempts`` stay under this — a
#: genuinely-poison chunk (real dim-mismatch) must still terminate rather than
#: loop forever. Transient endpoint-down failures no longer re-strand under
#: current code, so this only backstops the rare genuine per-row fault.
_EMBED_REOPEN_MAX_ATTEMPTS = 6


def _embed_reopen_limit() -> int:
    """Per-pass cap on transient-failed embed rows re-opened
    (``PRECIS_EMBED_REOPEN_LIMIT``, default 1000, floor 0; 0 disables). Bounded
    so each sweep stays cheap — the backlog drains over successive passes and
    the load-gated embed pass, not this re-open, is the real throughput throttle.
    """
    raw = os.environ.get("PRECIS_EMBED_REOPEN_LIMIT")
    if raw is None:
        return 1000
    try:
        return max(0, int(raw))
    except ValueError:
        return 1000


def _reopen_transient_failed_embeds(store: Store, *, limit: int) -> int:
    """Re-open a bounded batch of transient-classified ``status='failed'``
    ``chunk_embeddings`` rows so the embed pass re-claims and re-embeds them.

    Deletes the failed row (and any lingering ``chunk_claims`` lease) — the
    exact remediation ``EmbedHandler.write_ok`` prescribes: the base claim
    treats a ``failed`` row as terminal ("done until a manual DELETE",
    ``base.py``), so removing it makes the chunk claimable again. Idempotent:
    once the backlog is drained the SELECT finds nothing. ``FOR UPDATE SKIP
    LOCKED`` dedups racing sweepers across nodes. Returns the count re-opened.
    """
    if limit <= 0:
        return 0
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            WITH doomed AS (
                SELECT chunk_id
                  FROM chunk_embeddings
                 WHERE embedder = %(embedder)s
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
                 WHERE cl.chunk_id = d.chunk_id AND cl.artifact = %(embedder)s
            )
            DELETE FROM chunk_embeddings e
             USING doomed d
             WHERE e.chunk_id = d.chunk_id AND e.embedder = %(embedder)s
            RETURNING e.chunk_id
            """,
            {
                "embedder": _EMBED_MODEL,
                "max_attempts": _EMBED_REOPEN_MAX_ATTEMPTS,
                "patterns": list(_EMBED_TRANSIENT_ERROR_PATTERNS),
                "limit": limit,
            },
        ).fetchall()
        conn.commit()
        return len(rows)


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
    reopened = _reopen_transient_failed_embeds(store, limit=_embed_reopen_limit())
    if reopened:
        log.info(
            "sweeper: re-opened %d transient-failed embed row(s) for re-embedding "
            "(also un-blocks their chunk_keywords)",
            reopened,
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
               AND rt.created_at < now() - %s::interval
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
               AND rt.created_at < now() - %s::interval
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
    # Bubble runs in its own transaction so the parent's tag write
    # is durable even if the caller's loop crashes mid-rotation. The
    # bubble helper is idempotent (re-applying the same
    # ``child-failed:<job>`` tag is a no-op), so the explicit call
    # doesn't race with anything the JobHandler.tag path may do
    # later if the operator re-tags by hand.
    bubble_job_failure(store, orphan.ref_id)
    return True


__all__ = ["STUCK_JOB_HOURS", "run_sweeper_pass"]
