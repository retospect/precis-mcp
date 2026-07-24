"""``/cfp`` — call-for-proposal / requirements reader (proposal writing).

A ``cfp`` is a spec-role sibling of ``paper`` (same ingest + two-pane
reader), so this router is deliberately thin: it reuses the paper
reader's machinery from :mod:`precis_web.routes.papers` rather than
duplicating it.

* ``GET /cfp`` — retired into the unified Drive surface (WS1b): redirects
  to the ``kind=cfp`` facet preset. The recent-CFP *list* this used to
  render (reusing the paper index template) folds there; nothing else
  read it.
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
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from precis.errors import NotFound
from precis_web.deps import get_store
from precis_web.routes.papers import _render_detail, _resolve_paper

router = APIRouter(prefix="/cfp", tags=["cfp"])


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    """Retired into the unified Drive surface (WS1b) — redirects to the
    ``kind=cfp`` facet preset."""
    return RedirectResponse(url="/drive?k=cfp&submitted=1")


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
