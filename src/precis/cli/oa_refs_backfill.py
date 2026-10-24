"""``precis backfill-oa-refs`` — source reference lists from OpenAlex
``referenced_works`` for papers bib_parse never parsed (gr513755 slice 2).

Thin CLI over :func:`precis.backfill.oa_refs.backfill_oa_refs`. One
OpenAlex work call per paper (zero when ``meta.openalex`` already caches
the list), through the shared ``openalex`` limiter row — the run stops
cleanly when the daily cap refuses. ``--limit`` bounds papers per run;
the sweep is idempotent (any existing ``paper_bib_entries`` row skips the
paper) so re-running continues where the last run stopped.

Usage::

    precis backfill-oa-refs --dry-run            # count candidates only
    precis backfill-oa-refs --limit 50           # smoke batch
    precis backfill-oa-refs --limit 10000        # the full sweep
"""

from __future__ import annotations

import argparse
import sys

from precis import settings
from precis.cli._common import resolve_dsn


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``backfill-oa-refs`` subparser on ``sub``."""
    p = sub.add_parser(
        "backfill-oa-refs",
        help="Source paper_bib_entries from OpenAlex referenced_works (gr513755).",
        description=(
            "For each live paper with a references chunk, no bib entries "
            "and a DOI/OpenAlex id: fetch the OpenAlex work's "
            "referenced_works, write one OpenAlex-sourced bib entry per "
            "referenced work, resolve held targets, mint cites links. "
            "Newest first, idempotent, stops on the shared openalex "
            "limiter's daily-cap refusal."
        ),
    )
    p.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Max papers to sweep this run (default 100).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Count candidates only — no fetch, no writes.",
    )
    p.add_argument(
        "--email",
        default=None,
        help="Contact email for OpenAlex's polite pool "
        "(default contact.polite_email / PRECIS_UNPAYWALL_EMAIL).",
    )
    p.add_argument("--database-url", default=None, help="Override PRECIS_DATABASE_URL.")
    return p


def run(args: argparse.Namespace) -> None:
    """Execute ``precis backfill-oa-refs``."""
    from precis.backfill.oa_refs import backfill_oa_refs, unparsed_candidates
    from precis.config import load_config
    from precis.runtime import build_runtime

    cfg = load_config()
    dsn = resolve_dsn(args.database_url, cfg=cfg)
    cfg = cfg.model_copy(update={"database_url": dsn})
    store = build_runtime(cfg).store
    if store is None:
        print(
            "backfill-oa-refs: no database configured - set PRECIS_DATABASE_URL",
            file=sys.stderr,
        )
        sys.exit(2)

    if args.dry_run:
        n = len(unparsed_candidates(store, limit=args.limit))
        print(
            f"backfill-oa-refs [DRY-RUN]: {n} candidate papers (limit={args.limit}).",
            file=sys.stderr,
        )
        return

    email = args.email or (settings.get_str("contact.polite_email") or "").strip()
    print(f"backfill-oa-refs: sweeping up to {args.limit} papers", file=sys.stderr)
    r = backfill_oa_refs(store, limit=args.limit, email=email)

    print(
        f"backfill-oa-refs: {r.candidates} candidates — {r.written} written "
        f"({r.entries} entries, {r.held} held, {r.links} cites links ensured; "
        f"{r.fetches} fetches), {r.skipped} skipped, {r.miss} not-in-openalex, "
        f"{r.empty} no-references, {r.no_identifier} no-identifier, "
        f"{r.failed} failed.",
        file=sys.stderr,
    )
    if r.rate_limited:
        print(
            "backfill-oa-refs: stopped — shared openalex limiter refused "
            "(daily cap or rate-starved); re-run to continue.",
            file=sys.stderr,
        )


__all__ = ["add_parser", "run"]
