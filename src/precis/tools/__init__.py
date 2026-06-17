"""Shared tool registry for MCP server and CLI interface.

This module provides the single source of truth for all tool definitions,
ensuring the MCP server and CLI interface stay automatically synchronized.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from precis.tools.core import (
    CLI_HELP,
    delete,
    edit,
    get,
    link,
    more,
    put,
    search,
    tag,
)

# Tool registry - single source of truth for both MCP and CLI
TOOL_REGISTRY: dict[str, dict[str, Any]] = {}


def _register_tool(name: str, func: Callable) -> None:
    """Register a tool with its metadata for both MCP and CLI consumption."""
    TOOL_REGISTRY[name] = {
        "func": func,
        "doc": func.__doc__ or "",
        "signature": inspect.signature(func),
        "parameters": _extract_parameters(func),
        # Per-arg ``--help`` strings for the CLI argparse adapter. The
        # MCP-facing docstrings were trimmed to a tight summary +
        # discovery pointer (see
        # ``docs/design/mcp-cold-start-token-budget.md``); explicit
        # per-arg help lives in :data:`precis.tools.core.CLI_HELP`.
        "cli_help": CLI_HELP.get(name, {}),
    }


def _extract_parameters(func: Callable) -> dict[str, dict[str, Any]]:
    """Extract parameter metadata from function signature."""
    sig = inspect.signature(func)
    params = {}

    for name, param in sig.parameters.items():
        if name == "self":
            continue

        param_info = {
            "name": name,
            "required": param.default == param.empty,
            "default": param.default if param.default != param.empty else None,
            "annotation": param.annotation,
            "kind": param.kind,
        }

        # Determine CLI flag name
        param_info["cli_flag"] = f"--{name.replace('_', '-')}"

        # Handle special types
        if param.annotation == list[str] or str(param.annotation).startswith("list["):
            param_info["is_list"] = True
        else:
            param_info["is_list"] = False

        params[name] = param_info

    return params


# Register all tools
_register_tool("get", get)
_register_tool("search", search)
_register_tool("put", put)
_register_tool("edit", edit)
_register_tool("delete", delete)
_register_tool("tag", tag)
_register_tool("link", link)
# Pagination tool: agent calls this with a cursor handed back inside
# a chunked verb response to retrieve the next page.
_register_tool("more", more)


def get_tool_names() -> list[str]:
    """Get all registered tool names."""
    return list(TOOL_REGISTRY.keys())


def get_tool_info(name: str) -> dict[str, Any]:
    """Get metadata for a specific tool."""
    if name not in TOOL_REGISTRY:
        raise ValueError(f"Tool '{name}' not found in registry")
    return TOOL_REGISTRY[name]
