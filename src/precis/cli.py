"""Single CLI entry point: ``precis serve | migrate | jobs ...``.

Subcommands:
    serve     Run the MCP server on stdio.
    migrate   Apply pending DB migrations.
    jobs      Run a one-shot maintenance job:
              - ingest-bundle    one .acatome file
              - ingest-bundles   walk a directory of bundles

All DB-touching subcommands require ``PRECIS_DATABASE_URL`` (or a
``--database-url`` override).
"""

from __future__ import annotations

import argparse
import logging
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
    print(f"jobs: unknown subcommand {args.job!r}", file=sys.stderr)
    sys.exit(2)


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
