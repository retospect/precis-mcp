"""CLI interface for precis tools using shared registry.

This module provides the command-line interface for all precis tools,
automatically generated from the shared tool registry to stay in sync
with the MCP server interface.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

from precis.tools.cli_adapter import add_tool_parsers, run_tool_from_cli

log = logging.getLogger(__name__)


def run(args: argparse.Namespace) -> None:
    """Run the tools CLI subcommand."""
    if not args.tool:
        print("tools: no tool specified", file=sys.stderr)
        sys.exit(2)
    
    try:
        result = run_tool_from_cli(args.tool, args)
        print(result)
    except Exception as e:
        log.exception("Error running tool %s", args.tool)
        print(f"[error:Exception] {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)


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
