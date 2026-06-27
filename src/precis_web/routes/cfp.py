"""``/cfp`` — call-for-proposal / requirements reader (proposal writing).

A ``cfp`` is a spec-role sibling of ``paper`` (same ingest + two-pane
reader), so this router is deliberately thin: it reuses the paper
reader's machinery from :mod:`precis_web.routes.papers` rather than
duplicating it.

* ``GET /cfp`` — recent-CFP list (reuses the paper index template).
* ``GET /cfp/{ident}`` — the two-pane reader. Resolves the cfp ref, then
  delegates to ``papers._render_detail``. The detail template drives its
  sidebar fetches (search / toc / chunk / pdf) off the ref id against the
  ``/papers/{ref_id}/…`` endpoints, which accept the document family
  (``paper`` + ``cfp``) — so the reader, PDF viewer, and in-document
  search all work unchanged.

The CFP intentionally does **not** appear under ``/papers`` (a different
kind, ``corpus_role='spec'``) so it never mixes into the literature
corpus or gets cited as evidence.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from precis.errors import NotFound
from precis_web.deps import get_store, templates
from precis_web.routes.papers import (
    _PAGE_SIZE,
    _links_from_ids,
    _paper_row,
    _render_detail,
    _resolve_paper,
)

router = APIRouter(prefix="/cfp", tags=["cfp"])


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(request: Request, page: int = 1) -> HTMLResponse:
    """Recent call-for-proposal documents (reuses the paper list view)."""
    store = get_store(request)
    page = max(1, page)
    offset = (page - 1) * _PAGE_SIZE
    refs = store.list_refs(kind="cfp", limit=_PAGE_SIZE + 1, offset=offset)
    has_next = len(refs) > _PAGE_SIZE
    refs = refs[:_PAGE_SIZE]
    rows = [_paper_row(r) for r in refs]
    chunked = store.ref_ids_with_chunks([row["id"] for row in rows])
    for row in rows:
        row["has_chunks"] = row["id"] in chunked
    ids_map = store.identifiers_for_refs([row["id"] for row in rows])
    for row in rows:
        row["links"] = _links_from_ids(ids_map.get(row["id"], {}))
    return templates.TemplateResponse(
        request,
        "papers/index.html.j2",
        {
            "active_tab": "cfp",
            "q": "",
            "has_pdf": False,
            "has_chunks": False,
            "papers": rows,
            "page": page,
            "has_next": has_next,
            "paged": True,
            "list_kind": "cfp",
            "list_title": "Calls for proposal",
        },
    )


@router.get("/{ident}", response_class=HTMLResponse, response_model=None)
async def detail(
    request: Request, ident: str, tab: str = ""
) -> HTMLResponse | RedirectResponse:
    """CFP detail: the two-pane reader, reusing the paper renderer."""
    store = get_store(request)
    ref = _resolve_paper(store, ident, kinds=("cfp",))
    if ref is None:
        raise NotFound(f"cfp {ident!r} not found")
    if ident.isdigit() and ref.slug:
        target = f"/cfp/{ref.slug}"
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(url=target, status_code=301)
    return _render_detail(request, ref, initial_tab=tab.strip().capitalize())
