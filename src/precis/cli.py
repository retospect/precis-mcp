"""Single CLI entry point: ``precis serve | migrate | jobs ...``.

Subcommands:
    serve     Run the MCP server on stdio.
    migrate   Apply pending DB migrations.
    jobs      Run a one-shot maintenance job:
              - ingest-bundle      one .acatome file
              - ingest-bundles     walk a directory of bundles
              - ingest-md          walk a directory of markdown files
              - import-perplexity  bulk put(mode='import') over a
                                   directory of Perplexity reports

All DB-touching subcommands require ``PRECIS_DATABASE_URL`` (or a
``--database-url`` override).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.cmd == "serve":
        from precis.server import main as serve

        serve()
        return

    if args.cmd == "migrate":
        _run_migrate(args)
        return

    if args.cmd == "jobs":
        _run_jobs(args)
        return

    parser.error(f"unknown command: {args.cmd!r}")


# ---------------------------------------------------------------------------
# Argparse construction
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="precis",
        description="precis-mcp v2 — paper, document, state, and tool access.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="Run the MCP server (stdio).")

    migrate = sub.add_parser("migrate", help="Apply pending DB migrations.")
    migrate.add_argument(
        "--database-url",
        default=None,
        help="Override PRECIS_DATABASE_URL.",
    )
    migrate.add_argument(
        "--dry-run",
        action="store_true",
        help="Show pending migrations without applying.",
    )

    jobs = sub.add_parser("jobs", help="Run a one-shot maintenance job.")
    jobs_sub = jobs.add_subparsers(dest="job", required=True)

    ib = jobs_sub.add_parser(
        "ingest-bundle",
        help="Ingest a single .acatome bundle.",
    )
    ib.add_argument("path", help="Path to .acatome file.")
    ib.add_argument("--database-url", default=None)

    ibs = jobs_sub.add_parser(
        "ingest-bundles",
        help="Walk a directory of .acatome bundles.",
    )
    ibs.add_argument("dir", help="Directory containing .acatome files.")
    ibs.add_argument("--database-url", default=None)
    ibs.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate bundle parsing without writing.",
    )
    ibs.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after N bundles (sorted lexicographically).",
    )

    # Phase 6 — markdown ingest. The handler ingests lazily on every
    # `get`, but this command lets the operator pre-warm a directory
    # (useful before launching long-running searches).
    im = jobs_sub.add_parser(
        "ingest-md",
        help="Pre-warm the store by ingesting every .md under a root.",
    )
    im.add_argument(
        "root",
        nargs="?",
        default=None,
        help="Markdown root (defaults to PRECIS_MARKDOWN_ROOT).",
    )
    im.add_argument("--database-url", default=None)
    im.add_argument(
        "--force",
        action="store_true",
        help="Re-ingest every file even if its mtime hasn't changed.",
    )

    # Bulk import of Perplexity-generated reports. The typical source
    # is a directory of markdown files exported from the Perplexity
    # web UI by a Pro subscriber — free content that populates the
    # same cache rows paid API calls would have created.
    ip = jobs_sub.add_parser(
        "import-perplexity",
        help="Bulk put(mode='import') a directory of Perplexity reports.",
    )
    ip.add_argument(
        "dir",
        help="Directory to walk (recursively) for report files.",
    )
    ip.add_argument(
        "--kind",
        choices=("websearch", "think", "research"),
        default="research",
        help="Which Perplexity tier to import under (default: research).",
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

    return parser


# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------


def _run_migrate(args: argparse.Namespace) -> None:
    from precis.store import Migrator

    dsn = _resolve_dsn(getattr(args, "database_url", None))
    migrations_dir = Path(__file__).parent / "migrations"

    m = Migrator(dsn, migrations_dir)
    pending = m.pending()
    if args.dry_run:
        if not pending:
            print("migrate: nothing to apply")
            return
        print(f"migrate: would apply {len(pending)} migration(s):")
        for v in pending:
            print(f"  - {v}")
        return

    if not pending:
        print("migrate: nothing to apply")
        return

    applied = m.apply_all()
    print(f"migrate: applied {len(applied)} migration(s):")
    for v in applied:
        print(f"  - {v}")


def _run_jobs(args: argparse.Namespace) -> None:
    if args.job == "ingest-bundle":
        _run_ingest_bundle(args)
        return
    if args.job == "ingest-bundles":
        _run_ingest_bundles(args)
        return
    if args.job == "ingest-md":
        _run_ingest_md(args)
        return
    if args.job == "import-perplexity":
        _run_import_perplexity(args)
        return
    print(f"jobs: unknown subcommand {args.job!r}", file=sys.stderr)
    sys.exit(2)


def _run_ingest_md(args: argparse.Namespace) -> None:
    from precis.config import load_config
    from precis.embedder import make_embedder
    from precis.handlers.markdown import MarkdownHandler
    from precis.store import Store

    cfg = load_config()
    root_str = args.root or cfg.markdown_root
    if not root_str:
        print(
            "ingest-md: root not specified and PRECIS_MARKDOWN_ROOT not set",
            file=sys.stderr,
        )
        sys.exit(2)
    root = Path(root_str).resolve()
    if not root.is_dir():
        print(f"ingest-md: not a directory: {root}", file=sys.stderr)
        sys.exit(2)

    dsn = _resolve_dsn(args.database_url, cfg=cfg)
    store = Store.connect(dsn)
    try:
        embedder = make_embedder(cfg.embedder, dim=store.embedding_dim())
        handler = MarkdownHandler(store=store, root=root, embedder=embedder)

        ingested = 0
        skipped = 0
        failed = 0
        # Walk the same way the handler's index does — keeps slug
        # derivation identical.
        from precis.utils.md_parse import file_slug_from_path, is_valid_file_slug

        for dirpath, _dirs, files in os.walk(root):
            for name in sorted(files):
                if not name.endswith((".md", ".markdown")):
                    continue
                p = Path(dirpath) / name
                try:
                    rel = str(p.relative_to(root))
                    slug = file_slug_from_path(rel)
                except ValueError:
                    failed += 1
                    print(f"  fail  {p}  — invalid path")
                    continue
                if not is_valid_file_slug(slug):
                    failed += 1
                    print(f"  fail  {p}  — invalid slug {slug!r}")
                    continue
                ref_before = store.get_ref(kind="markdown", id=slug)
                ref = handler._ensure_ingested(slug, force=args.force)
                if ref is None:
                    failed += 1
                    print(f"  fail  {p}  — ingest returned None")
                    continue
                if ref_before is None:
                    ingested += 1
                    print(f"  ok    {slug}  ({store.count_blocks(ref.id)} blocks)")
                else:
                    if args.force or (ref_before.meta or {}).get("sha256") != (
                        ref.meta or {}
                    ).get("sha256"):
                        ingested += 1
                        print(f"  upd   {slug}  ({store.count_blocks(ref.id)} blocks)")
                    else:
                        skipped += 1

        print(
            f"ingest-md: ingested={ingested}  skipped={skipped}  "
            f"failed={failed}  [embedder={cfg.embedder}]"
        )
        if failed:
            sys.exit(1)
    finally:
        store.close()


def _run_ingest_bundle(args: argparse.Namespace) -> None:
    from precis.config import load_config
    from precis.embedder import make_embedder
    from precis.store import Store

    path = Path(args.path)
    if not path.is_file():
        print(f"ingest-bundle: file not found: {path}", file=sys.stderr)
        sys.exit(2)

    cfg = load_config()
    dsn = _resolve_dsn(args.database_url, cfg=cfg)
    store = Store.connect(dsn)
    try:
        embedder = make_embedder(cfg.embedder, dim=store.embedding_dim())
        result = store.ingest_bundle(path, embedder=embedder)
        verb = "inserted" if result.inserted else "skipped (already present)"
        print(
            f"ingest-bundle: {verb} {result.slug} "
            f"({result.block_count} blocks) [embedder={cfg.embedder}]"
        )
    finally:
        store.close()


def _run_ingest_bundles(args: argparse.Namespace) -> None:
    from precis.config import load_config
    from precis.embedder import make_embedder
    from precis.errors import PrecisError
    from precis.ingest import parse_bundle, read_bundle
    from precis.store import Store

    base = Path(args.dir)
    if not base.is_dir():
        print(f"ingest-bundles: not a directory: {base}", file=sys.stderr)
        sys.exit(2)

    bundles = sorted(base.rglob("*.acatome"))
    if args.limit is not None:
        bundles = bundles[: args.limit]
    if not bundles:
        print(f"ingest-bundles: no .acatome files under {base}")
        return

    cfg = load_config()

    if args.dry_run:
        ok = bad = 0
        for path in bundles:
            try:
                raw = read_bundle(path)
                parse_bundle(raw, embedding_dim=1024)
                ok += 1
            except PrecisError as e:
                print(f"  FAIL  {path}  — {e.cause}", file=sys.stderr)
                bad += 1
            except Exception as e:
                print(f"  FAIL  {path}  — {e}", file=sys.stderr)
                bad += 1
        print(f"ingest-bundles: dry-run  ok={ok}  failed={bad}")
        if bad:
            sys.exit(1)
        return

    dsn = _resolve_dsn(args.database_url, cfg=cfg)
    store = Store.connect(dsn)
    try:
        embedder = make_embedder(cfg.embedder, dim=store.embedding_dim())
        inserted = skipped = failed = 0
        for path in bundles:
            try:
                result = store.ingest_bundle(path, embedder=embedder)
            except PrecisError as e:
                print(f"  FAIL  {path.name}  — {e.cause}", file=sys.stderr)
                failed += 1
                continue
            except Exception as e:
                log.exception("unexpected error ingesting %s", path)
                print(f"  FAIL  {path.name}  — {e}", file=sys.stderr)
                failed += 1
                continue

            if result.inserted:
                inserted += 1
                print(f"  ok    {result.slug}  ({result.block_count} blocks)")
            else:
                skipped += 1
                print(f"  skip  {result.slug}  (already present)")
        print(
            f"ingest-bundles: inserted={inserted}  skipped={skipped}  "
            f"failed={failed}  [embedder={cfg.embedder}]"
        )
        if failed:
            sys.exit(1)
    finally:
        store.close()


def _run_import_perplexity(args: argparse.Namespace) -> None:
    """Walk a directory and bulk-import every matching file as a
    perplexity ref via ``put(mode='import')``.

    Dry-run prints the derived query per file without touching the DB
    — useful for sanity-checking the ``--query-from`` heuristic
    before a real run.
    """
    from precis.config import load_config
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
        "think": ThinkHandler,
        "research": ResearchHandler,
    }[args.kind]

    cfg = load_config()

    # Dry run: parse + derive query per file; don't open a DB.
    if args.dry_run:
        for p in files:
            query = _derive_perplexity_query(p, strategy=args.query_from, base=base)
            print(f"  {p.relative_to(base)} -> {query!r}")
        print(f"import-perplexity: dry-run  {len(files)} file(s) would import")
        return

    dsn = _resolve_dsn(args.database_url, cfg=cfg)
    store = Store.connect(dsn)
    try:
        embedder = make_embedder(cfg.embedder, dim=store.embedding_dim())
        handler = handler_cls(store=store, embedder=embedder)

        imported = failed = 0
        for p in files:
            rel = p.relative_to(base)
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                failed += 1
                print(f"  fail  {rel}  — read error: {exc}", file=sys.stderr)
                continue
            if not text.strip():
                failed += 1
                print(f"  fail  {rel}  — empty file", file=sys.stderr)
                continue
            query = _derive_perplexity_query(p, strategy=args.query_from, base=base)
            try:
                handler.put(id=query, text=text, mode="import")
            except Exception as exc:
                failed += 1
                print(f"  fail  {rel}  — {exc}", file=sys.stderr)
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


def _derive_perplexity_query(
    path: Path,
    *,
    strategy: str,
    base: Path,
) -> str:
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


def _resolve_dsn(override: str | None, *, cfg: Any = None) -> str:
    """Pick the database DSN: CLI override > config > env.

    `cfg` may be passed in by callers that already loaded it, to avoid
    re-reading env / .env multiple times in one CLI invocation.
    """
    if override:
        return override
    if cfg is None:
        from precis.config import load_config

        cfg = load_config()
    if cfg.database_url:
        return cfg.database_url
    print(
        "no database_url configured — set PRECIS_DATABASE_URL or pass --database-url",
        file=sys.stderr,
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
