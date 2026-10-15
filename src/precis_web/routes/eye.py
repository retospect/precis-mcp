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
* ``GET /eye/{handle}?extent=<rung>&q=<kind:label>&kind=<csv>&space=<csv>``
  — the focus. The rung defaults to ``fisheye+1hop`` (the neighbourhood,
  which is the point of the page); the ladder is a row of links; ``q=``
  expands one second-hop group at ``fisheye+2hop``, as on the MCP door.
  ``kind=``/``space=`` narrow every ring — by neighbour kind and the
  ``SPACE:`` tag axis (``fisheye-everywhere.md`` in-scope 1), parsed by
  the same :mod:`precis_web.graph_filters` the ``/graph`` pages read —
  the URL is the state and the chips above the body reflect it.
"""

from __future__ import annotations

import html
import re
import time
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from precis.handlers._eye import EYE_LADDER, is_eye_view
from precis.utils import handle_registry
from precis.utils.eye_render import _TREE_KINDS
from precis_web.deps import await_dispatch, get_store, templates, timed_response
from precis_web.graph_filters import parse_kind_filter, parse_space_filter
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

#: A ``fisheye+2hop`` count line as rendered (already HTML-escaped) by
#: ``eye_render._second_hop``: "  12 paper via cites". The label may read
#: ``&lt;-rel`` for an inbound edge with no inverse name.
_SECOND_HOP_LINE = re.compile(r"(?m)^  (\u2265?\d+) ([a-z][a-z0-9-]*) via (\S+)[ \t]*$")


def _eye_href(
    handle: str,
    extent: str = DEFAULT_EXTENT,
    *,
    kinds: frozenset[str] | None = None,
    spaces: frozenset[str] | None = None,
) -> str:
    href = f"/eye/{quote(handle, safe=':')}?extent={quote(extent, safe='')}"
    if kinds:
        href += "&kind=" + quote(",".join(sorted(kinds)), safe="")
    if spaces:
        href += "&space=" + quote(",".join(sorted(spaces)), safe="")
    return href


def linkify_handles(
    text: str,
    *,
    extent: str = DEFAULT_EXTENT,
    kinds: frozenset[str] | None = None,
    spaces: frozenset[str] | None = None,
) -> str:
    """HTML-escape ``text`` and turn every handle it names into a link to
    that handle's focus page at ``extent`` — the active ring filter
    (``kinds``/``spaces``) rides the URL so a click keeps the narrowed
    view. Pure text transform (no store): a handle that does not parse —
    ``ab12`` with no such code — is left as text, so a false positive
    costs a plain word, never a dead link."""

    def _record(m: re.Match[str]) -> str:
        h = m.group(1)
        if handle_registry.parse(h) is None:
            return h
        href = _eye_href(h, extent, kinds=kinds, spaces=spaces)
        return f'<a class="eye-handle" href="{href}">{h}</a>'

    def _skill(m: re.Match[str]) -> str:
        h = m.group(0)
        href = _eye_href(h, extent, kinds=kinds, spaces=spaces)
        return f'<a class="eye-handle" href="{href}">{h}</a>'

    escaped = html.escape(text, quote=False)
    return _SKILL_IN_TEXT.sub(_skill, _HANDLE_IN_TEXT.sub(_record, escaped))


def _tree_root_handle(request: Request, target: dict[str, Any]) -> str | None:
    """For a whole-draft/plan record handle (``dr<id>`` / ``po<id>``) — no
    eye of its own, the tree kind focuses one section — return the root
    section's chunk handle so ``/eye/dr<id>`` lands on it instead of
    dead-ending on the MCP ``Unsupported`` (gr489478). ``None`` for a
    non-tree target, a dead ref or an empty tree (the dispatch error page
    then says what happened)."""
    parsed = handle_registry.parse(str(target.get("id") or ""))
    if parsed is None or parsed[1] or parsed[0] not in _TREE_KINDS:
        return None
    kind, _is_chunk, pk = parsed
    ref = get_store(request).get_ref(kind=kind, id=pk)
    if ref is None:
        return None
    order = get_store(request).drafts.reading_order(int(ref.id), kind=kind)
    if not order:
        return None
    return handle_registry.format_handle(kind, int(order[0].chunk_id), chunk=True)


def _linkify_second_hop(
    body_html: str,
    handle: str,
    extent: str,
    *,
    kinds: frozenset[str] | None = None,
    spaces: frozenset[str] | None = None,
) -> str:
    """Make each ``fisheye+2hop`` count line a link to that group's
    expansion — ``?q=<kind>:<label>`` on the same rung, carrying the
    active ring filter (gr489577). Other rungs render no count lines, so
    the pass is a no-op off ``fisheye+2hop`` anyway; gating on it keeps
    the intent legible."""
    base = extent[: -len("+recall")] if extent.endswith("+recall") else extent
    if base != "fisheye+2hop":
        return body_html

    def _sub(m: re.Match[str]) -> str:
        kind, label = m.group(2), html.unescape(m.group(3))
        href = _eye_href(handle, extent, kinds=kinds, spaces=spaces)
        href += "&q=" + quote(f"{kind}:{label}", safe="")
        return f'  <a class="eye-handle" href="{href}">{m.group(0).strip()}</a>'

    return _SECOND_HOP_LINE.sub(_sub, body_html)


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


def _ladder(
    handle: str,
    extent: str,
    *,
    kinds: frozenset[str] | None = None,
    spaces: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    base = extent[: -len("+recall")] if extent.endswith("+recall") else extent
    recall = extent != base
    rungs = [
        {
            "label": r,
            "href": _eye_href(
                handle,
                r + ("+recall" if recall else ""),
                kinds=kinds,
                spaces=spaces,
            ),
            "on": r == base,
        }
        for r in EYE_LADDER
    ]
    rungs.append(
        {
            "label": "+recall",
            "href": _eye_href(
                handle,
                base if recall else f"{base}+recall",
                kinds=kinds,
                spaces=spaces,
            ),
            "on": recall,
        }
    )
    return rungs


def _filter_chips(
    handle: str,
    extent: str,
    *,
    kinds: frozenset[str] | None,
    spaces: frozenset[str] | None,
) -> list[dict[str, str]]:
    """One chip per active ring-filter value, each a link to the same page
    minus that value (the URL is the state, the chips reflect it). A
    trailing ``clear`` chip drops every axis when more than one is on."""
    on_kinds = kinds or frozenset()
    on_spaces = spaces or frozenset()
    chips = [
        {
            "label": f"kind:{k}",
            "href": _eye_href(
                handle, extent, kinds=on_kinds - {k} or None, spaces=spaces
            ),
        }
        for k in sorted(on_kinds)
    ]
    chips += [
        {
            "label": f"space:{s}",
            "href": _eye_href(
                handle, extent, kinds=kinds, spaces=on_spaces - {s} or None
            ),
        }
        for s in sorted(on_spaces)
    ]
    if len(chips) > 1:
        chips.append({"label": "clear", "href": _eye_href(handle, extent)})
    return chips


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
    kind: str = "",
    space: str = "",
) -> Any:
    """The focus page for one handle at one rung, optionally narrowed by
    ``kind=``/``space=`` ring filters."""
    t0 = time.monotonic()
    target = _handle_target(handle)
    if target is None:
        return timed_response(
            request,
            templates.TemplateResponse(
                request,
                "error.html.j2",
                {
                    "title": "Not a handle",
                    "status": 404,
                    "detail": f"{handle!r} is not a universal handle (me4641, pc13234, sk:<slug>)",
                },
                status_code=404,
            ),
            t0,
        )
    extent = extent.strip() or DEFAULT_EXTENT
    if not is_eye_view(extent):
        return timed_response(
            request,
            templates.TemplateResponse(
                request,
                "error.html.j2",
                {
                    "title": "Unknown rung",
                    "status": 400,
                    "detail": f"extent={extent!r}; the ladder is {', '.join(EYE_LADDER)}, "
                    "optionally +recall",
                },
                status_code=400,
            ),
            t0,
        )
    try:
        kinds = parse_kind_filter(kind)
        spaces = parse_space_filter(space)
    except ValueError as e:
        return timed_response(
            request,
            templates.TemplateResponse(
                request,
                "error.html.j2",
                {"title": "Bad ring filter", "status": 400, "detail": str(e)},
                status_code=400,
            ),
            t0,
        )
    root = _tree_root_handle(request, target)
    if root is not None:
        href = _eye_href(root, extent, kinds=kinds, spaces=spaces)
        if q:
            href += "&q=" + quote(q, safe="")
        return timed_response(request, RedirectResponse(href, status_code=303), t0)
    args: dict[str, Any] = {**target, "extent": extent}
    if q:
        args["q"] = q
    ring: dict[str, list[str]] = {}
    if kinds:
        ring["kinds"] = sorted(kinds)
    if spaces:
        ring["space"] = sorted(spaces)
    if ring:
        args["ring"] = ring
    body, is_error = await await_dispatch(request, "get", args)
    status = 200
    if is_error:
        first = body.splitlines()[0] if body else ""
        status = 404 if "[error:NotFound]" in first or "[error:Gone]" in first else 200
    canonical = handle.strip()
    return timed_response(
        request,
        templates.TemplateResponse(
            request,
            "eye/focus.html.j2",
            {
                "handle": canonical,
                "extent": extent,
                "q": q or "",
                "body": _linkify_second_hop(
                    linkify_handles(body, extent=extent, kinds=kinds, spaces=spaces),
                    canonical,
                    extent,
                    kinds=kinds,
                    spaces=spaces,
                ),
                "is_error": is_error,
                "ladder": _ladder(canonical, extent, kinds=kinds, spaces=spaces),
                "chips": _filter_chips(canonical, extent, kinds=kinds, spaces=spaces),
                "ring": ring or None,
                "reader_url": _reader_url(canonical),
            },
            status_code=status,
        ),
        t0,
    )
