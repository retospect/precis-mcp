"""``precis post-migration-check`` — list pending post-migration steps by name.

Read-only assert for the steps a migration cannot run inside its own
transaction (backfills, ``VALIDATE CONSTRAINT``, ``CREATE INDEX
CONCURRENTLY``): unvalidated ``pg_constraint`` rows plus the expected
per-kind partial HNSW indexes derived from
:data:`precis.store._chunks_ops._ANN_PARTIAL_KINDS`, each named with the
command that clears it (:mod:`precis.migration_postchecks`).

``scripts/release`` runs this against prod after the deploy's migrate play
(gr514955); the health-digest ``post_migration_steps`` row reports the
same probe continuously. Warn-only by default so a caller can stream the
output; ``--strict`` exits 2 when anything is pending.
"""

from __future__ import annotations

import argparse

import psycopg

from precis.cli._common import resolve_dsn


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``post-migration-check`` subparser."""
    parser = sub.add_parser(
        "post-migration-check",
        help="List pending post-migration steps (NOT VALID constraints, "
        "missing partial ANN indexes).",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Override PRECIS_DATABASE_URL.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 2 when steps are pending (default warns at exit 0).",
    )
    return parser


def run(args: argparse.Namespace) -> None:
    """Probe and print the pending list; never runs the steps it names."""
    from precis.migration_postchecks import format_pending, pending_post_steps

    dsn = resolve_dsn(getattr(args, "database_url", None))
    with psycopg.connect(dsn) as conn:
        items = pending_post_steps(conn)
    print(format_pending(items))
    if args.strict and items:
        raise SystemExit(2)


__all__ = ["add_parser", "run"]
