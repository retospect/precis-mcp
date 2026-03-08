"""FastMCP server wiring for Precis."""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from precis.tools import Session, PrecisError, activate, toc, get, put, move

log = logging.getLogger(__name__)

mcp = FastMCP("precis")
session = Session()


def _error(e: PrecisError) -> str:
    return e.format()


@mcp.tool()
async def tool_activate(file: str) -> str:
    """Open or create a .docx or .tex document and show its table of contents.

    Args:
        file: Path to .docx or .tex file (created if missing)
    """
    try:
        return await activate(session, file)
    except PrecisError as e:
        return _error(e)


@mcp.tool()
async def tool_toc(scope: str = "", grep: str = "") -> str:
    """Navigate and search the active document. One line per node, truncated at ~120 chars.

    Args:
        scope: Path prefix to limit tree, e.g. "H2.1"
        grep: Filter nodes — plain text, /regex/, or /regex/i
    """
    try:
        return await toc(session, scope=scope, grep=grep)
    except PrecisError as e:
        return _error(e)


@mcp.tool()
async def tool_get(id: str) -> str:
    """Read full content by slug, path, label, or comma-separated list.

    Lines starting with >> are metadata. Everything else is content.

    Args:
        id: Slug, path, label, or comma-separated list
    """
    try:
        return await get(session, id=id)
    except PrecisError as e:
        return _error(e)


@mcp.tool()
async def tool_put(
    id: str = "",
    text: str = "",
    mode: str = "replace",
    tracked: bool = True,
) -> str:
    """Mutate the active document. One paragraph per call.

    Modes: replace, after, before, delete, append.
    For DOCX, tracked=true writes Word track-changes markup.
    Use # prefix in text to create headings.

    Args:
        id: Target node slug/path (required except for append)
        text: New content (single paragraph). Use # for headings.
        mode: replace / after / before / delete / append
        tracked: DOCX: write as track-changes (default true). LaTeX: ignored.
    """
    try:
        return await put(session, id=id, text=text, mode=mode, tracked=tracked)
    except PrecisError as e:
        return _error(e)


@mcp.tool()
async def tool_move(id: str, after: str) -> str:
    """Reorder nodes within the document.

    Slugs don't change (content unchanged). Paths are recomputed.

    Args:
        id: Slug or comma-separated slugs to move
        after: Target slug — moved nodes placed after this node
    """
    try:
        return await move(session, id=id, after=after)
    except PrecisError as e:
        return _error(e)


def main():
    """Entry point for `precis` CLI command."""
    logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")
    mcp.run()


if __name__ == "__main__":
    main()
