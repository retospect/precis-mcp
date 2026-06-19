"""``precis fix-metadata`` — repair papers ingested with junk metadata.

One-off (re-runnable) remediation driver around
:func:`precis.ingest.remediate.run_remediation`. Finds local papers whose
title is empty / a generator default ("No Job Name") or whose author list
is empty — the symptom of the pre-fix lookup cascade falling through to
junk embedded ``/Info`` metadata — re-derives their metadata from the
on-disk PDF, and either repairs them (title/authors/year/abstract +
cite_key rename + PDF move + card-chunk rewrite) or tags them
``needs-triage`` for the manual paste-title flow.

Dry-run by default: it prints the planned title + slug rename per paper
and writes nothing. Pass ``--apply`` to commit. Re-running with
``--apply`` is safe and resumable (already-fixed papers no longer match;
``needs-triage`` papers are skipped unless ``--retry-triaged``).
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from precis.cli._common import resolve_dsn


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``fix-metadata`` subparser on ``sub``."""
    p = sub.add_parser(
        "fix-metadata",
        help="Re-derive metadata for papers ingested with junk/empty titles.",
        description=(
            "Repair local papers whose title is empty / 'No Job Name' or "
            "whose authors are empty. Dry-run by default; pass --apply to "
            "write."
        ),
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Commit changes. Without this flag the command is a dry-run.",
    )
    p.add_argument(
        "--corpus-dir",
        action="append",
        type=Path,
        default=None,
        help=(
            "Corpus root to search for PDFs (repeatable). Defaults to the "
            "configured corpus_dir."
        ),
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N suspect papers (default: all).",
    )
    p.add_argument(
        "--retry-triaged",
        action="store_true",
        help="Re-attempt papers already tagged needs-triage.",
    )
    p.add_argument("--database-url", default=None, help="Override PRECIS_DATABASE_URL.")
    return p


def run(args: argparse.Namespace) -> None:
    """Execute ``precis fix-metadata``."""
    from precis.config import load_config
    from precis.ingest.remediate import run_remediation
    from precis.runtime import build_runtime

    cfg = load_config()
    dsn = resolve_dsn(args.database_url, cfg=cfg)
    cfg = cfg.model_copy(update={"database_url": dsn})
    runtime = build_runtime(cfg)
    store = runtime.store
    if store is None:
        print(
            "fix-metadata: no database configured - set PRECIS_DATABASE_URL",
            file=sys.stderr,
        )
        sys.exit(2)

    corpus_dirs: tuple[Path, ...]
    if args.corpus_dir:
        corpus_dirs = tuple(args.corpus_dir)
    elif cfg.corpus_dir:
        corpus_dirs = (Path(cfg.corpus_dir),)
    else:
        corpus_dirs = (Path.home() / "work" / "corpus",)

    dry_run = not args.apply
    mode = "DRY-RUN" if dry_run else "APPLY"
    print(
        f"fix-metadata [{mode}]: corpus={[str(d) for d in corpus_dirs]} "
        f"limit={args.limit}",
        file=sys.stderr,
    )

    outcomes = run_remediation(
        store,
        corpus_dirs,
        dry_run=dry_run,
        limit=args.limit,
        skip_triaged=not args.retry_triaged,
    )

    for o in outcomes:
        print(o.line())

    counts = Counter(o.action for o in outcomes)
    print(
        f"\nfix-metadata [{mode}] done: {len(outcomes)} processed — "
        + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())),
        file=sys.stderr,
    )
    if dry_run and outcomes:
        print("Re-run with --apply to commit.", file=sys.stderr)


__all__ = ["add_parser", "run"]
