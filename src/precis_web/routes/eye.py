"""The focus page — ``/eye/<handle>``: the fisheye ladder in the browser.

``docs/backlog/fisheye-everywhere.md`` in-scope 4. Renders the same text
the MCP ``get(id=<handle>, extent=<rung>)`` returns, through the same
dispatcher (so every kind the ladder covers — and every refusal — is the
agent's, not a second renderer), with every handle in it linked to its own
focus page. It renders, it does not act: the ``focus`` verb and the
render→act loop stay in ``fisheye-level2.md``; the picture (radial SVG,
``/graph/<kind>/<id>``) is ``web-graph-navigation.md``'s and the two
cross-link.

* ``GET /eye/`` — a box that takes a handle (``me4641``, ``pc13234``,
  ``sk:precis-get-help``) or a query. A handle redirects to its focus; a
  query runs the cross-kind ``search`` and shows the hits with their
  handles linked, so one click focuses the hit.
* ``GET /eye/{handle}?extent=<rung>&q=<kind:label>`` — the focus. The
  rung defaults to ``fisheye+1hop`` (the neighbourhood, which is the point
  of the page); the ladder is a row of links; ``q=`` expands one
  second-hop group at ``fisheye+2hop``, as on the MCP door.
"""

from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from precis.handlers._eye import EYE_LADDER, is_eye_view
from precis.utils import handle_registry
from precis_web.deps import await_dispatch, templates
from precis_web.ref_urls import ref_url

router = APIRouter(prefix="/eye", tags=["eye"])

#: The rung the page opens on: the neighbourhood is why one comes here.
DEFAULT_EXTENT = "fisheye+1hop"

#: A skill eye's handle form (``handlers.skill``'s slug addressing).
_SKILL_PREFIX = "sk:"

#: A universal record/chunk handle in rendered text (``me4641``,
#: ``pc13234``) — the shape ``handle_registry.parse`` decodes. Word-bounded
#: so ``mx2`` inside ``5mx20`` or a cite key doesn't match.
_HANDLE_IN_TEXT = re.compile(r"(?<![\w:/.-])([a-z]{2}\d+)(?![\w-])")
_SKILL_IN_TEXT = re.compile(r"(?<![\w/])sk:([a-z0-9][a-z0-9-]*)")


def _eye_href(handle: str, extent: str = DEFAULT_EXTENT) -> str:
    return f"/eye/{quote(handle, safe=':')}?extent={quote(extent, safe='')}"


def linkify_handles(text: str, *, extent: str = DEFAULT_EXTENT) -> str:
    """HTML-escape ``text`` and turn every handle it names into a link to
    that handle's focus page at ``extent``. Pure text transform (no store):
    a handle that does not parse — ``ab12`` with no such code — is left as
    text, so a false positive costs a plain word, never a dead link."""

    def _record(m: re.Match[str]) -> str:
        h = m.group(1)
        if handle_registry.parse(h) is None:
            return h
        return f'<a class="eye-handle" href="{_eye_href(h, extent)}">{h}</a>'

    def _skill(m: re.Match[str]) -> str:
        h = m.group(0)
        return f'<a class="eye-handle" href="{_eye_href(h, extent)}">{h}</a>'

    escaped = html.escape(text, quote=False)
    return _SKILL_IN_TEXT.sub(_skill, _HANDLE_IN_TEXT.sub(_record, escaped))


def _handle_target(handle: str) -> dict[str, Any] | None:
    """The ``get`` args that focus ``handle``, or ``None`` when it is not a
    handle at all (a query)."""
    h = handle.strip()
    if h.startswith(_SKILL_PREFIX):
        return {"kind": "skill", "id": h[len(_SKILL_PREFIX) :]}
    normalized = handle_registry.normalize(h)
    if handle_registry.parse(normalized) is not None:
        # kind= from the code so a dead handle is still routed to its kind's
        # eye (NotFound/Gone) rather than falling through to slug inference.
        kind, _is_chunk, _pk = handle_registry.parse(normalized) or ("", False, 0)
        return {"kind": kind, "id": normalized}
    return None


def _reader_url(handle: str) -> str | None:
    """The native reader for a record handle, for the "open" link."""
    parsed = handle_registry.parse(handle)
    if parsed is None or parsed[1]:
        return None
    return ref_url(parsed[0], parsed[2])


def _ladder(handle: str, extent: str) -> list[dict[str, Any]]:
    base = extent[: -len("+recall")] if extent.endswith("+recall") else extent
    recall = extent != base
    rungs = [
        {
            "label": r,
            "href": _eye_href(handle, r + ("+recall" if recall else "")),
            "on": r == base,
        }
        for r in EYE_LADDER
    ]
    rungs.append(
        {
            "label": "+recall",
            "href": _eye_href(handle, base if recall else f"{base}+recall"),
            "on": recall,
        }
    )
    return rungs


@router.get("/", response_class=HTMLResponse)
async def eye_index(request: Request, q: str | None = None) -> Any:
    """The box: a handle redirects to its focus, a query shows hits."""
    hits: str | None = None
    error: str | None = None
    if q and q.strip():
        if _handle_target(q) is not None:
            return RedirectResponse(_eye_href(q.strip()), status_code=303)
        body, is_error = await await_dispatch(request, "search", {"q": q.strip()})
        if is_error:
            error = body
        else:
            hits = linkify_handles(body)
    return templates.TemplateResponse(
        request,
        "eye/index.html.j2",
        {"q": q or "", "hits": hits, "error": error, "ladder": EYE_LADDER},
    )


@router.get("/{handle:path}", response_class=HTMLResponse)
async def eye_focus(
    request: Request,
    handle: str,
    extent: str = DEFAULT_EXTENT,
    q: str | None = None,
) -> Any:
    """The focus page for one handle at one rung."""
    target = _handle_target(handle)
    if target is None:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "title": "Not a handle",
                "status": 404,
                "detail": f"{handle!r} is not a universal handle (me4641, pc13234, sk:<slug>)",
            },
            status_code=404,
        )
    extent = extent.strip() or DEFAULT_EXTENT
    if not is_eye_view(extent):
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "title": "Unknown rung",
                "status": 400,
                "detail": f"extent={extent!r}; the ladder is {', '.join(EYE_LADDER)}, "
                "optionally +recall",
            },
            status_code=400,
        )
    args: dict[str, Any] = {**target, "extent": extent}
    if q:
        args["q"] = q
    body, is_error = await await_dispatch(request, "get", args)
    status = 200
    if is_error:
        first = body.splitlines()[0] if body else ""
        status = 404 if "[error:NotFound]" in first or "[error:Gone]" in first else 200
    canonical = handle.strip()
    return templates.TemplateResponse(
        request,
        "eye/focus.html.j2",
        {
            "handle": canonical,
            "extent": extent,
            "q": q or "",
            "body": linkify_handles(body, extent=extent),
            "is_error": is_error,
            "ladder": _ladder(canonical, extent),
            "reader_url": _reader_url(canonical),
        },
        status_code=status,
    )
