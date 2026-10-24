"""``precis openalex-snapshot`` — stream OpenAlex snapshot parts, stage edges.

Slice 1 of ``docs/backlog/openalex-snapshot-cites.md``: a one-shot pass over
``s3://openalex`` works ``part_NNNN.gz`` files (anonymous HTTPS, streamed —
never written to disk) that keeps only citation edges touching a held paper
(``ref_identifiers`` ``openalex``/``doi``) into ``openalex_cite_edges``,
checkpointed per file in ``openalex_snapshot_files`` so a re-run resumes.

Usage::

    precis openalex-snapshot --partition updated_date=2016-07-22 --dry-run
    precis openalex-snapshot --partition updated_date=2026-10-14 --limit-files 20

``--dry-run`` reads and counts but writes nothing — it also ignores the
checkpoint state so it reports the full requested set. ``--snapshot-date``
defaults to the manifest's release ``date``.
"""

from __future__ import annotations

import argparse
import re
import sys

from precis.cli._common import resolve_dsn

_PARTITION_RE = re.compile(r"^(?:updated_date=)?(\d{4}-\d{2}-\d{2})$")


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``openalex-snapshot`` subparser on ``sub``."""
    p = sub.add_parser(
        "openalex-snapshot",
        help="Stream OpenAlex snapshot works parts; stage held-paper cite edges.",
        description=(
            "Stream one works partition of the public OpenAlex snapshot "
            "(s3://openalex, anonymous) and stage every citation edge that "
            "touches a held paper into openalex_cite_edges. Idempotent and "
            "resumable per file via openalex_snapshot_files."
        ),
    )
    p.add_argument(
        "--partition",
        required=True,
        help="Works partition: updated_date=YYYY-MM-DD (bare date accepted).",
    )
    p.add_argument(
        "--limit-files",
        type=int,
        default=None,
        help="Read at most N part files (default: the whole partition).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Count kept edges; write nothing (checkpoint state ignored).",
    )
    p.add_argument(
        "--snapshot-date",
        default=None,
        help="Release date stamped on the rows (default: manifest.json 'date').",
    )
    p.add_argument("--database-url", default=None, help="Override PRECIS_DATABASE_URL.")
    return p


def run(args: argparse.Namespace) -> None:
    """Execute ``precis openalex-snapshot``."""
    from precis.store import Store
    from precis.workers import openalex_snapshot as oas

    m = _PARTITION_RE.match(args.partition.strip())
    if not m:
        raise SystemExit(
            f"openalex-snapshot: --partition {args.partition!r} is not "
            "updated_date=YYYY-MM-DD"
        )
    mode = "DRY-RUN" if args.dry_run else "APPLY"
    store = Store.connect(resolve_dsn(args.database_url))
    try:
        try:
            summary = oas.run_partition(
                store,
                partition=m.group(1),
                snapshot_date=args.snapshot_date,
                dry_run=args.dry_run,
                limit_files=args.limit_files,
            )
        except ValueError as exc:
            raise SystemExit(
                f"openalex-snapshot: {exc} — pass --snapshot-date YYYY-MM-DD"
            ) from exc
    finally:
        store.close()

    print(
        f"openalex-snapshot [{mode}]: {summary.files_done} done, "
        f"{summary.files_skipped} skipped, {summary.files_failed} failed — "
        f"{summary.records_seen} records, {summary.edges_kept} edges kept "
        f"(held map: {summary.held_openalex} openalex + {summary.held_doi} doi).",
        file=sys.stderr,
    )
    for o in summary.outcomes:
        if o.status == "failed":
            print(f"  failed {o.s3_key}: {o.error}", file=sys.stderr)


__all__ = ["add_parser", "run"]
