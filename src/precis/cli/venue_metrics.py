"""``precis venue-metrics`` — load Scimago SJR + OpenAlex venue stats (gr606422).

Two legs fill ``venue_metrics`` (migration 0293):

* ``--scimago FILE.csv`` parses one scimagojr yearly export (CC BY) into
  per-ISSN ``sjr``/``sjr_quartile`` rows. The vintage year comes from
  ``--year`` or a 4-digit run in the filename.
* ``--openalex`` fetches ``/sources`` stats (2yr mean citedness +
  h-index) for every distinct ISSN the live paper corpus carries —
  targeted calls, not the whole source dump.

Both upsert idempotently (re-running a vintage refreshes it; an OA
refresh never nulls SJR columns). ``--dry-run`` parses/fetches and
counts but writes nothing; ``--limit N`` caps the OA leg's ISSN sweep
for probe runs.

Usage::

    precis venue-metrics --scimago "scimagojr 2024.csv"
    precis venue-metrics --scimago sjr.csv --year 2024 --dry-run
    precis venue-metrics --openalex --limit 50
    precis venue-metrics --openalex            # full corpus sweep
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from precis.cli._common import resolve_dsn

_HELP = "Load Scimago SJR / OpenAlex journal metrics into venue_metrics."


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``venue-metrics`` subparser on ``sub``."""
    p = sub.add_parser(
        "venue-metrics",
        help=_HELP,
        description=(
            "Load journal venue metrics (gr606422): a Scimago SJR yearly "
            "CSV leg and an OpenAlex /sources leg keyed by the corpus's "
            "distinct ISSNs. Idempotent upserts; --dry-run writes nothing."
        ),
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--scimago",
        metavar="CSV",
        help="scimagojr yearly export (CC BY) — per-ISSN SJR + quartile rows.",
    )
    g.add_argument(
        "--openalex",
        action="store_true",
        help="Fetch OpenAlex /sources stats for the corpus's distinct ISSNs.",
    )
    p.add_argument(
        "--year",
        type=int,
        default=None,
        help="Vintage year stamped on the rows (Scimago: defaults to a "
        "4-digit run in the filename; OpenAlex: defaults to the fetch year).",
    )
    p.add_argument(
        "--email",
        default=None,
        help="Polite-pool mailto for OpenAlex (default contact.polite_email).",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the OpenAlex leg to the first N corpus ISSNs.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse/fetch and count; write nothing.",
    )
    p.add_argument("--database-url", default=None, help="Override PRECIS_DATABASE_URL.")
    return p


def run(args: argparse.Namespace) -> None:
    """Execute ``precis venue-metrics``."""
    from precis import settings
    from precis.ingest import venue_metrics as vm
    from precis.store import Store

    mode = "DRY-RUN" if args.dry_run else "APPLY"
    if args.scimago:
        year = args.year or vm.scimago_year_from_name(args.scimago)
        if year is None:
            raise SystemExit(
                f"venue-metrics: no year in {args.scimago!r} — pass --year YYYY"
            )
        text = Path(args.scimago).read_text(encoding="utf-8-sig")
        rows = vm.parse_scimago_csv(text, year=year)
        written = 0
        if not args.dry_run:
            store = Store.connect(resolve_dsn(args.database_url))
            try:
                written = store.upsert_venue_metrics(rows)
            finally:
                store.close()
        else:
            written = len(rows)
        print(
            f"venue-metrics scimago [{mode}]: {len(rows)} ISSN rows at "
            f"{year} parsed; {written} upserted.",
            file=sys.stderr,
        )
        return

    email = args.email or (settings.get_str("contact.polite_email") or "").strip()
    year = args.year or datetime.now(UTC).year
    store = Store.connect(resolve_dsn(args.database_url))
    try:
        issns = vm.corpus_issns(store)
        rows = list(
            vm.openalex_venue_rows(issns, year=year, email=email, limit=args.limit)
        )
        written = 0 if args.dry_run else store.upsert_venue_metrics(rows)
    finally:
        store.close()
    print(
        f"venue-metrics openalex [{mode}]: {min(len(issns), args.limit or len(issns))} "
        f"corpus ISSNs swept at {year}; {len(rows)} rows fetched, "
        f"{written} upserted.",
        file=sys.stderr,
    )


__all__ = ["add_parser", "run"]
