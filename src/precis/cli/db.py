"""``precis db ...`` — database baseline maintenance.

Today this hosts one subcommand, ``dump-schema``, which regenerates the
per-release baseline snapshot (``migrations/baseline/schema.sql``). It
is a container op: it needs ``pg_dump`` on PATH and a Postgres server it
can create a scratch database on.

The snapshot is the migration chain compiled to one file. Fresh
installs load it in one shot instead of replaying every numbered
migration; existing databases are untouched and migrate forward as
always. See ``precis.store.schema_dump`` and ADR 0031.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from precis.cli._common import resolve_dsn


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``db`` command and its subcommands on ``sub``."""
    parser = sub.add_parser("db", help="Database baseline maintenance.")
    db_sub = parser.add_subparsers(dest="db_cmd", required=True)

    dump = db_sub.add_parser(
        "dump-schema",
        help="Regenerate migrations/baseline/schema.sql from the migration chain.",
    )
    dump.add_argument(
        "--database-url",
        default=None,
        help=(
            "Maintenance DSN whose server hosts the scratch DB "
            "(override PRECIS_DATABASE_URL). The connecting role needs CREATEDB."
        ),
    )
    dump.add_argument(
        "--output",
        default=None,
        type=Path,
        help="Write the snapshot here instead of the in-tree baseline path.",
    )
    dump.add_argument(
        "--scratch-db",
        default=None,
        help="Name of the throwaway database to build and drop (default precis_schema_dump).",
    )
    dump.add_argument(
        "--pg-dump-bin",
        default="pg_dump",
        help="Path to the pg_dump binary (default: looked up on PATH).",
    )
    return parser


def run(args: argparse.Namespace) -> None:
    """Dispatch ``precis db <subcommand>``."""
    if args.db_cmd == "dump-schema":
        _run_dump_schema(args)
        return
    raise SystemExit(f"unknown db subcommand: {args.db_cmd!r}")


def _run_dump_schema(args: argparse.Namespace) -> None:
    from precis.store.schema_dump import (
        DEFAULT_SCRATCH_DB,
        baseline_path,
        builtin_migrations_dir,
        write_baseline,
    )

    pg_dump_bin = args.pg_dump_bin
    if shutil.which(pg_dump_bin) is None and not Path(pg_dump_bin).exists():
        print(
            f"pg_dump not found ({pg_dump_bin!r}). dump-schema is a container op — "
            "run it inside the precis dev container, or install Postgres client "
            "tools and point --pg-dump-bin at the binary.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    dsn = resolve_dsn(getattr(args, "database_url", None))
    migrations_dir = builtin_migrations_dir()
    out = args.output or baseline_path(migrations_dir)

    written = write_baseline(
        dsn,
        migrations_dir,
        output=out,
        scratch_db=args.scratch_db or DEFAULT_SCRATCH_DB,
        pg_dump_bin=pg_dump_bin,
    )
    print(f"dump-schema: wrote baseline snapshot to {written}")


__all__ = ["add_parser", "run"]
