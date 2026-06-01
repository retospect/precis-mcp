"""``precis stubs`` — list paper refs needing PDFs (with last fetch attempt).

The chase worker creates stub paper refs (DOI / arXiv / S2 id
registered; ``pdf_sha256 IS NULL``) when a finding's chain reaches
a paper the corpus doesn't have. This command surfaces the
backlog: each row names the cite_key, the most useful identifier,
the last attempt the fetcher worker made (if any), and a one-line
status.

Read flow: queries ``refs WHERE pdf_sha256 IS NULL AND kind='paper'``
joined with the **latest** ``ref_events`` row per ref where
``source LIKE 'fetcher:%'``. TOON-table by default; rich table on
a TTY; JSON for downstream tooling.

Typical use:

* ``precis stubs`` — full backlog, newest stubs first.
* ``precis stubs --limit 100``
* ``precis stubs --awaiting`` — only stubs never attempted (or
  attempted >24h ago and still pending).
* ``precis stubs --format json`` — for piping into a workflow.

Sibling commands:

* ``precis worker --only fetch`` — drive the fetcher cascade
  against the same backlog.
* ``scripts/doilist`` — the operator-facing DOI-list fetcher (file-
  driven, pre-existing; complementary).
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from precis.cli._common import (
    add_format_argument,
    resolve_dsn,
    resolve_format,
)
from precis.format import serialize
from precis.store import Store

# Column order pinned here so TOON / JSON / table all see the same
# shape. Adding a column lands in one place.
_SCHEMA: list[str] = [
    "ref_id",
    "cite_key",
    "identifier",
    "last_attempt",
    "last_source",
    "last_event",
    "state",
]


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "stubs",
        help="List paper refs needing PDFs (with last fetcher attempt).",
        description=(
            "Surface the stub backlog: paper refs whose PDF hasn't "
            "landed yet, with the latest fetcher attempt summarised. "
            "Drive a fetch pass via ``precis worker --only fetch``."
        ),
    )
    p.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Max rows to return (default 50).",
    )
    p.add_argument(
        "--awaiting",
        action="store_true",
        help="Show only stubs never attempted or attempted >24h ago "
        "and still pending (i.e. the queue the fetcher would target "
        "on its next pass).",
    )
    p.add_argument(
        "--database-url",
        default=None,
        help="Override PRECIS_DATABASE_URL.",
    )
    add_format_argument(p)


def run(args: argparse.Namespace) -> None:
    dsn = resolve_dsn(args.database_url)
    store = Store.connect(dsn)
    try:
        rows = _query_stubs(store, limit=args.limit, awaiting=args.awaiting)
    finally:
        store.close()

    if not rows:
        print(
            "stubs: no stub paper refs found "
            "(every paper has either a pdf_sha256 or no external identifier)",
            file=sys.stderr,
        )
        return
    print(serialize(rows, format=resolve_format(args), schema=_SCHEMA))


def _query_stubs(
    store: Store, *, limit: int, awaiting: bool
) -> list[dict[str, Any]]:
    """Return one dict per stub paper ref, newest-stub-first.

    Joins ``refs`` (stub predicate) with the latest ``ref_events``
    row per ref where ``source LIKE 'fetcher:%'``. The ``state``
    column is a one-line summary the operator can scan: "awaiting
    fetch" / "OK — PDF expected soon" / "no OA version" / etc.

    When ``awaiting=True``, restricts to rows where the last
    attempt is NULL or older than 24h AND the event isn't
    ``fetch_ok`` (the watcher would have picked the file up by now
    so the row would have left this query's result set the next
    time precis_add ran). Practical filter: "what would
    ``precis worker --only fetch`` actually try?".
    """
    sql = """
        WITH stubs AS (
            SELECT r.ref_id,
                   (SELECT id_value FROM ref_identifiers
                     WHERE ref_id = r.ref_id AND id_kind = 'cite_key') AS cite_key,
                   COALESCE(
                     (SELECT id_value FROM ref_identifiers
                       WHERE ref_id = r.ref_id AND id_kind = 'doi'),
                     (SELECT 'arxiv:' || id_value FROM ref_identifiers
                       WHERE ref_id = r.ref_id AND id_kind = 'arxiv'),
                     (SELECT 's2:' || id_value FROM ref_identifiers
                       WHERE ref_id = r.ref_id AND id_kind = 's2')
                   ) AS identifier,
                   r.ref_id AS sort_key
              FROM refs r
             WHERE r.kind = 'paper'
               AND r.pdf_sha256 IS NULL
               AND r.deleted_at IS NULL
               AND EXISTS (
                     SELECT 1 FROM ref_identifiers ri
                      WHERE ri.ref_id = r.ref_id
                        AND ri.id_kind IN ('doi', 'arxiv', 's2')
               )
        ),
        latest_event AS (
            SELECT DISTINCT ON (ref_id) ref_id, ts, source, event
              FROM ref_events
             WHERE source LIKE 'fetcher:%%'
             ORDER BY ref_id, ts DESC
        )
        SELECT s.ref_id, s.cite_key, s.identifier,
               le.ts, le.source, le.event
          FROM stubs s
          LEFT JOIN latest_event le ON le.ref_id = s.ref_id
         WHERE
            CASE WHEN %s::bool THEN
                (le.ref_id IS NULL
                 OR (le.ts < now() - INTERVAL '24 hours' AND le.event <> 'fetch_ok'))
            ELSE TRUE END
         ORDER BY s.sort_key DESC
         LIMIT %s
    """
    out: list[dict[str, Any]] = []
    with store.pool.connection() as conn:
        rows = conn.execute(sql, (awaiting, limit)).fetchall()
    for row in rows:
        out.append({
            "ref_id": int(row[0]),
            "cite_key": row[1] or "",
            "identifier": row[2] or "",
            "last_attempt": row[3].isoformat() if row[3] is not None else "",
            "last_source": row[4] or "",
            "last_event": row[5] or "",
            "state": _state_summary(row[5], row[3]),
        })
    return out


def _state_summary(last_event: str | None, last_ts: Any) -> str:
    """One-line state per stub for operator triage."""
    if last_event is None:
        return "awaiting fetch (never tried)"
    if last_event == "fetch_ok":
        # If the file is on disk and the watcher hasn't ingested it
        # yet, the row will leave the stub backlog as soon as
        # precis_add runs. Until then it lingers — flag explicitly.
        return "PDF downloaded; awaiting watcher ingest"
    if last_event == "no_oa_version":
        return "no OA version available"
    if last_event in ("fetch_failed", "api_error"):
        return f"{last_event} — will retry in 24h"
    if last_event == "rate_limited":
        return "rate-limited — backed off"
    if last_event == "invalid_identifier":
        return "identifier rejected — operator review"
    return last_event


__all__ = ["add_parser", "run"]
