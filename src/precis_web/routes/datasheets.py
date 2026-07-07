"""``/datasheets`` — component-datasheet reader (ADR 0042 §7).

A ``datasheet`` is an *evidence-role* sibling of ``paper`` (same Marker →
chunks ingest + two-pane reader), so this router is deliberately thin: it
reuses the paper reader's machinery from :mod:`precis_web.routes.papers`
rather than duplicating it — exactly as ``/cfp`` does.

* ``GET /datasheets/{ident}`` — the two-pane reader. Resolves the datasheet
  ref, then delegates to ``papers._render_detail``. The detail template
  drives its sidebar fetches (search / toc / chunk / pdf) off the ref id
  against the ``/papers/{ref_id}/…`` endpoints, which accept the document
  family (``paper`` + ``cfp`` + ``pres`` + ``datasheet``) — so the reader,
  vendored pdf.js viewer, and in-document search all work unchanged.

There is intentionally **no** ``/datasheets`` list page or nav tab (like
``/pres``): a datasheet is reached from the part it documents, from Drive,
or from cross-kind search — never browsed as its own literature list. It is
scoped out of ``/papers`` (``corpus_role='evidence'`` but a distinct kind)
so component docs never mix into the academic corpus or vice-versa.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from precis.errors import NotFound
from precis_web.deps import get_store
from precis_web.routes.papers import _render_detail, _resolve_paper

router = APIRouter(prefix="/datasheets", tags=["datasheets"])


@router.get("/{ident}", response_class=HTMLResponse, response_model=None)
async def detail(
    request: Request, ident: str, tab: str = ""
) -> HTMLResponse | RedirectResponse:
    """Datasheet detail: the two-pane reader, reusing the paper renderer."""
    store = get_store(request)
    ref = _resolve_paper(store, ident, kinds=("datasheet",))
    if ref is None:
        raise NotFound(f"datasheet {ident!r} not found")
    if ident.isdigit() and ref.slug:
        target = f"/datasheets/{ref.slug}"
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(url=target, status_code=301)
    return _render_detail(request, ref, initial_tab=tab.strip().capitalize())
