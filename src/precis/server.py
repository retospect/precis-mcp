"""FastMCP server — unified tools for papers and documents.

4 tools: search(), get(), put(), move(), plus a read-only stats().

**Plugin protocol v2 (Phase 1) additions**:

- Tools accept an optional ``type=`` argument naming the kind. Aliases are
  resolved to canonical names at URI parse. Bare-id dispatch still works
  (back-compat) via :func:`_to_uri`'s auto-detection.
- ``PRECIS_KINDS`` (see §13 of docs/plugin-architecture.md) is parsed at
  startup in :func:`_load_kinds_mask` and stored in the registry.  Fatal
  config errors (alias in config, unknown verb, empty brackets, duplicate
  kind) print a single line to stderr and exit with code 2.
- :func:`stats` surfaces the list of enabled kinds per verb and any
  accumulated startup warnings.
"""

from __future__ import annotations

import os.path
import sys

from mcp.server.fastmcp import FastMCP

from precis import tools
from precis.kinds_config import ConfigError, load_from_env
from precis.paper_id import classify_paper_id
from precis.protocol import CallContext, ErrorCode
from precis.registry import (
    ALIASES,
    KINDS,
    SCHEMES,
    STARTUP_WARNINGS,
    RegistryError,
    _discover,
    _format_error,
    add_startup_warning,
    invoke_handler,
    resolve_alias,
    set_kinds_mask,
    visible_kinds,
)
from precis.uri import _SEP_CHARS, SEP

mcp = FastMCP("precis")

# File extensions that trigger the file: scheme
_FILE_EXTENSIONS = {".docx", ".tex", ".md", ".markdown", ".rst", ".txt"}

# Max chars for multi-ID results before paginating
_MULTI_ID_BUDGET = 6000


def _split_sep(text: str) -> list[str]:
    """Split text at the first selector separator (› or ~)."""
    import re as _re

    return _re.split(r"[" + _re.escape(_SEP_CHARS) + r"]", text, maxsplit=1)


def _to_uri(id: str, kind: str = "") -> str:
    """Convert a user-facing id to an internal URI.

    Phase 1: when ``kind`` is provided, the scheme is taken from
    ``kind`` (after alias resolution) and prepended to ``id`` unless
    ``id`` already carries a scheme prefix.  Alias hop::

        _to_uri("foo", kind="perplexity")  # → "web:foo" when 'perplexity'
                                           #   is an alias of 'web'

    When ``kind`` is empty, falls through to the legacy auto-detection:

    1. Known scheme prefix (``doi:``, ``arxiv:``, …) → keep as-is
    2. File extension (``.docx``, ``.tex``, …) → ``file:`` scheme
    3. Bare DOI pattern (``10.NNNN/...``) → ``doi:`` scheme
    4. Otherwise → ``paper:`` scheme (slug lookup)
    """
    _discover()  # ensure SCHEMES / ALIASES are populated

    # Phase 1: explicit kind hint — resolve alias, stamp scheme, done.
    # Phase 5 refinement: when the user-supplied kind also exists as a
    # registered scheme (e.g. ``pmid``, ``doi``, ``arxiv`` are aliases of
    # the ``paper`` kind but also schemes on the PaperHandler), emit the
    # kind name as the URI scheme so the handler can branch on identifier
    # type.  Fall back to canonical-kind-as-scheme otherwise.
    if kind:
        # If id already carries a scheme prefix, keep it — respect the
        # caller's specificity.
        for scheme in SCHEMES:
            if id.startswith(scheme + ":"):
                return id
        scheme_name = kind if kind in SCHEMES else resolve_alias(kind)
        return f"{scheme_name}:{id}" if id else f"{scheme_name}:"

    # Legacy path (no kind hint) — Phase 5: delegate to classify_paper_id
    # for paper-ish ids after stripping accidental prefixes and handling
    # the file-extension short-circuit.
    if not id:
        return "paper:"
    # Known scheme prefixes — keep their scheme intact
    for scheme in SCHEMES:
        prefix = scheme + ":"
        if id.startswith(prefix):
            return id  # already a valid URI
    # Strip accidental scheme prefixes the LLM might copy
    for prefix in ("slug:", "s2:", "ref:"):
        if id.startswith(prefix):
            id = id[len(prefix) :]
            break
    # File-extension short-circuit — .docx/.tex/.md/... go to file:
    # scheme, which the paper-id classifier doesn't (and shouldn't) know
    # about.  Check this BEFORE classification so documents don't get
    # misrouted as slugs.
    bare = _split_sep(id)[0]
    base = bare.split("/")[0]
    _, ext = os.path.splitext(base)
    if ext.lower() in _FILE_EXTENSIONS:
        return f"file:{id}"
    # Phase 5: classify DOI / arXiv / PMCID / ISBN / ISSN / slug.
    # Selector suffix (``›chunk``, ``/view``) must ride along; strip for
    # classification, re-attach for the final URI.
    suffix = id[len(bare) :]
    classified = classify_paper_id(bare)
    return f"{classified.scheme}:{classified.value}{suffix}"


# ── PRECIS_KINDS startup wiring (§13) ───────────────────────────────


def _load_kinds_mask(*, env: dict[str, str] | None = None) -> None:
    """Parse ``PRECIS_KINDS`` and install the mask.

    Fatal config errors (``ConfigError``) print to stderr and exit(2).
    Non-fatal warnings (unknown kinds) are funnelled into
    ``STARTUP_WARNINGS`` via :func:`add_startup_warning`.  No-ops when the
    env var is empty or unset; the server then exposes every registered
    kind with every verb.
    """
    _discover()  # so KINDS / ALIASES are populated for known_kinds check
    warnings: list[str] = []
    try:
        mask = load_from_env(
            env=env,
            aliases=ALIASES,
            known_kinds=KINDS,
            warnings_out=warnings,
        )
    except ConfigError as exc:
        print(f"precis-mcp: {exc}", file=sys.stderr)
        sys.exit(2)
    for w in warnings:
        add_startup_warning(w)
    set_kinds_mask(mask)


# ── Dispatch helper (Phase 2) ────────────────────────────────────────


def _kind_from_uri(uri: str) -> str:
    """Return the canonical kind name for a URI's scheme.

    Runs through :func:`resolve_alias` so that ``"arxiv:..."`` and
    ``"doi:..."`` both resolve to ``"paper"``.
    """
    scheme = uri.split(":", 1)[0] if ":" in uri else uri
    return resolve_alias(scheme)


def _ambiguous_kind_error(
    verb: str,
    *,
    cause: str,
    args: dict[str, object] | None = None,
) -> str:
    """Render a ``KIND_UNKNOWN`` error for an ambiguous no-type call.

    The previous behaviour was to silently default to ``paper:`` whenever
    a caller omitted both ``type=`` and any disambiguating id/scope.
    That made it easy to, e.g. search todos but get paper results back
    because the agent forgot a keyword.  The strict form instead lists
    the visible kinds and tells the caller to re-issue with ``type=``.

    Ordering mirrors :func:`visible_kinds` output (alphabetical), so the
    agent always sees a stable enum.
    """
    kinds = [k.spec.name for k in visible_kinds(verb)]
    ctx = CallContext(kind="", verb=verb, args=dict(args) if args else {})
    return _format_error(
        ErrorCode.KIND_UNKNOWN,
        ctx,
        cause=cause,
        options=kinds or None,
        next_hint=(
            "re-issue with an explicit type=, e.g. "
            f"{verb}(type='paper', ...) — "
            "stats() lists every kind currently available"
        ),
    )


def _dispatch(
    kind: str,
    verb: str,
    call,  # type: ignore[no-untyped-def]  # Callable[[], str]
    args: dict[str, object] | None = None,
) -> str:
    """Wrap a handler call in :func:`invoke_handler` and render the Result.

    Phase 2: every tool response flows through here, so every response
    carries a cost footer (default ``[cost: free]``) and session stats
    accumulate for the ``stats()`` tool.

    Parameters:
        kind: Canonical kind name, already alias-resolved.
        verb: One of ``"search" | "get" | "put" | "move"``.
        call: Zero-arg callable that does the actual work (usually a
            closure over ``tools.read`` / ``tools.put``).
        args: Optional call-argument dict, passed to the wrapper for
            error formatting (``CallContext.args`` populates the
            ``where:`` line in error messages).

    Returns the rendered response string (``Result.render()``).  If the
    kind is not in ``KINDS`` (e.g. orphan scheme from a legacy plugin
    that skipped ``KindSpec``), falls back to calling ``call()`` raw so
    behaviour stays identical to the pre-wrapping path.  This keeps the
    server forgiving for third-party plugins that haven't migrated to
    plugin protocol v2 yet.
    """
    registered = KINDS.get(kind)
    if registered is None:
        # Unknown canonical kind — raw call-through (legacy path).
        try:
            return call()
        except Exception as exc:  # pragma: no cover — pre-Phase-2 behaviour
            return f"!! ERROR {type(exc).__name__}: {exc}"
    handler = registered.handler_cls()
    result = invoke_handler(kind, verb, handler, call, args=args)
    return result.render()


# ── Tools ────────────────────────────────────────────────────────────


@mcp.tool()
def search(
    query: str = "",
    top_k: int = 5,
    scope: str = "",
    type: str = "",
) -> str:
    """Semantic search over stored papers.

    query: natural language search query (REQUIRED)
    top_k: number of results (default 5)
    scope: slug or filename to restrict search (omit to search ALL papers)
    type:  optional kind name (e.g. 'paper', 'memory').  Aliases accepted.

    Examples:
      search(query='CO2 capture metal-organic frameworks')
      search(query='selectivity', scope='wang2020state')
      search(query='methods', scope='planning.docx')
      search(query='CO2 capture', type='paper')

    Without scope, searches across the entire paper library.
    Returns ranked results with snippets.
    Use get(id='wang2020state›N') to read full chunk text.
    """
    if not query.strip():
        return "ERROR: query is required. Example: search(query='CO2 capture MOF')"
    if scope:
        uri = _to_uri(scope, kind=type)
    elif type:
        uri = _to_uri("", kind=type)
    else:
        # No type, no scope — don't silently default to the paper corpus.
        # Silent defaults caused silent cross-kind leaks in agent flows
        # (e.g. an intended todo search returning paper chunks).
        return _ambiguous_kind_error(
            "search",
            cause=(
                "search() requires an explicit type= when no scope is given — "
                "otherwise the call is ambiguous across kinds"
            ),
            args={"query": query, "top_k": top_k},
        )
    kind = _kind_from_uri(uri)
    return _dispatch(
        kind,
        "search",
        lambda: tools.read(uri=uri, query=query, page=1, top_k=top_k),
        args={"id": scope, "query": query, "top_k": top_k},
    )


@mcp.tool()
def get(
    id: str = "",
    grep: str = "",
    depth: int = 0,
    type: str = "",
) -> str:
    """Read content by identifier. What you get depends on the id.

    id: identifier — dispatches by file extension vs paper slug
    grep: filter nodes — plain text, /regex/, or /regex/i
    depth: heading depth. 0=all, 1=H1, 2=H1+H2, 4=headings only

    Papers:
      get(id='wang2020state')              — overview (title, abstract, hints)
      get(id='wang2020state/toc')          — chunk index
      get(id='wang2020state/abstract')     — abstract text
      get(id='wang2020state/summary')      — enrichment summary
      get(id='wang2020state›38')           — chunk 38 full text
      get(id='wang2020state›38..42')       — chunks 38–42
      get(id='wang2020state›38/summary')   — chunk summary
      get(id='wang2020state/cite/bib')     — BibTeX citation
      get(id='wang2020state/fig')          — list figures
      get(id='wang2020state/fig/3')        — figure 3 overview + caption
      get(id='wang2020state/fig/3/legend') — caption text only
      get(id='wang2020state/fig/3/image')  — encoded image data
      get(id='wang2020state/fig/3/image/export') — save to ./figures/
      get(id='doi:10.1021/jacs.2c01234')   — lookup by DOI
      get(id='10.1021/jacs.2c01234')       — bare DOI (auto-detected)
      get(id='arxiv:2301.12345')           — lookup by arXiv ID
      get(grep='MOF')                  — filter paper list by keyword
      get(grep='ingested:today')       — papers added today
      get(grep='ingested:this-week')   — papers added this week
      get(grep='year:2020-2024')       — published 2020–2024
      get(grep='tag:review')           — filter by tag

    Documents:
      get(id='doc.docx')               — table of contents
      get(id='doc.docx›PLXDX')        — paragraph by slug
      get(id='doc.docx›S2.1')         — section scope
      get(id='doc.docx›PLXDX,ABCDE')  — multiple nodes
      get(id='doc.docx', grep='methods') — grep document
      get(id='doc.docx', depth=2)      — outline only
    """
    if not id and not grep:
        return (
            "ERROR: id or grep is required. Do not call get() with empty parameters.\n"
            "  get(id='wang2020state')      — paper overview\n"
            f"  get(id='wang2020state{SEP}5')    — read chunk 5\n"
            "  get(id='wang2020state/toc')  — table of contents\n"
            f"  get(id='slug1{SEP}4,slug2{SEP}9')   — multiple chunks at once\n"
            "  get(id='report.docx')        — document toc\n"
            "  get(grep='MOF')              — filter paper list"
        )
    # Comma-separated multi-ID: dispatch each, paginate if over budget
    ids = [s.strip() for s in id.split(",") if s.strip()] if id else []
    if len(ids) > 1:
        parts: list[str] = []
        total = 0
        for i, single_id in enumerate(ids):
            uri = _to_uri(single_id, kind=type)
            kind = _kind_from_uri(uri)
            extra = {"grep": grep} if grep else {}
            result = _dispatch(
                kind,
                "get",
                lambda uri=uri, extra=extra: tools.read(
                    uri=uri, query="", depth=depth, **extra
                ),
                args={"id": single_id, "grep": grep, "depth": depth},
            )
            total += len(result)
            parts.append(result)
            # Check budget after adding (always include at least 1 result)
            if total > _MULTI_ID_BUDGET and i < len(ids) - 1:
                remaining = ids[i + 1 :]
                parts.append(
                    f"\n[{i + 1} of {len(ids)} IDs shown. "
                    f"Remaining: get(id='{','.join(remaining)}')]"
                )
                break
        return "\n---\n".join(parts)
    if id:
        uri = _to_uri(id, kind=type)
    elif type:
        uri = _to_uri("", kind=type)
    else:
        # Only grep= provided — was previously a silent paper-list filter.
        # Make the caller commit to a kind so agents that meant
        # get(type='todo', grep='foo') don't accidentally browse papers.
        return _ambiguous_kind_error(
            "get",
            cause=(
                "get() with only grep= is ambiguous — specify type= so the "
                "filter runs over the intended corpus"
            ),
            args={"grep": grep, "depth": depth},
        )
    kind = _kind_from_uri(uri)
    extra = {"grep": grep} if grep else {}
    return _dispatch(
        kind,
        "get",
        lambda: tools.read(uri=uri, query="", depth=depth, **extra),
        args={"id": id, "grep": grep, "depth": depth},
    )


@mcp.tool()
def put(
    id: str,
    text: str = "",
    mode: str = "replace",
    tracked: bool = True,
    note: str = "",
    link: str = "",
    type: str = "",
) -> str:
    """Write, annotate, or delete content.

    id: target identifier (file›slug for docs, paper slug for notes)
    text: content to write.
    mode: append / replace / after / before / delete / comment / note
    tracked: DOCX track-changes (default true). LaTeX: ignored.
    note: annotation text — creates a note on the target ref or block.
    link: link spec as 'target_slug:relation' — creates a typed link.

    Headings: start line with # markers. Never number them.
      # Document Title    (Title style — one per document)
      ## Section           (Heading 1)
      ### Subsection       (Heading 2)
      #### Sub-subsection  (Heading 3, max depth)

    NEW content → mode='append' (creates file if needed):
      put(id='report.docx', text='## Methods', mode='append')
      put(id='report.docx', text='First paragraph.', mode='append')

    EDIT existing content → mode='replace' (requires ›SLUG in id):
      put(id='report.docx›PLXDX', text='Revised.', mode='replace')
      put(id='report.docx›PLXDX', text='New para.', mode='after')
      put(id='report.docx›PLXDX', mode='delete')
      put(id='report.docx›PLXDX', text='Fix this.', mode='comment')

    Citations (DOCX):
      Cite: [@slug] in text — slug is the paper name, NEVER include ›chunk.
      ✓ [@piscopo2020strategies]  ✗ [piscopo2020strategies›54]  ✗ [piscopo2020strategies]
      Define: put(id='report.docx', text='[@slug]: Author, Title, 2024.', mode='append')
      Undefined [@slug] references are flagged after each write.

    Notes (on any ref or block):
      put(id='wang2020state', note='Key finding about MOFs')
      put(id='wang2020state›38', note='Important result here')

    Links (between refs or blocks):
      put(id='wang2020state', link='jones2023surface:cites')
      put(id='wang2020state›38', link='jones2023surface:discusses')
      put(id='wang2020state', link='jones2023surface')  — defaults to 'references'

    Paper notes (legacy, still works):
      put(id='wang2020state', text='Key finding', mode='note')

    Multiple paragraphs separated by newlines are auto-split.
    """
    if not id and not type:
        # Previously fell through to ``paper:`` which then returned a
        # readonly error — confusing the agent about whether ``put`` was
        # wrong or the kind was wrong.  Be explicit: list the writable
        # kinds and tell the caller to re-issue with type=.
        return _ambiguous_kind_error(
            "put",
            cause=(
                "put() requires either an id with a scheme prefix or an "
                "explicit type= — neither was provided"
            ),
            args={"mode": mode},
        )
    uri = _to_uri(id, kind=type)
    kind = _kind_from_uri(uri)
    return _dispatch(
        kind,
        "put",
        lambda: tools.put(
            uri=uri, text=text, mode=mode, tracked=tracked, note=note, link=link
        ),
        args={"id": id, "mode": mode},
    )


@mcp.tool()
def move(
    id: str,
    after: str,
    type: str = "",
) -> str:
    """Reorder nodes within a document.

    id: doc.docx›SLUG or doc.docx›SLUG1,SLUG2 to move
    after: doc.docx›SLUG — moved nodes placed after this node
    type: optional kind name (aliases accepted).

    Slugs don't change. Paths are recomputed.
    """
    uri = _to_uri(id, kind=type)
    # Extract the 'after' slug from id format (strip file part if present)
    after_sel = _split_sep(after)[-1] if any(c in after for c in _SEP_CHARS) else after
    kind = _kind_from_uri(uri)
    return _dispatch(
        kind,
        "move",
        lambda: tools.put(uri=uri, text=after_sel, mode="move"),
        args={"id": id, "after": after},
    )


@mcp.tool()
def stats() -> str:
    """Read-only server introspection — §8, §10.2.

    Lists what the server is currently exposing: enabled kinds per verb,
    active ``PRECIS_KINDS`` mask (if any), session call counts + last
    cost per kind, and accumulated startup warnings.  No secrets.
    Always public — there is no hidden admin mode (§18 non-goal).

    Example output::

        service: precis-mcp
        mask: PRECIS_KINDS set
        kinds by verb:
          search paper, memory
          get    paper, memory, doc
          put    memory
          move   (none)
        session:
          paper   calls=12  errors=0  last_cost=free
          web     calls=3   errors=1  last_cost=~$0.002/call
        startup warnings:
          - kind 'news' hidden — missing env: PG_DATABASE_URL
    """
    from precis.registry import get_kinds_mask, get_session_stats

    _discover()
    lines: list[str] = ["service: precis-mcp"]
    mask = get_kinds_mask()
    lines.append(
        f"mask: {'PRECIS_KINDS set' if mask is not None else 'unset (expose all)'}"
    )
    lines.append("kinds by verb:")
    for verb in ("search", "get", "put", "move"):
        kinds = [k.spec.name for k in visible_kinds(verb)]
        shown = ", ".join(kinds) if kinds else "(none)"
        lines.append(f"  {verb:<6} {shown}")
    session = get_session_stats()
    if session:
        lines.append("session:")
        # Sort by kind name so output is stable across runs.
        for name in sorted(session):
            s = session[name]
            lines.append(
                f"  {name:<8} calls={s.calls}  errors={s.errors}  "
                f"last_cost={s.last_cost}"
            )
    else:
        lines.append("session: (no calls yet)")
    if STARTUP_WARNINGS:
        lines.append("startup warnings:")
        for w in STARTUP_WARNINGS:
            lines.append(f"  - {w}")
    else:
        lines.append("startup warnings: none")
    return "\n".join(lines)


def main() -> None:
    """Run the MCP server.

    Startup order (§10.1):

    1. Discover plugins via :func:`precis.registry._discover`.  A kind-name
       collision raises :class:`RegistryError` here.
    2. Parse ``PRECIS_KINDS`` and install the mask.  Grammar / alias /
       verb errors raise :class:`ConfigError` here.
    3. Hand off to FastMCP's stdio loop.

    Any fatal error in steps 1 or 2 prints one line to stderr and exits
    with code 2.  The agent-side MCP client then sees a clean launch
    failure it can surface to the operator.
    """
    try:
        _load_kinds_mask()
    except RegistryError as exc:
        # _discover() inside _load_kinds_mask could fail on kind collisions.
        print(f"precis-mcp: {exc}", file=sys.stderr)
        sys.exit(2)
    mcp.run()
