"""``precis maintenance run`` — nightly housekeeping driver.

Composes the small primitives the agent surface already exposes
(``search(tags=['WATCH:daily'])`` + ``get(..., mode='refresh')``)
plus a few SQL-level chores that don't belong in the MCP surface
(VACUUM ANALYZE, soft-delete purge) into one cron-friendly command.

Designed to be the **only** scheduled job a fresh deploy needs.
Default invocation:

::

    precis maintenance run

Suggested crontab line (runs at 03:17 every night):

::

    17 3 * * *  /opt/precis/venv/bin/precis maintenance run >> /var/log/precis-maint.log 2>&1

What it does, in order:

1. **WATCH refresh sweep** — iterate every cache-backed ref carrying
   a ``WATCH:<interval>`` tag whose interval is due, and call
   ``get(..., mode='refresh')`` on it. ``hourly`` runs every pass;
   ``daily`` runs once per ~24h; ``weekly`` once per ~7d;
   ``monthly`` once per ~30d. Driven by ``cache_state.fetched_at``
   so reruns within the interval are no-ops.

2. **Soft-delete purge** — hard-delete ``deleted_at IS NOT NULL``
   refs whose tombstone is older than ``--purge-after-days``
   (default: 30 days). Audit trail lives on the disk-backed WAL
   for as long as backups retain it.

3. **VACUUM ANALYZE** — reclaim bloat and update planner stats on
   the hot tables (``refs``, ``chunks``, ``cache_state``,
   ``ref_tags``, ``chunk_tags``, ``links``, ``ref_events``).
   Skipped if ``--no-vacuum`` is passed.

Each phase is independently togglable via flags so you can dry-run
or do partial passes during incident response.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import TYPE_CHECKING, Any

from psycopg import sql

from precis.cli._common import resolve_dsn

if TYPE_CHECKING:
    from precis.runtime import PrecisRuntime
    from precis.store import Store

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------


_INTERVAL_SECONDS: dict[str, int] = {
    # Tolerate clock skew + cron drift by docking a few minutes off
    # each interval — a "daily" sweep at 03:17 should still catch a
    # ref that was last fetched at 03:14 the previous day, even if
    # the wall-clock difference is 86 220 s rather than a clean
    # 86 400 s. The dock is roughly the cron jitter we expect at
    # cluster scale.
    "hourly": 60 * 55,  # 55 min
    "daily": 60 * 60 * 23,  # 23 h
    "weekly": 60 * 60 * 24 * 6 + 60 * 60 * 12,  # 6.5 d
    "monthly": 60 * 60 * 24 * 28,  # 28 d
}


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``maintenance`` subparser on ``sub``."""
    mp = sub.add_parser(
        "maintenance",
        help="Daily housekeeping: refresh WATCH:* tags, purge tombstones, VACUUM.",
    )
    mp_sub = mp.add_subparsers(dest="maint_cmd", required=True)
    run_p = mp_sub.add_parser(
        "run",
        help="Run the full nightly sweep (refreshes + purge + VACUUM).",
    )
    run_p.add_argument("--database-url", default=None)
    run_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan the work and print what would happen, but make no writes.",
    )
    run_p.add_argument(
        "--no-refresh",
        action="store_true",
        help="Skip the WATCH:* refresh sweep.",
    )
    run_p.add_argument(
        "--no-purge",
        action="store_true",
        help="Skip soft-delete purging.",
    )
    run_p.add_argument(
        "--no-vacuum",
        action="store_true",
        help="Skip VACUUM ANALYZE.",
    )
    run_p.add_argument(
        "--purge-after-days",
        type=int,
        default=30,
        help=(
            "Hard-delete soft-deleted refs whose tombstone is older "
            "than this many days (default: 30)."
        ),
    )
    run_p.add_argument(
        "--intervals",
        default="hourly,daily,weekly,monthly",
        help=(
            "Comma-separated WATCH intervals to refresh on this pass. "
            "Default sweeps all four; restrict with e.g. "
            "'--intervals=daily,weekly' for a 03:17 cron that skips "
            "hourly (left to a separate hourly cron) or monthly "
            "(processed by the monthly run)."
        ),
    )
    run_p.add_argument(
        "--max-refresh-per-pass",
        type=int,
        default=200,
        help=(
            "Cap on refreshes per invocation (default: 200) so a runaway "
            "WATCH list can't drain API budget in one cron tick. "
            "Remaining due refs sweep on the next pass."
        ),
    )
    return mp


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> None:
    """Execute ``precis maintenance run``."""
    if args.maint_cmd != "run":
        # argparse already enforces required=True; defensive.
        print(f"maintenance: unknown subcommand {args.maint_cmd!r}", file=sys.stderr)
        sys.exit(2)

    intervals = [s.strip() for s in args.intervals.split(",") if s.strip()]
    bad = [i for i in intervals if i not in _INTERVAL_SECONDS]
    if bad:
        print(
            f"maintenance: unknown interval(s) {bad!r}; "
            f"valid: {sorted(_INTERVAL_SECONDS)}",
            file=sys.stderr,
        )
        sys.exit(2)

    from precis.config import load_config
    from precis.runtime import build_runtime

    cfg = load_config()
    dsn = resolve_dsn(args.database_url, cfg=cfg)
    # ``PrecisConfig`` is a frozen pydantic model — copy with the
    # resolved DSN rather than mutating in place. Keeps the rest
    # of the config (embedder, corpus, …) intact.
    cfg = cfg.model_copy(update={"database_url": dsn})
    runtime = build_runtime(cfg)
    store = runtime.store
    if store is None:
        print(
            "maintenance: no database configured - set PRECIS_DATABASE_URL",
            file=sys.stderr,
        )
        sys.exit(2)

    started = time.monotonic()
    summary: dict[str, Any] = {
        "refreshed": 0,
        "refresh_errors": 0,
        "purged": 0,
        "vacuum_skipped": True,
    }

    try:
        if not args.no_refresh:
            refreshed, errors = _refresh_due_watches(
                runtime=runtime,
                store=store,
                intervals=intervals,
                max_per_pass=args.max_refresh_per_pass,
                dry_run=args.dry_run,
            )
            summary["refreshed"] = refreshed
            summary["refresh_errors"] = errors

        if not args.no_purge:
            summary["purged"] = _purge_soft_deleted(
                store=store,
                older_than_days=args.purge_after_days,
                dry_run=args.dry_run,
            )

        if not args.no_vacuum:
            _vacuum_analyze(store=store, dry_run=args.dry_run)
            summary["vacuum_skipped"] = False
    finally:
        store.close()

    elapsed = time.monotonic() - started
    print(
        f"maintenance: refreshed={summary['refreshed']} "
        f"errors={summary['refresh_errors']} "
        f"purged={summary['purged']} "
        f"vacuum_skipped={summary['vacuum_skipped']} "
        f"elapsed={elapsed:.1f}s "
        f"{'(dry-run)' if args.dry_run else ''}".rstrip()
    )


# ---------------------------------------------------------------------------
# Phase 1 — WATCH:* refresh sweep
# ---------------------------------------------------------------------------


def _refresh_due_watches(
    *,
    runtime: PrecisRuntime,
    store: Store,
    intervals: list[str],
    max_per_pass: int,
    dry_run: bool,
) -> tuple[int, int]:
    """Refresh every WATCH-tagged ref whose interval has elapsed.

    Returns ``(n_refreshed, n_errors)``. Iteration order: shorter
    intervals first (hourly → monthly) so a tight refresh budget
    favours the most-urgent watches.
    """
    n_refreshed = 0
    n_errors = 0

    # Sort intervals shortest-first so a budget cap drops the
    # least-urgent first.
    intervals_sorted = sorted(intervals, key=lambda i: _INTERVAL_SECONDS[i])

    for interval in intervals_sorted:
        if n_refreshed + n_errors >= max_per_pass:
            log.info(
                "maintenance: hit max-per-pass=%d, deferring %s+ to next run",
                max_per_pass,
                interval,
            )
            break

        # Pull refs tagged WATCH:<interval>. The store filter accepts
        # canonical-form tag strings; ``WATCH:daily`` is canonical
        # closed-prefix form (uppercase prefix, lowercase value).
        budget = max_per_pass - n_refreshed - n_errors
        refs = store.list_refs(
            tags=[f"WATCH:{interval}"],
            limit=budget,
        )
        if not refs:
            continue

        cutoff_seconds = _INTERVAL_SECONDS[interval]
        log.info(
            "maintenance: %s sweep - %d candidate ref(s) (cutoff=%ds)",
            interval,
            len(refs),
            cutoff_seconds,
        )

        for ref in refs:
            if n_refreshed + n_errors >= max_per_pass:
                break

            # ``Ref.slug`` is ``str | None`` because numeric-id kinds
            # (todo, memory, …) carry no slug — but cache-backed kinds
            # always do. The WATCH tag only attaches to cache-backed
            # rows, so a None here would mean a tag landed on a kind
            # that doesn't support refresh; skip rather than crash.
            if not ref.slug:
                log.warning(
                    "maintenance: WATCH:%s on %s id=%d has no slug, skipping",
                    interval,
                    ref.kind,
                    ref.id,
                )
                continue

            cached = store.get_cache_entry_by_slug(kind=ref.kind, slug=ref.slug)
            if cached is None:
                # Tagged but no cache row — likely a ref that was
                # tagged before being ingested, or a stale tag from
                # a previous schema. Log and skip; don't error.
                log.warning(
                    "maintenance: %s/%s tagged WATCH:%s but no cache row, skipping",
                    ref.kind,
                    ref.slug,
                    interval,
                )
                continue

            _ref, cache = cached
            if cache.fetched_at is None:
                age_seconds = float("inf")
            else:
                from datetime import UTC, datetime

                now = datetime.now(UTC)
                age_seconds = (now - cache.fetched_at).total_seconds()

            if age_seconds < cutoff_seconds:
                log.debug(
                    "maintenance: %s/%s aged %ds < %ds, not yet due",
                    ref.kind,
                    ref.slug,
                    int(age_seconds),
                    cutoff_seconds,
                )
                continue

            if dry_run:
                log.info(
                    "maintenance: would refresh %s/%s (aged %ds, WATCH:%s)",
                    ref.kind,
                    ref.slug,
                    int(age_seconds),
                    interval,
                )
                n_refreshed += 1
                continue

            # ``dispatch_with_status`` returns ``(body, is_error)``;
            # the runtime never raises on handler errors — they're
            # rendered as text + flagged via the boolean. Use the
            # status flag to count failures so a failed Perplexity
            # API call shows up as an error rather than a successful
            # refresh that happens to contain ``[error:Upstream]``
            # in the response body.
            body, is_error = runtime.dispatch_with_status(
                "get",
                {
                    "kind": ref.kind,
                    "id": ref.slug,
                    "__extras__": {"mode": "refresh"},
                },
            )
            if is_error:
                n_errors += 1
                # Body is the rendered error envelope — first 200
                # chars are usually enough to diagnose.
                log.warning(
                    "maintenance: refresh failed for %s/%s: %s",
                    ref.kind,
                    ref.slug,
                    body[:200].replace("\n", " "),
                )
            else:
                n_refreshed += 1
                log.info(
                    "maintenance: refreshed %s/%s (was %ds old)",
                    ref.kind,
                    ref.slug,
                    int(age_seconds),
                )

    return n_refreshed, n_errors


# ---------------------------------------------------------------------------
# Phase 2 — soft-delete purge
# ---------------------------------------------------------------------------


def _purge_soft_deleted(
    *,
    store: Store,
    older_than_days: int,
    dry_run: bool,
) -> int:
    """Hard-delete refs whose ``deleted_at`` is older than ``older_than_days``.

    Cascades to chunks, cache_state, and tag tables via the
    referential constraints. Returns the count of rows affected.
    Audit trail (request to recover) is gone after this fires —
    the soft-delete recovery window is the ``older_than_days``
    gate.
    """
    sql_count = (
        "SELECT count(*) FROM refs "
        "WHERE deleted_at IS NOT NULL "
        "  AND deleted_at < now() - (%s || ' days')::interval"
    )
    sql_delete = (
        "DELETE FROM refs "
        "WHERE deleted_at IS NOT NULL "
        "  AND deleted_at < now() - (%s || ' days')::interval"
    )
    with store.pool.connection() as conn:
        row = conn.execute(sql_count, (str(older_than_days),)).fetchone()
        n = int(row[0]) if row else 0

    if n == 0:
        log.info("maintenance: purge - no tombstones older than %dd", older_than_days)
        return 0
    if dry_run:
        log.info(
            "maintenance: purge - would hard-delete %d ref(s) older than %dd",
            n,
            older_than_days,
        )
        return n
    with store.tx() as conn:
        conn.execute(sql_delete, (str(older_than_days),))
    log.info("maintenance: purged %d soft-deleted ref(s)", n)
    return n


# ---------------------------------------------------------------------------
# Phase 3 — VACUUM ANALYZE
# ---------------------------------------------------------------------------


_VACUUM_TABLES: tuple[str, ...] = (
    "refs",
    "chunks",
    "cache_state",
    "ref_tags",
    "chunk_tags",
    "links",
    "ref_events",
)


def _vacuum_analyze(*, store: Store, dry_run: bool) -> None:
    """VACUUM ANALYZE the hot tables.

    Reclaims dead rows left by UPDATE / DELETE traffic (cache
    refreshes, soft-deletes) and refreshes planner stats so
    pgvector + tsvector queries pick the right plan after large
    ingest passes. ``VACUUM`` can't run inside a transaction, so
    we use psycopg's autocommit mode for the duration of this
    call.
    """
    if dry_run:
        log.info(
            "maintenance: vacuum - would run VACUUM ANALYZE on %s",
            ", ".join(_VACUUM_TABLES),
        )
        return
    # VACUUM requires no surrounding transaction; psycopg's pool
    # gives us a clean connection, and we toggle autocommit so each
    # VACUUM lands as its own statement-level command.
    with store.pool.connection() as conn:
        conn.autocommit = True
        try:
            for table in _VACUUM_TABLES:
                log.info("maintenance: VACUUM ANALYZE %s", table)
                # sql.Identifier quotes/escapes the table name even
                # though _VACUUM_TABLES is a hard-coded tuple today
                # — defense-in-depth for the day someone wires a
                # CLI flag here.
                conn.execute(sql.SQL("VACUUM ANALYZE {}").format(sql.Identifier(table)))
        finally:
            conn.autocommit = False


__all__ = ["add_parser", "run"]
