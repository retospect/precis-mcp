"""Operator CLI for saved SEC EDGAR full-text watches."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC

from precis.cli._common import bind_process_store, resolve_dsn
from precis.cli.patent import _parse_interval
from precis.config import edgar_raw_root, edgar_user_agent
from precis.errors import PrecisError
from precis.handlers import _edgar_watch_db as watch_db
from precis.handlers._edgar_client import EdgarClient, EdgarError
from precis.handlers._edgar_query import build_fts_params
from precis.jobs.edgar_watch import run_one_pass
from precis.store import Store


def add_parsers(sub: argparse._SubParsersAction) -> None:
    """Register the three saved-watch commands."""
    watch = sub.add_parser(
        "watch-edgar", help="Create or delete a saved EDGAR FTS watch."
    )
    watch.add_argument("query", nargs="?", help="Full-text keywords or phrase.")
    watch.add_argument("--name", required=True, help="Watch slug.")
    watch.add_argument("--form", action="append", help="SEC form (repeatable).")
    watch.add_argument("--cik", action="append", help="Issuer CIK (repeatable).")
    watch.add_argument(
        "--every", default="7d", help="Cadence, e.g. 1h, 1d or 1w (default 7d)."
    )
    watch.add_argument("--delete", action="store_true", help="Delete the named watch.")

    listing = sub.add_parser("list-edgar-watches", help="List saved EDGAR watches.")
    listing.add_argument(
        "--show-params", action="store_true", help="Print saved FTS parameters."
    )

    runner = sub.add_parser("run-edgar-watches", help="Run due EDGAR watches once.")
    runner.add_argument("--name", help="Force a run for one named watch.")
    runner.add_argument(
        "--dry-run", action="store_true", help="Search without ingest or watch updates."
    )
    runner.add_argument(
        "--fair-use-limit-gb",
        type=float,
        help="Override the rolling seven-day GiB limit.",
    )
    for parser in (watch, listing, runner):
        parser.add_argument("--database-url", help="Override the database URL.")


def run_watch(args: argparse.Namespace) -> None:
    """Create or remove a saved watch."""
    store = Store.connect(resolve_dsn(args.database_url))
    try:
        if args.delete:
            watch_db.delete(store, args.name)
            print(f"Deleted EDGAR watch {args.name!r}")
            return
        tags = [f"form:{form}" for form in args.form or []]
        tags.extend(f"cik:{cik}" for cik in args.cik or [])
        watch = watch_db.create(
            store,
            name=args.name,
            params=build_fts_params(q=args.query, tags=tags),
            interval_s=_parse_interval(args.every),
        )
        print(f"Created EDGAR watch {watch.name!r} (every {watch.interval_s}s)")
    except (PrecisError, ValueError) as exc:
        print(f"watch-edgar: {exc}", file=sys.stderr)
        sys.exit(2)
    finally:
        store.close()


def run_list(args: argparse.Namespace) -> None:
    """Display the watches and their checkpoints."""
    store = Store.connect(resolve_dsn(args.database_url))
    try:
        watches = watch_db.list_all(store)
        if not watches:
            print("No saved EDGAR watches")
        for watch in watches:
            last = watch.last_run_at
            stamp = last.astimezone(UTC).isoformat() if last is not None else "never"
            print(
                f"{watch.name}  every={watch.interval_s}s  "
                f"last={stamp}  seen={len(watch.last_seen_accessions)}"
            )
            if args.show_params:
                print(f"  {json.dumps(watch.params, sort_keys=True)}")
    finally:
        store.close()


def run_runner(args: argparse.Namespace) -> None:
    """Execute the CLI or launchd watch pass."""
    store = Store.connect(resolve_dsn(args.database_url))
    bind_process_store(store)
    try:
        summary = run_one_pass(
            store=store,
            client=EdgarClient(user_agent=edgar_user_agent()),
            raw_root=edgar_raw_root(),
            only_name=args.name,
            dry_run=args.dry_run,
            limit_gb=args.fair_use_limit_gb,
        )
    except (EdgarError, PrecisError, ValueError) as exc:
        print(f"run-edgar-watches: {exc}", file=sys.stderr)
        sys.exit(2)
    finally:
        store.close()
    if summary.busy:
        print("An EDGAR watch pass is already running")
    elif summary.paused:
        print(
            f"EDGAR watches paused at {summary.rolling_bytes / 1024**3:.2f} GiB over seven days"
        )
    elif not summary.results:
        print("No EDGAR watches due")
    for result in summary.results:
        print(
            f"{result.name}: new={len(result.new_accessions)} "
            f"completed={len(result.completed)} errors={len(result.errors)}"
        )
        for error in result.errors:
            print(f"  {error}", file=sys.stderr)
    if any(result.errors for result in summary.results):
        sys.exit(1)
