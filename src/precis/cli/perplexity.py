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
    from precis.cli._common import cli_embedder
    from precis.config import load_config
    from precis.dispatch import Hub
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
        embedder = cli_embedder(
            cfg, store, needed=False, prog="precis jobs import-perplexity"
        )
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


# ---------------------------------------------------------------------------
# backfill-perplexity-cites — mint stub web refs for cached [N] citations
# ---------------------------------------------------------------------------

_CITE_KINDS = ("websearch", "perplexity-reasoning", "perplexity-research")


def add_cite_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``backfill-perplexity-cites`` subparser on ``sub``."""
    p = sub.add_parser(
        "backfill-perplexity-cites",
        help="Mint stub web refs for [N] citations in cached perplexity reports.",
    )
    p.add_argument(
        "--kind",
        choices=_CITE_KINDS,
        default=None,
        help="Restrict to one tier (default: all three).",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after N cache rows per kind.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be minted; don't write.",
    )
    p.add_argument("--database-url", default=None)
    return p


def run_cites(args: argparse.Namespace) -> None:
    """Implements ``precis jobs backfill-perplexity-cites``.

    Walks the perplexity cache rows, parses each body's ``Sources:``
    block (or the fetch-stamped ``citation_map`` meta) and mints a
    born-stale ``web`` stub per cited URL — the same materialisation the
    render path does lazily (gr548801). Operator-run only: never wired
    into a cadence. No network — the stubs fetch on first access.
    """
    from precis.config import load_config
    from precis.handlers._cache_base import dedupe_overlap_join
    from precis.store import Store
    from precis.utils.web_refs import (
        canonical_web_key,
        citation_pairs,
        materialize_web_refs,
    )

    kinds = [args.kind] if args.kind else list(_CITE_KINDS)
    cfg = load_config()
    dsn = resolve_dsn(args.database_url, cfg=cfg)
    store = Store.connect(dsn)
    try:
        scanned = cited = minted = already = invalid = 0
        for kind in kinds:
            offset = 0
            seen_rows = 0
            while True:
                page = store.list_cache_entries(
                    kind=kind, provider="perplexity", limit=200, offset=offset
                )
                if not page:
                    break
                offset += len(page)
                for ref, cache in page:
                    seen_rows += 1
                    if args.limit is not None and seen_rows > args.limit:
                        break
                    scanned += 1
                    body = dedupe_overlap_join(
                        [b.text or "" for b in store.chunks.list_chunks_for_ref(ref.id)]
                    )
                    pairs = citation_pairs(cache.meta or {}, body)
                    if not pairs:
                        continue
                    cited += 1
                    origin = f"{kind}:{ref.id}"
                    had = materialize_web_refs(
                        store, pairs, origin=origin, create=False
                    )
                    already += len(had)
                    n_invalid = sum(
                        1 for _n, u in pairs if canonical_web_key(u) is None
                    )
                    invalid += n_invalid
                    if args.dry_run:
                        minted += len(pairs) - len(had) - n_invalid
                        continue
                    mapping = materialize_web_refs(store, pairs, origin=origin)
                    new = len(mapping) - len(had)
                    minted += new
                    if new:
                        print(
                            f"  {origin}  +{new} web ref(s) "
                            f"({len(mapping)} cited total)"
                        )
                if args.limit is not None and seen_rows > args.limit:
                    break
        verb = "would mint" if args.dry_run else "minted"
        print(
            f"backfill-perplexity-cites: scanned={scanned} with-citations={cited} "
            f"{verb}={minted} already-cached={already} invalid-url={invalid}"
        )
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


__all__ = ["add_cite_parser", "add_parser", "run", "run_cites"]
