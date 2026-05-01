"""``precis migrate`` — apply pending DB migrations.

Forward-only numbered SQL migrations live in
``precis/migrations/``. The :class:`precis.store.Migrator` computes
the pending set, this module's ``run`` applies (or just reports)
them.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from precis.cli._common import resolve_dsn


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``migrate`` subparser on ``sub``.

    Returned for symmetry with the other subcommand modules;
    callers typically ignore the return value.
    """
    parser = sub.add_parser("migrate", help="Apply pending DB migrations.")
    parser.add_argument(
        "--database-url",
        default=None,
        help="Override PRECIS_DATABASE_URL.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show pending migrations without applying.",
    )
    return parser


def run(args: argparse.Namespace) -> None:
    """Execute ``precis migrate`` against the resolved DSN."""
    from precis.store import Migrator

    dsn = resolve_dsn(getattr(args, "database_url", None))
    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"

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


__all__ = ["add_parser", "run"]
