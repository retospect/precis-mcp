"""``precis reconcile-duplicates`` — collapse same-file duplicate papers.

Phase 2 of the dedup plan (``docs/design/duplicate-paper-handling.md``):
a standing sweep that finds live paper refs sharing a ``pdf_sha256`` (the
same file ingested as two refs) and merges each group down to the best
survivor (DOI → non-junk title → most authors → lowest id), soft-deleting
the rest with a ``supersedes`` edge + audit trail.

Dry-run by default; pass ``--apply`` to commit. Re-runnable. (Phase 1 —
the re-derived-DOI-conflict class — is handled inside ``fix-metadata``;
this catches exact-file dups that share no resolvable identifier mismatch.)
"""

from __future__ import annotations

import argparse
import sys

from precis.cli._common import resolve_dsn


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``reconcile-duplicates`` subparser on ``sub``."""
    p = sub.add_parser(
        "reconcile-duplicates",
        help="Merge duplicate paper refs (pdf_sha256 / doi-case / fuzzy title).",
        description=(
            "Collapse duplicate paper refs — same file (pdf_sha256), same DOI "
            "modulo case, and id-less title-only stubs that duplicate a held "
            "paper (fuzzy title, high-confidence only) — to the best survivor, "
            "soft-deleting the rest. Ambiguous title matches are flagged for "
            "review, never merged. Dry-run by default; --apply to commit."
        ),
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Commit the merges. Without this flag the command is a dry-run.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N duplicate groups (default: all).",
    )
    p.add_argument("--database-url", default=None, help="Override PRECIS_DATABASE_URL.")
    return p


def run(args: argparse.Namespace) -> None:
    """Execute ``precis reconcile-duplicates``."""
    from precis.config import load_config
    from precis.ingest.dedup import (
        TitleMatchReview,
        reconcile_by_doi_case,
        reconcile_by_pdf_sha256,
        reconcile_by_title_similarity,
    )
    from precis.runtime import build_runtime

    cfg = load_config()
    dsn = resolve_dsn(args.database_url, cfg=cfg)
    cfg = cfg.model_copy(update={"database_url": dsn})
    runtime = build_runtime(cfg)
    store = runtime.store
    if store is None:
        print(
            "reconcile-duplicates: no database configured - set PRECIS_DATABASE_URL",
            file=sys.stderr,
        )
        sys.exit(2)

    dry_run = not args.apply
    mode = "DRY-RUN" if dry_run else "APPLY"
    print(f"reconcile-duplicates [{mode}]: limit={args.limit}", file=sys.stderr)

    # Phase 1b — DOI-case duplicates (stub ↔ ingested paper sharing a DOI
    # modulo case). Runs first: it keeps the chunked copy and normalises stored
    # DOIs to lowercase, which also collapses would-be pdf_sha256 groups.
    doi_outcomes = reconcile_by_doi_case(store, dry_run=dry_run, limit=args.limit)
    for o in doi_outcomes:
        print(o.line())

    # Phase 2 — same-file duplicates (shared pdf_sha256).
    outcomes = reconcile_by_pdf_sha256(store, dry_run=dry_run, limit=args.limit)
    for o in outcomes:
        print(o.line())

    # Phase 3 — id-less title-only stubs that duplicate a held paper (the
    # near-duplicate class that shares no identifier). Auto-merges only the
    # high-confidence band; the ambiguous band is printed for human review,
    # never merged.
    review: list[TitleMatchReview] = []
    title_outcomes = reconcile_by_title_similarity(
        store, dry_run=dry_run, limit=args.limit, review_out=review
    )
    for o in title_outcomes:
        print(o.line())
    for r in review:
        print(r.line())

    all_outcomes = doi_outcomes + outcomes + title_outcomes
    merged = sum(len(o.duplicate_ref_ids) for o in all_outcomes)
    verb = "would merge" if dry_run else "merged"
    review_note = f", {len(review)} flagged for review" if review else ""
    print(
        f"\nreconcile-duplicates [{mode}] done: {verb} {merged} duplicate ref(s) "
        f"across {len(all_outcomes)} group(s) "
        f"({len(doi_outcomes)} doi-case, {len(outcomes)} pdf_sha256, "
        f"{len(title_outcomes)} title){review_note}.",
        file=sys.stderr,
    )
    if dry_run and all_outcomes:
        print("Re-run with --apply to commit.", file=sys.stderr)


__all__ = ["add_parser", "run"]
