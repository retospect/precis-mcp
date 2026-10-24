"""``precis backfill-bib-cites`` — mint paper→paper ``cites`` links from
already-matched bib entries.

Thin CLI over :func:`precis.backfill.bib_cites.backfill_bib_cites` (see that
module's docstring for the selection). Dry-run by default; ``--apply`` writes.
Run once post-deploy to cover entries matched before bib_parse minted links
at match time; new matches keep their own edges via the match write.
"""

from __future__ import annotations

import argparse
import sys

from precis.cli._common import resolve_dsn


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``backfill-bib-cites`` subparser on ``sub``."""
    p = sub.add_parser(
        "backfill-bib-cites",
        help="Mint paper→paper cites links from matched bib entries (gr513755).",
        description=(
            "Mint one paper-level 'cites' link per distinct held_ref_id among "
            "matched paper_bib_entries rows that lack one (self-cites and "
            "retired targets excluded). Dry-run by default; --apply to write."
        ),
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Write the links. Without this the command is a dry-run.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="At most N missing pairs (default: all).",
    )
    p.add_argument(
        "--batch",
        type=int,
        default=200,
        help="Citing papers per write transaction (default: 200).",
    )
    p.add_argument("--database-url", default=None, help="Override PRECIS_DATABASE_URL.")
    return p


def run(args: argparse.Namespace) -> None:
    """Execute ``precis backfill-bib-cites``."""
    from precis.backfill.bib_cites import backfill_bib_cites
    from precis.config import load_config
    from precis.runtime import build_runtime

    cfg = load_config()
    dsn = resolve_dsn(args.database_url, cfg=cfg)
    cfg = cfg.model_copy(update={"database_url": dsn})
    store = build_runtime(cfg).store
    if store is None:
        print(
            "backfill-bib-cites: no database configured - set PRECIS_DATABASE_URL",
            file=sys.stderr,
        )
        sys.exit(2)

    apply = args.apply
    mode = "APPLY" if apply else "DRY-RUN"
    print(
        f"backfill-bib-cites [{mode}]: limit={args.limit} batch={args.batch}",
        file=sys.stderr,
    )

    result = backfill_bib_cites(
        store, apply=apply, limit=args.limit, batch_refs=args.batch
    )

    verb = "ensured" if apply else "would ensure"
    print(
        f"backfill-bib-cites [{mode}] {verb} {result.links_ensured or result.pairs_missing} "
        f"cites links over {result.papers} citing papers "
        f"({result.pairs_missing} missing pairs selected).",
        file=sys.stderr,
    )
    if not apply and result.pairs_missing:
        print("Re-run with --apply to write.", file=sys.stderr)


__all__ = ["add_parser", "run"]
