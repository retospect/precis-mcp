"""MCP stdio server. Thin FastMCP wrapper around `PrecisRuntime`.

Four tools — `get`, `search`, `put`, `move` — are registered at module
import time. Each delegates to the module-level runtime, which is
initialized in `main()` before `mcp.run()` is called.

Tests should not import this module; they construct `PrecisRuntime`
directly via `precis.runtime.build_runtime()` and call
`.dispatch(verb, args)` to bypass the MCP transport.
"""

from __future__ import annotations

import logging
import sys

from mcp.server.fastmcp import FastMCP

from precis.runtime import PrecisRuntime, build_runtime

_INSTRUCTIONS = (
    "precis-mcp v2 — four-verb agent tool surface.\n\n"
    "Verbs: get, search, put, move.  Discriminator: kind=.\n"
    "Read the `precis-overview` skill (get(kind='skill', id='precis-overview'))\n"
    "for the full mental model: kind topology, addressing, views, modes,\n"
    "tags, links, and cache."
)


mcp: FastMCP = FastMCP("precis", instructions=_INSTRUCTIONS)


_runtime: PrecisRuntime | None = None


def _rt() -> PrecisRuntime:
    if _runtime is None:
        raise RuntimeError(
            "precis runtime not initialised — server.main() must run first"
        )
    return _runtime


# ---------------------------------------------------------------------------
# Tools — the four verbs
# ---------------------------------------------------------------------------


@mcp.tool()
async def get(
    kind: str,
    id: str | int | None = None,
    view: str | None = None,
    q: str | None = None,
) -> str:
    """Read a ref or compute a value.

    Args:
        kind: Which kind to read from (e.g. 'calc', 'paper', 'todo').
        id:   Identifier — string slug for slug kinds, int for numeric kinds.
        view: Display variant (kind-specific; e.g. 'bibtex' for paper).
        q:    Free-text query (used by some kinds in lieu of id).
    """
    return await _rt().dispatch("get", {"kind": kind, "id": id, "view": view, "q": q})


@mcp.tool()
async def search(
    q: str,
    kind: str | None = None,
    scope: str | None = None,
    top_k: int = 10,
) -> str:
    """Search across kinds.

    Args:
        q:     Free-text query (lexical + semantic, hybrid-fused).
        kind:  Restrict to a single kind. Omit for cross-corpus search.
        scope: Restrict to one ref's blocks (slug or numeric id).
        top_k: Max results.
    """
    return await _rt().dispatch(
        "search",
        {"kind": kind, "q": q, "scope": scope, "top_k": top_k},
    )


@mcp.tool()
async def put(
    kind: str,
    mode: str,
    id: str | int | None = None,
    text: str | None = None,
) -> str:
    """Write or annotate.

    Args:
        kind: Which kind to write to.
        mode: Operation (e.g. 'append', 'replace', 'after', 'before',
              'delete', 'comment', 'note', 'tag', 'link').  Per-kind.
        id:   Target ref or block.
        text: Content for write modes.
    """
    return await _rt().dispatch(
        "put", {"kind": kind, "id": id, "text": text, "mode": mode}
    )


@mcp.tool()
async def move(
    kind: str,
    id: str | int,
    after: str | int,
) -> str:
    """Reorder a node within a structured ref.

    Args:
        kind:  Which kind owns the structure (e.g. 'docx', 'tex').
        id:    Node to move.
        after: Reference node — moved node lands after this one.
    """
    return await _rt().dispatch("move", {"kind": kind, "id": id, "after": after})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    global _runtime
    _runtime = build_runtime()
    logging.basicConfig(
        level=_runtime.config.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
