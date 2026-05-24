"""Shared helpers for the CLI subcommands.

Deliberately thin — only what every subcommand uses. Anything
specific to a single subcommand stays in that subcommand's module
so callers don't have to grep across the tree to find the impl.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

# Format names recognised by ``--format``. Mirrors the registry keys
# in :mod:`precis.format` so the CLI doesn't quietly accept formats
# that ``serialize`` would then reject at runtime. New formats land
# in both places at the same time.
_FORMAT_CHOICES: tuple[str, ...] = ("toon", "json", "table")


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


def add_format_argument(parser: argparse.ArgumentParser) -> None:
    """Register the standard ``--format`` flag on *parser*.

    Subcommands that emit tabular data (``precis worker --status``,
    eventually ``precis search``, ``precis show``, …) opt in by
    calling this helper. The default is ``None`` so
    :func:`resolve_format` can pick a sensible default based on
    whether stdout is a TTY.
    """
    parser.add_argument(
        "--format",
        choices=_FORMAT_CHOICES,
        default=None,
        help=(
            "Output format. Defaults to 'table' on a TTY and 'toon' "
            "when piped. 'json' is also available for nested or "
            "single-record output."
        ),
    )


def resolve_format(
    args: argparse.Namespace,
    *,
    default_tty: str = "table",
    default_pipe: str = "toon",
) -> str:
    """Pick the effective output format for a CLI invocation.

    Precedence:

    1. ``args.format`` if the operator passed ``--format``.
    2. ``default_tty`` when :func:`sys.stdout.isatty` reports true.
    3. ``default_pipe`` otherwise.

    A namespace that lacks the ``format`` attribute (because the
    subcommand forgot to call :func:`add_format_argument`) is
    tolerated — we degrade to the contextual default rather than
    raising. The CLI surface is the priority; output formatting
    should not be a tripwire.
    """
    flag = getattr(args, "format", None)
    if flag:
        return flag
    if sys.stdout.isatty():
        return default_tty
    return default_pipe


__all__ = [
    "add_format_argument",
    "resolve_dsn",
    "resolve_format",
]
