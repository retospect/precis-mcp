"""Shared read-only prod connection for the build_mesh_* gold builders.

Two ways in:

- ``--database-url DSN`` — any Postgres (a dev/test DB for plumbing tests).
- ``--prod`` — the secrets-file DSN rewritten to ``agent_ro`` and verified
  on a real connection by :mod:`precis_web.readonly_dsn` (the same path
  ``scripts/guide-web --db prod`` uses). No password on argv; the DSN is
  never printed.

Either way the build runs inside ``SET TRANSACTION READ ONLY`` — belt on
top of the role. Never writes, never ``SET``s a session parameter (the
pgbouncer poisoning rule from ``scripts/prod-psql``'s header applies here
too).
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any


def add_db_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument(
        "--database-url",
        default=None,
        help="Postgres DSN (dev/test DB). Ignored with --prod.",
    )
    ap.add_argument(
        "--prod",
        action="store_true",
        help="Read prod via the agent_ro role (secrets-file DSN, rewritten + "
        "verified read-only).",
    )


def _resolve_dsn(args: argparse.Namespace) -> str:
    """The DSN ``connect_ro``/``store_ro`` open, resolved once."""
    if args.prod:
        # Load readonly_dsn by path: ``import precis_web`` pulls fastapi, which
        # a non-web env lacks — the rewrite/verify module itself is psycopg-only.
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "readonly_dsn",
            Path(__file__).resolve().parents[2]
            / "src"
            / "precis_web"
            / "readonly_dsn.py",
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        dsn_file = Path.home() / ".secrets" / "pw" / "PRECIS_DATABASE_URL"
        dsn = mod.rewrite_dsn(dsn_file.read_text(encoding="utf-8"))
        mod.verify_read_only(dsn)
        return dsn
    if args.database_url:
        return str(args.database_url)
    raise SystemExit("pass --prod or --database-url")


def connect_ro(args: argparse.Namespace) -> Any:
    """Open a psycopg connection inside a read-only transaction."""
    import psycopg

    conn = psycopg.connect(_resolve_dsn(args))
    conn.execute("SET TRANSACTION READ ONLY")
    return conn


def store_ro(args: argparse.Namespace) -> Any:
    """Open a :class:`precis.store.Store` on the same resolved DSN.

    For builders whose candidate pool needs the store API (``search_chunks``,
    ``render_eye``) rather than raw SQL. Under ``--prod`` the DSN is the
    verified ``agent_ro`` role, so read-only holds at the DB role; builders
    issue reads only.
    """
    from precis.store import Store

    return Store.connect(_resolve_dsn(args))
