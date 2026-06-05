"""``precis jobs check-provenance`` + ``precis jobs sync-retraction-watch``.

Two subcommands sharing the provenance module:

- ``check-provenance`` — DOI preflight against Crossref for the
  manuscript-release workflow. One DOI per line; report grouped by
  severity.
- ``sync-retraction-watch`` — monthly ETL that pulls the RW dataset
  (Crossref-distributed, CC-BY) into ``provenance_rw_cache`` so the
  check pulls reason codes alongside notice DOIs.

The preflight use case from ``docs/design/provenance-kind-plan.md``: an
operator has 250 papers cited in a manuscript and wants to know
before release which ones are retracted, under expression of concern,
or corrected. Run:

    precis jobs check-provenance --refs preflight.txt --out preflight.md

``preflight.txt`` is one DOI per line; ``#`` comments and blank
lines are stripped. The CLI does the same work as
``get(kind='provenance', q='...')`` but with file-based I/O for the
"sit down and audit my bibliography" workflow.

Out of scope here: bibtex parsing (deferred per the plan — keep the
dependency surface honest), DSN / store wiring (the CLI runs
store=None by default, so notice ingest does NOT happen; use the
MCP surface when you want write-through).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from precis.cli._common import resolve_dsn

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parser registration
# ---------------------------------------------------------------------------


def add_parsers(sub: argparse._SubParsersAction) -> None:
    """Register both provenance subcommands on a shared parsers action."""
    _add_check_parser(sub)
    _add_sync_parser(sub)


def _add_check_parser(sub: argparse._SubParsersAction) -> None:
    """Register ``precis jobs check-provenance``."""
    p = sub.add_parser(
        "check-provenance",
        help=(
            "Check a batch of DOIs against Crossref for retractions, "
            "expressions of concern, and corrections."
        ),
    )
    p.add_argument(
        "--doi",
        action="append",
        default=[],
        metavar="DOI",
        help="DOI to check. Repeatable: --doi 10.x/a --doi 10.x/b",
    )
    p.add_argument(
        "--refs",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Read DOIs from a text file (one per line). Lines starting "
            "with '#' are comments; blank lines and bullet markers "
            "(- * +) are stripped."
        ),
    )
    p.add_argument(
        "--view",
        choices=("default", "blockers", "json"),
        default="default",
        help=(
            "Report shape. 'default' = full triaged markdown; "
            "'blockers' = only 🔴/🟠; 'json' = structured payload."
        ),
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        metavar="PATH",
        help="Write report to a file instead of stdout.",
    )
    p.add_argument(
        "--max-workers",
        type=int,
        default=8,
        help=(
            "Max parallel Crossref requests (default 8). Tune higher "
            "for very large batches with the polite-pool mailto set."
        ),
    )
    p.add_argument(
        "--database-url",
        default=None,
        help=(
            "PG DSN. When provided, notice DOIs are auto-ingested and "
            "STATUS tags applied to papers found in the local store. "
            "When omitted, the report is informational only."
        ),
    )
    p.add_argument(
        "--mailto",
        default=None,
        help=(
            "Crossref polite-pool email. Defaults to "
            "PRECIS_CROSSREF_MAILTO. Strongly recommended for batches "
            ">50 DOIs."
        ),
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _add_sync_parser(sub: argparse._SubParsersAction) -> None:
    """Register ``precis jobs sync-retraction-watch``."""
    p = sub.add_parser(
        "sync-retraction-watch",
        help=(
            "Pull the Retraction Watch dataset (CC-BY via Crossref) into "
            "the local provenance cache. Run monthly via cron."
        ),
    )
    p.add_argument(
        "--database-url",
        default=None,
        help="PG DSN. Required — the sync writes into provenance_rw_cache.",
    )
    p.add_argument(
        "--mailto",
        default=None,
        help=(
            "Crossref polite-pool email. Required to use the Labs API "
            "primary source; without it the job goes straight to the "
            "GitLab mirror. Defaults to PRECIS_CROSSREF_MAILTO."
        ),
    )
    p.add_argument(
        "--source",
        choices=("auto", "labs", "gitlab"),
        default="auto",
        help=(
            "Force a specific source. 'auto' (default) tries Labs first "
            "with --mailto, falls back to GitLab on failure. 'labs' "
            "requires --mailto. 'gitlab' skips Labs entirely."
        ),
    )


def run(args: argparse.Namespace) -> None:
    """Entry point for ``precis jobs check-provenance``."""
    from precis.handlers._provenance_report import render_batch
    from precis.ingest.provenance import check_dois, parse_doi_list

    raw_inputs: list[str] = list(args.doi)
    if args.refs is not None:
        try:
            text = args.refs.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"check-provenance: cannot read {args.refs}: {exc}", file=sys.stderr)
            sys.exit(2)
        raw_inputs.append(text)

    if not raw_inputs:
        print(
            "check-provenance: pass at least one --doi or a --refs file",
            file=sys.stderr,
        )
        sys.exit(2)

    # parse_doi_list handles each input string; concatenate token
    # lists across all inputs to preserve user order (--doi entries
    # come first, then file contents).
    dois: list[str] = []
    for raw in raw_inputs:
        dois.extend(parse_doi_list(raw))

    if not dois:
        print("check-provenance: no DOIs found in input", file=sys.stderr)
        sys.exit(2)

    # Mailto resolution: CLI flag → env. We never silently send no
    # mailto for a batch >50, since that's discourteous on the
    # polite pool — warn instead.
    mailto = args.mailto or os.environ.get("PRECIS_CROSSREF_MAILTO") or None
    if mailto is None and len(dois) > 50:
        log.warning(
            "check-provenance: %d DOIs without a polite-pool mailto. "
            "Set PRECIS_CROSSREF_MAILTO or pass --mailto to be a good "
            "Crossref citizen.",
            len(dois),
        )

    # Store wiring is optional. When --database-url is provided (or
    # PRECIS_DATABASE_URL is set), we open a store so write-through
    # to local paper refs happens. Otherwise the report is read-only.
    store = None
    if args.database_url is not None or os.environ.get("PRECIS_DATABASE_URL"):
        from precis.store import Store

        try:
            dsn = resolve_dsn(args.database_url)
        except Exception as exc:
            print(f"check-provenance: cannot resolve DSN: {exc}", file=sys.stderr)
            sys.exit(2)
        store = Store.connect(dsn)

    log.info("check-provenance: %d DOIs, view=%s", len(dois), args.view)
    results = check_dois(
        dois,
        store=store,
        mailto=mailto,
        max_workers=args.max_workers,
    )

    body = render_batch(results, view=args.view)  # type: ignore[arg-type]

    if args.out is not None:
        args.out.write_text(body, encoding="utf-8")
        log.info("check-provenance: wrote %d bytes to %s", len(body), args.out)
    else:
        sys.stdout.write(body)


def run_sync(args: argparse.Namespace) -> None:
    """Entry point for ``precis jobs sync-retraction-watch``."""
    from precis.jobs.provenance_rw_sync import run_sync as do_sync
    from precis.store import Store

    try:
        dsn = resolve_dsn(args.database_url)
    except Exception as exc:
        print(f"sync-retraction-watch: cannot resolve DSN: {exc}", file=sys.stderr)
        sys.exit(2)

    mailto = args.mailto or os.environ.get("PRECIS_CROSSREF_MAILTO") or None
    if args.source == "labs" and not mailto:
        print(
            "sync-retraction-watch: --source=labs requires --mailto "
            "or PRECIS_CROSSREF_MAILTO (Crossref polite-pool convention)",
            file=sys.stderr,
        )
        sys.exit(2)

    force = None if args.source == "auto" else args.source

    store = Store.connect(dsn)
    result = do_sync(store=store, mailto=mailto, force_source=force)

    log.info(
        "sync-retraction-watch: status=%s, source=%s, rows=%d",
        result.status,
        result.source_url,
        result.rows_upserted,
    )
    if result.status != "ok":
        print(
            f"sync-retraction-watch: {result.status} — {result.error or 'see logs'}",
            file=sys.stderr,
        )
        sys.exit(1 if result.status == "failed" else 0)
