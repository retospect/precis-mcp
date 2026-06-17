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

import shlex
from typing import Any

import re

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from precis.tools import TOOL_REGISTRY, get_tool_names
from precis.tools.cli_adapter import _convert_value
from precis_web.deps import await_dispatch, templates

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
]

_QUICK_BY_VALUE: dict[str, dict[str, str]] = {s["value"]: s for s in QUICK_SERVICES}


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

    Raises ``ValueError`` on a malformed token or an unknown arg —
    surfaced to the user as an inline message rather than a 500.
    """
    if verb not in TOOL_REGISTRY:
        raise ValueError(f"unknown verb {verb!r}")
    params = TOOL_REGISTRY[verb]["parameters"]
    payload: dict[str, Any] = {}
    for tok in shlex.split(args_text or ""):
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
    }
    ctx.update(overrides)
    return ctx


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "console.html.j2",
        _quick_context(),
    )


@router.post("/run", response_class=HTMLResponse)
async def run(
    request: Request,
    verb: str = Form(...),
    args_text: str = Form(""),
) -> HTMLResponse:
    """Execute one verb call and render its output."""
    is_error = False
    try:
        payload = _parse_args(verb, args_text)
    except ValueError as exc:
        result, is_error = f"[input error] {exc}", True
    else:
        result, is_error = await await_dispatch(request, verb, payload)
    return templates.TemplateResponse(
        request,
        "console.html.j2",
        _quick_context(verb=verb, args_text=args_text, result=result, is_error=is_error),
    )


@router.post("/resolve", response_model=None)
async def resolve(
    request: Request,
    handle: str = Form(""),
) -> HTMLResponse | RedirectResponse:
    """Smart-resolve a pasted handle.

    Accepts: ``paper:slug``, ``kind:id``, bare cite_keys (``charlier07~374``),
    DOIs (``10.1234/foo``), arXiv ids (``2501.01234``), YouTube ids
    (``dQw4w9WgXcQ``), bare discord handles. Redirects to the canonical
    view via the ``/r/{kind}/{id}`` resolver; falls back to a cross-kind
    search when the handle shape isn't recognised so the operator
    always lands somewhere useful.
    """
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
