"""CLI adapter for auto-generating argument parsers from tool signatures.

This module provides the bridge between the shared tool registry and
the command-line interface, automatically creating argparse parsers
that stay in sync with the tool function signatures.
"""

from __future__ import annotations

import argparse
import shlex
from typing import Any

from precis.tools import get_tool_info, get_tool_names


def _parse_list_value(value: str) -> list[str]:
    """Parse a list value from CLI string.
    
    Supports:
    - Comma-separated: "tag1,tag2,tag3"
    - Space-separated: "tag1 tag2 tag3" (quoted)
    - Single value: "tag1"
    """
    if not value:
        return []
    
    # Try comma-separated first
    if ',' in value:
        return [item.strip() for item in value.split(',') if item.strip()]
    
    # Fall back to space-separated
    return [item.strip() for item in value.split() if item.strip()]


def _convert_value(value: str, param_info: dict[str, Any]) -> Any:
    """Convert CLI string value to the appropriate Python type."""
    if param_info["is_list"]:
        return _parse_list_value(value)
    
    # Handle basic types
    annotation = str(param_info["annotation"])
    
    if "int" in annotation:
        try:
            return int(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"Expected integer, got: {value}")
    
    if "bool" in annotation:
        if value.lower() in ("true", "1", "yes", "on"):
            return True
        elif value.lower() in ("false", "0", "no", "off"):
            return False
        else:
            raise argparse.ArgumentTypeError(f"Expected boolean, got: {value}")
    
    # Default to string
    return value


def build_parser_for_tool(
    tool_name: str, 
    subparsers: argparse._SubParsersAction
) -> argparse.ArgumentParser:
    """Build an argparse parser for a specific tool from its signature."""
    tool_info = get_tool_info(tool_name)
    
    parser = subparsers.add_parser(
        tool_name,
        help=tool_info["doc"].split('\n')[0] if tool_info["doc"] else f"Run {tool_name} tool",
        description=tool_info["doc"] or f"Run the {tool_name} tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    
    # Add arguments based on function signature
    for param_name, param_info in tool_info["parameters"].items():
        cli_flag = param_info["cli_flag"]
        
        # Skip 'args' parameter for CLI - it's complex and rarely used
        if param_name == "args":
            continue
        
        # Build argument help from docstring if possible
        help_text = f"Parameter {param_name}"
        if tool_info["doc"]:
            # Try to extract help from docstring
            for line in tool_info["doc"].split('\n'):
                if f"{param_name}:" in line:
                    help_text = line.split(":", 1)[1].strip()
                    break
        
        # Configure argument based on parameter properties
        if param_info["required"]:
            parser.add_argument(
                cli_flag,
                required=True,
                help=help_text,
                type=lambda v: _convert_value(v, param_info)
            )
        else:
            parser.add_argument(
                cli_flag,
                default=param_info["default"],
                help=f"{help_text} (default: {param_info['default']})",
                type=lambda v: _convert_value(v, param_info)
            )
    
    return parser


def add_tool_parsers(subparsers: argparse._SubParsersAction) -> None:
    """Add parsers for all registered tools."""
    for tool_name in get_tool_names():
        build_parser_for_tool(tool_name, subparsers)


def convert_args_to_payload(tool_name: str, args: argparse.Namespace) -> dict[str, Any]:
    """Convert parsed CLI arguments to the payload format expected by tools."""
    tool_info = get_tool_info(tool_name)
    payload = {}
    
    for param_name, param_info in tool_info["parameters"].items():
        if param_name == "args":
            continue  # Skip complex args parameter
        
        # Get the value from parsed args (convert CLI flag name to param name)
        cli_attr_name = param_name.replace('-', '_')
        value = getattr(args, cli_attr_name, None)
        
        # Only include if value is not None or if it's explicitly required
        if value is not None or param_info["required"]:
            payload[param_name] = value
    
    return payload


def run_tool_from_cli(tool_name: str, args: argparse.Namespace) -> str:
    """Execute a tool from parsed CLI arguments.

    Tool functions in :mod:`precis.tools.core` return ``str`` on
    success and ``mcp.types.CallToolResult`` (with
    ``isError=True``) on failure — the shape FastMCP needs to set
    the MCP protocol-level error flag. CLI consumers don't have a
    protocol envelope; they get the rendered text directly. This
    adapter unwraps the ``CallToolResult`` so operators see the
    same ``[error:Class] cause / next`` body without any wrapper
    glyphs.
    """
    from precis.tools import TOOL_REGISTRY

    # Convert CLI args to tool payload
    payload = convert_args_to_payload(tool_name, args)

    # Get and call the tool function
    tool_func = TOOL_REGISTRY[tool_name]["func"]

    # Call the tool with the converted arguments
    try:
        result = tool_func(**payload)
    except Exception as e:
        # Return error message in a consistent format
        return f"[error:Exception] {type(e).__name__}: {e}"

    # Unwrap the MCP error envelope for CLI display. The
    # ``CallToolResult`` carries the ``isError`` flag plus a single
    # ``TextContent`` block; the operator wants the body text.
    if _is_call_tool_result(result):
        return result.content[0].text  # type: ignore[union-attr]
    return result


def _is_call_tool_result(value: Any) -> bool:
    """Return True if *value* is an MCP ``CallToolResult`` envelope.

    Imported lazily — ``mcp.types`` may not be installed in every
    environment that consumes the tools (CLI-only builds, tests
    that monkey-patch the dummy ``CallToolResult`` from
    ``precis.tools.core``). Falling back on duck-typing
    (``content[0].text`` + ``isError``) covers both paths without
    a hard dep.
    """
    try:
        from mcp.types import CallToolResult

        if isinstance(value, CallToolResult):
            return True
    except ImportError:
        pass
    return (
        hasattr(value, "isError")
        and hasattr(value, "content")
        and bool(getattr(value, "content", None))
    )
