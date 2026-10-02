"""``precis enrich-rearm`` — re-arm metadata enrichment for named papers.

``paper_meta_enrich`` visits each paper once and stamps
``meta.authors_resolved_at``; it claims only unstamped rows. A paper
visited before the pass learned a new field (Crossref ``volume`` /
``issue`` / ``page``, say) therefore never gets it. This CLI clears that
stamp on the papers you name
(:func:`precis.ingest.paper_hygiene.requeue_papers_for_enrich`) so the
worker pass re-claims them on its next cycle. Only papers that carry a DOI
and are currently stamped are selected. Dry-run by default; pass
``--apply`` to commit.

A re-visit is safe: the pass never clobbers human-verified authors
(``refs.human_verified_at``) or any meta field the ref already carries —
every field it writes is fill-blanks-only.

No network is touched here — this only decides *which* refs the worker
looks at again, under its own cadence and fetch budget. Like
``title-backfill`` it is deliberately not on a schedule: it is an operator
one-shot, since a re-armed ref whose DOI doesn't resolve would be
re-fetched and re-stamped forever.
"""

from __future__ import annotations

import argparse
import sys

from precis.cli._common import resolve_dsn


def _parse_refs(raw: str) -> list[int]:
    """``"12,34, 56"`` → ``[12, 34, 56]``; raises ``ValueError`` on junk."""
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            out.append(int(part))
    return out


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``enrich-rearm`` subparser on ``sub``."""
    p = sub.add_parser(
        "enrich-rearm",
        help="Re-arm metadata enrichment for named papers (refills new fields).",
        description=(
            "Clear the metadata-enrichment idempotency stamp on the given "
            "paper refs (those with a DOI) so the paper_meta_enrich worker "
            "re-visits them and fills fields added since their first visit "
            "(e.g. volume/issue/pages). Never clobbers verified authors or "
            "existing fields. Dry-run by default; --apply to commit."
        ),
    )
    p.add_argument(
        "--refs",
        required=True,
        help="Comma-separated paper ref ids, e.g. 12,34,56.",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Commit the re-arm. Without this flag the command is a dry-run.",
    )
    p.add_argument("--database-url", default=None, help="Override PRECIS_DATABASE_URL.")
    return p


def run(args: argparse.Namespace) -> None:
    """Execute ``precis enrich-rearm``."""
    from precis.config import load_config
    from precis.ingest.paper_hygiene import requeue_papers_for_enrich
    from precis.runtime import build_runtime

    try:
        ref_ids = _parse_refs(args.refs)
    except ValueError:
        print(
            f"enrich-rearm: bad --refs {args.refs!r} (want 12,34,56)", file=sys.stderr
        )
        sys.exit(2)

    cfg = load_config()
    dsn = resolve_dsn(args.database_url, cfg=cfg)
    cfg = cfg.model_copy(update={"database_url": dsn})
    runtime = build_runtime(cfg)
    store = runtime.store
    if store is None:
        print(
            "enrich-rearm: no database configured - set PRECIS_DATABASE_URL",
            file=sys.stderr,
        )
        sys.exit(2)

    dry_run = not args.apply
    mode = "DRY-RUN" if dry_run else "APPLY"
    print(f"enrich-rearm [{mode}]: {len(ref_ids)} ref(s) named", file=sys.stderr)

    queued = requeue_papers_for_enrich(store, ref_ids, dry_run=dry_run)
    for ref_id in queued:
        print(f"ref_id={ref_id}")

    verb = "would re-arm" if dry_run else "re-armed"
    skipped = len(set(ref_ids)) - len(queued)
    print(
        f"\nenrich-rearm [{mode}] done: {verb} {len(queued)} paper(s); "
        f"{skipped} skipped (not a live paper, no DOI, or not yet visited). "
        "The paper_meta_enrich worker pass re-visits them on its next cycle.",
        file=sys.stderr,
    )
    if dry_run and queued:
        print("Re-run with --apply to commit.", file=sys.stderr)


__all__ = ["add_parser", "run"]
