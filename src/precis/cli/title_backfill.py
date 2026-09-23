"""``precis title-backfill`` — re-arm papers stuck at the no-title sentinel.

A DOI-only acquire mints its paper stub with
:data:`precis.identity.PLACEHOLDER_TITLE` and a NULL year; filling those
in is ``paper_meta_enrich``'s job, but that pass wrote everything *except*
``refs.title``/``refs.year`` and then stamped itself done — so the row sat
at ``(no title)`` with a complete Crossref abstract and byline beside it.
This CLI runs the re-arm only
(:func:`precis.ingest.paper_hygiene.requeue_placeholder_title_papers`):
it clears the ``meta.authors_resolved_at`` idempotency stamp so the
(now title-filling) worker pass re-claims those refs on its next cycle.
Dry-run by default; pass ``--apply`` to commit.

No network and no title is written here — this only decides *which* refs
the worker looks at again, under its own hourly cadence and fetch budget.

Deliberately **not** wired into ``precis.workers.paper_reconcile``, unlike
the other hygiene heals. This is a one-shot backfill over rows the old
code left behind: the enrichment pass now fills the title on a stub's
*first* visit, so nothing new accumulates. Running it on a cadence would
also defeat that pass's one-visit-per-ref contract — a placeholder whose
DOI simply doesn't resolve at Crossref would be re-armed, re-fetched and
re-stamped forever.
"""

from __future__ import annotations

import argparse
import sys

from precis.cli._common import resolve_dsn


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``title-backfill`` subparser on ``sub``."""
    p = sub.add_parser(
        "title-backfill",
        help="Re-arm metadata enrichment for papers stuck at '(no title)'.",
        description=(
            "Find live papers still carrying the no-title placeholder "
            "despite having a DOI and an already-completed metadata "
            "enrichment pass, and clear that pass's idempotency stamp so "
            "it re-runs and fills the title/year. Dry-run by default; "
            "--apply to commit."
        ),
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Commit the re-arm. Without this flag the command is a dry-run.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N candidates (default: all).",
    )
    p.add_argument("--database-url", default=None, help="Override PRECIS_DATABASE_URL.")
    return p


def run(args: argparse.Namespace) -> None:
    """Execute ``precis title-backfill``."""
    from precis.config import load_config
    from precis.ingest.paper_hygiene import requeue_placeholder_title_papers
    from precis.runtime import build_runtime

    cfg = load_config()
    dsn = resolve_dsn(args.database_url, cfg=cfg)
    cfg = cfg.model_copy(update={"database_url": dsn})
    runtime = build_runtime(cfg)
    store = runtime.store
    if store is None:
        print(
            "title-backfill: no database configured - set PRECIS_DATABASE_URL",
            file=sys.stderr,
        )
        sys.exit(2)

    dry_run = not args.apply
    mode = "DRY-RUN" if dry_run else "APPLY"
    print(f"title-backfill [{mode}]: limit={args.limit}", file=sys.stderr)

    queued = requeue_placeholder_title_papers(store, dry_run=dry_run, limit=args.limit)
    for ref_id in queued:
        print(f"ref_id={ref_id}")

    verb = "would re-arm" if dry_run else "re-armed"
    print(
        f"\ntitle-backfill [{mode}] done: {verb} {len(queued)} placeholder-title "
        "paper(s). The paper_meta_enrich worker pass fills them on its next "
        "cycle.",
        file=sys.stderr,
    )
    if dry_run and queued:
        print("Re-run with --apply to commit.", file=sys.stderr)


__all__ = ["add_parser", "run"]
