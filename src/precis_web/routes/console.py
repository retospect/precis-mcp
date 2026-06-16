"""Console tab — interactive precis-query over the seven verbs.

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

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

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
