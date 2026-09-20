"""``precis markup-backfill`` — re-queue front-matter-only Elsevier previews.

gr372781 item 3: Elsevier's Article Retrieval API can return a well-formed
but entitlement-limited preview PDF (title/affiliations/abstract/first
paragraphs, no references) that ingests silently as a handful of body
chunks — invisible to the ordinary stranded-fetch heal because it DOES
carry a ``pdf_sha256``. This CLI runs the detector
(:func:`precis.ingest.paper_hygiene.requeue_front_matter_only_papers`)
only: it pins the affected refs and clears their fetch backoff so the
fetcher's markup leg re-claims them. Dry-run by default; pass ``--apply``
to commit.

Deliberately **not** wired into ``precis.workers.paper_reconcile`` — the
detector fires on ~7800 prod papers and the operator wants to eyeball a
dry-run before anything re-queues automatically. Worker wiring is a
deliberate follow-up, not done here.
"""

from __future__ import annotations

import argparse
import sys

from precis.cli._common import resolve_dsn


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``markup-backfill`` subparser on ``sub``."""
    p = sub.add_parser(
        "markup-backfill",
        help="Re-queue front-matter-only Elsevier preview papers for re-fetch.",
        description=(
            "Detect live papers whose only body is Elsevier's "
            "entitlement-limited preview page (footer boilerplate present, "
            "no reference list) and pin them for re-fetch via the "
            "markup-first leg. Dry-run by default; --apply to commit."
        ),
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Commit the re-queue. Without this flag the command is a dry-run.",
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
    """Execute ``precis markup-backfill``."""
    from precis.config import load_config
    from precis.ingest.paper_hygiene import requeue_front_matter_only_papers
    from precis.runtime import build_runtime

    cfg = load_config()
    dsn = resolve_dsn(args.database_url, cfg=cfg)
    cfg = cfg.model_copy(update={"database_url": dsn})
    runtime = build_runtime(cfg)
    store = runtime.store
    if store is None:
        print(
            "markup-backfill: no database configured - set PRECIS_DATABASE_URL",
            file=sys.stderr,
        )
        sys.exit(2)

    dry_run = not args.apply
    mode = "DRY-RUN" if dry_run else "APPLY"
    print(f"markup-backfill [{mode}]: limit={args.limit}", file=sys.stderr)

    queued = requeue_front_matter_only_papers(store, dry_run=dry_run, limit=args.limit)
    for ref_id in queued:
        print(f"ref_id={ref_id}")

    verb = "would re-queue" if dry_run else "re-queued"
    print(
        f"\nmarkup-backfill [{mode}] done: {verb} {len(queued)} "
        f"front-matter-only paper(s).",
        file=sys.stderr,
    )
    if dry_run and queued:
        print("Re-run with --apply to commit.", file=sys.stderr)


__all__ = ["add_parser", "run"]
