"""``/datasheets`` — component-datasheet reader (ADR 0042 §7).

A ``datasheet`` is an *evidence-role* sibling of ``paper`` (same Marker →
chunks ingest + two-pane reader), so this router is deliberately thin: it
reuses the paper reader's machinery from :mod:`precis_web.routes.papers`
rather than duplicating it — exactly as ``/cfp`` does. The one thing it does
*not* reuse is the bibliographic Meta panel: a datasheet is described by its
**vendor**, **sub-type** (datasheet / app-note / errata / reference-manual)
and the **part** it documents, not by authors + DOI. So the reader plugs in
its own ``datasheets/_meta_panel.html.j2`` (via ``_render_detail``'s override
hook) and persists those three fields through the datasheet ``edit`` verb.
Those same meta fields flow into the bibliography + docx reference line
(``export.latex`` / ``export.docx``).

* ``GET /datasheets/{ident}`` — the two-pane reader (paper renderer + the
  datasheet Meta panel). The sidebar fetches (search / toc / chunk / pdf) hit
  the ``/papers/{ref_id}/…`` endpoints, which accept the document family
  (``paper`` + ``cfp`` + ``pres`` + ``datasheet``).
* ``POST /datasheets/{ref_id}/edit`` — persist vendor / sub-type / part.

There is intentionally **no** ``/datasheets`` list page or nav tab (like
``/pres``): a datasheet is reached from the part it documents, from Drive, or
from cross-kind search.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from precis.errors import NotFound
from precis.export.latex import _DATASHEET_SUBTYPE_LABELS, datasheet_pub_label
from precis_web.deps import await_dispatch, get_store, templates
from precis_web.routes.papers import _render_detail, _resolve_paper

router = APIRouter(prefix="/datasheets", tags=["datasheets"])


def _ds_panel_ctx(ref: Any) -> dict[str, Any]:
    """Template vars for the datasheet Meta panel, read from ``meta``."""
    meta = ref.meta or {}
    return {
        "ds": {
            "vendor": str(meta.get("vendor") or ""),
            "subtype": str(meta.get("subtype") or "datasheet"),
            "subtype_label": datasheet_pub_label(meta),
            "part_lcsc": str(meta.get("part_lcsc") or ""),
        },
        # (value, label) options for the sub-type <select>, default first.
        "ds_subtypes": list(_DATASHEET_SUBTYPE_LABELS.items()),
    }


@router.get("/{ident}", response_class=HTMLResponse, response_model=None)
async def detail(
    request: Request, ident: str, tab: str = ""
) -> HTMLResponse | RedirectResponse:
    """Datasheet detail: the two-pane reader with the datasheet Meta panel."""
    store = get_store(request)
    ref = _resolve_paper(store, ident, kinds=("datasheet",))
    if ref is None:
        raise NotFound(f"datasheet {ident!r} not found")
    if ident.isdigit() and ref.slug:
        target = f"/datasheets/{ref.slug}"
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(url=target, status_code=301)
    return _render_detail(
        request,
        ref,
        initial_tab=tab.strip().capitalize(),
        meta_panel="datasheets/_meta_panel.html.j2",
        list_url="/drive",
        list_label="drive",
        extra=_ds_panel_ctx(ref),
    )


@router.post("/{ref_id}/edit", response_model=None)
async def edit(
    request: Request,
    ref_id: int,
    title: str = Form(""),
    vendor: str = Form(""),
    subtype: str = Form("datasheet"),
    part_lcsc: str = Form(""),
) -> RedirectResponse | HTMLResponse:
    """Persist the datasheet's vendor / sub-type / part (and title) via the
    ``edit`` verb, then redirect back to the reader."""
    store = get_store(request)
    ref = store.fetch_refs_by_ids([ref_id], include_deleted=False).get(ref_id)
    if ref is None or ref.kind != "datasheet":
        raise NotFound(f"datasheet id={ref_id} not found")
    slug = ref.slug or str(ref_id)
    payload: dict[str, Any] = {
        "kind": "datasheet",
        "id": slug,
        "vendor": vendor,
        "subtype": subtype,
        "part_lcsc": part_lcsc,
    }
    # Only send title when non-blank — the paper editor treats blank as "keep".
    if title.strip():
        payload["title"] = title
    body, is_error = await await_dispatch(request, "edit", payload)
    if is_error:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {"title": "Edit error", "detail": body, "status": 400},
            status_code=400,
        )
    return RedirectResponse(url=f"/datasheets/{slug}", status_code=303)
