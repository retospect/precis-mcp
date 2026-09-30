"""CLI interface for precis tools using shared registry.

This module provides the command-line interface for all precis tools,
automatically generated from the shared tool registry to stay in sync
with the MCP server interface.

Exit codes, which matter because this CLI is scripted (``scripts/prod-precis``
is the documented fallback when the session MCP is dead):

* **0** — the verb succeeded; its payload is on stdout and is data.
* **2** — no tool named.
* **3** — the verb *refused*: it returned an ``[error:…]`` payload. That is the
  seven-verb protocol working as designed (the MCP surface reports errors as
  rendered strings, not exceptions), but for a shell caller a rendered refusal
  is not data, so it goes to stderr with a non-zero exit. It used to go to
  stdout at exit 0, which made ``cmd > out && process out`` run ``process`` on
  an error string (gr458317).
* **1** — the CLI itself crashed on an unexpected exception. Kept distinct from
  3 so a caller can tell "the verb said no" from "the tool is broken".
"""

from __future__ import annotations

import argparse
import logging
import sys

from precis.cli._common import REFUSAL_EXIT, is_refusal
from precis.tools.cli_adapter import add_tool_parsers, run_tool_from_cli

log = logging.getLogger(__name__)


def run(args: argparse.Namespace) -> None:
    """Run the tools CLI subcommand."""
    if not args.tool:
        print("tools: no tool specified", file=sys.stderr)
        sys.exit(2)

    try:
        result = run_tool_from_cli(args.tool, args)
    except Exception as e:
        log.exception("Error running tool %s", args.tool)
        print(f"[error:Exception] {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)

    if is_refusal(result):
        print(result, file=sys.stderr)
        sys.exit(REFUSAL_EXIT)
    print(result)


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    """Add the tools subcommand parser."""
    parser = subparsers.add_parser(
        "tools",
        help="Run precis tools (get, search, put, edit, delete, tag, link)",
        description="Command-line interface for precis seven-verb API tools",
    )

    # Add subparser for each tool
    tool_subparsers = parser.add_subparsers(
        dest="tool",
        required=True,
        help="Available tools",
    )

    # Auto-generate parsers for all tools from the shared registry
    add_tool_parsers(tool_subparsers)

    parser.set_defaults(func=run)


__all__ = [
    "REFUSAL_EXIT",
    "add_parser",
    "run",
]
