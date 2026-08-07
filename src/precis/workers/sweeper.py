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

**Retired for lease-owning executors (§H piece 6, compute-lane-lease-
epoch.md — "lease is the single job-liveness authority").** ``ssh_node``,
``claude_inproc`` and ``claude_docker`` all now opt into
``reclaim_stale_running`` (§H pieces 1-3): a claim under any of those
executors stamps a per-generation ``lease_boot_id`` and self-heals via
BOTH the epoch arm (a same-node successor proves the holder replaced,
reclaims in one pass) and the expiry arm (a same-generation hang, capped
by ``meta.attempts``/:func:`claim_executor_jobs`'s poison guard) — no
external wall-clock timeout is needed, and one racing against the
executor's own lease-steal only risks stranding the barrier (see the
dead-node-reap note below, unchanged). :func:`_enumerate_orphans`
therefore excludes rows from all three, not just ``ssh_node`` as before —
see its docstring. ``coordinator`` is the one exception: it does NOT opt
into ``reclaim_stale_running`` (a crashed slice has no re-claim path of
its own — see ``executors/coordinator.py``'s own note), so its rows still
depend on this wall-clock sweep as their only crash recovery; the
exclusion set is deliberately the three lease-owning executors, not
"every row that happens to carry a lease".

**Dead-node compute-lane reap (gr172886 part-b).** ``ssh_node`` jobs are
excluded from the generic timeout sweep above (see the module note on
:func:`_enumerate_orphans`) because that executor owns its own crash
recovery — an external terminalize here would race, and could win, the
executor's own lease-steal. :func:`_reap_dead_node_orphans` is a narrower,
DISTINCT reap that only fires in the one case where there is no live
executor left to race: the job's ``meta.params.target_node`` host is
*provably dead* (see :func:`_enumerate_dead_node_orphans`), not merely
lease-expired. It terminalizes exactly like the ssh_node poison-guard
(``executors/ssh_node.py``'s ``_MAX_ATTEMPTS`` path): ``STATUS:failed`` +
``meta.failure_class='infra'`` + ``bubble_job_failure`` — an infra death,
never a physical rule-out — tagged ``reaped:dead-node-orphan`` (distinct
from both ``swept:claim-orphaned`` above and ``reaped:reboot-orphan`` in
``quest/loop.py``, so all three recovery paths stay legible in the tag
history).

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
6. **Active container reap (gripe 50905).** Failing the DB row does not by
   itself stop whatever OS process/container the job launched. Two
   best-effort hooks close that gap, for two different situations:
   * ``_kill_job_container`` fires right here, in this same timeout
     transition, for whatever job it's sweeping — but every lease-owning
     executor (``ssh_node``, and since §H piece 6 also ``claude_inproc`` /
     ``claude_docker``) is excluded from this transition entirely (see
     :func:`_enumerate_orphans`), so in current practice this call is
     defense-in-depth for a FUTURE non-lease-owning, container-per-job
     executor, not any executor live today — ``claude_docker`` now reaps
     its own container on every terminal path (including a wall-clock
     deadline kill), and a ``struct_relax`` DFT relax (``ssh_node``) never
     reaches this call site either; see :func:`_kill_job_container`'s
     docstring for the one path that IS still live (the dead-node reap).
   * ``_reap_stale_dft_containers`` is a separate, unconditional watchdog
     run every pass on the DFT node: it force-removes any ``precis-job-*``
     container past a safe age regardless of any job's DB-row state. Since
     ``struct_relax`` never reaches the immediate-kill path above, this
     watchdog is what actually recovers a stuck relax — e.g. the ~56h
     ``gpaw-relax`` that held a GPU after its row was already swept.

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
    set_meta,
)
from precis.workers.nursery import DEAD_WORKER_SILENCE_MIN, WORKER_CONTINUOUS_PROCESSES
from precis.workers.runner import BatchResult

#: Cap the unschedulable scan so a huge queue can't make the per-minute
#: sweep expensive; a genuine capability outage trips the alert on the
#: first few jobs regardless.
_UNSCHEDULABLE_SCAN_CAP = 500

#: Executors that own the full lease-lifecycle for their own jobs (§H,
#: compute-lane-lease-epoch.md pieces 1-3: boot epoch + epoch-aware
#: reclaim + generalized attempt cap) — :func:`_enumerate_orphans`
#: excludes their rows from the generic wall-clock sweep (see its
#: docstring). ``coordinator`` is deliberately absent: it doesn't opt
#: into ``reclaim_stale_running`` and has no reclaim path of its own, so
#: it still depends on this wall-clock sweep as its only crash recovery.
_LEASE_OWNING_EXECUTORS = frozenset({"ssh_node", "claude_inproc", "claude_docker"})

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
    meta: dict


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
    """Strip ``meta.transcript`` (job refs) and the ADR-0038 input-capture
    pair ``meta.assembled_context`` / ``meta.assembled_context_at`` — the
    latter lands on a plan_tick's ``kind='job'`` ref or on the digest memory
    a structural/deep-review pass wrote (see
    :func:`precis.utils.prompt.persist_assembled_context`) — once older than
    the retention window. Two cheap UPDATEs on the same clock; returns the
    combined row count reaped."""
    days = _transcript_retention_days()
    with store.pool.connection() as conn:
        cur = conn.execute(
            "UPDATE refs SET meta = meta - 'transcript' "
            "WHERE kind = 'job' AND meta ? 'transcript' "
            "  AND created_at < now() - %s::interval",
            (f"{days} days",),
        )
        reaped = cur.rowcount or 0
        cur2 = conn.execute(
            "UPDATE refs SET meta = meta - 'assembled_context' - 'assembled_context_at' "
            "WHERE kind IN ('job', 'memory') "
            "  AND (meta ? 'assembled_context' OR meta ? 'assembled_context_at') "
            "  AND created_at < now() - %s::interval",
            (f"{days} days",),
        )
        reaped += cur2.rowcount or 0
        conn.commit()
        return reaped


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


#: Rows to prune per sweep from ``worker_logs`` — bounded so the first drain of a
#: long-unpruned table stays a short DELETE; successive per-minute passes finish
#: the backlog, then it trickles at the aging rate.
_WORKER_LOG_GC_BATCH = 50_000
#: Fleet-wide single-flight key for the worker_logs pruner (ascii ``"wlgc"``).
#: The sweeper runs on every host, so an unguarded retention DELETE would pile up
#: — the exact failure ``route_log.gc`` had. Xact-scoped, auto-released at commit.
_WORKER_LOG_GC_LOCK = 0x776C6763


def _worker_logs_retention_days() -> int:
    """Days to keep ``worker_logs`` rows before GC. The fleet's log sink is
    insert-only and was previously unpruned (~200 MB/day, grew past 7 GB /
    ~20M rows). ``PRECIS_WORKER_LOG_RETENTION_DAYS`` (default 30)."""
    raw = os.environ.get("PRECIS_WORKER_LOG_RETENTION_DAYS")
    if not raw:
        return 30
    try:
        return max(1, int(raw))
    except ValueError:
        return 30


def _gc_worker_logs(store: Store) -> int:
    """Prune ``worker_logs`` past the retention window, one bounded batch per
    pass. Returns rows deleted. Two guards mirror the ``route_log.gc`` fix — the
    same shape (a fleet-wide sweeper pass over a big log table):

    * **Single-flight** — a fleet advisory lock (:data:`_WORKER_LOG_GC_LOCK`);
      the sweeper runs on every host, so an unguarded DELETE would pile up.
    * **Batched** — ``LIMIT`` per pass so the first drain of a long-unpruned
      table (millions of aged rows) can't become one multi-second DELETE
      holding a lock. Old rows sit at the head of the append-only heap, so the
      unordered ``LIMIT`` subquery finds a batch without a dedicated ts index.
    """
    days = _worker_logs_retention_days()
    with store.pool.connection() as conn:
        got = conn.execute(
            "SELECT pg_try_advisory_xact_lock(%s)", (_WORKER_LOG_GC_LOCK,)
        ).fetchone()
        if not got or not got[0]:
            return 0  # another host is already pruning — don't pile on
        cur = conn.execute(
            "DELETE FROM worker_logs WHERE log_id IN ("
            "  SELECT log_id FROM worker_logs"
            "   WHERE ts < now() - %s::interval LIMIT %s)",
            (f"{days} days", _WORKER_LOG_GC_BATCH),
        )
        conn.commit()
        return cur.rowcount or 0


#: Fleet-wide single-flight key for the vault.events pruner (ascii ``"vegc"``).
_VAULT_EVENTS_GC_LOCK = 0x76656763


def _vault_events_retention_days() -> int:
    """Days to keep ``vault.events`` rows. Deliberately long (180) — the point
    of a secret-access log is to still be there when you finally go looking,
    and unlike ``worker_logs`` each row is tiny. ``PRECIS_VAULT_EVENT_RETENTION_DAYS``."""
    raw = os.environ.get("PRECIS_VAULT_EVENT_RETENTION_DAYS")
    if not raw:
        return 180
    try:
        return max(1, int(raw))
    except ValueError:
        return 180


def _gc_vault_events(store: Store) -> int:
    """Prune ``vault.events`` past the retention window. Returns rows deleted.

    Same fleet-wide single-flight guard as the other log pruners — the sweeper
    runs on every host. Best-effort: a DB without migration 0111 has no
    ``vault.gc_events``, and an audit-retention sweep must never fail a pass
    that also does real work.
    """
    days = _vault_events_retention_days()
    try:
        with store.pool.connection() as conn:
            got = conn.execute(
                "SELECT pg_try_advisory_xact_lock(%s)", (_VAULT_EVENTS_GC_LOCK,)
            ).fetchone()
            if not got or not got[0]:
                return 0  # another host is already pruning
            row = conn.execute("SELECT vault.gc_events(%s)", (days,)).fetchone()
            conn.commit()
            return int(row[0]) if row and row[0] is not None else 0
    except Exception:
        log.debug("sweeper: vault.events GC unavailable", exc_info=True)
        return 0


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

    Also GCs stale LLM transcripts + assembled-context captures
    (``meta.transcript`` / ``meta.assembled_context`` older than the
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
        log.info(
            "sweeper: GC'd %d stale job transcript / assembled-context row(s)",
            reaped,
        )
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
    pruned_worker_logs = _gc_worker_logs(store)
    if pruned_worker_logs:
        log.info("sweeper: GC'd %d stale worker_logs row(s)", pruned_worker_logs)
    pruned_vault_events = _gc_vault_events(store)
    if pruned_vault_events:
        log.info("sweeper: GC'd %d stale vault.events row(s)", pruned_vault_events)
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
    reaped_dft = _reap_stale_dft_containers()
    if reaped_dft:
        log.warning(
            "sweeper: reaped %d stale DFT compute container(s) past the "
            "safety threshold (gripe 50905)",
            reaped_dft,
        )
    # Dead-node compute-lane reap (gr172886 part-b) — a DISTINCT pass from the
    # generic timeout sweep below, which excludes ``ssh_node`` entirely (see
    # ``_enumerate_orphans``). Runs every pass, fleet-wide, not gated behind
    # any dark-launch flag.
    _reap_dead_node_orphans(store, limit=limit)
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

    **Lease-owning executors are excluded (§H piece 6).** ``ssh_node``,
    ``claude_inproc`` and ``claude_docker`` all reclaim their own
    expired-lease running jobs (epoch-aware lease-steal + generalized
    attempt cap, ``executors/_common.py``'s ``claim_executor_jobs`` /
    ``poison_guard``), so a swept→failed here would race — and could win —
    the steal, stranding the barrier instead of retrying it. Each of those
    executors owns the full crash-recovery story for its jobs now (a
    same-node successor reclaims in one pass via the epoch arm; a hang is
    still caught by the expiry arm and capped by ``meta.attempts``) — the
    sweeper must not fail them out from under it — *unless* there is no
    live executor left to race at all, which is the one case
    :func:`_enumerate_dead_node_orphans` / :func:`_reap_dead_node_orphans`
    reap instead (gr172886 part-b, ``ssh_node``-only, a distinct pass —
    this exclusion itself is unchanged). ``coordinator`` (and any future
    executor that doesn't opt into ``reclaim_stale_running``) is
    deliberately NOT in this exclusion list — it still relies on this
    wall-clock sweep as its only crash recovery (see
    ``executors/coordinator.py``'s own note on the gap), so excluding
    "every row with a lease" rather than "every row from a lease-owning
    executor" would silently strand a crashed coordinator slice forever.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.title, rt.created_at, r.meta
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'job'
               AND r.deleted_at IS NULL
               AND t.namespace = 'STATUS'
               AND t.value = 'running'
               AND COALESCE(r.meta->>'executor', '') <> ALL(%s)
               AND rt.created_at < now() - %s::interval
               AND (
                    (r.meta->>'lease_until') IS NULL
                 OR (r.meta->>'lease_until')::timestamptz < now()
               )
             ORDER BY r.ref_id
             LIMIT %s
            """,
            (list(_LEASE_OWNING_EXECUTORS), f"{threshold_hours} hours", limit),
        ).fetchall()
    return [
        _Orphan(
            ref_id=int(r[0]),
            title=r[1],
            running_since=r[2],
            meta=dict(r[3] or {}),
        )
        for r in rows
    ]


#: Grace past ``lease_until`` before a dead-node orphan is reap-eligible
#: (seconds). Small and dedicated (unlike :data:`STUCK_JOB_HOURS`) because
#: the predicate that actually gates the reap is target-node deadness, not
#: this margin — it only absorbs clock skew / a lease stamped moments ago.
#: ``PRECIS_DEAD_NODE_REAP_GRACE_S``, default 300 (5 min).
def _dead_node_reap_grace_s() -> int:
    raw = os.environ.get("PRECIS_DEAD_NODE_REAP_GRACE_S")
    if raw is None:
        return 300
    try:
        return max(0, int(raw))
    except ValueError:
        return 300


@dataclass(frozen=True, slots=True)
class _DeadNodeOrphan:
    """One dead-node ``ssh_node`` orphan candidate, identified before the
    locked transition."""

    ref_id: int
    target_node: str
    meta: dict


def _enumerate_dead_node_orphans(
    store: Store, grace_s: int, *, limit: int
) -> list[_DeadNodeOrphan]:
    """Find ``STATUS:running`` ``ssh_node`` jobs whose target node is dead.

    Distinct from :func:`_enumerate_orphans` (which excludes ``ssh_node``
    entirely). A candidate here must satisfy ALL of:

    1. ``meta.executor = 'ssh_node'`` and currently ``STATUS:running``;
    2. ``meta.lease_until`` non-null and expired by more than ``grace_s``
       — the same lease-expired predicate :func:`_enumerate_orphans` /
       :func:`claim_executor_jobs` use, plus a small margin;
    3. ``meta.params.target_node`` is set (a null target can never be
       proven dead, so such a job is left alone — see the NULL join below),
       AND that host is *provably dead*: no ``worker_logs`` row for either
       continuous daemon (:data:`WORKER_CONTINUOUS_PROCESSES`) within
       :data:`DEAD_WORKER_SILENCE_MIN`, AND no fresh ``host_heartbeat`` row
       (mirrors ``nursery.py``'s ``_detect_dead_workers`` host-alive check,
       3-minute freshness) — i.e. the *host itself* looks down, not just one
       wedged process, which is what makes an external terminalize safe:
       there is no live executor left on that node to race via
       ``reclaim_stale_running``.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.meta->'params'->>'target_node' AS target_node, r.meta
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'job'
               AND r.deleted_at IS NULL
               AND t.namespace = 'STATUS'
               AND t.value = 'running'
               AND r.meta->>'executor' = 'ssh_node'
               AND (r.meta->>'lease_until') IS NOT NULL
               AND (r.meta->>'lease_until')::timestamptz < now() - %(grace)s::interval
               AND (r.meta->'params'->>'target_node') IS NOT NULL
               AND NOT EXISTS (
                     SELECT 1 FROM worker_logs wl
                      WHERE wl.host = r.meta->'params'->>'target_node'
                        AND wl.process = ANY(%(procs)s)
                        AND wl.ts > now() - %(silence)s::interval
                   )
               AND NOT EXISTS (
                     SELECT 1 FROM host_heartbeat hh
                      WHERE hh.host = r.meta->'params'->>'target_node'
                        AND hh.ts > now() - interval '3 minutes'
                   )
             ORDER BY r.ref_id
             LIMIT %(limit)s
            """,
            {
                "grace": f"{grace_s} seconds",
                "procs": list(WORKER_CONTINUOUS_PROCESSES),
                "silence": f"{DEAD_WORKER_SILENCE_MIN} minutes",
                "limit": limit,
            },
        ).fetchall()
    return [
        _DeadNodeOrphan(ref_id=int(r[0]), target_node=str(r[1]), meta=dict(r[2] or {}))
        for r in rows
    ]


def _transition_dead_node_orphan_to_failed(
    store: Store, orphan: _DeadNodeOrphan, grace_s: int
) -> bool:
    """Lock the job, re-verify the dead-node predicate, terminalize to failed.

    Mirrors the ``ssh_node`` executor's own poison-guard terminal transition
    (``executors/ssh_node.py``'s ``_MAX_ATTEMPTS`` crash-loop guard)
    **exactly**: ``STATUS`` → ``failed``, ``meta.failure_class = 'infra'``,
    then :func:`bubble_job_failure` — so this reads as an infra death to
    every downstream consumer (the quest harvest's ``failure_class``-gated
    retry-vs-rule-out branch, ADR 0064 §C), never a physical rule-out.
    Tagged ``reaped:dead-node-orphan`` — distinct from
    :func:`_transition_to_failed`'s ``swept:claim-orphaned`` and
    ``quest/loop.py``'s ``reaped:reboot-orphan`` — so the three recovery
    paths stay legible in the tag history.

    Returns ``True`` on successful transition, ``False`` on a lost race
    (someone else already transitioned the row, or the dead-node predicate
    no longer holds by the time this locks it — e.g. the node came back).
    """
    with store.tx() as conn:
        row = conn.execute(
            """
            SELECT r.ref_id
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.ref_id = %(ref_id)s
               AND r.kind = 'job'
               AND r.deleted_at IS NULL
               AND t.namespace = 'STATUS'
               AND t.value = 'running'
               AND r.meta->>'executor' = 'ssh_node'
               AND (r.meta->>'lease_until') IS NOT NULL
               AND (r.meta->>'lease_until')::timestamptz < now() - %(grace)s::interval
               AND (r.meta->'params'->>'target_node') = %(node)s
               AND NOT EXISTS (
                     SELECT 1 FROM worker_logs wl
                      WHERE wl.host = %(node)s
                        AND wl.process = ANY(%(procs)s)
                        AND wl.ts > now() - %(silence)s::interval
                   )
               AND NOT EXISTS (
                     SELECT 1 FROM host_heartbeat hh
                      WHERE hh.host = %(node)s
                        AND hh.ts > now() - interval '3 minutes'
                   )
             FOR UPDATE OF r SKIP LOCKED
            """,
            {
                "ref_id": orphan.ref_id,
                "grace": f"{grace_s} seconds",
                "node": orphan.target_node,
                "procs": list(WORKER_CONTINUOUS_PROCESSES),
                "silence": f"{DEAD_WORKER_SILENCE_MIN} minutes",
            },
        ).fetchone()
        if row is None:
            return False
        store.add_tag(
            orphan.ref_id,
            Tag.closed("STATUS", "failed"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        # INFRA, not a physical verdict — the worker/node died, the compute
        # never rendered a real answer (mirrors ssh_node.py's own poison-guard
        # ``_set_meta(conn, ref_id, failure_class="infra")``).
        set_meta(conn, orphan.ref_id, failure_class="infra")
        store.add_tag(
            orphan.ref_id,
            Tag.open("reaped:dead-node-orphan"),
            set_by="system",
            conn=conn,
        )
        store.append_event(
            orphan.ref_id,
            source="sweeper",
            event="compute-reaped",
            payload={"target_node": orphan.target_node, "cause": "dead-node-orphan"},
            conn=conn,
        )
        # Refund the crashed job's resource reservation (mirrors
        # ``_transition_to_failed`` — idempotent no-op if nothing reserved).
        release_job_reservation(conn, orphan.ref_id)
    # Bubble + container-kill run outside the tx, same as
    # ``_transition_to_failed`` — see its comment for why.
    bubble_job_failure(store, orphan.ref_id)
    _kill_job_container(orphan.ref_id, orphan.meta)
    return True


def _reap_dead_node_orphans(store: Store, *, limit: int) -> int:
    """Run the dead-node compute-lane reap pass (gr172886 part-b).

    Per-job wrapped so one job's failure (a raised exception mid-transition)
    can never abort the rest of the sweep — the same defensive shape as
    :func:`_kill_job_container` / :func:`_reap_stale_dft_containers`, just
    applied per-candidate here since this reap does real DB writes (those
    two are already fully self-contained try/except). Returns the count
    actually reaped.
    """
    grace_s = _dead_node_reap_grace_s()
    candidates = _enumerate_dead_node_orphans(store, grace_s, limit=limit)
    n = 0
    for orphan in candidates:
        try:
            if _transition_dead_node_orphan_to_failed(store, orphan, grace_s):
                n += 1
                log.warning(
                    "sweeper: job #%d reaped (dead-node orphan, target_node=%s "
                    "provably dead — lease expired, no live executor to race)",
                    orphan.ref_id,
                    orphan.target_node,
                )
        except Exception:  # pragma: no cover - defensive
            log.warning(
                "sweeper: dead-node reap for job #%d raised",
                orphan.ref_id,
                exc_info=True,
            )
    return n


def _kill_job_container(ref_id: int, meta: dict) -> None:
    """Best-effort, immediate reap of a compute-lane job's container the
    moment the sweeper fails its DB row (gripe 50905) — instead of leaving a
    live container (and, for a DFT relax, the GPU it holds) to a lazy
    per-boot reconcile. Matches only a known container-naming convention
    keyed off ``meta`` (never a blanket kill); a docker/ssh failure is
    logged and swallowed so it can never abort the sweep.

    Only the ``struct_relax`` (``ssh_node``-executor) branch is live today:
    it's reached from :func:`_transition_dead_node_orphan_to_failed`
    (gr172886 part-b's dead-node reap, the one case ``ssh_node`` rows DO
    still terminalize outside their own executor). A ``claude_docker``
    branch used to fire from the generic timeout sweep too, but §H piece 6
    excluded that executor from :func:`_enumerate_orphans` (it now reaps
    its own container on every terminal path, including a wall-clock
    deadline kill — see ``executors/claude_docker.py``'s ``_poll_job`` /
    ``_terminate``), so that branch would never be called and was removed
    rather than left as unreachable dead code.
    """
    try:
        job_type = meta.get("job_type")
        if job_type == "struct_relax":
            from precis.workers.job_types import struct_relax

            target_node = (meta.get("params") or {}).get("target_node")
            struct_relax.kill_container(ref_id, node=target_node)
    except Exception:  # pragma: no cover - defensive
        log.warning("sweeper: container kill for job #%d raised", ref_id, exc_info=True)


def _reap_stale_dft_containers() -> int:
    """Belt-and-suspenders for gripe 50905: on the DFT compute node itself,
    force-remove any ``precis-job-*`` container past the stale-age
    threshold, independent of its owning job's DB row (covers a container
    whose row was already swept/deleted, or any other way a container
    outlives its row — e.g. the ``ssh_node`` executor is excluded from the
    generic timeout sweep above, so ``_kill_job_container`` never fires for
    it). Gated to the DFT node so the scan doesn't ssh-fan-out from every
    cluster node on every per-minute sweep. Best-effort, never raises —
    including the import itself, so a broken/missing job_type module can
    never abort the rest of the sweeper pass (timeout sweep, embed
    re-open, log GC, …)."""
    try:
        from precis.workers.job_types import struct_relax

        if os.environ.get("PRECIS_NODE") != struct_relax._NODE:
            return 0
        return struct_relax.reap_stale_containers()
    except Exception:  # pragma: no cover - defensive
        log.warning("sweeper: reap_stale_containers raised", exc_info=True)
        return 0


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
    # Immediate active reap (gripe 50905) — kill the job's container (by the
    # naming convention its executor/job_type uses), instead of leaving an
    # orphaned OS process/container for a lazy per-boot reconcile.
    _kill_job_container(orphan.ref_id, orphan.meta)
    return True


__all__ = ["STUCK_JOB_HOURS", "run_sweeper_pass"]
