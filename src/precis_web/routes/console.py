"""Console tab — interactive precis-query over the seven verbs.

Also hosts a "smart resolve" surface that takes any reasonable-looking
identifier — paper cite_key, DOI, arXiv id, YouTube id, kind:slug,
discord handle — and routes to the canonical view. The detection
order matters: more specific patterns first so ``charlier07~374``
goes to the paper chunk rather than getting interpreted as raw text.


A thin web mirror of ``precis repl``: pick a verb, type
``key=value`` arguments (shlex-split, quote values with spaces), and
the call runs through the same in-process runtime the MCP server and
CLI use. Arg types are coerced via the shared ``TOOL_REGISTRY`` so
ints / bools / lists behave the same as on the CLI.

Read-only by habit but not by enforcement: every verb is reachable
(including ``put`` / ``delete``), exactly like the REPL. The web
process runs as ``web:owner`` (owner), so tree guards treat it as the owner.

Also exposes a "quick query" surface for the common external-service
shortcuts (Wolfram Alpha, Perplexity research/reasoning, YouTube
transcript) so the operator doesn't have to remember each kind's
canonical call shape — pick a service, pick online/cache, type the
query.
"""

from __future__ import annotations

import asyncio
import re
import shlex
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from precis.format import render_agent_table
from precis.tools import TOOL_REGISTRY, get_tool_names
from precis.tools.cli_adapter import _convert_value
from precis.utils.handle_registry import format_handle
from precis.utils.search_header import format_search_headline
from precis_web.deps import await_dispatch, get_runtime, get_store, templates

router = APIRouter(prefix="/console", tags=["console"])

#: Quick-query shortcuts. Each entry maps a UI label to the kind it
#: dispatches against. ``cache_param`` / ``online_param`` are the
#: arg-name the verb expects: ``get`` takes ``id=`` everywhere we
#: care about, ``search`` takes ``q=``. Kept as data (not branches)
#: so adding another service is one row.
QUICK_SERVICES: list[dict[str, str]] = [
    {
        "value": "math",
        "label": "Wolfram Alpha",
        "kind": "math",
        "hint": "Natural-language or symbolic — e.g. 'population of Ireland'.",
    },
    {
        "value": "perplexity-research",
        "label": "Perplexity Research",
        "kind": "perplexity-research",
        "hint": "Deep web research — citations included.",
    },
    {
        "value": "perplexity-reasoning",
        "label": "Perplexity Reasoning",
        "kind": "perplexity-reasoning",
        "hint": "Multi-step reasoning over a question.",
    },
    {
        "value": "youtube",
        "label": "YouTube Transcript",
        "kind": "youtube",
        # The user-asked YouTube hint goes here so the form renders
        # it next to the field; matters because the input shape isn't
        # obvious to a newcomer.
        "hint": (
            "Paste the video ID (the part after v= in a YouTube URL, "
            "e.g. dQw4w9WgXcQ) or the full URL."
        ),
    },
    {
        "value": "patent",
        "label": "Patents (EPO)",
        "kind": "patent",
        "hint": (
            "Search term, applicant name, or patent publication number "
            "(US / EP / WO format)."
        ),
    },
    {
        "value": "semanticscholar",
        "label": "Semantic Scholar",
        "kind": "semanticscholar",
        "hint": (
            "Paper search — natural-language query. Returns top 10 hits "
            "with authors / year / DOI / abstract / citation count."
        ),
    },
]

_QUICK_BY_VALUE: dict[str, dict[str, str]] = {s["value"]: s for s in QUICK_SERVICES}


#: Worked examples, grouped. Each example is a ``get``/``search`` call
#: (the only verbs a GET deep-link will *run* — see
#: :data:`_GET_RUNNABLE_VERBS`) so every link is one-click, prefills the
#: form, and renders an already-run result. Kept as data (not template
#: markup) so the set is one place to grow and the template just renders
#: the group dropdown over it. ``key`` is an ascii handle the client-side
#: group filter compares against (group titles carry spaces / ``&``).
CONSOLE_EXAMPLES: list[dict[str, Any]] = [
    {
        "key": "papers",
        "group": "Papers & research",
        "examples": [
            {
                "verb": "search",
                "args": 'kind=paper q="attention is all you need" page_size=5',
                "note": "find a paper by title / topic",
            },
            {
                "verb": "get",
                "args": "kind=paper id=pa2928",
                "note": "one paper's top-level overview (Attention Is All You Need)",
            },
            {
                "verb": "get",
                "args": "kind=paper id=pa2928 view=toc",
                "note": "that paper's table of contents",
            },
            {
                "verb": "search",
                "args": 'kind=finding q="CO2 capture"',
                "note": "chain-of-evidence findings over a citation chase",
            },
            {
                "verb": "search",
                "args": 'kind=citation q="efficiency"',
                "note": "verified claims → their source quotes",
            },
        ],
    },
    {
        "key": "tasks",
        "group": "Tasks, jobs & ops",
        "examples": [
            {
                "verb": "search",
                "args": 'kind=todo q="ingest"',
                "note": "open tasks matching a topic",
            },
            {
                "verb": "search",
                "args": "kind=todo view=projects",
                "note": "the projects dashboard (workspace-owning roots)",
            },
            {
                "verb": "search",
                "args": 'kind=job q="fix_gripe"',
                "note": "recent execution jobs",
            },
            {
                "verb": "search",
                "args": 'kind=gripe q="slow"',
                "note": "the bug / niggle tracker",
            },
            {
                "verb": "search",
                "args": 'kind=memory q="deploy"',
                "note": "agent notes & scratchpad",
            },
            {
                "verb": "get",
                "args": "kind=alert id=/open",
                "note": "open ops / health alerts",
            },
        ],
    },
    {
        "key": "docs",
        "group": "Skills & docs",
        "examples": [
            {
                "verb": "get",
                "args": "kind=skill id=precis-overview",
                "note": "orientation: the seven verbs + kinds table",
            },
            {
                "verb": "get",
                "args": "kind=skill id=toc",
                "note": "the full skill index",
            },
            {
                "verb": "search",
                "args": 'kind=skill q="how do I cite a paper"',
                "note": "find the right how-to skill",
            },
            {
                "verb": "search",
                "args": 'kind=tex q="introduction"',
                "note": "LaTeX section sources under PRECIS_ROOT",
            },
            {
                "verb": "search",
                "args": 'kind=markdown q="notes"',
                "note": "markdown files in the editable sandbox",
            },
        ],
    },
    {
        "key": "discovery",
        "group": "Oracle & discovery",
        "examples": [
            {
                "verb": "get",
                "args": "kind=oracle",
                "note": "list the wisdom traditions",
            },
            {
                "verb": "search",
                "args": 'kind=oracle q="patience"',
                "note": "consult wisdom across traditions",
            },
            {
                "verb": "get",
                "args": "kind=random",
                "note": "a random block — inspiration / warm-up",
            },
            {
                "verb": "search",
                "args": 'q="graphene"',
                "note": "cross-kind fan-out (no kind= → searches everything)",
            },
        ],
    },
    {
        "key": "tools",
        "group": "Calculators & cached services",
        "examples": [
            {
                "verb": "get",
                "args": 'kind=calc q="2+3*4"',
                "note": "local SymPy arithmetic (free)",
            },
            {
                "verb": "get",
                "args": 'kind=calc q="solve(Eq(x**2-4, 0), x)"',
                "note": "symbolic solve (free)",
            },
            {
                "verb": "search",
                "args": 'kind=math q="population of Ireland"',
                "note": "cached Wolfram Alpha answers (online run via Quick box below)",
            },
            {
                "verb": "search",
                "args": 'kind=websearch q="perovskite stability"',
                "note": "cached Perplexity answers",
            },
            {
                "verb": "search",
                "args": 'kind=youtube q="transformer"',
                "note": "cached video transcripts",
            },
        ],
    },
]


# ---- smart-resolve detection ----------------------------------------
#
# Patterns checked in order; first match wins. Each maps to a target
# URL the operator gets redirected to. The detection is intentionally
# pragmatic — we accept some false positives (covid19 → /r/paper/covid19
# 404s cleanly) over a tight regex that would miss real cite_keys.

_RESOLVE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # ``kind:slug`` or ``kind:#id`` with optional ``~N`` chunk address —
    # the explicit, unambiguous form.
    (
        re.compile(
            r"^(?P<kind>[a-z][a-z0-9-]*):"
            r"(?P<id>#?[0-9]+|[A-Za-z0-9][A-Za-z0-9_/-]*)"
            r"(?:~(?P<chunk>p?[0-9]+(?:\.\.[0-9]+)?))?$"
        ),
        "kind_prefixed",
    ),
    # Bare discord conv handle.
    (
        re.compile(
            r"^discord/[0-9]+/[0-9]+/[0-9]+(?:~(?P<chunk>p?[0-9]+(?:\.\.[0-9]+)?))?$"
        ),
        "bare_conv",
    ),
    # DOI: ``10.<registrant>/<suffix>``.
    (re.compile(r"^10\.\d{3,9}/[^\s]+$"), "doi"),
    # arXiv id (modern post-2007: ``NNNN.NNNNN`` with optional version).
    (re.compile(r"^\d{4}\.\d{4,5}(?:v\d+)?$"), "arxiv"),
    # YouTube video id — 11 chars [A-Za-z0-9_-].
    (re.compile(r"^[A-Za-z0-9_-]{11}$"), "youtube"),
    # Bare paper cite_key: ``<surname><2-digit-year><optional-letter>``
    # with optional chunk suffix. Accept ≥2 letters here (more lenient
    # than the inline linkifier) since the operator typed it explicitly.
    (
        re.compile(
            r"^[a-z]{2,}[0-9]{2}[a-z]?(?:~(?P<chunk>p?[0-9]+(?:\.\.[0-9]+)?))?$"
        ),
        "paper_cite",
    ),
)


def _smart_resolve(query: str) -> str | None:
    """Return the redirect URL for ``query`` if it matches a known shape.

    Returns ``None`` when nothing matches — caller falls back to a
    cross-kind search.
    """
    q = query.strip()
    if not q:
        return None
    for pat, shape in _RESOLVE_PATTERNS:
        m = pat.match(q)
        if m is None:
            continue
        if shape == "kind_prefixed":
            kind = m.group("kind")
            ref_id = m.group("id").lstrip("#")
            chunk = m.group("chunk")
            url = f"/r/{kind}/{ref_id}"
            if chunk:
                url += f"?chunk={chunk}"
            return url
        if shape == "bare_conv":
            chunk = m.group("chunk")
            slug = q.split("~", 1)[0]
            url = f"/r/conv/{slug}"
            if chunk:
                url += f"?chunk={chunk}"
            return url
        if shape == "doi":
            # No direct DOI resolver yet — search papers by DOI string.
            from urllib.parse import quote_plus

            return f"/refs?q={quote_plus(q)}&kinds=paper&all=1"
        if shape == "arxiv":
            from urllib.parse import quote_plus

            return f"/refs?q={quote_plus(q)}&kinds=paper&all=1"
        if shape == "youtube":
            return f"/r/youtube/{q}"
        if shape == "paper_cite":
            chunk = m.group("chunk")
            slug = q.split("~", 1)[0]
            url = f"/r/paper/{slug}"
            if chunk:
                url += f"?chunk={chunk}"
            return url
    return None


def _parse_args(verb: str, args_text: str) -> dict[str, Any]:
    """Turn ``key=value`` tokens into a typed payload for ``verb``.

    Tokens are space-separated (``kind=draft id=test01``), but operators
    routinely paste the Python-call style (``kind=draft, id=test01``) —
    ``shlex`` keeps the comma glued to the value, so ``kind=draft,`` was
    dispatched as the literal kind ``'draft,'`` and bounced as an
    "unknown kind". Strip surrounding commas off each token (and drop a
    lone ``,``) so both styles parse the same; a comma *inside* a quoted
    value survives untouched.

    Raises ``ValueError`` on a malformed token or an unknown arg —
    surfaced to the user as an inline message rather than a 500.
    """
    if verb not in TOOL_REGISTRY:
        raise ValueError(f"unknown verb {verb!r}")
    params = TOOL_REGISTRY[verb]["parameters"]
    payload: dict[str, Any] = {}
    for tok in shlex.split(args_text or ""):
        tok = tok.strip(",")
        if not tok:
            continue
        if "=" not in tok:
            raise ValueError(f"expected key=value, got {tok!r}")
        key, _, raw = tok.partition("=")
        key = key.strip()
        if key not in params:
            allowed = ", ".join(params.keys())
            raise ValueError(f"unknown arg {key!r} for {verb} (allowed: {allowed})")
        payload[key] = _convert_value(raw, params[key])
    return payload


def _quick_context(**overrides: Any) -> dict[str, Any]:
    """Shared context shape so index/run/quick all hand the same keys
    to the template."""
    ctx: dict[str, Any] = {
        "active_tab": "console",
        "verbs": list(get_tool_names()),
        "verb": "search",
        "args_text": "",
        "result": None,
        "is_error": False,
        "quick_services": QUICK_SERVICES,
        "quick_service": QUICK_SERVICES[0]["value"],
        "quick_mode": "online",
        "quick_query": "",
        "quick_call": None,
        "console_examples": CONSOLE_EXAMPLES,
    }
    ctx.update(overrides)
    return ctx


#: Verbs that a ``GET /console?verb=…&args_text=…`` deep-link is
#: allowed to *run* on load. The rest still prepopulate the form (so a
#: link can stage a ``put``/``edit`` for the operator to eyeball and
#: hit Run), but never fire — a GET must stay safe/idempotent so a
#: shared link or a browser prefetch can't mutate the tree.
_GET_RUNNABLE_VERBS = frozenset({"get", "search"})


#: ``kind=`` spellings that mean "paper" for the console search override
#: below — the bare kind and its ADR-0036 2-char handle code. Matched on
#: the raw string the operator typed, before the dispatcher's own
#: ``kind='pa'`` → ``'paper'`` expansion runs (gripe 162400 repro used
#: both spellings).
_PAPER_KIND_SPELLINGS = frozenset({"paper", "pa"})

#: Real ``search`` params (``tools/core.py``) that ``_paper_console_search``
#: does not forward to ``PaperHandler.search_hits()``. Present + truthy on
#: any of these ⇒ bail to normal dispatch rather than silently returning a
#: narrower result set than the real MCP call would.
_PAPER_SEARCH_UNSUPPORTED_PARAMS = frozenset(
    {
        "scope",
        "source",
        "angle",
        "n",
        "like",
        "view",
        "status",
        "queries",
        "answers",
        "per_paper",
        "folder",
        "sort",
        "since",
        "until",
    }
)


def _paper_console_rows(store: Any, hits: Any, page_size: int) -> list[dict[str, str]]:
    """Collapse ranked block hits to one row per paper (best rank first).

    ``hits`` is the ``search_hits()`` output — several hits often land
    on the same paper, so this keeps the first (best-ranked) occurrence
    of each ``ref_id`` and drops the rest, up to ``page_size`` distinct
    papers. Reuses ``routes.papers._authors_str`` for the author join
    rather than re-deriving it from ``ref.meta``.
    """
    # Local import — avoids a module-load cycle risk (papers.py is a
    # sibling route module, not a dependency of console.py otherwise).
    from precis_web.routes.papers import _authors_str

    seen: set[int] = set()
    order: list[int] = []
    for h in hits:
        rid = h.ref_id
        if rid is None or rid in seen:
            continue
        seen.add(rid)
        order.append(rid)
        if len(order) >= page_size:
            break
    if not order:
        return []
    refs = store.fetch_refs_by_ids(order, include_deleted=False)
    rows: list[dict[str, str]] = []
    for rid in order:
        ref = refs.get(rid)
        if ref is None:
            continue
        meta = getattr(ref, "meta", None) or {}
        title = (getattr(ref, "title", None) or "").split("\n", 1)[0]
        rows.append(
            {
                "handle": format_handle(ref.kind, ref.id) or f"paper:{ref.id}",
                "title": title,
                "authors": _authors_str(ref),
                "year": str(getattr(ref, "year", None) or meta.get("year") or ""),
                "venue": str(meta.get("journal") or ""),
            }
        )
    return rows


def _paper_console_search(
    request: Request, payload: dict[str, Any]
) -> tuple[str, bool] | None:
    """Console-only override: ``search(kind='paper'/'pa', q=...)`` shows
    one row per paper (title/authors/year/venue), not the MCP-facing
    chunk-level hit table.

    ``PaperHandler.search`` deliberately renders ``{handle,
    chunk_keywords}`` per *chunk* hit — the right shape for an agent,
    which wants the drill-in handle and would waste tokens re-reading
    the same title on every hit of a multi-chunk paper (it already has
    ``get(id=...)`` for that). An operator scanning the console instead
    wants to *recognise* the paper at a glance, so this collapses the
    same ranked hits to one row per ref and renders bibliographic
    fields — reusing the handler's structured ``search_hits()`` (the
    engine ``search()`` itself calls, built for cross-kind fusion) and
    the Papers tab's author-formatting helper, rather than a new search
    path. Scoped to the console: the MCP ``search`` verb's own render is
    untouched — the bigger ``unique_per='paper'`` MCP-default change is
    a separate, already-tracked backlog item.

    Returns ``None`` — "not applicable, dispatch normally" — for every
    shape this override doesn't cover: a different kind, no ``q=``,
    ``title=``/``author=``/``good=True`` (those already return paper-
    level output through the ordinary handler path), or any search
    param this override doesn't forward to ``search_hits()`` (``scope``,
    ``source``, ``angle``, ``n``, ``like``, ``view``, ``status``,
    ``queries``, ``answers``, ``per_paper``, ``folder``, ``sort``,
    ``since``, ``until``) — forwarding only a subset while silently
    dropping the rest would return a different result set than the real
    MCP ``search(kind='paper', ...)`` call with no indication anything
    was ignored, so any of those present bails to normal dispatch
    instead.
    """
    kind = str(payload.get("kind") or "").strip().lower()
    if kind not in _PAPER_KIND_SPELLINGS:
        return None
    if payload.get("title") or payload.get("author") or payload.get("good"):
        return None
    if any(payload.get(k) for k in _PAPER_SEARCH_UNSUPPORTED_PARAMS):
        return None
    q = str(payload.get("q") or "").strip()
    if not q:
        return None

    try:
        store = get_store(request)
        handler = get_runtime(request).hub.handler_for("paper")
        raw_page_size = payload.get("page_size") or 10
        page_size = max(1, min(int(raw_page_size), 50))
        hits = handler.search_hits(
            q=q,
            tags=payload.get("tags"),
            # Over-fetch chunk hits so collapsing to distinct papers
            # still fills a full page — several hits often land on the
            # same paper.
            page_size=max(page_size * 5, 50),
            exclude=payload.get("exclude"),
            mode=payload.get("mode"),
        )
        rows = _paper_console_rows(store, hits, page_size)
    except (
        Exception
    ) as exc:  # pragma: no cover - defensive, mirrors dispatch's catch-all
        return f"[error] {exc}", True

    if not rows:
        return f"no paper matches {q!r}", False
    head = format_search_headline(
        n_returned=len(rows), total=len(rows), noun="paper", query=q
    )
    table_text = render_agent_table(
        rows, schema=["handle", "title", "authors", "year", "venue"]
    )
    body = (
        f"{head}\n\n{table_text}\n\n"
        "(console view — one row per paper; MCP search(kind='paper') "
        "returns chunk-level hits, by design — see precis-search-help)"
    )
    return body, False


async def _run_verb(request: Request, verb: str, args_text: str) -> tuple[Any, bool]:
    """Parse + dispatch one verb call; return ``(result, is_error)``.

    Shared by the POST ``/run`` form and the GET deep-link so both
    honour the same arg-parse / error-surfacing path.
    """
    try:
        payload = _parse_args(verb, args_text)
    except ValueError as exc:
        return f"[input error] {exc}", True
    if verb == "search":
        override = await asyncio.to_thread(_paper_console_search, request, payload)
        if override is not None:
            return override
    return await await_dispatch(request, verb, payload)


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Render the console — optionally prepopulated / pre-run.

    A bare ``/console`` lands on a blank form. A deep-link carrying
    ``?verb=&args_text=`` query params prefills the verb form, and —
    for read-only verbs (:data:`_GET_RUNNABLE_VERBS`) — runs the call
    and renders the result, so a single shareable URL like

        /console?verb=search&args_text=kind%3Dpaper+q%3Dtransformer

    lands on a filled-in, already-run query. ``args_text`` absent ⇒
    blank landing; present (even empty) ⇒ prepopulate.
    """
    verb = request.query_params.get("verb", "search")
    args_text = request.query_params.get("args_text")
    if args_text is None:
        return templates.TemplateResponse(request, "console.html.j2", _quick_context())
    result: Any = None
    is_error = False
    if verb in _GET_RUNNABLE_VERBS:
        result, is_error = await _run_verb(request, verb, args_text)
    return templates.TemplateResponse(
        request,
        "console.html.j2",
        _quick_context(
            verb=verb, args_text=args_text, result=result, is_error=is_error
        ),
    )


@router.post("/run", response_class=HTMLResponse)
async def run(
    request: Request,
    verb: str = Form(...),
    args_text: str = Form(""),
) -> HTMLResponse:
    """Execute one verb call and render its output."""
    result, is_error = await _run_verb(request, verb, args_text)
    return templates.TemplateResponse(
        request,
        "console.html.j2",
        _quick_context(
            verb=verb, args_text=args_text, result=result, is_error=is_error
        ),
    )


@router.post("/resolve", response_model=None)
async def resolve(
    request: Request,
    handle: str = Form(""),
) -> HTMLResponse | RedirectResponse:
    """Smart-resolve a pasted handle.

    Accepts: universal handles (``pa5``, ``pc579575`` — ADR 0036),
    ``paper:slug``, ``kind:id``, bare cite_keys (``charlier07~374``),
    DOIs (``10.1234/foo``), arXiv ids (``2501.01234``), YouTube ids
    (``dQw4w9WgXcQ``), bare discord handles. Redirects to the canonical
    view via the ``/r/{kind}/{id}`` resolver; falls back to a cross-kind
    search when the handle shape isn't recognised so the operator
    always lands somewhere useful.
    """
    # Universal handles (``pa5`` record / ``pc579575`` chunk) resolve
    # against the DB first: they look like bare cite_keys to the shape
    # patterns (``pa55`` matches the paper_cite regex), so they must win
    # before ``_smart_resolve`` mis-routes them to ``/r/paper/pa55``. A
    # chunk handle carries the chunk's ord, which the ``/r/`` resolver
    # turns into the ``?chunk=`` surface (a cited-passage card for papers).
    resolved = get_store(request).resolve_handle(handle.strip())
    if resolved is not None:
        url = f"/r/{resolved.kind}/{resolved.ref_id}"
        if resolved.chunk_ord is not None:
            url += f"?chunk={resolved.chunk_ord}"
        return RedirectResponse(url=url, status_code=303)
    target = _smart_resolve(handle)
    if target is not None:
        return RedirectResponse(url=target, status_code=303)
    if handle.strip():
        from urllib.parse import quote_plus

        return RedirectResponse(
            url=f"/refs?q={quote_plus(handle.strip())}&all=1",
            status_code=303,
        )
    return templates.TemplateResponse(
        request,
        "console.html.j2",
        _quick_context(),
    )


@router.post("/quick", response_class=HTMLResponse)
async def quick(
    request: Request,
    service: str = Form(...),
    mode: str = Form(...),
    query: str = Form(""),
) -> HTMLResponse:
    """Assemble + dispatch a shortcut call.

    ``service`` is one of :data:`QUICK_SERVICES` ``value`` entries;
    ``mode`` is ``"online"`` (→ ``get``) or ``"cache"`` (→ ``search``);
    ``query`` is the user's question / video id / expression.

    The assembled call is rendered back into the form as a
    ``quick_call`` breadcrumb so the operator can verify what ran (and
    copy/edit it into the main verb form for further runs).
    """
    spec = _QUICK_BY_VALUE.get(service)
    if spec is None:
        return templates.TemplateResponse(
            request,
            "console.html.j2",
            _quick_context(
                quick_service=service,
                quick_mode=mode,
                quick_query=query,
                result=f"[input error] unknown service {service!r}",
                is_error=True,
            ),
        )
    query = (query or "").strip()
    if not query:
        return templates.TemplateResponse(
            request,
            "console.html.j2",
            _quick_context(
                quick_service=service,
                quick_mode=mode,
                quick_query=query,
                result="[input error] query is required",
                is_error=True,
            ),
        )

    kind = spec["kind"]
    # ``online`` → get(id=...) hits upstream (or returns a cache hit
    # for the same key). ``cache`` → search(q=...) finds prior cached
    # refs by query text. The arg-name flip is intentional — get is
    # keyed; search is full-text.
    if mode == "cache":
        verb = "search"
        payload: dict[str, Any] = {"kind": kind, "q": query}
        quick_call = f"search(kind={kind!r}, q={query!r})"
    else:
        verb = "get"
        payload = {"kind": kind, "id": query}
        quick_call = f"get(kind={kind!r}, id={query!r})"
    result, is_error = await await_dispatch(request, verb, payload)
    return templates.TemplateResponse(
        request,
        "console.html.j2",
        _quick_context(
            verb=verb,
            args_text=f"kind={kind} {'q' if mode == 'cache' else 'id'}={shlex.quote(query)}",
            result=result,
            is_error=is_error,
            quick_service=service,
            quick_mode=mode,
            quick_query=query,
            quick_call=quick_call,
        ),
    )
