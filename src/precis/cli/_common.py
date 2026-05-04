"""Shared helpers for the CLI subcommands.

Deliberately thin — only what every subcommand uses. Anything
specific to a single subcommand stays in that subcommand's module
so callers don't have to grep across the tree to find the impl.
"""

from __future__ import annotations

import sys
from typing import Any


def resolve_dsn(override: str | None, *, cfg: Any = None) -> str:
    """Pick the database DSN: CLI override > config > env.

    ``cfg`` may be passed in by callers that already loaded it, to
    avoid re-reading env / .env multiple times in one CLI invocation.
    Returns the DSN string; exits the process with code 2 when no
    DSN is configured anywhere — every DB-touching subcommand needs
    one and the error message is identical across them.
    """
    if override:
        return override
    if cfg is None:
        from precis.config import load_config

        cfg = load_config()
    if cfg.database_url:
        return cfg.database_url
    print(
        "no database_url configured - set PRECIS_DATABASE_URL or pass --database-url",
        file=sys.stderr,
    )
    sys.exit(2)


__all__ = ["resolve_dsn"]
