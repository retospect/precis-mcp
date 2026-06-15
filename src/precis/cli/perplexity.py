"""``precis jobs import-perplexity`` — bulk import Perplexity reports.

Walks a directory of markdown reports (typically exported from the
Perplexity web UI by a Pro subscriber) and runs each through
``put(mode='import')`` on the chosen kind (``websearch`` /
``perplexity-reasoning`` / ``perplexity-research``). Reuses the
existing cache wiring so imported content is indistinguishable from
content fetched via the paid API.

The ``id=`` query per file is derived with one of two heuristics:

* ``h1``: use the first ``# Heading`` line; fall back to filename.
* ``filename``: use the stem, normalised for whitespace.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from precis.cli._common import resolve_dsn


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``import-perplexity`` subparser on ``sub``."""
    ip = sub.add_parser(
        "import-perplexity",
        help="Bulk put(mode='import') a directory of Perplexity reports.",
    )
    ip.add_argument(
        "dir",
        help="Directory to walk (recursively) for report files.",
    )
    ip.add_argument(
        "--kind",
        choices=("websearch", "perplexity-reasoning", "perplexity-research"),
        default="perplexity-research",
        help="Which Perplexity tier to import under (default: perplexity-research).",
    )
    ip.add_argument(
        "--glob",
        default="*.md",
        help="Filename glob within the directory (default: *.md).",
    )
    ip.add_argument(
        "--query-from",
        choices=("h1", "filename"),
        default="h1",
        help=(
            "How to derive the `id=` query for each file: use the "
            "first H1 heading when present (falls back to filename), "
            "or always use the filename (default: h1)."
        ),
    )
    ip.add_argument("--database-url", default=None)
    ip.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after N files (sorted lexicographically).",
    )
    ip.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse each file and print the derived query; don't write.",
    )
    return ip


def run(args: argparse.Namespace) -> None:
    """Implements ``precis jobs import-perplexity``."""
    from precis.config import load_config
    from precis.dispatch import Hub
    from precis.embedder import make_embedder
    from precis.handlers.perplexity import (
        ResearchHandler,
        ThinkHandler,
        WebsearchHandler,
    )
    from precis.store import Store

    base = Path(args.dir)
    if not base.is_dir():
        print(f"import-perplexity: not a directory: {base}", file=sys.stderr)
        sys.exit(2)

    files = sorted(p for p in base.rglob(args.glob) if p.is_file())
    if args.limit is not None:
        files = files[: args.limit]
    if not files:
        print(f"import-perplexity: no files matched {args.glob!r} under {base}")
        return

    handler_cls = {
        "websearch": WebsearchHandler,
        "perplexity-reasoning": ThinkHandler,
        "perplexity-research": ResearchHandler,
    }[args.kind]

    cfg = load_config()

    # Dry run: parse + derive query per file; don't open a DB.
    if args.dry_run:
        for p in files:
            query = _derive_query(p, strategy=args.query_from, base=base)
            print(f"  {p.relative_to(base)} -> {query!r}")
        print(f"import-perplexity: dry-run  {len(files)} file(s) would import")
        return

    dsn = resolve_dsn(args.database_url, cfg=cfg)
    store = Store.connect(dsn)
    try:
        embedder = make_embedder(cfg.embedder, dim=store.embedding_dim())
        handler = handler_cls(hub=Hub(store=store, embedder=embedder))

        imported = failed = 0
        for p in files:
            rel = p.relative_to(base)
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                failed += 1
                print(f"  fail  {rel}  - read error: {exc}", file=sys.stderr)
                continue
            if not text.strip():
                failed += 1
                print(f"  fail  {rel}  - empty file", file=sys.stderr)
                continue
            query = _derive_query(p, strategy=args.query_from, base=base)
            try:
                handler.put(id=query, text=text, mode="import")
            except Exception as exc:
                failed += 1
                print(f"  fail  {rel}  - {exc}", file=sys.stderr)
                continue
            imported += 1
            print(f"  ok    {rel}  -> {query!r}")

        print(
            f"import-perplexity: kind={args.kind} imported={imported} "
            f"failed={failed}  [embedder={cfg.embedder}]"
        )
        if failed:
            sys.exit(1)
    finally:
        store.close()


def _derive_query(path: Path, *, strategy: str, base: Path) -> str:
    """Pick the ``id=`` query for a report file.

    ``h1``:  first ``# Heading`` in the file, else fall back to filename.
    ``filename``: always the stem with hyphens turned into spaces.
    """
    if strategy == "filename":
        return _query_from_filename(path)
    # strategy == "h1"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.lstrip()
            if stripped.startswith("# ") and not stripped.startswith("## "):
                heading = stripped[2:].strip()
                if heading:
                    return heading
    except (OSError, UnicodeDecodeError):
        pass  # fall through to filename
    return _query_from_filename(path)


def _query_from_filename(path: Path) -> str:
    """Stem with underscores/hyphens normalized to spaces."""
    stem = path.stem
    # Normalize common separators to spaces; collapse repeats.
    for ch in ("-", "_"):
        stem = stem.replace(ch, " ")
    while "  " in stem:
        stem = stem.replace("  ", " ")
    return stem.strip()


__all__ = ["add_parser", "run"]
