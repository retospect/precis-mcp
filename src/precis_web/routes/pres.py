"""``/pres`` — slide-deck reader + attribution editor.

A two-pane view (deck body + attribution form left, PDF right) modelled
on the paper reader. Unlike ``paper``/``cfp`` — which are paper-shaped
and delegate straight to ``papers._render_detail`` — a ``pres`` carries
its citation *attribution* in ``meta`` (it has no first-class
``authors``/``year`` columns), so it needs its own metadata form and
persists through the pres ``edit`` verb.

The genuinely shared machinery is reused, not duplicated: the PDF is
resolved with :func:`precis_web.corpus.resolve_pdf_for_ref` (which honours
``pdfs.storage_path`` — how pres decks are filed) and rendered by the same
vendored pdf.js viewer as the paper reader. Only the left-pane form
differs.

There is intentionally **no** ``/pres`` list page or nav tab yet — the
merged papers/patents/edgar/slides filtered list will own discovery; this
router is just the single-deck editor + its endpoints.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
)

from precis.errors import NotFound
from precis.utils.handle_registry import format_handle
from precis_web.corpus import resolve_pdf_for_ref
from precis_web.deps import await_dispatch, get_store, get_web_config, templates

router = APIRouter(prefix="/pres", tags=["pres"])


def _form_attribution(ref: Any) -> dict[str, str]:
    """Shape a pres ref's ``meta`` attribution for the edit form.

    ``authors`` is joined one-per-line for the textarea; the scalar
    fields pass through as strings.
    """
    meta = ref.meta or {}
    return {
        "authors": "\n".join(str(a) for a in (meta.get("authors") or [])),
        "venue": str(meta.get("venue") or ""),
        "date": str(meta.get("date") or ""),
        "url": str(meta.get("url") or ""),
        "note": str(meta.get("note") or ""),
        "bibtex_type": str(meta.get("bibtex_type") or "misc"),
    }


def _load_pres(request: Request, ref_id: int) -> Any:
    store = get_store(request)
    ref = store.fetch_refs_by_ids([ref_id], include_deleted=False).get(ref_id)
    if ref is None or ref.kind != "pres":
        raise NotFound(f"presentation id={ref_id} not found")
    return ref


@router.get("/{slug}", response_class=HTMLResponse)
async def detail(request: Request, slug: str) -> HTMLResponse:
    """Two-pane deck reader + attribution editor at ``/pres/<slug>``."""
    store = get_store(request)
    ident: int | str = int(slug) if slug.isdigit() else slug
    ref = store.get_ref(kind="pres", id=ident)
    if ref is None:
        raise NotFound(f"presentation {slug!r} not found")

    blocks = store.list_blocks_for_ref(ref.id)
    slides = [
        {
            "pos": b.pos,
            "title": (b.meta or {}).get("slide_title") or f"Slide {b.pos + 1}",
        }
        for b in blocks
    ]

    cfg = get_web_config(request)
    pdf_path = resolve_pdf_for_ref(store, cfg.corpus_dirs, ref)

    # Render the BibTeX inline so the user can copy it without a round
    # trip — same formatter the get(view='bibtex') MCP path uses.
    bibtex, _err = await await_dispatch(
        request, "get", {"kind": "pres", "id": ref.slug or slug, "view": "bibtex"}
    )

    # ``doc`` drives the shared two-pane reader (_reader/reader.html.j2):
    # the generic Navigate/Jump sidebar + PDF pane, with the pres Meta
    # panel plugged in. The sidebar's search/toc/chunk fetches hit the
    # kind-agnostic /papers/{id}/… endpoints (pres is in _DOC_FAMILY).
    doc = {
        "id": ref.id,
        "title": ref.title,
        "handle": format_handle("pres", ref.id),
        "slug": ref.slug or slug,
        "list_url": "/drive",
        "list_label": "drive",
        "n_chunks": len(slides),
        "pdf_on_disk": pdf_path is not None,
        "has_pdf": bool(ref.pdf_sha256),
        "cited_ord": -1,
        # Open on the attribution panel — the deck editor's primary purpose.
        "initial_tab": "Meta",
        "pdf_url": f"/pres/{ref.id}/pdf",
        "meta_panel": "pres/_meta_panel.html.j2",
        "cite_key": ref.slug or "",
        "pdf_lookup_paths": [],
        "corpus_dirs": [str(p) for p in cfg.corpus_dirs],
    }

    return templates.TemplateResponse(
        request,
        "pres/detail.html.j2",
        {
            "ref": ref,
            "slug": ref.slug or slug,
            "slides": slides,
            "attribution": _form_attribution(ref),
            "bibtex": bibtex,
            "doc": doc,
        },
    )


@router.get("/{ref_id}/pdf")
async def pdf(request: Request, ref_id: int) -> FileResponse:
    """Stream the deck PDF (inline, for the viewer) — shares the paper
    reader's corpus resolution."""
    ref = _load_pres(request, ref_id)
    cfg = get_web_config(request)
    path = resolve_pdf_for_ref(get_store(request), cfg.corpus_dirs, ref)
    if path is None:
        raise NotFound(
            f"no PDF on disk for pres id={ref_id} (slug={ref.slug!r}). "
            "Add its root to PRECIS_CORPUS_DIR for the web process and restart."
        )
    return FileResponse(
        path,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{ref.slug or ref_id}.pdf"'},
    )


@router.get("/{ref_id}/bibtex", response_class=PlainTextResponse)
async def bibtex(request: Request, ref_id: int) -> PlainTextResponse:
    """Plain-text BibTeX for the deck (copy target / download)."""
    ref = _load_pres(request, ref_id)
    body, _err = await await_dispatch(
        request, "get", {"kind": "pres", "id": ref.slug or ref_id, "view": "bibtex"}
    )
    return PlainTextResponse(body, media_type="text/plain; charset=utf-8")


@router.post("/{ref_id}/edit", response_model=None)
async def edit(
    request: Request,
    ref_id: int,
    title: str = Form(""),
    authors: str = Form(""),
    venue: str = Form(""),
    date: str = Form(""),
    url: str = Form(""),
    note: str = Form(""),
    bibtex_type: str = Form("misc"),
) -> RedirectResponse | HTMLResponse:
    """Persist attribution metadata via the pres ``edit`` verb.

    The form is pre-populated with current values, so re-submitting
    preserves them; clearing a box clears that field. ``title`` is left
    untouched when blank (it's the deck's display name).
    """
    ref = _load_pres(request, ref_id)
    slug = ref.slug or str(ref_id)
    payload: dict[str, Any] = {
        "kind": "pres",
        "id": slug,
        "title": title,
        "authors": authors,
        "venue": venue,
        "date": date,
        "url": url,
        "note": note,
        "bibtex_type": bibtex_type,
    }
    body, is_error = await await_dispatch(request, "edit", payload)
    if is_error:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {"title": "Edit error", "detail": body, "status": 400},
            status_code=400,
        )
    return RedirectResponse(url=f"/pres/{slug}", status_code=303)
