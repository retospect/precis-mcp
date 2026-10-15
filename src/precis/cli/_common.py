"""Shared helpers for the CLI subcommands.

Deliberately thin — only what every subcommand uses. Anything
specific to a single subcommand stays in that subcommand's module
so callers don't have to grep across the tree to find the impl.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

# Format names recognised by ``--format``. Mirrors the registry keys
# in :mod:`precis.format` so the CLI doesn't quietly accept formats
# that ``serialize`` would then reject at runtime. New formats land
# in both places at the same time.
_FORMAT_CHOICES: tuple[str, ...] = ("toon", "json", "table")

#: A verb *refusal*, matched only at the very START of a rendered payload.
#: Anchored on purpose: skills and help text quote ``[error:NotFound]`` inline
#: while documenting it, and reading those docs is a success.
_REFUSAL_RE = re.compile(r"^\[error:[A-Za-z]\w*\]")

#: Exit code for a verb-level refusal, shared by ``precis tools`` and
#: ``precis eval`` so the two agree. Distinct from 1 (the CLI itself crashed)
#: so a caller can tell "the verb said no" from "the tool is broken", and from
#: 2 (bad invocation).
REFUSAL_EXIT = 3


def is_refusal(payload: object) -> bool:
    """True when a rendered verb result is an ``[error:…]`` refusal.

    The seven-verb protocol returns errors as rendered strings rather than
    raised exceptions — right for the MCP surface, where an agent reads the
    text. A shell caller needs the opposite: a refusal is not data, so it
    belongs on stderr at a non-zero exit. Both CLI surfaces used to print it to
    stdout, one of them at exit 0, which made ``cmd > out && process out`` run
    ``process`` on an error string (gr458317).
    """
    return bool(_REFUSAL_RE.match(str(payload).lstrip()))


def bind_process_store(store: Store) -> None:
    """Bind ``store`` as the process store the model router reads, as
    ``precis worker`` does at boot: route log, budget meter, DB settings.

    Call it right after ``Store.connect`` in any command that reaches the
    router. Unbound, ``budget.meter.active_store()`` is None and every
    ``live_config`` override (``llm.backend``, ``llm.chain.<tier>``,
    ``llm.op.<source>``) silently reads as "no override", because that layer
    is failure-tolerant by design. ``tests/test_cli_binds_store.py`` holds
    every command to it.
    """
    from precis import route_log, settings
    from precis.budget import bind_store

    route_log.bind_store(store)
    bind_store(store)
    settings.bind_store(store)


def connect_router_store(database_url: str | None) -> Store | None:
    """Connect and bind a store for a command that has no database work of
    its own but calls the model router, so live routing still applies.

    Best effort: with no DSN configured, or the database unreachable, it
    warns and returns None, and the router runs on its env/compiled defaults.
    """
    from precis.config import load_config
    from precis.store import Store

    dsn = database_url or load_config().database_url
    if not dsn:
        log.warning("no database_url: live LLM routing overrides not applied")
        return None
    try:
        store = Store.connect(dsn)
    except Exception as exc:
        log.warning("database unreachable (%s): live LLM routing not applied", exc)
        return None
    bind_process_store(store)
    return store


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
    "REFUSAL_EXIT",
    "add_format_argument",
    "is_refusal",
    "resolve_dsn",
    "resolve_format",
]
