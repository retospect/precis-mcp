"""Inline ``kind:ref`` preview + click-through router.

Two surfaces:

* ``GET /preview/{kind}/{id}`` — small HTML fragment used by the
  linkifier's htmx hover popover. Returns kind chip + title + body
  snippet. 400 / 404 stubs render gracefully inside the popover.
* ``GET /r/{kind}/{id}`` — click-target redirector. Resolves the
  ref's canonical address and 303s to its native view
  (``/papers/{ref_id}`` for paper, ``/tasks?focus={ref_id}`` for
  todo, generic ``/refs/{kind}/{ref_id}`` for everything else). For
  paper refs with a ``?chunk=…`` suffix, the resolver translates the
  chunk address into a PDF page and lands on
  ``/papers/{ref_id}/pdf#page=N``.

The redirector also accepts ``chunk=pN`` directly — that's the
"linkify already knew the PDF page" shortcut, no chunks-table lookup
needed.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from precis_web.deps import get_store, templates

router = APIRouter(tags=["preview"])


_NUMERIC_KINDS: frozenset[str] = frozenset(
    {
        "memory",
        "todo",
        "job",
        "gripe",
        "finding",
        "citation",
        "flashcard",
        "cron",
        "message",
    }
)

#: Per-kind URL shapes for the click-through redirector. Slug or id
#: substituted via ``{id}``. The match order is unimportant — falls
#: back to ``/refs/{kind}/{id}`` for unlisted kinds.
_NATIVE_URL: dict[str, str] = {
    "paper": "/papers/{id}",
    "todo": "/tasks?focus={id}",
    "job": "/tasks?focus={id}",
    "patent": "/refs/patent/{id}",
    "memory": "/refs/memory/{id}",
    "conv": "/refs/conv/{id}",
    "oracle": "/refs/oracle/{id}",
    "gripe": "/refs/gripe/{id}",
    "pres": "/refs/pres/{id}",
}

#: ``~chunk`` suffix variants the resolver understands. Anything else
#: is ignored and the redirector lands on the ref overview.
_PAGE_RE = re.compile(r"^p(?P<page>\d+)$")
_CHUNK_RE = re.compile(r"^(?P<from>\d+)(?:\.\.(?P<to>\d+))?$")


def _resolve_ref_id(store: Any, kind: str, raw_id: str) -> int | None:
    """Map a ``kind:id`` pair to the numeric ``refs.ref_id``.

    Numeric kinds accept the id directly; slug kinds route through the
    ``ref_identifiers`` lookup (slug stored as ``id_kind='cite_key'``).
    Returns ``None`` when the ref isn't found.
    """
    raw_id = raw_id.lstrip("#")
    if kind in _NUMERIC_KINDS:
        try:
            return int(raw_id)
        except ValueError:
            return None
    # Slug kind.
    with store.pool.connection() as conn:  # type: ignore[attr-defined]
        row = conn.execute(
            "SELECT ref_id FROM ref_identifiers "
            "WHERE id_kind = 'cite_key' AND id_value = %s",
            (raw_id,),
        ).fetchone()
    if row is None:
        return None
    return int(row[0])


def _chunk_to_page(store: Any, ref_id: int, ord_pos: int) -> int | None:
    """Look up ``page_first`` for a chunk at ``ord=ord_pos`` on ``ref_id``."""
    with store.pool.connection() as conn:  # type: ignore[attr-defined]
        row = conn.execute(
            "SELECT page_first FROM chunks WHERE ref_id = %s AND ord = %s",
            (ref_id, ord_pos),
        ).fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0])


@router.get("/preview/{kind}/{ref_id}", response_class=HTMLResponse)
async def preview(request: Request, kind: str, ref_id: str) -> HTMLResponse:
    """Render the small popover fragment for a ``kind:ref`` mention.

    Cheap path: resolve the ref, fetch ``title`` and a short body
    excerpt. 404 / unknown-kind paths render a stub rather than
    bouncing — the popover that already opened should say *something*
    on hover. Errors aren't agent-actionable here.
    """
    store = get_store(request)
    numeric_id = _resolve_ref_id(store, kind, ref_id)
    if numeric_id is None:
        return templates.TemplateResponse(
            request,
            "preview/popover.html.j2",
            {
                "kind": kind,
                "label": f"{kind}:{ref_id}",
                "missing": True,
            },
        )
    with store.pool.connection() as conn:  # type: ignore[attr-defined]
        row = conn.execute(
            "SELECT title, deleted_at IS NOT NULL FROM refs WHERE ref_id = %s",
            (numeric_id,),
        ).fetchone()
        body_row = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND ord >= 0 "
            "ORDER BY ord LIMIT 1",
            (numeric_id,),
        ).fetchone()
    if row is None:
        return templates.TemplateResponse(
            request,
            "preview/popover.html.j2",
            {
                "kind": kind,
                "label": f"{kind}:{ref_id}",
                "missing": True,
            },
        )
    title = (row[0] or "").split("\n", 1)[0]
    if len(title) > 100:
        title = title[:100].rstrip() + "…"
    body_preview = ""
    if body_row and body_row[0]:
        flat = " ".join(body_row[0].split())
        body_preview = flat[:200] + ("…" if len(flat) > 200 else "")
    return templates.TemplateResponse(
        request,
        "preview/popover.html.j2",
        {
            "kind": kind,
            "label": f"{kind}:{ref_id}",
            "ref_id": numeric_id,
            "title": title or "(untitled)",
            "body_preview": body_preview,
            "deleted": bool(row[1]),
            "missing": False,
        },
    )


@router.get("/r/{kind}/{ref_id}")
async def resolve(
    request: Request,
    kind: str,
    ref_id: str,
    chunk: str | None = None,
) -> RedirectResponse:
    """Resolve a ``kind:ref`` click target to its native view.

    Paper refs with a chunk suffix translate into PDF page jumps:

    * ``?chunk=pN`` (linkifier passed a literal page) → ``#page=N`` on
      the embedded PDF viewer URL.
    * ``?chunk=N`` or ``?chunk=N..M`` → look up ``page_first`` for
      chunk ord=N and use that as the PDF page.

    Other kinds ignore the chunk suffix; the click lands on the ref's
    overview page.
    """
    store = get_store(request)
    numeric_id = _resolve_ref_id(store, kind, ref_id)
    if numeric_id is None:
        raise HTTPException(
            status_code=404,
            detail=f"no such {kind}:{ref_id}",
        )

    # Paper + page/chunk address → jump to the PDF viewer at a page.
    if kind == "paper" and chunk:
        page = _page_from_chunk_suffix(store, numeric_id, chunk)
        if page is not None:
            return RedirectResponse(
                url=f"/papers/{numeric_id}/pdf#page={page}",
                status_code=303,
            )

    template = _NATIVE_URL.get(kind, "/refs/{kind}/{id}")
    target = template.format(kind=kind, id=numeric_id)
    return RedirectResponse(url=target, status_code=303)


def _page_from_chunk_suffix(store: Any, ref_id: int, suffix: str) -> int | None:
    """Translate a ``~chunk`` suffix into a PDF page number, or None."""
    m_page = _PAGE_RE.match(suffix)
    if m_page is not None:
        try:
            return int(m_page.group("page"))
        except ValueError:
            return None
    m_chunk = _CHUNK_RE.match(suffix)
    if m_chunk is None:
        return None
    try:
        ord_pos = int(m_chunk.group("from"))
    except ValueError:
        return None
    return _chunk_to_page(store, ref_id, ord_pos)
