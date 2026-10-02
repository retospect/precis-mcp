"""Top-level CLI entry point and dispatcher.

Hosts :func:`main` (the ``precis`` console script entry point) and
:func:`_build_parser` (the argparse tree). Each subcommand's parser
registration and implementation live in a sibling module, named by a
row in :mod:`precis.cli.registry`. Only the invoked subcommand's module
is imported; the rest appear in ``--help`` from their registry rows, so
one subcommand's missing optional dependency cannot break another.
``serve`` and the ``jobs`` group are built here.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import sys
from types import ModuleType

from precis.cli.registry import JOB_COMMANDS, Command, commands
from precis.utils.utc_logging import force_utc_timestamps

log = logging.getLogger(__name__)


def main() -> None:
    """``precis`` console-script entry point.

    Parses argv, configures root logging, and dispatches to the
    owning subcommand module. ``serve`` is special-cased inline
    because it's the only subcommand with no arguments of its own
    and no database touch.
    """
    argv = sys.argv[1:]
    args = _build_parser(_command_path(argv)).parse_args(argv)

    force_utc_timestamps()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.cmd == "serve":
        from precis.server import main as serve

        serve(
            transport=args.transport,
            host=args.host,
            port=args.port,
            token=args.token,
            fd=args.fd,
        )
        return

    if args.cmd == "jobs":
        _call(_select(JOB_COMMANDS, args.job), args)
        return

    _call(_select(commands(), args.cmd), args)


# ---------------------------------------------------------------------------
# Argparse construction
# ---------------------------------------------------------------------------


def _build_parser(path: tuple[str, ...] | None = None) -> argparse.ArgumentParser:
    """Build the top-level argparse tree.

    ``path`` is the command (and, under ``jobs``, the job) being invoked,
    from :func:`_command_path`: only its module is imported and given its
    real subparser; every other command is a help-only stub. ``()``
    imports nothing, which is what bare ``precis --help`` needs. ``None``
    builds every real subparser — tests use it to parse any command line.
    """
    parser = argparse.ArgumentParser(
        prog="precis",
        description="precis-mcp v2 - paper, document, state, and tool access.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve_parser = sub.add_parser(
        "serve", help="Run the MCP server (stdio by default)."
    )
    serve_parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="stdio (default, every existing caller) | sse | streamable-http "
        "(network transports need --token or PRECIS_MCP_TOKEN — used by "
        "the sandbox_run precis_access:read per-run callback).",
    )
    serve_parser.add_argument(
        "--host", default="127.0.0.1", help="Bind host for a network transport."
    )
    serve_parser.add_argument(
        "--port", type=int, default=8765, help="Bind port for a network transport."
    )
    serve_parser.add_argument(
        "--token",
        default=None,
        help="Bearer token required on a network transport (or set "
        "PRECIS_MCP_TOKEN). Ignored on stdio.",
    )
    serve_parser.add_argument(
        "--fd",
        type=int,
        default=None,
        help="Serve a network transport on this already-listening inherited "
        "socket instead of binding --host/--port (set by precis.mcp_supervisor).",
    )
    _add_commands(sub, commands(), path[0] if path else None, full=path is None)

    if path is None or path[:1] == ("jobs",):
        jobs = sub.add_parser("jobs", help=_JOBS_HELP)
        jobs_sub = jobs.add_subparsers(dest="job", required=True)
        selected = path[1] if path is not None and len(path) > 1 else None
        _add_commands(jobs_sub, JOB_COMMANDS, selected, full=path is None)
    else:
        sub.add_parser("jobs", help=_JOBS_HELP, add_help=False)

    return parser


_JOBS_HELP = "Run a one-shot maintenance job."


def _command_path(argv: list[str]) -> tuple[str, ...]:
    """The subcommand names in ``argv`` that pick which modules to import.

    Neither the top-level parser nor ``jobs`` takes an option of its own
    besides ``-h``, so the first positional token is the command and,
    under ``jobs``, the second is the job.
    """
    words = [a for a in argv if not a.startswith("-")]
    return tuple(words[:2]) if words[:1] == ["jobs"] else tuple(words[:1])


def _add_commands(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    cmds: tuple[Command, ...],
    selected: str | None,
    *,
    full: bool,
) -> None:
    """Register ``cmds`` on ``sub``: real parsers for the selected module, stubs for the rest.

    A stub carries the name and one-line help only, which is all
    ``--help`` one level up renders, so listing a command never imports
    it. Every command of the selected command's module gets its real
    parser, since one registrar can add several (``jobs ingest``).
    """
    real = {c.module for c in cmds if full or c.name == selected}
    registered: set[tuple[str, str]] = set()
    for c in cmds:
        if c.module not in real:
            if c.help is None:
                sub.add_parser(c.name, add_help=False)
            else:
                sub.add_parser(c.name, help=c.help, add_help=False)
            continue
        if (c.module, c.register) not in registered:
            registered.add((c.module, c.register))
            getattr(_load(c), c.register)(sub)


def _load(cmd: Command) -> ModuleType:
    """Import ``cmd``'s module, or exit naming the dependency it lacks."""
    try:
        return importlib.import_module(cmd.module)
    except ImportError as exc:
        missing = getattr(exc, "name", None)
        why = f"missing dependency {missing!r}" if missing else str(exc)
        print(
            f"precis {cmd.name}: unavailable — importing {cmd.module} failed: {why}",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc


def _select(cmds: tuple[Command, ...], name: str) -> Command:
    for c in cmds:
        if c.name == name:
            return c
    print(f"precis: unknown subcommand {name!r}", file=sys.stderr)
    raise SystemExit(2)


def _call(cmd: Command, args: argparse.Namespace) -> None:
    """Run ``cmd``; its module is already imported by the parser build."""
    getattr(_load(cmd), cmd.run)(args)


if __name__ == "__main__":
    main()
