"""``precis backfill-ref-bytes`` — fill the ``ref_body_bytes`` side
table after 0266.

0266 stores the per-ref body byte count (``sum(octet_length(text))``
over live ``ord >= 0`` chunks) in ``ref_body_bytes``, kept current off
the write path by the ``ref_body_bytes`` worker passes. Refs with no
row yet render no size — this command fills them without one
table-wide write: batches of ``--batch`` refs (default 200), one short
transaction per batch, resumable — "no side-table row" is the resume
cursor, so a killed run picks up where it stopped (``--after`` skips
ahead explicitly). ``--refresh`` recomputes rows that already have one
(the daily verify pass's job, runnable by hand). Idempotent, safe to
re-run, and it never takes a lock on ``refs``: it reads ``chunks`` and
writes only the side table.

Dry-run by default (counts only); ``--apply`` writes.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass

import psycopg
from psycopg import Connection

from precis.cli._common import resolve_dsn

#: Refs with no side-table row past the cursor — the default (fill)
#: batch pick.
_BATCH_IDS_SQL = (
    "SELECT r.ref_id FROM refs r "
    "WHERE r.ref_id > %s "
    "  AND NOT EXISTS (SELECT 1 FROM ref_body_bytes b "
    "                  WHERE b.ref_id = r.ref_id) "
    "ORDER BY r.ref_id LIMIT %s"
)
#: ``--refresh`` picks every ref past the cursor — existing rows are
#: recomputed, not skipped.
_REFRESH_IDS_SQL = "SELECT ref_id FROM refs WHERE ref_id > %s ORDER BY ref_id LIMIT %s"


@dataclass
class BackfillResult:
    """Outcome of one :func:`backfill_ref_bytes` run."""

    unfilled_before: int
    updated: int
    last_ref_id: int
    unfilled_after: int | None = None


def _unfilled(conn: Connection) -> int:
    row = conn.execute(
        "SELECT count(*) FROM refs r "
        "WHERE NOT EXISTS (SELECT 1 FROM ref_body_bytes b "
        "                  WHERE b.ref_id = r.ref_id)"
    ).fetchone()
    return int(row[0]) if row else 0


def backfill_ref_bytes(
    conn: Connection,
    *,
    apply: bool,
    batch: int = 200,
    after: int = 0,
    sleep_s: float = 0.2,
    batch_timeout_s: int = 60,
    refresh: bool = False,
) -> BackfillResult:
    """Fill ``ref_body_bytes`` in ``batch``-sized ref_id windows.

    Each window selects the lowest ``batch`` unfilled ids past
    ``after`` (every ref when ``refresh``), recomputes them from live
    chunks and upserts the side table in the same transaction — a
    re-run after a kill touches only what is left. On an autocommit
    ``conn`` each window commits separately (the nested
    ``conn.transaction()``); on a transactional one they nest as
    savepoints, as the tests do. No statement here touches ``refs``
    for write — the only lock is on the ``ref_body_bytes`` rows the
    batch owns.
    """
    from precis.workers.ref_body_bytes import recompute_body_byte_counts

    before = _unfilled(conn)
    if not apply:
        return BackfillResult(before, 0, after)
    pick = _REFRESH_IDS_SQL if refresh else _BATCH_IDS_SQL
    updated = 0
    last = after
    while True:
        with conn.transaction():
            conn.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (f"{int(batch_timeout_s)}s",),
            )
            ids = [int(r[0]) for r in conn.execute(pick, (last, batch)).fetchall()]
            if not ids:
                break
            n = recompute_body_byte_counts(conn, ids)
        updated += n
        last = max(ids)
        if sleep_s and n:
            time.sleep(sleep_s)
    return BackfillResult(before, updated, last, _unfilled(conn))


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``backfill-ref-bytes`` subparser on ``sub``."""
    p = sub.add_parser(
        "backfill-ref-bytes",
        help="Fill the ref_body_bytes side table from live chunk text (0266).",
        description=(
            "Fill ref_body_bytes (sum of live body-chunk octet_length, "
            "0266) in batches — a missing row is the resume cursor, so "
            "a re-run continues where the last stopped. Dry-run by "
            "default."
        ),
    )
    p.add_argument("--apply", action="store_true", help="Write.")
    p.add_argument("--batch", type=int, default=200, help="Refs per transaction.")
    p.add_argument(
        "--after",
        type=int,
        default=0,
        help="Resume after this ref_id (batches still skip filled rows).",
    )
    p.add_argument(
        "--refresh",
        action="store_true",
        help="Recompute refs that already have a row (default fills only missing).",
    )
    p.add_argument("--sleep", type=float, default=0.2, help="Seconds between batches.")
    p.add_argument(
        "--batch-timeout",
        type=int,
        default=60,
        help="statement_timeout (s) per batch.",
    )
    p.add_argument("--database-url", default=None, help="Override PRECIS_DATABASE_URL.")
    return p


def run(args: argparse.Namespace) -> None:
    """Execute ``precis backfill-ref-bytes``."""
    dsn = resolve_dsn(args.database_url)
    mode = "APPLY" if args.apply else "DRY-RUN"
    with psycopg.connect(dsn, autocommit=True, connect_timeout=30) as conn:
        res = backfill_ref_bytes(
            conn,
            apply=args.apply,
            batch=args.batch,
            after=args.after,
            sleep_s=args.sleep,
            batch_timeout_s=args.batch_timeout,
            refresh=args.refresh,
        )
    line = (
        f"backfill-ref-bytes [{mode}]: unfilled before "
        f"{res.unfilled_before}, updated {res.updated}"
        + (
            f", unfilled after {res.unfilled_after}"
            if res.unfilled_after is not None
            else ""
        )
    )
    if res.unfilled_after:
        line += f". Resume with --after {res.last_ref_id}"
    print(line, file=sys.stderr)


__all__ = ["BackfillResult", "add_parser", "backfill_ref_bytes", "run"]
