"""Console tab — interactive precis-query over the seven verbs.

A thin web mirror of ``precis repl``: pick a verb, type
``key=value`` arguments (shlex-split, quote values with spaces), and
the call runs through the same in-process runtime the MCP server and
CLI use. Arg types are coerced via the shared ``TOOL_REGISTRY`` so
ints / bools / lists behave the same as on the CLI.

Read-only by habit but not by enforcement: every verb is reachable
(including ``put`` / ``delete``), exactly like the REPL. The web
process runs as ``web:owner`` (owner), so tree guards treat it as the owner.
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


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "console.html.j2",
        {
            "active_tab": "console",
            "verbs": list(get_tool_names()),
            "verb": "search",
            "args_text": "",
            "result": None,
            "is_error": False,
        },
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
        {
            "active_tab": "console",
            "verbs": list(get_tool_names()),
            "verb": verb,
            "args_text": args_text,
            "result": result,
            "is_error": is_error,
        },
    )
