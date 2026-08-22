"""``precis eval '<call>'`` — execute one verb call in the command syntax.

Second consumer of :mod:`precis.tools.command_parser` (the frontier
MCP profile, ``PRECIS_MCP_PROFILE=command``, is the first) — lets an
operator run ``precis eval "get(kind='skill', id='toc')"`` without
building the eight-flag ``precis tools <verb> --flag value`` command
line. Routes through the same ``TOOL_REGISTRY[verb]['func']`` call
every other surface uses, so validation and error rendering match.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from precis.tools import TOOL_REGISTRY
from precis.tools.cli_adapter import _is_call_tool_result
from precis.tools.command_parser import CommandParseError, parse_command


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "eval",
        help="Execute one verb call, e.g. eval \"get(kind='skill', id='toc')\".",
        description=__doc__,
    )
    p.add_argument(
        "command",
        help="Call expression with keyword-literal args, "
        "e.g. get(kind='skill', id='toc').",
    )
    p.add_argument(
        "--text",
        default=None,
        help="Large text payload merged in as text= (avoids quote-escaping "
        "it inside command).",
    )
    p.add_argument(
        "--text-file",
        type=Path,
        default=None,
        help="Read the text= payload from this file (UTF-8) instead of --text.",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    text = args.text
    if args.text_file is not None:
        if text is not None:
            print(
                "eval: --text and --text-file are mutually exclusive", file=sys.stderr
            )
            sys.exit(2)
        text = args.text_file.read_text(encoding="utf-8")

    try:
        verb, kwargs = parse_command(args.command, text=text)
    except CommandParseError as e:
        print(f"[error:CommandParseError] {e}", file=sys.stderr)
        sys.exit(1)

    tool_func = TOOL_REGISTRY[verb]["func"]
    try:
        result = tool_func(**kwargs)
    except TypeError as e:
        print(f"[error:BadInput] {verb}(...): {e}", file=sys.stderr)
        sys.exit(1)

    if _is_call_tool_result(result):
        print(result.content[0].text)
        sys.exit(1)
    print(result)


__all__ = ["add_parser", "run"]
