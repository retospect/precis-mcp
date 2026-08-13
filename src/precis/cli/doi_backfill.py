"""``precis backfill-dois`` — recover a real DOI for DOI-less paper refs.

Thin CLI over :func:`precis.ingest.doi_backfill.backfill_dois` (see that
module's docstring for the two-phase recovery strategy). Dry-run by
default; ``--apply`` writes.
"""

from __future__ import annotations

import argparse
import sys

from precis import settings
from precis.cli._common import resolve_dsn
from precis.secrets import get_secret


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``backfill-dois`` subparser on ``sub``."""
    p = sub.add_parser(
        "backfill-dois",
        help="Recover a real DOI for DOI-less paper refs (S2 id / title match).",
        description=(
            "Recover a Crossref-usable DOI for kind='paper' refs that have "
            "none: phase A resolves via a carried S2/arXiv id, phase B via "
            "an S2 title-search match (reusing resolve-metadata's gating, "
            "writing only the DOI). Dry-run by default; --apply to write."
        ),
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Write the recovered DOIs. Without this the command is a dry-run.",
    )
    p.add_argument(
        "--limit", type=int, default=None, help="At most N cohort refs (default: all)."
    )
    p.add_argument("--ids", default=None, help="Comma-separated ref_ids (focused run).")
    p.add_argument(
        "--draft",
        default=None,
        help="Backfill only the DOI-less refs this draft cites.",
    )
    p.add_argument(
        "--order",
        choices=("desc", "asc", "random"),
        default="desc",
        help="Cohort order: desc=newest (default), asc=oldest, random=unbiased sample.",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="Per-lookup wall-clock cap, seconds.",
    )
    p.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Politeness pause between lookups, seconds.",
    )
    p.add_argument(
        "--no-id-phase", action="store_true", help="Skip phase A (id match)."
    )
    p.add_argument(
        "--no-title-phase", action="store_true", help="Skip phase B (title match)."
    )
    p.add_argument("--database-url", default=None, help="Override PRECIS_DATABASE_URL.")
    return p


def run(args: argparse.Namespace) -> None:
    """Execute ``precis backfill-dois``."""
    from precis.config import load_config
    from precis.ingest.doi_backfill import backfill_dois
    from precis.runtime import build_runtime

    cfg = load_config()
    dsn = resolve_dsn(args.database_url, cfg=cfg)
    cfg = cfg.model_copy(update={"database_url": dsn})
    store = build_runtime(cfg).store
    if store is None:
        print(
            "backfill-dois: no database configured - set PRECIS_DATABASE_URL",
            file=sys.stderr,
        )
        sys.exit(2)

    # ACATOME_CROSSREF_MAILTO is unset in prod; contact.polite_email is
    # the working Crossref polite-pool contact — see resolve-metadata.
    mailto = get_secret("ACATOME_CROSSREF_MAILTO", store=store) or (
        settings.get_str("contact.polite_email", store=store) or ""
    )
    s2_key = get_secret("SEMANTIC_SCHOLAR_API_KEY", store=store) or ""
    if not s2_key:
        print(
            "backfill-dois: SEMANTIC_SCHOLAR_API_KEY unset — S2 lookups will be "
            "heavily rate-limited (slow).",
            file=sys.stderr,
        )

    ids = [int(x) for x in args.ids.split(",")] if args.ids else None
    apply = args.apply
    mode = "APPLY" if apply else "DRY-RUN"
    print(
        f"backfill-dois [{mode}]: limit={args.limit} order={args.order} "
        f"draft={args.draft} ids={ids}",
        file=sys.stderr,
    )

    result = backfill_dois(
        store,
        apply=apply,
        limit=args.limit,
        ids=ids,
        order=args.order,
        draft=args.draft,
        mailto=mailto,
        s2_api_key=s2_key,
        call_timeout=args.timeout,
        delay=args.delay,
        do_id_phase=not args.no_id_phase,
        do_title_phase=not args.no_title_phase,
    )

    if not args.no_id_phase:
        print(
            f"[{mode}] phase A (id): {len(result.recovered_id)} real DOIs, "
            f"{len(result.id_owned_elsewhere)} skipped (DOI owned by another ref), "
            f"{len(result.id_write_failed)} write-failed (transient error, not a "
            "conflict)",
            file=sys.stderr,
        )
        for rid, doi in list(result.recovered_id.items())[:10]:
            print(f"  id  #{rid} -> {doi}")
        for rid in result.id_write_failed[:10]:
            print(f"  WRITE-FAILED #{rid}")

    if not args.no_title_phase:
        print(
            f"[{mode}] phase B (title): {len(result.recovered_title)} real DOIs, "
            f"{len(result.arxiv_only)} arxiv-only (no DOI exists), "
            f"{len(result.review)} review",
            file=sys.stderr,
        )
        for rid, doi in list(result.recovered_title.items())[:10]:
            print(f"  ttl #{rid} -> {doi}")
        for rid, reason in result.review[:10]:
            print(f"  REVIEW #{rid} {reason}")

    verb = "wrote" if apply else "would write"
    print(
        f"\nbackfill-dois [{mode}] {verb} {result.total_recovered} DOIs over "
        f"{len(result.cohort)} cohort refs "
        f"({len(result.arxiv_only)} are DOI-less preprints — nothing to recover).",
        file=sys.stderr,
    )
    if not apply and result.total_recovered:
        print("Re-run with --apply to write.", file=sys.stderr)


__all__ = ["add_parser", "run"]
