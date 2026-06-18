"""Core tool implementations shared between MCP server and CLI.

These are the actual tool functions that implement the seven-verb API.
Both the MCP server and CLI interface consume these functions through
the shared registry in tools/__init__.py.
"""

from __future__ import annotations

import logging
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


def _monotonic() -> float:
    """Late-binding monotonic; lazy import so test stubs don't break boot."""
    import time as _t

    return _t.monotonic()


_TOOL_CALL_LOGGER = logging.getLogger("precis.tools.mcp_calls")


def _log_tool_call(
    *,
    verb: str,
    payload: dict[str, Any],
    duration_ms: float,
    error: bool,
) -> None:
    """Emit one structured log line per MCP tool call.

    Correlation keys come from the per-tick env vars the planner
    runner sets (``PRECIS_CURRENT_TODO``, ``PRECIS_WORKSPACE``,
    ``PRECIS_CURRENT_MODEL``). Without that env we still log, with
    None markers — useful for distinguishing operator-driven calls
    from cascade calls during diagnosis.

    Payload fields are sampled (kind, id, name, mode, length of
    text= for puts) rather than dumped wholesale — we want a
    grep-friendly audit, not the full LLM payload that lives in
    ``job_summary``.
    """
    import os

    parent_todo = os.environ.get("PRECIS_CURRENT_TODO", "-")
    workspace = os.environ.get("PRECIS_WORKSPACE", "-")
    model = os.environ.get("PRECIS_CURRENT_MODEL", "-")

    sample: dict[str, Any] = {}
    for key in ("kind", "id", "name", "mode", "rel", "view"):
        if key in payload:
            v = payload[key]
            if isinstance(v, str) and len(v) > 80:
                v = v[:80] + "…"
            sample[key] = v
    if "text" in payload:
        text = payload.get("text")
        if isinstance(text, str):
            sample["text_chars"] = len(text)
    if "tags" in payload:
        tags = payload.get("tags")
        if isinstance(tags, list):
            sample["tags"] = tags[:8]
    if "args" in payload and isinstance(payload["args"], dict):
        sample["args_keys"] = sorted(payload["args"].keys())[:8]

    _TOOL_CALL_LOGGER.info(
        "mcp_call verb=%s parent_todo=%s workspace=%s model=%s "
        "duration_ms=%.1f error=%s payload=%s",
        verb,
        parent_todo,
        workspace,
        model,
        duration_ms,
        error,
        sample,
    )


def _get_runtime():
    """Get the current runtime instance.

    Registers an ``atexit`` hook on first build to close the runtime's
    store before interpreter shutdown. Without this, the psycopg
    ConnectionPool's ``__del__`` runs during finalization and tries to
    join its background scheduler thread — Python 3.13+ raises
    ``PythonFinalizationError: cannot join thread at interpreter
    shutdown`` because the GIL is locked into teardown by then. The
    exception is "ignored" but spams every CLI invocation. Closing
    eagerly via atexit shuts the pool's threads cleanly first.
    """
    global _runtime
    if _runtime is None:
        import atexit

        from precis.runtime import build_runtime

        _runtime = build_runtime()

        def _close_runtime() -> None:
            try:
                store = getattr(_runtime, "store", None)
                if store is not None:
                    store.close()
            except Exception:
                # atexit is best-effort; swallow so nothing else
                # blocks on a close that failed.
                pass

        atexit.register(_close_runtime)
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
    started = _monotonic()
    is_error = False
    try:
        body, is_error = runtime.dispatch_with_status(verb, payload)
        if is_error:
            return CallToolResult(
                content=[TextContent(type="text", text=body)],
                isError=True,
            )
        return body
    except Exception as e:
        is_error = True
        # Handle unexpected exceptions
        error_body = runtime.render_error(e)
        return CallToolResult(
            content=[TextContent(type="text", text=error_body)],
            isError=True,
        )
    finally:
        # Structured per-tool-call audit log. The single line per MCP
        # call is the diagnostic surface that's been missing — without
        # it, "what did the LLM do?" requires reading the raw stdout
        # captured in job_summary. With it, ``precis logs --process
        # precis-serve --since 5m`` shows every put/get/search/tag/etc
        # the cascade made, correlated by parent_todo so you can pull
        # the trace for one leaf's tick.
        try:
            _log_tool_call(
                verb=verb,
                payload=payload or {},
                duration_ms=(_monotonic() - started) * 1000.0,
                error=is_error,
            )
        except Exception:
            # Logging is best-effort — never fail a tool call because
            # the audit emitter threw.
            pass


def _validation_error(body: str) -> _ToolReturn:
    """Wrap a pre-dispatch validation error string in a ``CallToolResult``.

    The MCP protocol distinguishes successful tool results from errors
    via the ``isError`` flag on ``CallToolResult``. Pre-dispatch
    validation paths (e.g. ``_check_reserved_args``, the ``search``
    ``page_size`` cap) build the rendered text via
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


# Hard cap on page_size for search tool
_SEARCH_PAGE_SIZE_MAX: int = 100

# Hard cap on ``text=`` payloads to put / edit. The embedder
# (BGE-M3 / 1024-d) tokenises and forwards the payload in worker
# passes; an uncapped multi-MB write OOMs the model. 2 MiB is well
# above any sane note/markdown/abstract while still bounding a
# malicious or accidental write. Boundary check lives at the tool
# surface so the runtime + handlers see only validated input.
_TEXT_PAYLOAD_MAX_BYTES: int = 2 * 1024 * 1024


def _check_text_payload_size(verb: str, text: str | None) -> str | None:
    """Reject oversize ``text=`` at the verb boundary.

    Returns an MCP-shaped validation-error envelope when ``text``
    exceeds :data:`_TEXT_PAYLOAD_MAX_BYTES`; otherwise ``None``.
    """
    if text is None:
        return None
    size = len(text.encode("utf-8", errors="ignore"))
    if size <= _TEXT_PAYLOAD_MAX_BYTES:
        return None
    from precis.errors import BadInput

    runtime = _get_runtime()
    cap_mib = _TEXT_PAYLOAD_MAX_BYTES // (1024 * 1024)
    return _validation_error(
        runtime.render_error(
            BadInput(
                f"{verb}: text payload is {size} bytes, exceeds the "
                f"{_TEXT_PAYLOAD_MAX_BYTES}-byte ({cap_mib} MiB) cap",
                next=(
                    "split the write into smaller blocks or compress the "
                    "input; the cap protects the embedder from OOM on the "
                    "downstream worker pass"
                ),
            )
        )
    )


def get(
    # See ``search`` for the Optional-required pattern (round-2 picky N-1).
    kind: str | None = None,
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

    Full reference: get(kind='skill', id='precis-get-help'), or
    search(kind='skill', q='reading a paper') for a topical lookup.
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
    # ``q`` is functionally required but declared ``Optional`` so a
    # missing-arg call returns the runtime's canonical
    # ``[error:BadInput]`` envelope rather than FastMCP's raw pydantic
    # ValidationError (`Field required ... https://errors.pydantic.dev/2.13/v/missing`).
    # Round-2 picky N-1, 2026-05-30. Handlers already gate the empty-q
    # case (degrade to list view when ``tags=`` is supplied, raise
    # ``BadInput`` otherwise) so the validation moves cleanly.
    q: str | None = None,
    kind: str | None = None,
    scope: str | None = None,
    page_size: int = 10,
    page: int = 1,
    tags: list[str] | None = None,
    source: str | None = None,
    exclude: list[str] | None = None,
    angle: float | None = None,
    n: int | None = None,
    like: str | None = None,
    view: str | None = None,
    # finding-specific filter: short-circuits a STATUS:<value> tag
    # filter. Defaults to 'established' on the finding handler; pass
    # 'tracing' / 'multi_candidate' / 'dead_chain' to inspect a
    # specific lifecycle cohort, or '*' to see all. Declared at the
    # verb level so the schema advertises it to strict-schema clients.
    status: str | None = None,
) -> str:
    """Hybrid lexical + semantic search across kinds.

    `page_size` must be a positive int ≤ 100. Omit `kind` (or pass
    `'*'`) for cross-kind fan-out. `page=N` (default 1) returns the
    Nth page of `page_size` results — server-side OFFSET, no need to
    thread `exclude=` lists manually. `exclude=` is still useful for
    hand-skipping specific slugs. `source=` is patent-only
    (`'both'`/`'local'`/`'remote'`); ignored elsewhere.

    **Angle spray**: `angle=` (cosine in `[-1,1]`) +/- `like='kind:id'`
    returns `n` diverse items at that cosine from the seed (a cone
    sample, not a ranked list). **`view='dreamable'`**: the most-due
    salient seed + its neighbourhood. **`view='stubs'`**: the
    paper-acquisition backlog (an id but no PDF). See the skill help.

    Full reference: get(kind='skill', id='precis-search-help'). For
    per-kind nuances (patent prior-art, finding chase, etc.) search
    the skill index with a natural-language goal.
    """
    # Validate page_size at the boundary. Errors round-trip via
    # ``_validation_error`` so the MCP ``isError`` flag survives.
    from precis.errors import BadInput

    if not isinstance(page_size, int) or page_size <= 0:
        runtime = _get_runtime()
        return _validation_error(
            runtime.render_error(
                BadInput(
                    f"page_size must be a positive integer, got {page_size!r}",
                    next="search(kind='paper', q='...', page_size=10)",
                )
            )
        )
    if page_size > _SEARCH_PAGE_SIZE_MAX:
        runtime = _get_runtime()
        return _validation_error(
            runtime.render_error(
                BadInput(
                    f"page_size={page_size} exceeds maximum {_SEARCH_PAGE_SIZE_MAX}",
                    next=(
                        f"narrow with scope= or paginate; "
                        f"max page_size is {_SEARCH_PAGE_SIZE_MAX}"
                    ),
                )
            )
        )

    # Validate + default page. A positive int >=1; clamp silently
    # rather than erroring so a stray ``page=0`` doesn't reject the
    # whole search call.
    if not isinstance(page, int) or page < 1:
        page = 1

    payload: dict[str, Any] = {
        "kind": kind,
        "q": q,
        "scope": scope,
        "page_size": page_size,
        "page": page,
    }

    # Only forward optional kwargs when set
    if tags is not None:
        payload["tags"] = tags
    if source is not None:
        payload["source"] = source
    if exclude is not None:
        payload["exclude"] = exclude
    # Angle-spray knobs — forwarded only when set so a plain search
    # never trips the angle interception path in the runtime.
    if angle is not None:
        payload["angle"] = angle
    if n is not None:
        payload["n"] = n
    if like is not None:
        payload["like"] = like
    # ``view='dreamable'`` routes to the salience focus-region search
    # (seed + ANN ring); other views pass through to the handler.
    if view is not None:
        payload["view"] = view
    if status is not None:
        payload["status"] = status

    # See ``get`` for the ``str | CallToolResult`` return contract.
    return _dispatch("search", payload)


def put(
    # See ``search`` for the Optional-required pattern (round-2 picky N-1).
    kind: str | None = None,
    mode: str | None = None,
    id: str | int | None = None,
    text: str | None = None,
    tags: list[str] | None = None,
    untags: list[str] | None = None,
    link: str | None = None,
    unlink: str | None = None,
    rel: str | None = None,
    # Kind-specific kwargs. Declared here (rather than tunnelled through
    # ``args=``) so the JSON Schema advertised over MCP matches what the
    # help skills document — strict-schema clients (Claude Desktop, etc.)
    # otherwise reject these calls before the server-side per-kind error
    # paths can teach the agent what's missing.
    # finding (see precis-finding-help):
    title: str | None = None,
    body: str | None = None,
    scope: dict[str, Any] | None = None,
    cited_in: str | None = None,
    # citation (see precis-citation-help):
    source_handle: str | None = None,
    source_quote: str | None = None,
    char_offset: int | None = None,
    verifier_confidence: float | None = None,
    verifier_caveats: str | None = None,
    verified_at: str | None = None,
    # job (see precis-job-help):
    job_type: str | None = None,
    executor: str | None = None,
    params: dict[str, Any] | None = None,
    idem_key: str | None = None,
    # presentation (see precis-pres-help):
    pos: int | None = None,
    meta: dict[str, Any] | None = None,
    ref_meta: dict[str, Any] | None = None,
    subtype: str | None = None,
    chunk_kind: str | None = None,
    # conversation (see precis-conv-help):
    author: str | None = None,
    msg_id: str | None = None,
    # cron (see precis-cron-help):
    target: str | None = None,
    when: str | None = None,
    in_: str | None = None,
    recurring: str | None = None,
    catch_up: bool | None = None,
    # paper stub-mint (see precis-stubs-help / precis-paper-help): a
    # paper put requests a paper into the "papers we need" backlog —
    # put(kind='paper', doi='10…' | arxiv='2401.00001' | title='…').
    # Paper bodies stay import-only; put only ever mints a stub.
    doi: str | None = None,
    arxiv: str | None = None,
    identifier: str | None = None,
    year: int | None = None,
    reason: str | None = None,
) -> str:
    """Write or annotate. Creates new refs; for region rewrites use `edit`.

    `mode=` matrix:
      - File kinds (`markdown`, `plaintext`, `tex`, `python`):
        `mode='create'` (region edits live on `edit`,
        whole-file deletes on `delete`).
      - Paid-import kinds (`websearch`, `perplexity-reasoning`,
        `perplexity-research`): `mode='import'` to ingest an existing
        payload (e.g. a Perplexity report) without re-running the
        upstream call.
      - Numeric-ref kinds (`memory`, `todo`, `gripe`, `conv`, `flashcard`):
        omit `mode=` to create.
    `tags=` adds, `untags=` removes. `link=`/`unlink=` use canonical
    `kind:identifier[~selector]` form; `rel=` defaults to `related-to`.

    Full reference: get(kind='skill', id='precis-put-help'), or
    search(kind='skill', q='saving a note') for a topical lookup.
    """
    err = _check_text_payload_size("put", text)
    if err is not None:
        return err
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
            # Kind-specific kwargs — the dispatcher strips None values
            # before calling the handler, and handlers carry **_kw
            # catch-alls so unrecognised keys are no-ops for kinds that
            # don't use them.
            "title": title,
            "body": body,
            "scope": scope,
            "cited_in": cited_in,
            "source_handle": source_handle,
            "source_quote": source_quote,
            "char_offset": char_offset,
            "verifier_confidence": verifier_confidence,
            "verifier_caveats": verifier_caveats,
            "verified_at": verified_at,
            "job_type": job_type,
            "executor": executor,
            "params": params,
            "idem_key": idem_key,
            "pos": pos,
            "meta": meta,
            "ref_meta": ref_meta,
            "subtype": subtype,
            "chunk_kind": chunk_kind,
            "author": author,
            "msg_id": msg_id,
            "target": target,
            "when": when,
            "in_": in_,
            "recurring": recurring,
            "catch_up": catch_up,
            "doi": doi,
            "arxiv": arxiv,
            "identifier": identifier,
            "year": year,
            "reason": reason,
        },
    )


def edit(
    # ``kind`` and ``id`` are functionally required; see ``search`` for
    # the Optional-required pattern (round-2 picky N-1).
    kind: str | None = None,
    id: str | int | None = None,
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
    # finding-specific: pick the cite_key / ref_id that resolves an
    # established / multi_candidate finding. Declared at the verb level
    # so strict-schema MCP clients don't strip it (see precis-finding-help).
    pick_candidate: str | int | None = None,
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

    Full reference: get(kind='skill', id='precis-edit-help'), or
    search(kind='skill', q='changing existing content') for a topical
    lookup.
    """
    err = _check_text_payload_size("edit", text)
    if err is not None:
        return err
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
        "pick_candidate": pick_candidate,
    }
    # See ``get`` for the ``str | CallToolResult`` return contract.
    return _dispatch("edit", payload)


def delete(
    # See ``search`` for the Optional-required pattern (round-2 picky N-1).
    kind: str | None = None,
    id: str | int | None = None,
) -> str:
    """Delete a ref or addressed region.

    Numeric-ref kinds (memory, todo, gripe, flashcard, conv):
    soft-delete the ref (recoverable at SQL layer).

    File kinds with selector in `id=` (markdown, plaintext, tex,
    python): delete the addressed block/symbol/line range.
    Without a selector → BadInput. Use `edit(mode='replace', text='')`
    to clear a whole file, or `edit(mode='find-replace', find='…',
    text='')` to delete a matched span.

    Cache-backed / read-only kinds (calc, math, web, youtube,
    research, think, websearch, paper, patent): Unsupported.

    Full reference: get(kind='skill', id='precis-delete-help'), or
    search(kind='skill', q='removing a ref') for a topical lookup.
    """
    return _dispatch("delete", {"kind": kind, "id": id})


def tag(
    # See ``search`` for the Optional-required pattern (round-2 picky N-1).
    kind: str | None = None,
    id: str | int | None = None,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> str:
    """Add and/or remove tags on an existing ref (atomic).

    Closed UPPERCASE prefixes (`STATUS:`, `PRIO:`, `SRC:`, `CACHE:`)
    replace within the prefix on add; flag tags toggle; open tags
    are free-form. Bad axis on a kind raises
    `[error:BadInput] axis not allowed on kind 'K'`.

    Per-kind closed-prefix gating (summary):
    todo/gripe: STATUS+PRIO. finding/job: STATUS (lifecycle subsets).
    memory: DREAM (dreaming-worker provenance). flashcard/conv: none.
    paper/patent: SRC+CACHE.
    web/perplexity-research/perplexity-reasoning/websearch/youtube:
    CACHE+WATCH. oracle/skill: none. python/calc/math: tag unsupported.

    Full reference: get(kind='skill', id='precis-tag-help'), or
    search(kind='skill', q='classifying refs') for a topical lookup.
    `precis-tags` is the authoritative axis matrix.
    """
    return _dispatch("tag", {"kind": kind, "id": id, "add": add, "remove": remove})


def link(
    # See ``search`` for the Optional-required pattern (round-2 picky N-1).
    kind: str | None = None,
    id: str | int | None = None,
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

    Full reference: get(kind='skill', id='precis-link-help'), or
    search(kind='skill', q='connecting refs') for a topical lookup.
    `precis-relations` has the full vocabulary (cites, blocks,
    contradicts, ...).
    """
    return _dispatch(
        "link",
        {"kind": kind, "id": id, "target": target, "mode": mode, "rel": rel},
    )


def more(cursor: str) -> _ToolReturn:
    """Fetch the next page of a chunked response.

    Pagination kicks in when a verb's rendered body exceeds the MCP
    stdio frame budget. The over-large response is split on Markdown
    section boundaries; the head ends with ``Next: more(cursor='...')``.
    Call this tool with the cursor verbatim to retrieve the tail.

    Cursors are single-use and expire after a few minutes — if you
    miss the window, re-issue the original call to start fresh.
    """
    runtime = _get_runtime()
    started = _monotonic()
    is_error = False
    try:
        body, is_error = runtime.fetch_more(cursor)
        if is_error:
            return CallToolResult(
                content=[TextContent(type="text", text=body)],
                isError=True,
            )
        return body
    except Exception as e:
        is_error = True
        error_body = runtime.render_error(e)
        return CallToolResult(
            content=[TextContent(type="text", text=error_body)],
            isError=True,
        )
    finally:
        try:
            _log_tool_call(
                verb="more",
                payload={"cursor": cursor},
                duration_ms=(_monotonic() - started) * 1000.0,
                error=is_error,
            )
        except Exception:
            pass


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
    "page_size": "Max results per page. Positive int ≤ 100 (default 10).",
    "page": "Page number (default 1). Pass page=2 to see results "
    "page_size..2*page_size-1, etc.",
    "tags": "Per-kind tag filters (closed-vocab axes + open tags).",
    "source": "Patent only: 'both' (default) | 'local' | 'remote'.",
    "exclude": "Ref slugs to drop from results (hand-skip; page= is "
    "usually preferable).",
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
