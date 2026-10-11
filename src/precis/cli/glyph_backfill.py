"""``precis backfill-glyph-health`` — stamp ``meta.glyph_health`` on old papers.

Thin CLI over :func:`precis.ingest.glyph_backfill.backfill_glyph_health`
(font-level re-check of held PDFs; no re-extraction, no OCR). Run it on a
node that mounts the corpus. Dry-run by default; ``--apply`` writes.
"""

from __future__ import annotations

import argparse
import sys

from precis.cli._common import resolve_dsn


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``backfill-glyph-health`` subparser on ``sub``."""
    p = sub.add_parser(
        "backfill-glyph-health",
        help="Flag old papers whose PDF fonts garble Greek/micro (gr228594).",
        description=(
            "Re-read each held PDF's fonts through the ingest glyph_health "
            "detector and set meta.glyph_health on papers where it fires. "
            "Dry-run by default; --apply to write. Resume with --after."
        ),
    )
    p.add_argument("--apply", action="store_true", help="Write the flags.")
    p.add_argument("--limit", type=int, default=500, help="At most N refs per run.")
    p.add_argument(
        "--after", type=int, default=0, help="Resume after this ref_id (cursor)."
    )
    p.add_argument("--database-url", default=None, help="Override PRECIS_DATABASE_URL.")
    return p


def run(args: argparse.Namespace) -> None:
    """Execute ``precis backfill-glyph-health``."""
    from precis.config import load_config
    from precis.corpus_layout import corpus_roots_from_env
    from precis.ingest.glyph_backfill import backfill_glyph_health
    from precis.runtime import build_runtime

    cfg = load_config()
    dsn = resolve_dsn(args.database_url, cfg=cfg)
    cfg = cfg.model_copy(update={"database_url": dsn})
    store = build_runtime(cfg).store
    if store is None:
        print(
            "backfill-glyph-health: no database configured - set PRECIS_DATABASE_URL",
            file=sys.stderr,
        )
        sys.exit(2)

    mode = "APPLY" if args.apply else "DRY-RUN"
    res = backfill_glyph_health(
        store,
        corpus_roots_from_env(),
        apply=args.apply,
        limit=args.limit,
        after_ref_id=args.after,
    )
    for rid in res.flagged[:50]:
        print(f"  flagged #{rid}")
    print(
        f"backfill-glyph-health [{mode}]: scanned {res.scanned}, "
        f"flagged {len(res.flagged)}, clean {res.clean}, pdf-not-found "
        f"{res.no_pdf}, errors {res.errored}, written {res.written}. "
        f"Resume with --after {res.last_ref_id}.",
        file=sys.stderr,
    )


__all__ = ["add_parser", "run"]
