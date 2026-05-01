"""``precis jobs ingest-*`` subcommands.

Four related ingest jobs share this module:

- ``ingest-bundle`` / ``ingest-bundles`` — ``.acatome`` paper bundles
  (single file / directory walk).
- ``ingest-md`` — pre-warm markdown ingest under a configured root.
- ``ingest-oracles`` — seed the ``oracle`` kind from YAML wisdom
  files (defaults to the bundled ``data/oracle/`` directory).

Kept together because they all share the DSN resolution + embedder
construction + per-file stats output shape; splitting them further
would duplicate the boilerplate.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from precis.cli._common import resolve_dsn

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parser registration
# ---------------------------------------------------------------------------


def add_parsers(sub: argparse._SubParsersAction) -> None:
    """Register ingest-{bundle,bundles,md,oracles} on ``sub``."""
    ib = sub.add_parser(
        "ingest-bundle",
        help="Ingest a single .acatome bundle.",
    )
    ib.add_argument("path", help="Path to .acatome file.")
    ib.add_argument("--database-url", default=None)

    ibs = sub.add_parser(
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
    im = sub.add_parser(
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

    # Phase 5 — oracle seed ingest. Reads bundled wisdom YAMLs (or
    # a user-supplied directory) and writes one ``oracle`` ref per
    # tradition with one block per entry. Idempotent: skips refs
    # that already exist unless ``--overwrite`` is passed.
    io = sub.add_parser(
        "ingest-oracles",
        help="Seed the oracle kind from YAML wisdom files.",
    )
    io.add_argument(
        "src",
        nargs="?",
        default=None,
        help=(
            "Directory of oracle YAML files. Defaults to the bundled "
            "data/oracle/ shipped with the package."
        ),
    )
    io.add_argument("--database-url", default=None)
    io.add_argument(
        "--overwrite",
        action="store_true",
        help=(
            "Replace existing oracle refs (drops & re-inserts blocks); "
            "default is to skip already-ingested traditions."
        ),
    )
    io.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't write — show what would be ingested.",
    )


# ---------------------------------------------------------------------------
# ingest-bundle
# ---------------------------------------------------------------------------


def run_bundle(args: argparse.Namespace) -> None:
    """Implements ``precis jobs ingest-bundle`` — ingest a single file."""
    from precis.config import load_config
    from precis.embedder import make_embedder
    from precis.store import Store

    path = Path(args.path)
    if not path.is_file():
        print(f"ingest-bundle: file not found: {path}", file=sys.stderr)
        sys.exit(2)

    cfg = load_config()
    dsn = resolve_dsn(args.database_url, cfg=cfg)
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


# ---------------------------------------------------------------------------
# ingest-bundles
# ---------------------------------------------------------------------------


def run_bundles(args: argparse.Namespace) -> None:
    """Implements ``precis jobs ingest-bundles`` — walk a directory."""
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

    dsn = resolve_dsn(args.database_url, cfg=cfg)
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


# ---------------------------------------------------------------------------
# ingest-md
# ---------------------------------------------------------------------------


def run_md(args: argparse.Namespace) -> None:
    """Implements ``precis jobs ingest-md`` — pre-warm markdown ingest."""
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

    dsn = resolve_dsn(args.database_url, cfg=cfg)
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


# ---------------------------------------------------------------------------
# ingest-oracles
# ---------------------------------------------------------------------------


def run_oracles(args: argparse.Namespace) -> None:
    """Implements ``precis jobs ingest-oracles``.

    Walks a directory of YAML files (defaulting to the bundled
    ``data/oracle/``) and inserts one ``oracle`` ref per tradition
    with one block per entry. Idempotent: existing refs are skipped
    unless ``--overwrite`` is passed; ``--dry-run`` reports without
    touching the DB.
    """
    from precis.config import load_config
    from precis.embedder import make_embedder
    from precis.jobs.ingest_oracles import (
        bundled_oracle_dir,
        ingest_directory,
    )
    from precis.store import Store

    if args.src is not None:
        src = Path(args.src).expanduser()
    else:
        bundled = bundled_oracle_dir()
        if bundled is None:
            print(
                "ingest-oracles: bundled oracle dir not found and no path "
                "supplied; pass <src> as the first argument",
                file=sys.stderr,
            )
            sys.exit(2)
        src = bundled
    if not src.is_dir():
        print(f"ingest-oracles: not a directory: {src}", file=sys.stderr)
        sys.exit(2)

    cfg = load_config()

    if args.dry_run:
        # Dry-run still parses every YAML to validate the schema, but
        # never opens a DB connection — useful before pointing the
        # CLI at a fresh deploy.
        try:
            agg = ingest_directory(
                src,
                store=None,  # type: ignore[arg-type]
                embedder=None,
                overwrite=args.overwrite,
                dry_run=True,
            )
        except (FileNotFoundError, ValueError) as exc:
            print(f"ingest-oracles: {exc}", file=sys.stderr)
            sys.exit(2)
        print(
            f"ingest-oracles: dry-run from {src}\n"
            f"  files={agg['files']}  would-create={agg['created']}  "
            f"chunks={agg['chunks']}"
        )
        for fname, stats in agg["per_file"].items():
            print(f"  {fname:<28}  entries={stats['chunks']}")
        return

    dsn = resolve_dsn(args.database_url, cfg=cfg)
    store = Store.connect(dsn)
    try:
        embedder = make_embedder(cfg.embedder, dim=store.embedding_dim())
        try:
            agg = ingest_directory(
                src,
                store=store,
                embedder=embedder,
                overwrite=args.overwrite,
                dry_run=False,
            )
        except (FileNotFoundError, ValueError) as exc:
            print(f"ingest-oracles: {exc}", file=sys.stderr)
            sys.exit(2)

        print(
            f"ingest-oracles: from {src}  [embedder={cfg.embedder}]\n"
            f"  files={agg['files']}  created={agg['created']}  "
            f"replaced={agg['replaced']}  skipped={agg['skipped']}  "
            f"errors={agg['errors']}  total chunks={agg['chunks']}"
        )
        for fname, stats in agg["per_file"].items():
            print(
                f"  {fname:<28}  "
                f"created={stats['created']} replaced={stats['replaced']} "
                f"chunks={stats['chunks']} skipped={stats['skipped']} "
                f"errors={stats['errors']}"
            )
        if agg["errors"]:
            sys.exit(1)
    finally:
        store.close()


__all__ = [
    "add_parsers",
    "run_bundle",
    "run_bundles",
    "run_md",
    "run_oracles",
]
