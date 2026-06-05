"""Retraction Watch dataset sync — fetches CSV, upserts into the cache.

See ``docs/design/provenance-kind-plan.md`` § "Phase 3" for the design.

Source order:

1. **Primary — Crossref Labs API**
   ``https://api.labs.crossref.org/data/retractionwatch?<email>`` —
   note the unusual query format (email is the bare query string).
   Labs/experimental: may disappear or perform erratically.
2. **Secondary — GitLab mirror**
   ``https://gitlab.com/crossref/retraction-watch-data/-/raw/main/retraction_watch.csv``
   — Crossref-hosted with daily updates; more stable URL.

The sync job tries Labs first, falls back to GitLab on HTTP error or
empty response. ``provenance_rw_sync`` ledger records which source
served the data, so future operators can detect Labs disappearance
from the logs.

Idempotency: keyed on RW's own ``Record ID`` so re-runs reconcile in
place via ``INSERT ... ON CONFLICT DO UPDATE``. Safe to run repeatedly.

Stdlib-only fetch — uses ``urllib.request`` to avoid pulling in
``httpx`` for one job. The full CSV is ~40 MB; we stream it line-by-line
into the parser so we don't peak >100 MB memory at upsert time.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from psycopg.types.json import Jsonb

from precis.ingest._rw_csv import RWRow, parse_rw_rows

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)


GITLAB_RAW_URL = (
    "https://gitlab.com/crossref/retraction-watch-data/-/raw/main/retraction_watch.csv"
)
LABS_URL_TEMPLATE = "https://api.labs.crossref.org/data/retractionwatch?{email}"

# Reasonable timeout for a ~40 MB download. Tune up if Crossref Labs
# is on a slow day; the sync runs monthly so a slow fetch is fine.
_HTTP_TIMEOUT_SECONDS = 600  # 10 minutes


@dataclass(frozen=True, slots=True)
class SyncResult:
    """Outcome of one ``run_sync`` call."""

    source_url: str
    rows_upserted: int
    rows_seen: int
    status: str  # 'ok' | 'partial' | 'failed'
    error: str | None = None


def _build_url(*, mailto: str | None) -> tuple[str, str | None]:
    """Return ``(primary_url, secondary_url)``.

    Without a ``mailto``, the Labs primary endpoint is skipped — the
    polite-pool convention requires it. We fall straight to GitLab,
    which doesn't require auth.
    """
    if mailto:
        return LABS_URL_TEMPLATE.format(email=mailto), GITLAB_RAW_URL
    return GITLAB_RAW_URL, None


def _stream_lines(url: str) -> Iterator[str]:
    """Open ``url`` and yield decoded lines.

    Yields the response line-by-line so the ~40 MB CSV never lands
    entirely in memory. ``urllib.request`` is stdlib — no extra dep
    just for this monthly job.
    """
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "precis-mcp/provenance-rw-sync"},
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
        # Force UTF-8; RW publishes UTF-8 per their docs.
        for raw_line in resp:
            yield raw_line.decode("utf-8", errors="replace")


def _upsert_rows(store: Store, rows: Iterator[RWRow]) -> int:
    """Upsert RW rows into ``provenance_rw_cache``. Returns row count.

    Idempotent on ``record_id``: re-syncs update in place. We use a
    single batched executemany for throughput — 50k rows in one
    transaction is fine for Postgres.
    """
    sql = (
        "INSERT INTO provenance_rw_cache "
        "  (record_id, paper_doi, notice_doi, notice_nature, "
        "   reasons, retraction_date, paper_title, journal, raw, synced_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now()) "
        "ON CONFLICT (record_id) DO UPDATE SET "
        "  paper_doi = EXCLUDED.paper_doi, "
        "  notice_doi = EXCLUDED.notice_doi, "
        "  notice_nature = EXCLUDED.notice_nature, "
        "  reasons = EXCLUDED.reasons, "
        "  retraction_date = EXCLUDED.retraction_date, "
        "  paper_title = EXCLUDED.paper_title, "
        "  journal = EXCLUDED.journal, "
        "  raw = EXCLUDED.raw, "
        "  synced_at = now()"
    )
    batch: list[tuple] = []
    n = 0
    # Chunk size tuned for psycopg's executemany — small enough to
    # keep one chunk's memory bounded, large enough to amortise round
    # trips. RW has ~50k rows so this is 5 batches.
    chunk_size = 10_000
    with store.pool.connection() as conn:
        with conn.transaction():
            cur = conn.cursor()
            for row in rows:
                batch.append((
                    row.record_id,
                    row.paper_doi,
                    row.notice_doi or None,
                    row.notice_nature,
                    row.reasons,
                    row.retraction_date,
                    row.paper_title,
                    row.journal,
                    Jsonb(row.raw),
                ))
                if len(batch) >= chunk_size:
                    cur.executemany(sql, batch)
                    n += len(batch)
                    batch.clear()
            if batch:
                cur.executemany(sql, batch)
                n += len(batch)
    return n


def _record_sync(
    store: Store,
    *,
    source_url: str,
    rows_seen: int,
    status: str,
    error: str | None,
) -> None:
    """Write to the ``provenance_rw_sync`` ledger."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO provenance_rw_sync "
            "  (source_url, last_full_sync_at, last_row_count, "
            "   last_status, last_error) "
            "VALUES (%s, now(), %s, %s, %s) "
            "ON CONFLICT (source_url) DO UPDATE SET "
            "  last_full_sync_at = now(), "
            "  last_row_count = EXCLUDED.last_row_count, "
            "  last_status = EXCLUDED.last_status, "
            "  last_error = EXCLUDED.last_error",
            (source_url, rows_seen, status, error),
        )


def _try_source(
    store: Store,
    *,
    source_url: str,
) -> SyncResult:
    """Fetch + parse + upsert from one source. Records its own ledger row."""
    log.info("RW sync: fetching from %s", source_url)
    try:
        lines = _stream_lines(source_url)
        # parse_rw_rows is a generator; we tee through count tracking
        # by upserting directly. ``rows_seen`` is the post-parse count.
        seen_count = 0

        def counting_iter() -> Iterator[RWRow]:
            nonlocal seen_count
            for r in parse_rw_rows(lines):
                seen_count += 1
                yield r

        upserted = _upsert_rows(store, counting_iter())
    except (urllib.error.URLError, OSError) as exc:
        err = f"transport error: {exc}"
        log.warning("RW sync: %s for %s", err, source_url)
        _record_sync(
            store,
            source_url=source_url,
            rows_seen=0,
            status="failed",
            error=err,
        )
        return SyncResult(source_url, 0, 0, "failed", err)
    except Exception as exc:
        err = f"unexpected error: {exc}"
        log.exception("RW sync: %s", err)
        _record_sync(
            store,
            source_url=source_url,
            rows_seen=0,
            status="failed",
            error=err,
        )
        return SyncResult(source_url, 0, 0, "failed", err)

    if upserted == 0:
        err = "no rows parsed — source may be empty or malformed"
        log.warning("RW sync: %s for %s", err, source_url)
        _record_sync(
            store,
            source_url=source_url,
            rows_seen=seen_count,
            status="partial",
            error=err,
        )
        return SyncResult(source_url, 0, seen_count, "partial", err)

    log.info(
        "RW sync: %d rows upserted from %s (parsed %d)",
        upserted,
        source_url,
        seen_count,
    )
    _record_sync(
        store,
        source_url=source_url,
        rows_seen=upserted,
        status="ok",
        error=None,
    )
    return SyncResult(source_url, upserted, seen_count, "ok", None)


def run_sync(
    *,
    store: Store,
    mailto: str | None = None,
    force_source: str | None = None,
) -> SyncResult:
    """Run one RW sync. Returns the result of whichever source succeeded.

    ``mailto`` enables the Labs primary path (required by Crossref's
    polite-pool convention). When omitted, falls straight to GitLab.

    ``force_source`` (``'labs'`` or ``'gitlab'``) skips the failover —
    handy for ops debugging when one of the sources is misbehaving.
    """
    primary, secondary = _build_url(mailto=mailto)

    if force_source == "labs":
        if not mailto:
            raise ValueError(
                "force_source='labs' requires --mailto (polite-pool convention)"
            )
        return _try_source(store, source_url=LABS_URL_TEMPLATE.format(email=mailto))
    if force_source == "gitlab":
        return _try_source(store, source_url=GITLAB_RAW_URL)

    result = _try_source(store, source_url=primary)
    if result.status == "ok":
        return result
    if secondary is None:
        return result

    log.info(
        "RW sync: primary source failed (%s); falling back to %s",
        result.status,
        secondary,
    )
    return _try_source(store, source_url=secondary)


__all__ = [
    "GITLAB_RAW_URL",
    "LABS_URL_TEMPLATE",
    "SyncResult",
    "run_sync",
]
