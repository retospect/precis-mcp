"""Saved-CQL watch runner for the ``patent`` kind.

Designed for one process per pass — invoked by launchd / cron once
an hour:

    precis jobs run-patent-watches

The runner:

1. Reads every due watch via ``_patent_watch_db.list_due``.
2. For each watch, runs the **fair-use pre-check** (sums
   ``meta->>'fair_use_bytes'`` over the last 7 days). On overrun
   it logs a warning and exits without mutating any watch row;
   the next hourly tick re-tries naturally.
3. Issues one ``ops.search(cql, range_end=watch.max_per_pass or 50)``
   call per watch and parses out the publication numbers.
4. Diffs against ``last_seen_pn``. New ids → either ``auto_get``
   (call ``ingest_patent`` for each, dropping any past
   ``max_per_pass``) or open one quest summarising the lot.
5. Calls ``record_pass`` to bump ``last_run_at`` and union the
   *seen* ids — overflow ids that were dropped do NOT enter
   ``last_seen_pn``, so they resurface naturally on the next
   pass (the "drop-and-resurface" policy chosen at design time).

Per-watch failures are isolated: an OPS error on one watch logs
and moves on to the next. The runner returns a structured summary
so ``run-patent-watches`` can print a concise report.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from precis.handlers import _patent_watch_db as watch_db
from precis.handlers._patent_ingest import ingest_patent
from precis.handlers._patent_ops import OpsClientProto, OpsError
from precis.handlers._patent_xml import OpsHit, parse_search_response
from precis.jobs._patent_quest import QuestCreated, open_quest_for_hits

if TYPE_CHECKING:
    from precis.embedder import Embedder
    from precis.handlers._patent_watch_db import PatentWatch
    from precis.store import Store

log = logging.getLogger(__name__)

# Default fair-use cap, per the spec § Configuration. Overridden by
# ``PRECIS_PATENT_FAIR_USE_LIMIT_GB`` in ``cli.py`` / runner config.
DEFAULT_FAIR_USE_LIMIT_GB: float = 3.0

# Cap on how many hits we ask OPS for in one pass when the watch
# itself doesn't carry a budget. Mirrors the handler's default.
DEFAULT_REMOTE_PAGE: int = 50


# ---------------------------------------------------------------------------
# Result types — what the CLI prints / what tests assert against
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class WatchPassResult:
    """One watch's outcome."""

    watch_name: str
    new_pn: list[str] = field(default_factory=list)
    overflow_pn: list[str] = field(default_factory=list)
    ingested_pn: list[str] = field(default_factory=list)
    quest_slug: str | None = None
    bytes_fetched: int = 0
    error: str | None = None
    skipped_fair_use: bool = False
    skipped_dry_run: bool = False


@dataclass(slots=True)
class RunSummary:
    """Aggregate report for a full pass."""

    fair_use_bytes_before: int = 0
    fair_use_limit_bytes: int = 0
    paused_global: bool = False
    results: list[WatchPassResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Fair-use accounting
# ---------------------------------------------------------------------------


def compute_rolling_fair_use_bytes(store: Store) -> int:
    """Sum ``meta->>'fair_use_bytes'`` over patent refs created in the
    last 7 days.

    Phase-1 ingest now persists ``fair_use_bytes`` on every new
    patent ref's ``meta``. Hit-list cache rows are intentionally
    excluded — search responses are an order of magnitude smaller
    than full-document fetches and the rolling cap is dominated by
    ingest traffic.

    Returns 0 when no patent refs exist in the window.
    """
    sql = """
        SELECT COALESCE(
            SUM((meta->>'fair_use_bytes')::bigint),
            0
        )
        FROM refs
        WHERE kind = 'patent'
          AND created_at > now() - INTERVAL '7 days'
          AND deleted_at IS NULL
    """
    with store.pool.connection() as conn:
        row = conn.execute(sql).fetchone()
    if row is None:
        return 0
    return int(row[0] or 0)


def _gb_to_bytes(gb: float) -> int:
    """Convert GiB to bytes (int)."""
    return int(gb * (1024**3))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_one_pass(
    *,
    store: Store,
    ops: OpsClientProto,
    embedder: Embedder | None,
    raw_root: Path,
    only_name: str | None = None,
    dry_run: bool = False,
    fair_use_limit_gb: float = DEFAULT_FAIR_USE_LIMIT_GB,
) -> RunSummary:
    """Run one pass over every due watch (or just ``only_name``).

    Args:
        store, ops, embedder, raw_root: same as ``ingest_patent``.
        only_name: when given, restricts the pass to a single
            watch *regardless of due-ness*. Debug affordance for
            ``run-patent-watches --name <slug>``.
        dry_run: print what would happen, mutate nothing. Useful for
            inspecting which patents a watch would surface before
            committing to ``--auto-get``.
        fair_use_limit_gb: rolling 7-day cap. Defaults to 3 GiB
            (spec § Configuration).

    Returns:
        ``RunSummary`` listing every watch attempted and the per-watch
        outcome. The runner is best-effort: per-watch failures are
        recorded in ``WatchPassResult.error`` rather than propagated.
    """
    summary = RunSummary(
        fair_use_limit_bytes=_gb_to_bytes(fair_use_limit_gb),
    )

    # Global fair-use pre-check. Only one query, regardless of how
    # many watches are due. We re-check after each ingest the
    # auto-get path performs (per-watch logic below).
    summary.fair_use_bytes_before = compute_rolling_fair_use_bytes(store)
    if summary.fair_use_bytes_before >= summary.fair_use_limit_bytes:
        summary.paused_global = True
        log.warning(
            "patent_watch: paused — rolling 7d fair-use %.2f GiB ≥ limit %.2f GiB",
            summary.fair_use_bytes_before / (1024**3),
            fair_use_limit_gb,
        )
        return summary

    # Pick the watches to process.
    if only_name is not None:
        target = watch_db.get_by_name(store, only_name)
        watches = [target] if target is not None else []
    else:
        watches = watch_db.list_due(store)

    for w in watches:
        result = _run_one_watch(
            store=store,
            ops=ops,
            embedder=embedder,
            raw_root=raw_root,
            watch=w,
            dry_run=dry_run,
            fair_use_limit_bytes=summary.fair_use_limit_bytes,
        )
        summary.results.append(result)

    return summary


# ---------------------------------------------------------------------------
# Per-watch processing
# ---------------------------------------------------------------------------


def _run_one_watch(
    *,
    store: Store,
    ops: OpsClientProto,
    embedder: Embedder | None,
    raw_root: Path,
    watch: PatentWatch,
    dry_run: bool,
    fair_use_limit_bytes: int,
) -> WatchPassResult:
    """Process one watch. Catches all OPS / parse errors; never raises."""
    result = WatchPassResult(watch_name=watch.name)
    page_size = watch.max_per_pass or DEFAULT_REMOTE_PAGE

    # 1. Run the search.
    try:
        response = ops.search(watch.cql, range_start=1, range_end=page_size)
    except OpsError as e:
        result.error = f"OPS search failed: {e}"
        log.warning("patent_watch[%s]: %s", watch.name, result.error)
        return result

    result.bytes_fetched += response.bytes_out

    hits, _total = parse_search_response(response.xml)
    seen_set = {pn.lower() for pn in watch.last_seen_pn}
    new_hits: list[OpsHit] = [h for h in hits if h.docdb_id.lower() not in seen_set]
    new_pn = [h.docdb_id for h in new_hits]
    result.new_pn = list(new_pn)

    if dry_run:
        result.skipped_dry_run = True
        log.info(
            "patent_watch[%s]: dry-run — %d new (auto_get=%s)",
            watch.name,
            len(new_pn),
            watch.auto_get,
        )
        return result

    if not new_pn:
        # Even an empty pass bumps ``last_run_at`` so the watch cools.
        watch_db.record_pass(store, watch_id=watch.id, new_pn=[])
        log.info("patent_watch[%s]: no new hits", watch.name)
        return result

    # 2. Auto-get vs quest split. ``auto_get`` ingests directly;
    # otherwise we open a single quest.
    if watch.auto_get:
        ingested, overflow = _auto_get_with_overflow(
            store=store,
            ops=ops,
            embedder=embedder,
            raw_root=raw_root,
            new_hits=new_hits,
            max_per_pass=watch.max_per_pass,
            watch_name=watch.name,
            fair_use_limit_bytes=fair_use_limit_bytes,
            current_bytes_fetched=result.bytes_fetched,
        )
        result.ingested_pn = ingested
        result.overflow_pn = overflow
        # Only the *ingested* ids enter last_seen_pn — overflow
        # resurfaces next pass. (drop-and-resurface policy.)
        watch_db.record_pass(
            store,
            watch_id=watch.id,
            new_pn=ingested,
        )
    else:
        try:
            created = _open_quest_safely(
                store=store,
                watch_name=watch.name,
                cql=watch.cql,
                hits=new_hits,
            )
            result.quest_slug = created.quest_slug
        except Exception as e:
            result.error = f"quest creation failed: {e}"
            log.warning("patent_watch[%s]: %s", watch.name, result.error)
            return result
        # Quest mode: every new id enters last_seen_pn so we don't
        # re-surface them next pass.
        watch_db.record_pass(
            store,
            watch_id=watch.id,
            new_pn=new_pn,
        )

    log.info(
        "patent_watch[%s]: new=%d ingested=%d overflow=%d quest=%s bytes=%d",
        watch.name,
        len(new_pn),
        len(result.ingested_pn),
        len(result.overflow_pn),
        result.quest_slug or "-",
        result.bytes_fetched,
    )
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auto_get_with_overflow(
    *,
    store: Store,
    ops: OpsClientProto,
    embedder: Embedder | None,
    raw_root: Path,
    new_hits: list[OpsHit],
    max_per_pass: int | None,
    watch_name: str,
    fair_use_limit_bytes: int,
    current_bytes_fetched: int,
) -> tuple[list[str], list[str]]:
    """Ingest up to ``max_per_pass`` hits; return (ingested_pn, overflow_pn).

    Hits are sorted by ``publication_date`` ascending — oldest first.
    This is what we want when a watch comes back to life after a
    quiet period: catch up on the backlog in chronological order.
    Hits without a publication date sort last (we never want to
    silently advance past the tail of the queue).

    Stops early when:
        * we hit ``max_per_pass``;
        * the rolling fair-use sum exceeds the limit (re-checked
          before each ingest — auto_get bursts can blow the cap
          mid-pass).
        * any individual ingest raises (logged, hit goes to overflow).
    """
    sorted_hits = sorted(
        new_hits,
        key=lambda h: (h.publication_date is None, h.publication_date or ""),
    )
    cap = max_per_pass if max_per_pass is not None else len(sorted_hits)
    targets = sorted_hits[:cap]
    overflow_hits = sorted_hits[cap:]

    ingested: list[str] = []
    for hit in targets:
        # Per-ingest fair-use re-check — auto_get of 50 patents in
        # one pass can burn through the weekly cap, so we abort
        # early instead of running headlong.
        rolling = compute_rolling_fair_use_bytes(store)
        if rolling >= fair_use_limit_bytes:
            log.warning(
                "patent_watch[%s]: pausing mid-pass — rolling %.2f GiB hit",
                watch_name,
                rolling / (1024**3),
            )
            # Remaining targets join the overflow.
            cut = targets.index(hit)
            overflow_hits = targets[cut:] + overflow_hits
            break
        try:
            ingest_result = ingest_patent(
                hit.docdb_id,
                store=store,
                ops=ops,
                embedder=embedder,
                raw_root=raw_root,
            )
        except Exception as e:
            log.warning(
                "patent_watch[%s]: ingest %r failed: %s",
                watch_name,
                hit.docdb_id,
                e,
            )
            overflow_hits.append(hit)
            continue
        ingested.append(ingest_result.slug)

    overflow_pn = [h.docdb_id for h in overflow_hits]
    return ingested, overflow_pn


def _open_quest_safely(
    *,
    store: Store,
    watch_name: str,
    cql: str,
    hits: list[OpsHit],
) -> QuestCreated:
    """Wrap ``open_quest_for_hits`` so the call site can ``try/except``
    on ``Exception`` without leaking the underlying type. The runner
    treats quest-creation as best-effort — a failure here shouldn't
    crash the pass."""
    return open_quest_for_hits(store, watch_name=watch_name, cql=cql, hits=hits)


__all__ = [
    "DEFAULT_FAIR_USE_LIMIT_GB",
    "RunSummary",
    "WatchPassResult",
    "compute_rolling_fair_use_bytes",
    "run_one_pass",
]
