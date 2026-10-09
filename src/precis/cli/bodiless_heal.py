"""``precis bodiless-heal`` — judge and re-extract papers that hold a PDF but no body.

gr453860: a promoted paper can carry ``pdf_sha256`` with no ``ord >= 0``
chunk and sit in no queue. This CLI runs
:func:`precis.ingest.paper_hygiene.heal_bodiless_pdfs` — the same heal the
``paper_reconcile`` worker pass runs on its cadence — but under an
operator's eye and without the pass's per-run Marker cap, so the readable
tail can be drained in one sitting. Dry-run by default: prints the verdict
each candidate *would* get (preview / missing_file / unreadable /
readable) and writes nothing; ``--apply`` journals the verdicts and runs
Marker over the readable ones.

PDFs are resolved on *this* node (``--corpus-dir`` or ``PRECIS_CORPUS_DIR``);
a file only another node holds is reported ``deferred`` and left for that
node's pass.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from precis.cli._common import resolve_dsn


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``bodiless-heal`` subparser on ``sub``."""
    p = sub.add_parser(
        "bodiless-heal",
        help="Judge and re-extract papers that hold a PDF but no body text.",
        description=(
            "Find live papers with pdf_sha256 set and no body chunk, journal a "
            "verdict per paper (preview / missing_file / unreadable) and re-run "
            "Marker over the readable ones from the stored PDF. Dry-run by "
            "default; --apply to commit."
        ),
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Journal verdicts and extract. Without this flag the command is a dry-run.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Consider at most N candidates (default: all).",
    )
    p.add_argument(
        "--extract-limit",
        type=int,
        default=None,
        help="Run Marker over at most N readable PDFs (default: no cap).",
    )
    p.add_argument(
        "--marker-timeout-s",
        type=float,
        default=None,
        help="Marker wall-clock budget per PDF (default: the heal's 900 s).",
    )
    p.add_argument(
        "--corpus-dir",
        action="append",
        type=Path,
        default=None,
        help=(
            "Corpus root to resolve PDFs under (repeatable). Default: "
            "PRECIS_CORPUS_DIR, then the configured corpus_dir."
        ),
    )
    p.add_argument("--database-url", default=None, help="Override PRECIS_DATABASE_URL.")
    return p


def run(args: argparse.Namespace) -> None:
    """Execute ``precis bodiless-heal``."""
    from precis.config import load_config
    from precis.corpus_layout import corpus_roots_from_env
    from precis.ingest.paper_hygiene import (
        BODILESS_MARKER_TIMEOUT_S,
        heal_bodiless_pdfs,
    )
    from precis.runtime import build_runtime

    cfg = load_config()
    dsn = resolve_dsn(args.database_url, cfg=cfg)
    cfg = cfg.model_copy(update={"database_url": dsn})
    runtime = build_runtime(cfg)
    store = runtime.store
    if store is None:
        print(
            "bodiless-heal: no database configured - set PRECIS_DATABASE_URL",
            file=sys.stderr,
        )
        sys.exit(2)

    corpus_dirs: tuple[Path, ...]
    if args.corpus_dir:
        corpus_dirs = tuple(args.corpus_dir)
    elif cfg.corpus_dir:
        corpus_dirs = (Path(cfg.corpus_dir),)
    else:
        corpus_dirs = corpus_roots_from_env()

    dry_run = not args.apply
    mode = "DRY-RUN" if dry_run else "APPLY"
    print(
        f"bodiless-heal [{mode}]: corpus={[str(d) for d in corpus_dirs]} "
        f"limit={args.limit} extract_limit={args.extract_limit}",
        file=sys.stderr,
    )

    outcomes = heal_bodiless_pdfs(
        store,
        corpus_dirs=corpus_dirs,
        dry_run=dry_run,
        limit=args.limit,
        extract_limit=args.extract_limit,
        marker_timeout_s=(
            args.marker_timeout_s
            if args.marker_timeout_s is not None
            else BODILESS_MARKER_TIMEOUT_S
        ),
    )
    for o in outcomes:
        print(o.line())

    counts: dict[str, int] = {}
    for o in outcomes:
        counts[o.outcome] = counts.get(o.outcome, 0) + 1
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing"
    print(
        f"\nbodiless-heal [{mode}] done: {len(outcomes)} candidate(s) — {summary}.",
        file=sys.stderr,
    )
    if dry_run and outcomes:
        print("Re-run with --apply to journal verdicts and extract.", file=sys.stderr)


__all__ = ["add_parser", "run"]
