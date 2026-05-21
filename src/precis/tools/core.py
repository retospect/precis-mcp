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
    # Create dummy classes for type checking when MCP is not available
    class CallToolResult:
        def __init__(self, content, isError=False):
            self.content = content
            self.isError = isError
    
    class TextContent:
        def __init__(self, type, text):
            self.type = type
            self.text = text

# Runtime access - these will be imported when needed
_runtime = None


def _get_runtime():
    """Get the current runtime instance."""
    global _runtime
    if _runtime is None:
        from precis.runtime import build_runtime
        _runtime = build_runtime()
    return _runtime


def _dispatch(verb: str, payload: dict[str, Any]) -> str | CallToolResult:
    """Dispatch one verb call and shape the result.
    
    On success, returns the rendered string.
    On error, returns a CallToolResult with isError=True for MCP compatibility.
    CLI callers should check isinstance(result, CallToolResult) to detect errors.
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


def _check_reserved_args(
    args: dict[str, Any], *, reserved: tuple[str, ...]
) -> str | None:
    """Return a rendered error string if `args` shadows positional kwargs."""
    overlap = sorted(k for k in args if k in reserved)
    if not overlap:
        return None

    from precis.errors import BadInput

    return _get_runtime().render_error(
        BadInput(
            f"args={overlap!r} shadows the explicit kwargs {list(reserved)!r}",
            next="pass these as top-level keyword arguments, not inside args=",
        )
    )


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
            return err
        payload["__extras__"] = dict(args)
    
    result = _dispatch("get", payload)
    if isinstance(result, CallToolResult):
        return result.content[0].text  # CLI gets the error message
    return result


def search(
    q: str,
    kind: str | None = None,
    scope: str | None = None,
    top_k: int = 10,
    tags: list[str] | None = None,
    source: str | None = None,
    exclude: list[str] | None = None,
) -> str:
    """Search across kinds.

    Args:
        q:     Free-text query (lexical + semantic, hybrid-fused).
        kind:  Restrict to a single kind. **Omit (or pass ``'*'`` /
               ``'all'`` / ``'any'`` / ``''``) for cross-kind fan-out
               across every search-hits-capable kind**, RRF-merged
               with each hit tagged by its source kind. Comma-lists
               like ``'paper,memory,web'`` narrow the fan-out to a
               specific subset.
        scope: Restrict to one ref's blocks (slug or numeric id).
        top_k: Max results. Must be a positive integer ≤ 100. Larger
               values are rejected to bound response size and protect
               smaller models' context windows.
        tags:  Kind-specific closed / open tag filters (e.g.
               ``['cpc:B01J27/24']`` on ``kind='patent'``,
               ``['topic-xyz']`` on any ref kind). Tag axes allowed
               per kind follow the ``precis-tags`` matrix.
        source: Kind-specific source selector for handlers that
               merge multiple streams. Currently only ``kind='patent'``
               honours this — ``'both'`` (default) merges the local
               store and live OPS, ``'local'`` skips OPS, ``'remote'``
               skips local and dedupes OPS hits against already-
               fetched patents (prior-art sweep mode). Ignored by
               handlers that don't merge streams.
        exclude: List of ref slugs to omit from results. Coarse / ref-
               level — ``exclude=['wang2020state']`` drops every block
               of that paper. Use to paginate ("show me hits 6-10"):
               pass back the slugs of hits 1-5 from the prior call.
               The ``LIMIT`` applies after exclusion so ``top_k`` stays
               meaningful — ``top_k=10, exclude=[5 slugs]`` returns the
               next 10 hits, not 5. Stale / unknown slugs are silently
               dropped. Currently honoured by ``kind='paper'``; other
               block-level kinds ignore it.
    """
    # Validate top_k at the boundary
    from precis.errors import BadInput

    if not isinstance(top_k, int) or top_k <= 0:
        runtime = _get_runtime()
        return runtime.render_error(
            BadInput(
                f"top_k must be a positive integer, got {top_k!r}",
                next="search(kind='paper', q='...', top_k=10)",
            )
        )
    if top_k > _SEARCH_TOP_K_MAX:
        runtime = _get_runtime()
        return runtime.render_error(
            BadInput(
                f"top_k={top_k} exceeds maximum {_SEARCH_TOP_K_MAX}",
                next=(
                    f"narrow with scope= or paginate; "
                    f"max top_k is {_SEARCH_TOP_K_MAX}"
                ),
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
    
    result = _dispatch("search", payload)
    if isinstance(result, CallToolResult):
        return result.content[0].text
    return result


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
        mode:   Operation hint. Kind-specific:
                - File kinds (``markdown``, ``plaintext``, ``tex``,
                  ``python``): ``put`` is **creation-only** since the
                  seven-verb cutover — ``mode='create'`` is required
                  and is the only accepted value. Region edits
                  (``append`` / ``insert`` / ``replace`` /
                  ``find-replace``) live on the ``edit`` verb;
                  whole-file deletes live on ``delete``.
                - Numeric-ref kinds (``memory``, ``todo``, ``gripe``,
                  ``conv``, ``fc``, ``quest``): omit ``mode=`` to
                  create a new ref; ``mode='delete'`` soft-deletes.
                - ``perplexity``: ``mode='import'`` ingests a
                  pre-generated report as a $0 cache entry.
                Unknown modes are rejected. See each kind's help skill
                for the authoritative list.
        id:     Target ref or block. Omit to create a new ref on
                numeric-ref kinds; required (file path/slug) for file
                kinds with ``mode='create'``.
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
    result = _dispatch(
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
    if isinstance(result, CallToolResult):
        return result.content[0].text
    return result


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
    """Edit a region within an existing ref's content.

    Distinct from ``put`` — ``put`` creates new refs (or rewrites a
    whole-file ref); ``edit`` rewrites a region within an existing one.
    Each mode has a **fixed** required-argument set; the JSON Schema
    encodes the coupling so a call with the wrong shape is rejected
    before dispatch.

    - ``find-replace`` (default): anchor-based string replace. Requires
      ``find=`` AND ``text=``. Optional ``before=`` / ``after=`` /
      ``match=`` / ``nth=`` disambiguate. Pass ``text=''`` to **delete
      the matched span** — this is the canonical span-delete idiom.
    - ``insert``: insert ``text=`` adjacent to a ``find=`` anchor.
      Requires ``find=``, ``text=``, ``where='before'|'after'``.
    - ``append`` / ``replace``: whole-region region edits. Requires
      ``text=``. ``replace`` with ``id='slug~selector'`` rewrites one
      block; ``append`` adds to the end of the file.
    - ``reorder``: structured-file rearrangement (deferred — not yet
      wired). See migration doc D5.

    See each kind's help skill (``get(kind='skill', id='precis-edit-
    protocol')``) for the per-kind menu.

    Args:
        kind: Which kind to edit.
        id:   Existing ref id, optionally with selector for region edits.
        mode: ``find-replace`` (default), ``append``, ``insert``,
              ``replace``, ``reorder``.
        text: Replacement / inserted content. **Required** for every
              mode except ``reorder``. Pass ``text=''`` on
              ``mode='find-replace'`` to delete the matched span.
        find: Literal anchor string. **Required** for ``find-replace``
              and ``insert``.
        before / after: Optional literal bytes immediately preceding /
              following ``find=``. Disambiguate when the same ``find``
              appears more than once.
        where: ``'before'`` or ``'after'``. **Required** for ``insert``.
        match: ``'unique'`` (default), ``'first'``, ``'all'``, ``'nth'``.
        nth:   1-based index when ``match='nth'``.
        allow_rename: For find-replace edits that change a Python symbol
              name; opt in to the qualname-drop gate override.
        dry_run: Preview the edit without writing (``True`` / ``'diff'``
              / ``'full'``).
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
    result = _dispatch("edit", payload)
    if isinstance(result, CallToolResult):
        return result.content[0].text
    return result


def delete(
    kind: str,
    id: str | int,
) -> str:
    """Delete a ref or region.

    Behaviour is kind-specific:

    - Numeric-ref kinds (memory, todo, gripe, fc, quest, oracle, conv):
      soft-delete the ref. The row is retained for audit / undelete;
      it just stops appearing in list views and search.
    - File kinds with a selector in ``id=`` (markdown, plaintext,
      python): delete the addressed block / symbol / line range.
      Without a selector → ``BadInput`` — use
      ``edit(mode='replace', text='')`` to clear a whole file, or
      ``edit(mode='find-replace', find='…', text='')`` to delete a
      matched span without touching the surrounding block.
    - Cache-backed and read-only kinds (calc, math, web, youtube,
      research, think, websearch, paper): ``Unsupported``.

    No undo — soft-delete is recoverable at the SQL layer; selector
    deletes write the file out without the deleted region.

    Args:
        kind: Which kind to delete from.
        id:   Ref id (or ``slug~SELECTOR`` for region deletes on file
              kinds).
    """
    result = _dispatch("delete", {"kind": kind, "id": id})
    if isinstance(result, CallToolResult):
        return result.content[0].text
    return result


def tag(
    kind: str,
    id: str | int,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> str:
    """Add and/or remove tags on an existing ref.

    Both ``add`` and ``remove`` are accepted in the same call so a
    transactional STATUS bump (``add=['STATUS:done'], remove=['STATUS:open']``)
    happens atomically.

    Tag vocabulary mirrors ``put(tags=...)``:

    - **Closed UPPERCASE prefixes** (``STATUS:``, ``PRIO:``, ``SRC:``,
      ``CACHE:``) replace within the prefix when added — adding
      ``STATUS:done`` implicitly removes any existing ``STATUS:*``.
      **Gated per-kind** (see matrix below): a closed prefix rejected
      on one kind with ``[error:BadInput] axis not allowed on kind 'K'``
      is the expected response, not a bug. Re-read ``get(kind='skill',
      id='precis-tags')`` for the axis matrix.
    - **Flag tags** (bare lowercase like ``pinned``, ``draft``)
      toggle on / off.
    - **Open tags** (``topic-co2-capture``, ``namespace:value``) add
      and remove freely on every kind.

    **Per-kind closed-prefix gating (summary)**:

    - ``todo`` / ``gripe`` / ``quest``: ``STATUS`` + ``PRIO`` (workflow
      kinds — both axes allowed)
    - ``memory`` / ``fc`` / ``conv``: no closed axes (use open tags like
      ``confidence-strong``, ``topic-noxrr`` — memories intentionally
      have no ``PRIO:`` axis)
    - ``paper`` / ``patent``: ``SRC`` + ``CACHE`` (provenance +
      cache-freshness)
    - ``web`` / ``research`` / ``think`` / ``websearch`` / ``youtube``:
      ``CACHE`` only (agent-applied workflow axes rejected)
    - ``oracle`` / ``skill``: no closed axes (read-only references)
    - ``python`` / ``calc`` / ``math``: tag verb unsupported (read-only
      or stateless kinds)

    See ``get(kind='skill', id='precis-tags')`` for the authoritative
    axis matrix; this docstring is a summary that may lag.

    Args:
        kind:   Kind owning the ref.
        id:     Ref id (slug for slug kinds, int for numeric kinds).
        add:    Tags to add.
        remove: Tags to remove.
    """
    result = _dispatch("tag", {"kind": kind, "id": id, "add": add, "remove": remove})
    if isinstance(result, CallToolResult):
        return result.content[0].text
    return result


def link(
    kind: str,
    id: str | int,
    target: str | None = None,
    mode: str = "add",
    rel: str | None = None,
) -> str:
    """Add or remove a link between two refs.

    Args:
        kind:   Kind owning the source ref.
        id:     Source ref id.
        target: Canonical link target ``kind:id[~selector]`` —
                e.g. ``paper:wang2020``, ``paper:wang2020~38`` (block
                38), ``todo:158``. The ``kind:`` prefix is required.
        mode:   ``add`` (default) creates the edge. ``remove`` deletes
                it. With ``rel=`` on remove, removes the specific
                (target, relation) pair; without, removes every link
                to the target.
        rel:    Relation slug. Defaults to ``related-to`` on add. See
                ``precis-relations`` for the full vocabulary
                (``cites``, ``blocks``, ``contradicts``, ``derived-from``,
                ``supports``, …).
    """
    result = _dispatch(
        "link",
        {"kind": kind, "id": id, "target": target, "mode": mode, "rel": rel},
    )
    if isinstance(result, CallToolResult):
        return result.content[0].text
    return result
