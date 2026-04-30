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
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

from precis.runtime import PrecisRuntime, build_runtime

# FastMCP refuses ``str | CallToolResult`` return annotations (it bans
# CallToolResult inside unions; see ``func_metadata.py``). We still
# return ``CallToolResult`` at runtime on errors — FastMCP's
# ``FuncMetadata.convert_result`` passes ``CallToolResult`` instances
# through verbatim so the protocol-level ``isError`` flag is preserved.
# Each tool's annotation therefore stays ``str``; the actual return
# type is ``str | CallToolResult`` but only ``str`` is advertised to
# FastMCP.
_ToolReturn = Any  # documents runtime: str on success, CallToolResult on error

# mcp 1.27.0's ``FuncMetadata.convert_result`` validates
# ``CallToolResult.structuredContent`` against the auto-generated output
# schema whenever the tool has one.  Our error path returns a
# ``CallToolResult`` with only ``content`` + ``isError`` set, so the
# validation against the ``str``-shaped schema rejects ``structuredContent
# = None``.  Disabling structured output skips that validation and lets
# the success path render plain ``TextContent`` directly.  Agents see no
# difference — every wrapper has always grokked the ``[error:Class]
# cause / options / next`` text.
_TOOL_KW: dict[str, Any] = {"structured_output": False}

_INSTRUCTIONS = (
    "precis-mcp v2 — four-verb agent tool surface.\n\n"
    "Verbs: get, search, put, move.  Discriminator: kind=.\n"
    "Read the `precis-overview` skill (get(kind='skill', id='precis-overview'))\n"
    "for the full mental model: kind topology, addressing, views, modes,\n"
    "tags, links, and cache."
)

# Sanity check the instructions actually advertise every verb. The MCP
# critic flagged ``put, put`` as a silent typo that hides ``move`` from
# every caller relying on serverInfo.instructions; an assertion here
# catches future regressions at import time.
assert all(v in _INSTRUCTIONS for v in ("get", "search", "put", "move")), (
    "_INSTRUCTIONS must list every verb"
)


_runtime: PrecisRuntime | None = None


def _rt() -> PrecisRuntime:
    if _runtime is None:
        raise RuntimeError(
            "precis runtime not initialised — call _init_runtime() first"
        )
    return _runtime


def _init_runtime() -> PrecisRuntime:
    """Build the runtime once and register cleanup at process exit.

    Also wires the prompts and resources modalities — skill files
    surface as ``prompts/list`` entries and kind handlers as
    ``resources/list`` + ``resources/templates/list``.  Both
    surfaces delegate to the runtime so there is no parallel
    rendering pipeline.  See :mod:`precis.mcp_modalities`.
    """
    global _runtime
    if _runtime is not None:
        return _runtime
    _runtime = build_runtime()
    _wire_modalities(_runtime)
    atexit.register(_shutdown_runtime)
    return _runtime


def _wire_modalities(runtime: PrecisRuntime) -> None:
    """Register prompts + resources for the running MCP.

    Best-effort: if either registration fails we log and continue —
    a wiring bug must not prevent the MCP from booting and serving
    the four verb tools.  The MCP critic flagged the modality gap
    as MINOR; the tools surface remains the priority.
    """
    from precis.mcp_modalities import register_resources, register_skill_prompts

    try:
        register_skill_prompts(mcp, runtime)
    except Exception:
        log.exception("failed to register skill prompts")
    try:
        register_resources(mcp, runtime)
    except Exception:
        log.exception("failed to register resources")


def _shutdown_runtime() -> None:
    global _runtime
    if _runtime is not None and _runtime.store is not None:
        try:
            _runtime.store.close()
        except Exception:
            log.exception("error closing store")
    _runtime = None


log = logging.getLogger(__name__)
# Server name is ``precis-mcp`` so log lines and serverInfo unambiguously
# point at this package (the bare ``precis`` collides with other tooling).
mcp: FastMCP = FastMCP("precis-mcp", instructions=_INSTRUCTIONS)


# ---------------------------------------------------------------------------
# Tools — the four verbs
# ---------------------------------------------------------------------------


def _dispatch(verb: str, payload: dict[str, Any]) -> _ToolReturn:
    """Dispatch one verb call and shape the MCP-level result.

    On success, returns the rendered string — FastMCP wraps it as the
    sole text content of the tool result. On error, returns a
    :class:`CallToolResult` with ``isError=True`` so the protocol
    surface matches the body. The body itself stays the same
    ``[error:Class] cause / options / next`` text the runtime always
    rendered, so wrappers that already grok that shape keep working.
    (MCP critic MAJOR — errors-as-strings without ``isError``.)
    """
    body, is_error = _rt().dispatch_with_status(verb, payload)
    if not is_error:
        return body
    return CallToolResult(
        content=[TextContent(type="text", text=body)],
        isError=True,
    )


def _validation_error(body: str) -> _ToolReturn:
    """Wrap a pre-dispatch validation error in a CallToolResult.

    Used by the ``search`` and ``get`` tools when they reject malformed
    arguments before reaching the runtime. Keeps the protocol surface
    consistent with runtime-side errors.
    """
    return CallToolResult(
        content=[TextContent(type="text", text=body)],
        isError=True,
    )


@mcp.tool(**_TOOL_KW)
def get(
    kind: str,
    id: str | int | None = None,
    view: str | None = None,
    q: str | None = None,
    args: dict[str, Any] | None = None,
) -> str:
    """Read a ref or compute a value.

    Args:
        kind: Which kind to read from (e.g. 'calc', 'paper', 'memory').
        id:   Identifier — string slug for slug kinds, int for numeric kinds.
        view: Display variant (kind-specific; e.g. 'bibtex' for paper).
        q:    Free-text query (used by some kinds in lieu of id).
        args: Kind- and view-specific extra parameters as a dict. Used
              for views that need typed payloads beyond `id`/`view`/`q`,
              e.g. python's callgraph (``{'entry': 'pkg.mod:func',
              'depth': 3}``) or runtrace (``{'entry': '...', 'argv':
              [...], 'timeout': 10}``). See each kind's help skill for
              the accepted shape. Reserved key names (``kind``, ``id``,
              ``view``, ``q``) are rejected to prevent confusion with
              the explicit positional kwargs.
    """
    payload: dict[str, Any] = {"kind": kind, "id": id, "view": view, "q": q}
    if args:
        err = _check_reserved_args(args, reserved=("kind", "id", "view", "q"))
        if err is not None:
            return _validation_error(err)
        payload["__extras__"] = dict(args)
    return _dispatch("get", payload)


def _check_reserved_args(
    args: dict[str, Any], *, reserved: tuple[str, ...]
) -> str | None:
    """Return a rendered error string if `args` shadows positional kwargs.

    A model that mistakenly passes ``args={'id': 'foo'}`` instead of
    ``id='foo'`` would otherwise silently overwrite the positional
    `id` with the same value (or — worse — a different one). Surface
    the mistake at the boundary so the recovery hint is sharp.

    Mirrors the `search` tool's `top_k` validator: returns rendered
    text rather than raising, so the MCP transport sees a normal
    string response.
    """
    overlap = sorted(k for k in args if k in reserved)
    if not overlap:
        return None

    from precis.errors import BadInput

    return _rt()._render_error(  # type: ignore[attr-defined]
        BadInput(
            f"args={overlap!r} shadows the explicit kwargs {list(reserved)!r}",
            next="pass these as top-level keyword arguments, not inside args=",
        )
    )


#: Hard cap on ``top_k`` for the agent-facing search tool. The MCP
#: critic flagged ``top_k=9999`` returning 7 326 hits in a single
#: ~2.7 MB response, large enough to exhaust a 7B model's context
#: window in one call. 100 is comfortably above any sensible
#: pagination size and well below the response-size cliff.
_SEARCH_TOP_K_MAX: int = 100


@mcp.tool(**_TOOL_KW)
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
        top_k: Max results. Must be a positive integer ≤ 100. Larger
               values are rejected to bound response size and protect
               smaller models' context windows.
    """
    # Validate top_k at the MCP boundary so internal callers (tests,
    # SDK consumers) can still pass arbitrary values when they know
    # what they're doing. The agent-facing surface is the place to
    # enforce the cap. See MCP critic MAJOR #10.
    from precis.errors import BadInput

    if not isinstance(top_k, int) or top_k <= 0:
        return _validation_error(
            _rt()._render_error(  # type: ignore[attr-defined]
                BadInput(
                    f"top_k must be a positive integer, got {top_k!r}",
                    next="search(kind='paper', q='...', top_k=10)",
                )
            )
        )
    if top_k > _SEARCH_TOP_K_MAX:
        return _validation_error(
            _rt()._render_error(  # type: ignore[attr-defined]
                BadInput(
                    f"top_k={top_k} exceeds maximum {_SEARCH_TOP_K_MAX}",
                    next=(
                        f"narrow with scope= or paginate; "
                        f"max top_k is {_SEARCH_TOP_K_MAX}"
                    ),
                )
            )
        )
    return _dispatch(
        "search",
        {"kind": kind, "q": q, "scope": scope, "top_k": top_k},
    )


@mcp.tool(**_TOOL_KW)
def put(
    kind: str,
    mode: str | None = None,
    id: str | int | None = None,
    text: str | None = None,
    tags: list[str] | None = None,
    untags: list[str] | None = None,
    link: str | None = None,
    unlink: str | None = None,
    rel: str | None = None,
) -> str:
    """Write or annotate.

    Args:
        kind:   Which kind to write to.
        mode:   Operation hint. Currently the only widely-supported mode
                is 'delete' for soft-delete on numeric-ref kinds. File
                kinds (markdown, tex, …) accept 'append' / 'replace';
                see each kind's help skill. Unknown modes are rejected.
        id:     Target ref or block. Omit to create a new ref (numeric
                kinds).
        text:   Content for create or text update.
        tags:   Tag strings to apply (closed 'STATUS:done', flag 'pinned',
                or open 'topic-x'). On update, the tag list is *added* —
                use ``untags=`` to remove.
        untags: Tag strings to remove. Closed-prefix entries (e.g.
                'STATUS:open') match the prefix and value; flags and
                open tags match exactly. Removing a tag the ref doesn't
                carry is a no-op, not an error.
        link:   Add a link to another ref. Canonical form
                'kind:identifier[~selector]' — e.g. 'paper:wang2020',
                'paper:wang2020~38' (block 38), 'todo:158'. The kind
                prefix is required.
        unlink: Remove a link. Same canonical form as ``link=``. With
                ``rel=`` it removes one specific (target, relation)
                pair; without it removes every link to the target.
        rel:    Relation slug for ``link=`` / ``unlink=``. Defaults to
                'related-to'. See ``precis-relations`` for the full
                vocabulary (cites, blocks, contradicts, derived-from,
                supports, …). Required when adding non-default
                relations.
    """
    return _dispatch(
        "put",
        {
            "kind": kind,
            "id": id,
            "text": text,
            "mode": mode,
            "tags": tags,
            "untags": untags,
            "link": link,
            "unlink": unlink,
            "rel": rel,
        },
    )


@mcp.tool(**_TOOL_KW)
def move(
    kind: str,
    id: str | int,
    after: str | int,
) -> str:
    """Reorder a node within a structured ref.

    NOTE: No active kind in this build implements ``move``. The verb
    is reserved for structured file kinds (``docx``, ``tex``) that
    aren't wired yet; calling it returns an Unsupported error pointing
    you at ``put`` instead. The tool stays exposed so the surface
    matches ``precis-overview`` and so future kinds can light it up
    without re-registration.

    Args:
        kind:  Which kind owns the structure (e.g. 'docx', 'tex').
        id:    Node to move.
        after: Reference node — moved node lands after this one.
    """
    return _dispatch("move", {"kind": kind, "id": id, "after": after})


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
