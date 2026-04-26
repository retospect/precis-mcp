"""MCP stdio server. Thin FastMCP wrapper around `PrecisRuntime`.

Four tools — `get`, `search`, `put`, `move` — are registered as plain
sync functions. FastMCP runs sync tool callables in a worker thread, so
the rest of the codebase (runtime, store, handlers) stays sync.

The runtime — including the postgres connection pool — is built before
`mcp.run()` and torn down after it returns. Only the FastMCP loop
itself is async; everything below this file is sync.

Tests should not import this module; they construct `PrecisRuntime`
directly via fixtures and call `.dispatch(verb, args)` to bypass the
MCP transport.
"""

from __future__ import annotations

import atexit
import logging
import sys

from mcp.server.fastmcp import FastMCP

from precis.runtime import PrecisRuntime, build_runtime

_INSTRUCTIONS = (
    "precis-mcp v2 — four-verb agent tool surface.\n\n"
    "Verbs: get, search, put, put.  Discriminator: kind=.\n"
    "Read the `precis-overview` skill (get(kind='skill', id='precis-overview'))\n"
    "for the full mental model: kind topology, addressing, views, modes,\n"
    "tags, links, and cache."
)


_runtime: PrecisRuntime | None = None


def _rt() -> PrecisRuntime:
    if _runtime is None:
        raise RuntimeError(
            "precis runtime not initialised — call _init_runtime() first"
        )
    return _runtime


def _init_runtime() -> PrecisRuntime:
    """Build the runtime once and register cleanup at process exit."""
    global _runtime
    if _runtime is not None:
        return _runtime
    _runtime = build_runtime()
    atexit.register(_shutdown_runtime)
    return _runtime


def _shutdown_runtime() -> None:
    global _runtime
    if _runtime is not None and _runtime.store is not None:
        try:
            _runtime.store.close()
        except Exception:
            log.exception("error closing store")
    _runtime = None


log = logging.getLogger(__name__)
mcp: FastMCP = FastMCP("precis", instructions=_INSTRUCTIONS)


# ---------------------------------------------------------------------------
# Tools — the four verbs
# ---------------------------------------------------------------------------


@mcp.tool()
def get(
    kind: str,
    id: str | int | None = None,
    view: str | None = None,
    q: str | None = None,
) -> str:
    """Read a ref or compute a value.

    Args:
        kind: Which kind to read from (e.g. 'calc', 'paper', 'memory').
        id:   Identifier — string slug for slug kinds, int for numeric kinds.
        view: Display variant (kind-specific; e.g. 'bibtex' for paper).
        q:    Free-text query (used by some kinds in lieu of id).
    """
    return _rt().dispatch("get", {"kind": kind, "id": id, "view": view, "q": q})


@mcp.tool()
def search(
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
    return _rt().dispatch(
        "search",
        {"kind": kind, "q": q, "scope": scope, "top_k": top_k},
    )


@mcp.tool()
def put(
    kind: str,
    mode: str | None = None,
    id: str | int | None = None,
    text: str | None = None,
    tags: list[str] | None = None,
    link: str | None = None,
) -> str:
    """Write or annotate.

    Args:
        kind: Which kind to write to.
        mode: Operation hint (e.g. 'append', 'replace', 'delete', 'note').
              Some kinds infer mode from arguments and don't require it.
        id:   Target ref or block. Omit to create new (numeric kinds).
        text: Content for write modes.
        tags: Tag strings to apply (closed 'STATUS:done', flag 'pinned',
              or open 'topic-x').
        link: 'target_slug' or 'target_slug:relation' to add a link.
    """
    return _rt().dispatch(
        "put",
        {
            "kind": kind,
            "id": id,
            "text": text,
            "mode": mode,
            "tags": tags,
            "link": link,
        },
    )


@mcp.tool()
def move(
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
    return _rt().dispatch("move", {"kind": kind, "id": id, "after": after})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the MCP stdio server.

    Build the runtime (including postgres pool) before mcp.run takes
    over, register atexit shutdown, then hand control to FastMCP.
    """
    from precis.config import load_config

    config = load_config()
    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    _init_runtime()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
