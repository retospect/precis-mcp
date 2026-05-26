"""Core tool implementations shared between MCP server and CLI.

These are the actual tool functions that implement the seven-verb API.
Both the MCP server and CLI interface consume these functions through
the shared registry in tools/__init__.py.
"""

from __future__ import annotations

from typing import Any

# Conditional imports for MCP types (not available in all environments)
try:
    from mcp.types import CallToolResult, TextContent

    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False

    # Create dummy classes for type checking when MCP is not available.
    # mypy can't see the mutually-exclusive paths through the try/except
    # so it flags these as redefinitions of the imported names; the
    # ``no-redef`` ignores are the standard escape hatch for the
    # conditional-import pattern.
    class CallToolResult:  # type: ignore[no-redef]
        def __init__(self, content, isError=False):
            self.content = content
            self.isError = isError

    class TextContent:  # type: ignore[no-redef]
        def __init__(self, type, text):
            self.type = type
            self.text = text


# FastMCP refuses ``str | CallToolResult`` return annotations on tool
# functions (it bans ``CallToolResult`` inside unions; see upstream
# ``func_metadata.py``). We still return ``CallToolResult`` at runtime
# on errors — FastMCP's ``FuncMetadata.convert_result`` passes
# ``CallToolResult`` instances through verbatim so the MCP protocol-
# level ``isError`` flag is preserved. Each verb's wire annotation
# therefore stays ``-> str``; this alias documents the actual runtime
# contract and lets mypy accept the dual-shape returns.
_ToolReturn = Any  # documents runtime: str on success, CallToolResult on error.

# Runtime access - these will be imported when needed
_runtime = None


def _get_runtime():
    """Get the current runtime instance."""
    global _runtime
    if _runtime is None:
        from precis.runtime import build_runtime

        _runtime = build_runtime()
    return _runtime


def _dispatch(verb: str, payload: dict[str, Any]) -> _ToolReturn:
    """Dispatch one verb call and shape the result.

    On success, returns the rendered string.
    On error, returns a ``CallToolResult`` with ``isError=True`` for
    MCP compatibility. CLI callers should run the result through
    :func:`precis.tools.cli_adapter._is_call_tool_result` to detect
    errors before treating the value as text.
    """
    runtime = _get_runtime()
    try:
        body, is_error = runtime.dispatch_with_status(verb, payload)
        if is_error:
            return CallToolResult(
                content=[TextContent(type="text", text=body)],
                isError=True,
            )
        return body
    except Exception as e:
        # Handle unexpected exceptions
        error_body = runtime.render_error(e)
        return CallToolResult(
            content=[TextContent(type="text", text=error_body)],
            isError=True,
        )


def _validation_error(body: str) -> _ToolReturn:
    """Wrap a pre-dispatch validation error string in a ``CallToolResult``.

    The MCP protocol distinguishes successful tool results from errors
    via the ``isError`` flag on ``CallToolResult``. Pre-dispatch
    validation paths (e.g. ``_check_reserved_args``, the ``search``
    ``top_k`` cap) build the rendered text via
    ``runtime.render_error`` but must surface it through the same
    error-flag-bearing envelope the runtime uses for handler-side
    failures — otherwise MCP wrappers see a successful response with
    error-shaped text and never trigger their retry / recovery logic.
    (MCP critic MAJOR — errors-as-strings without ``isError``.)
    """
    return CallToolResult(
        content=[TextContent(type="text", text=body)],
        isError=True,
    )


def _check_reserved_args(
    args: dict[str, Any], *, reserved: tuple[str, ...]
) -> _ToolReturn:
    """Return a ``CallToolResult`` error if ``args`` shadows positional kwargs.

    Returns ``None`` when the args dict is clean. Returning the
    error envelope (rather than a bare string) preserves the MCP
    ``isError`` flag through the tool boundary so wrappers can
    detect and recover from the protocol error.
    """
    overlap = sorted(k for k in args if k in reserved)
    if not overlap:
        return None

    from precis.errors import BadInput

    body = _get_runtime().render_error(
        BadInput(
            f"args={overlap!r} shadows the explicit kwargs {list(reserved)!r}",
            next="pass these as top-level keyword arguments, not inside args=",
        )
    )
    return _validation_error(body)


# Hard cap on top_k for search tool
_SEARCH_TOP_K_MAX: int = 100


def get(
    kind: str,
    id: str | int | None = None,
    view: str | None = None,
    q: str | None = None,
    args: dict[str, Any] | None = None,
) -> str:
    """Read a ref or compute a value.

    `id=` is the slug / numeric id. `view=` picks a display variant
    (kind-specific, e.g. 'abstract', 'bibtex'). `q=` is a free-text
    query for compute-style kinds. `args=` is a dict of typed extras
    for views that need them (callgraph, runtrace, ...); reserved
    keys (`kind`, `id`, `view`, `q`) inside `args=` are rejected.

    Full reference: search(kind='skill', q='get <kind>') or
    get(kind='skill', id='precis-get-help').
    """
    payload: dict[str, Any] = {"kind": kind, "id": id, "view": view, "q": q}
    if args:
        err = _check_reserved_args(args, reserved=("kind", "id", "view", "q"))
        if err is not None:
            return err
        payload["__extras__"] = dict(args)

    # ``_dispatch`` returns ``str`` on success and ``CallToolResult``
    # with ``isError=True`` on failure. We propagate both verbatim;
    # FastMCP's ``FuncMetadata.convert_result`` passes
    # ``CallToolResult`` through unchanged so the protocol
    # ``isError`` flag is preserved. CLI consumers unwrap via
    # :func:`precis.tools.cli_adapter.run_tool_from_cli`.
    return _dispatch("get", payload)


def search(
    q: str,
    kind: str | None = None,
    scope: str | None = None,
    top_k: int = 10,
    tags: list[str] | None = None,
    source: str | None = None,
    exclude: list[str] | None = None,
) -> str:
    """Hybrid lexical + semantic search across kinds.

    `top_k` must be a positive int ≤ 100. Omit `kind` (or pass `'*'`)
    for cross-kind fan-out. `exclude=` takes ref slugs to drop — use
    to paginate by passing back prior-page slugs. `source=` is
    patent-only (`'both'`/`'local'`/`'remote'`); ignored elsewhere.

    Full reference: search(kind='skill', q='search <kind>') or
    get(kind='skill', id='precis-search-help'). For per-kind nuances
    (e.g. patent's prior-art sweep) search the skill index by topic.
    """
    # Validate top_k at the boundary. Errors round-trip via
    # ``_validation_error`` so the MCP ``isError`` flag survives.
    from precis.errors import BadInput

    if not isinstance(top_k, int) or top_k <= 0:
        runtime = _get_runtime()
        return _validation_error(
            runtime.render_error(
                BadInput(
                    f"top_k must be a positive integer, got {top_k!r}",
                    next="search(kind='paper', q='...', top_k=10)",
                )
            )
        )
    if top_k > _SEARCH_TOP_K_MAX:
        runtime = _get_runtime()
        return _validation_error(
            runtime.render_error(
                BadInput(
                    f"top_k={top_k} exceeds maximum {_SEARCH_TOP_K_MAX}",
                    next=(
                        f"narrow with scope= or paginate; "
                        f"max top_k is {_SEARCH_TOP_K_MAX}"
                    ),
                )
            )
        )

    payload: dict[str, Any] = {
        "kind": kind,
        "q": q,
        "scope": scope,
        "top_k": top_k,
    }

    # Only forward optional kwargs when set
    if tags is not None:
        payload["tags"] = tags
    if source is not None:
        payload["source"] = source
    if exclude is not None:
        payload["exclude"] = exclude

    # See ``get`` for the ``str | CallToolResult`` return contract.
    return _dispatch("search", payload)


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
    """Write or annotate. Creates new refs; for region rewrites use `edit`.

    File kinds (`markdown`, `plaintext`, `tex`, `python`) require
    `mode='create'` (only accepted value; region edits live on `edit`,
    whole-file deletes on `delete`). Numeric-ref kinds (`memory`,
    `todo`, `gripe`, `conv`, `fc`, `quest`) omit `mode=` to create.
    `tags=` adds, `untags=` removes. `link=`/`unlink=` use canonical
    `kind:identifier[~selector]` form; `rel=` defaults to `related-to`.

    Full reference: search(kind='skill', q='put <kind>') or
    get(kind='skill', id='precis-put-help').
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


def edit(
    kind: str,
    id: str | int,
    mode: str = "find-replace",
    text: str | None = None,
    find: str | None = None,
    before: str | None = None,
    after: str | None = None,
    where: str | None = None,
    match: str | None = None,
    nth: int | None = None,
    allow_rename: bool | None = None,
    dry_run: bool | None = None,
) -> str:
    """Edit a region within an existing ref's content (anchored).

    Distinct from `put` (which creates new refs). Each mode has a
    fixed required-argument set encoded in the JSON Schema:

    - `find-replace` (default): **Required** `find=` AND `text=`.
      Pass `text=''` to delete the matched span (canonical idiom).
    - `insert`: **Required** `find=`, `text=`, `where='before'|'after'`.
    - `append` / `replace`: **Required** `text=`. `replace` with
      `id='slug~selector'` rewrites one block.

    Optional anchors: `before=` / `after=` / `match=` (`unique`
    default | `first` | `all` | `nth`) / `nth=`. `dry_run=True`
    previews without writing.

    Full reference: search(kind='skill', q='edit') or
    get(kind='skill', id='precis-edit-help').
    """
    payload: dict[str, Any] = {
        "kind": kind,
        "id": id,
        "mode": mode,
        "text": text,
        "find": find,
        "before": before,
        "after": after,
        "where": where,
        "match": match,
        "nth": nth,
        "allow_rename": allow_rename,
        "dry_run": dry_run,
    }
    # See ``get`` for the ``str | CallToolResult`` return contract.
    return _dispatch("edit", payload)


def delete(
    kind: str,
    id: str | int,
) -> str:
    """Delete a ref or addressed region.

    Numeric-ref kinds (memory, todo, gripe, fc, quest, conv):
    soft-delete the ref (recoverable at SQL layer).

    File kinds with selector in `id=` (markdown, plaintext, tex,
    python): delete the addressed block/symbol/line range.
    Without a selector → BadInput. Use `edit(mode='replace', text='')`
    to clear a whole file, or `edit(mode='find-replace', find='…',
    text='')` to delete a matched span.

    Cache-backed / read-only kinds (calc, math, web, youtube,
    research, think, websearch, paper, patent): Unsupported.

    Full reference: search(kind='skill', q='delete') or
    get(kind='skill', id='precis-delete-help').
    """
    return _dispatch("delete", {"kind": kind, "id": id})


def tag(
    kind: str,
    id: str | int,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> str:
    """Add and/or remove tags on an existing ref (atomic).

    Closed UPPERCASE prefixes (`STATUS:`, `PRIO:`, `SRC:`, `CACHE:`)
    replace within the prefix on add; flag tags toggle; open tags
    are free-form. Bad axis on a kind raises
    `[error:BadInput] axis not allowed on kind 'K'`.

    Per-kind closed-prefix gating (summary):
    todo/gripe/quest: STATUS+PRIO. memory/fc/conv: none.
    paper/patent: SRC+CACHE. web/research/think/websearch/youtube:
    CACHE only. oracle/skill: none. python/calc/math: tag unsupported.

    Full reference: search(kind='skill', q='tag') or
    get(kind='skill', id='precis-tag-help'); `precis-tags` is the
    authoritative axis matrix.
    """
    return _dispatch("tag", {"kind": kind, "id": id, "add": add, "remove": remove})


def link(
    kind: str,
    id: str | int,
    target: str | None = None,
    mode: str = "add",
    rel: str | None = None,
) -> str:
    """Add or remove a typed link between two refs.

    `target=` uses canonical `kind:identifier[~selector]` form
    (kind prefix required). `mode='add'` (default) creates;
    `mode='remove'` with `rel=` removes one (target, rel) pair,
    without `rel=` removes every link to the target. `rel=` defaults
    to `related-to` on add.

    Full reference: search(kind='skill', q='link') or
    get(kind='skill', id='precis-link-help'); `precis-relations`
    has the full vocabulary (cites, blocks, contradicts, ...).
    """
    return _dispatch(
        "link",
        {"kind": kind, "id": id, "target": target, "mode": mode, "rel": rel},
    )


# ---------------------------------------------------------------------------
# CLI per-arg help strings.
#
# The MCP-facing tool descriptions (verb docstrings above) carry only a
# tight summary + wire-level constraints + a pointer to the per-verb help
# skill (see docs/design/mcp-cold-start-token-budget.md). The CLI surface
# still benefits from explicit per-arg ``--help`` strings, so we keep
# them here adjacent to the functions rather than scraping the trimmed
# docstrings. The CLI argparse adapter in :mod:`precis.tools.cli_adapter`
# reads these via :data:`TOOL_REGISTRY[<verb>]['cli_help']`.
# ---------------------------------------------------------------------------

_GET_HELP: dict[str, str] = {
    "kind": "Which kind to read from (e.g. 'paper', 'memory', 'calc').",
    "id": "Identifier — slug for slug kinds, int for numeric kinds.",
    "view": "Display variant (kind-specific; e.g. 'abstract', 'bibtex').",
    "q": "Free-text query (compute-style kinds in lieu of id).",
    "args": "Typed extras as a dict (callgraph, runtrace, ...). "
    "Reserved keys (kind/id/view/q) rejected.",
}

_SEARCH_HELP: dict[str, str] = {
    "q": "Free-text query (lexical + semantic, hybrid-fused).",
    "kind": "Restrict to a kind (or comma-list, '*', omit for fan-out).",
    "scope": "Restrict to one ref's blocks (slug or numeric id).",
    "top_k": "Max results. Positive int ≤ 100.",
    "tags": "Per-kind tag filters (closed-vocab axes + open tags).",
    "source": "Patent only: 'both' (default) | 'local' | 'remote'.",
    "exclude": "Ref slugs to drop from results (use to paginate).",
}

_PUT_HELP: dict[str, str] = {
    "kind": "Which kind to write to.",
    "mode": "Operation hint. File kinds: 'create' (only). "
    "Numeric-ref kinds: omit to create.",
    "id": "Target ref. Omit to create on numeric-ref kinds.",
    "text": "Content for create.",
    "tags": "Tags to add.",
    "untags": "Tags to remove.",
    "link": "Add a link 'kind:identifier[~selector]'.",
    "unlink": "Remove a link. Same canonical form as link=.",
    "rel": "Relation slug for link/unlink. Defaults to 'related-to'.",
}

_EDIT_HELP: dict[str, str] = {
    "kind": "Which kind to edit.",
    "id": "Existing ref id, optionally with selector for region edits.",
    "mode": "'find-replace' (default) | 'append' | 'insert' | 'replace'.",
    "text": "Replacement / inserted content. Required for every mode "
    "except 'reorder'. Pass text='' on find-replace to delete "
    "the matched span.",
    "find": "Literal anchor string. Required for find-replace and insert.",
    "before": "Literal bytes immediately preceding find=.",
    "after": "Literal bytes immediately following find=.",
    "where": "'before' or 'after'. Required for insert.",
    "match": "'unique' (default) | 'first' | 'all' | 'nth'.",
    "nth": "1-based index when match='nth'.",
    "allow_rename": "Opt in to qualname-drop gate override (Python).",
    "dry_run": "Preview without writing (True | 'diff' | 'full').",
}

_DELETE_HELP: dict[str, str] = {
    "kind": "Which kind to delete from.",
    "id": "Ref id (or 'slug~SELECTOR' for region deletes on file kinds).",
}

_TAG_HELP: dict[str, str] = {
    "kind": "Kind owning the ref.",
    "id": "Ref id.",
    "add": "Tags to add.",
    "remove": "Tags to remove.",
}

_LINK_HELP: dict[str, str] = {
    "kind": "Kind owning the source ref.",
    "id": "Source ref id.",
    "target": "Canonical link target 'kind:id[~selector]'.",
    "mode": "'add' (default) | 'remove'.",
    "rel": "Relation slug. Defaults to 'related-to' on add.",
}

CLI_HELP: dict[str, dict[str, str]] = {
    "get": _GET_HELP,
    "search": _SEARCH_HELP,
    "put": _PUT_HELP,
    "edit": _EDIT_HELP,
    "delete": _DELETE_HELP,
    "tag": _TAG_HELP,
    "link": _LINK_HELP,
}
