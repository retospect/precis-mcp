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

    Payload fields are sampled (kind, id, the ``q=`` query, the
    citation/finding write fields, length of ``text=`` for puts)
    rather than dumped wholesale — we want a grep-friendly audit, not
    the full LLM payload that lives in ``job_summary``. The point of
    the line is "what did the agent actually ask for?", so the search
    query and the addressing/citation fields are sampled verbatim
    (truncated), not just their presence.

    A **failed** call logs at WARNING with a fuller payload: when a
    call errors we want every field the agent passed so the exact
    misuse is reconstructable (a wrong ``kind=``, a ``cited_in=`` the
    resolver rejects, a ``q=`` that returned nothing). Emitting errors
    at WARNING also means they survive a server deployed at
    ``log_level=WARNING`` — the diagnostic surface stays alive even
    when the INFO firehose is off.
    """
    import os

    parent_todo = os.environ.get("PRECIS_CURRENT_TODO", "-")
    workspace = os.environ.get("PRECIS_WORKSPACE", "-")
    model = os.environ.get("PRECIS_CURRENT_MODEL", "-")

    # Verbatim-but-truncated scalar fields. ``q`` (the search query)
    # and the citation/finding/link addressing fields are exactly the
    # "what is the agent trying to do" signal that the old sample
    # dropped — agents fumble these the most (missing kind=, a
    # ``cited_in=`` the link parser rejects, a non-corpus ``doi=``).
    _SCALAR_KEYS = (
        "kind",
        "id",
        "name",
        "mode",
        "rel",
        "view",
        "q",
        "scope",
        "target",
        "link",
        "cited_in",
        "source_handle",
        "doi",
        "arxiv",
    )
    # Longer free-text fields kept only on the error path; the success
    # firehose keeps them as char-counts to stay grep-friendly.
    _ERROR_TEXT_KEYS = ("source_quote", "title", "body", "text")

    def _trunc(v: Any, limit: int) -> Any:
        if isinstance(v, str) and len(v) > limit:
            return v[:limit] + "…"
        return v

    sample: dict[str, Any] = {}
    for key in _SCALAR_KEYS:
        if key in payload and payload[key] is not None:
            sample[key] = _trunc(payload[key], 200 if key == "q" else 120)
    if "text" in payload and isinstance(payload.get("text"), str):
        sample["text_chars"] = len(payload["text"])
    if "tags" in payload and isinstance(payload.get("tags"), list):
        sample["tags"] = payload["tags"][:8]
    if "args" in payload and isinstance(payload["args"], dict):
        sample["args_keys"] = sorted(payload["args"].keys())[:8]

    if error:
        # On failure, widen the capture: include the free-text write
        # fields (bounded) so the failing call is fully reconstructable,
        # plus any payload key we didn't already sample, so a novel
        # misuse can't hide in an unlogged kwarg.
        for key in _ERROR_TEXT_KEYS:
            if key in payload and payload[key] is not None:
                sample[key] = _trunc(payload[key], 200)
        for key, v in payload.items():
            if key not in sample and key not in ("text", "tags", "args"):
                sample[key] = _trunc(v, 120)

    log_at = _TOOL_CALL_LOGGER.warning if error else _TOOL_CALL_LOGGER.info
    log_at(
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
    mode: str | None = None,
    # finding-specific filter: short-circuits a STATUS:<value> tag
    # filter. Defaults to 'established' on the finding handler; pass
    # 'tracing' / 'multi_candidate' / 'dead_chain' to inspect a
    # specific lifecycle cohort, or '*' to see all. Declared at the
    # verb level so the schema advertises it to strict-schema clients.
    status: str | None = None,
    # Broad / high-recall retrieval (paper kind). ``queries=`` are extra
    # question reformulations and ``answers=`` are hypothetical-answer
    # passages (HyDE); each becomes a ranked leg reciprocal-rank-fused
    # with ``q`` so a chunk that surfaces across phrasings wins —
    # robustness to the exact wording. ``per_paper=`` caps hits per paper
    # to spread coverage across more sources. Declared at the verb level
    # so the schema advertises them to strict-schema clients.
    queries: list[str] | None = None,
    answers: list[str] | None = None,
    per_paper: int | None = None,
    # Deep search (paper kind): ``good=True`` queues an async
    # coordinator campaign (fuse → LLM triage children → merged
    # verdict) and returns a job handle instead of hits — poll
    # ``get(kind='job', id=…)``.
    good: bool | None = None,
    # Field-scoped paper lookup (paper kind). ``title=`` / ``author=``
    # return paper *records* — handle + one-line citation + a cite path —
    # instead of body-block hits. The targeted finder for "I know the
    # title / an author": matches refs.title (trigram+FTS) / refs.authors
    # (jsonb) directly, held copies first, so an exact title or a bare
    # surname lands the paper itself rather than other papers' text.
    title: str | None = None,
    author: str | None = None,
    # ADR 0045: scope results to a folder's placement subtree.
    # Accepts the folder id, 'folder:N', the fo<N> handle, or the
    # folder's (unique) name. Forces the cross-kind fan-out even with
    # a single kind= so hits can be membership-filtered.
    folder: str | int | None = None,
) -> str:
    """Hybrid lexical + semantic search across kinds.

    `page_size` ≤ 100; `page=N` paginates. Omit `kind` (or `'*'`) for
    cross-kind fan-out; `exclude=` skips slugs; `source=` is patent-only;
    `folder=` scopes to a subtree (ADR 0045).

    `mode=` `'hybrid'` (default) / `'lexical'` (exact string) /
    `'semantic'`. `angle=`+`like=` spray `n` diverse items;
    `view='dreamable'`/`'stubs'` are special browses.

    Broad retrieval (paper): `queries=`/`answers=` (HyDE) fuse ranked
    legs; `per_paper=` spreads across papers; `good=True` queues a deep
    search; `title=`/`author=` look up by byline.

    Full reference: get(kind='skill', id='precis-search-help').
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

    # Validate the ranking mode at the boundary so an unknown value
    # surfaces as the canonical BadInput envelope, not a silent
    # default-to-hybrid that hides a typo.
    if mode is not None and mode.strip().lower() not in (
        "hybrid",
        "lexical",
        "semantic",
    ):
        runtime = _get_runtime()
        return _validation_error(
            runtime.render_error(
                BadInput(
                    f"unknown search mode {mode!r}",
                    next="mode='hybrid' (default) | 'lexical' | 'semantic'",
                )
            )
        )

    # Broad-retrieval leg cap: bound the fan-out so a runaway call can't
    # fire dozens of embeds + SQL legs. queries + answers each cap at 8.
    if queries is not None and len(queries) > 8:
        runtime = _get_runtime()
        return _validation_error(
            runtime.render_error(
                BadInput(
                    f"queries= has {len(queries)} entries, max 8",
                    next="pass up to 8 distinct rephrasings; merge the rest",
                )
            )
        )
    if answers is not None and len(answers) > 8:
        runtime = _get_runtime()
        return _validation_error(
            runtime.render_error(
                BadInput(
                    f"answers= has {len(answers)} entries, max 8",
                    next="pass up to 8 hypothetical-answer passages (HyDE)",
                )
            )
        )
    # NB ``isinstance(True, int)`` is True in Python — reject bools
    # explicitly so ``per_paper=True`` doesn't silently become cap 1.
    if per_paper is not None and (
        isinstance(per_paper, bool) or not isinstance(per_paper, int) or per_paper < 1
    ):
        runtime = _get_runtime()
        return _validation_error(
            runtime.render_error(
                BadInput(
                    f"per_paper must be a positive integer, got {per_paper!r}",
                    next="per_paper=2 keeps at most 2 hits per paper",
                )
            )
        )

    # Deep-search gate: ``good=True`` is a paper-only surface (it mints
    # a coordinator campaign, not a ranked page). Reject other kinds at
    # the boundary so the error is immediate rather than a silent
    # pass-through into a handler that ignores the flag.
    if good and kind != "paper":
        runtime = _get_runtime()
        return _validation_error(
            runtime.render_error(
                BadInput(
                    f"good=True is a paper-only deep search (kind={kind!r})",
                    next="search(kind='paper', q='…', good=True)",
                )
            )
        )

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
    if mode is not None:
        payload["mode"] = mode
    # Broad-retrieval knobs — forwarded only when set so a plain search
    # is byte-identical to its prior payload (single lex+sem path).
    if queries is not None:
        payload["queries"] = queries
    if answers is not None:
        payload["answers"] = answers
    if per_paper is not None:
        payload["per_paper"] = per_paper
    if good is not None:
        payload["good"] = bool(good)
    if title is not None:
        payload["title"] = title
    if author is not None:
        payload["author"] = author
    if folder is not None:
        payload["folder"] = folder

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
    # job retry (see precis-job-help): re-run a failed job —
    # put(kind='job', id=<failed>, mode='retry'[, model='sonnet']). model=
    # swaps the parent todo's LLM:<model> tag so the re-minted tick runs on
    # a different tier (opus|sonnet|haiku).
    model: str | None = None,
    # presentation (see precis-pres-help):
    pos: int | None = None,
    meta: dict[str, Any] | None = None,
    ref_meta: dict[str, Any] | None = None,
    subtype: str | None = None,
    chunk_kind: str | None = None,
    # draft (see precis-draft-help): create a draft (with project=) or add
    # a chunk (chunk_kind=, text=) placed by at={first|last|into|before|after}.
    at: dict[str, Any] | None = None,
    project: str | int | None = None,
    # draft data/table chunk (chunk_kind='table', ADR 0035): canonical data
    # in table={header,rows}; the markdown text is derived. caption= is the
    # legend; regen= records how the data was produced (provenance, inert).
    table: dict[str, Any] | None = None,
    caption: str | None = None,
    regen: dict[str, Any] | None = None,
    # draft figure (chunk_kind='figure'): image=<base64> for an uploaded image
    # (ADR 0034), or render=<python> + plots=[dc<id>] for a graph computed from
    # data chunks (ADR 0035) — caption= is the legend either way.
    render: str | None = None,
    plots: list[str] | None = None,
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

    `mode=`: file kinds use 'create'; paid-import kinds use 'import';
    numeric-ref kinds omit it. `tags=`/`untags=` add/remove; `link=`/`unlink=`
    use `kind:identifier[~selector]`, `rel=` defaults to `related-to`.

    Full reference: get(kind='skill', id='precis-put-help').
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
            "model": model,
            "pos": pos,
            "meta": meta,
            "ref_meta": ref_meta,
            "subtype": subtype,
            "chunk_kind": chunk_kind,
            "at": at,
            "project": project,
            "table": table,
            "caption": caption,
            "regen": regen,
            "render": render,
            "plots": plots,
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
    # memory (see precis-memory-help): edit(mode='replace') rewrites the body
    # prose; pass title= to also update the short header (omit to keep it).
    title: str | None = None,
    # todo (see precis-tasks-help): edit(mode='replace', body='…') sets/rewrites
    # the optional details body; combine with text= to rewrite the task line too.
    body: str | None = None,
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
    # draft (see precis-draft-help): reorder/reparent a chunk by intent,
    # move={before|after|into:'¶handle'} / {first|last:true}. No text.
    move: dict[str, Any] | None = None,
    # draft optimistic edit: the content_sha the caller saw when it read
    # the chunk (shown as ``sha:…`` in get(id='¶handle')). The edit fails
    # if the chunk changed since, so concurrent editors don't clobber.
    base_sha: str | None = None,
    # draft abbreviations: mark token(s) as NOT an abbreviation (chem
    # formula, model name, …) to silence the undefined-abbrev write hint.
    not_abbrev: list[str] | None = None,
    # draft data/table chunk (chunk_kind='table', ADR 0035): replace the
    # canonical data (table={header,rows}) / legend (caption=) / provenance
    # (regen=); the markdown text re-derives. text= is rejected on a table.
    table: dict[str, Any] | None = None,
    caption: str | None = None,
    regen: dict[str, Any] | None = None,
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
        "title": title,
        "body": body,
        "find": find,
        "before": before,
        "after": after,
        "where": where,
        "match": match,
        "nth": nth,
        "allow_rename": allow_rename,
        "dry_run": dry_run,
        "pick_candidate": pick_candidate,
        "move": move,
        "base_sha": base_sha,
        "not_abbrev": not_abbrev,
        "table": table,
        "caption": caption,
        "regen": regen,
    }
    # See ``get`` for the ``str | CallToolResult`` return contract.
    return _dispatch("edit", payload)


def delete(
    # See ``search`` for the Optional-required pattern (round-2 picky N-1).
    kind: str | None = None,
    id: str | int | None = None,
    # draft (see precis-draft-help): retiring a heading with children needs
    # mode='cascade' (delete contents) or 'promote' (lift them to the parent).
    mode: str | None = None,
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
    return _dispatch("delete", {"kind": kind, "id": id, "mode": mode})


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
    todo: STATUS+PRIO+LLM+AUDIT. gripe: STATUS+PRIO.
    finding: STATUS+AUDIT. job: STATUS (lifecycle subsets).
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
    section boundaries; the head ends with a ``⚠️ Truncated`` footer
    carrying a ``more(cursor='...')`` call. Call this tool with that
    cursor verbatim to retrieve the tail, and keep following each
    page's cursor until no footer remains before acting on the body.

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
    "mode": "Ranking: 'hybrid' (default) | 'lexical' (exact keyword/"
    "phrase/identifier, embedder-independent) | 'semantic'.",
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
