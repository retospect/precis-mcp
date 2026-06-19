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
    parser.add_argument(
        "--from-scratch",
        action="store_true",
        help=(
            "Ignore the baseline snapshot and replay every numbered "
            "migration. Default: a fresh DB loads migrations/baseline/"
            "schema.sql, then applies only the post-snapshot tail."
        ),
    )
    return parser


def run(args: argparse.Namespace) -> None:
    """Execute ``precis migrate`` against the resolved DSN.

    Source list: built-in precis migrations plus any plugin
    migrations advertised under the ``precis.migrations``
    entry-point group. Plugin failures are logged, not raised —
    one broken plugin must not block ``precis migrate``.
    """
    from precis.store import Migrator
    from precis.store.schema_dump import baseline_path

    dsn = resolve_dsn(getattr(args, "database_url", None))
    builtin_dir = Path(__file__).resolve().parent.parent / "migrations"

    # A fresh DB bootstraps from the per-release baseline snapshot
    # unless --from-scratch forces a full replay (used by the
    # convergence test and by dump-schema itself).
    baseline = (
        None if getattr(args, "from_scratch", False) else baseline_path(builtin_dir)
    )

    sources = Migrator.discover_sources(builtin_dir)
    m = Migrator(dsn, sources, baseline=baseline)
    pending = m.pending()
    if args.dry_run:
        if not pending:
            print("migrate: nothing to apply")
            return
        print(f"migrate: would apply {len(pending)} migration(s):")
        for plugin, version in pending:
            print(f"  - {plugin}/{version}")
        return

    if not pending:
        print("migrate: nothing to apply")
        return

    applied = m.apply_all()
    print(f"migrate: applied {len(applied)} migration(s):")
    for plugin, version in applied:
        print(f"  - {plugin}/{version}")


__all__ = ["add_parser", "run"]
