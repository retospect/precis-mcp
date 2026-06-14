"""Papers tab — search the corpus and read PDFs in-browser.

List / search read off the DB (``store.list_refs`` /
``store.search_refs_lexical``). The detail page embeds the browser's
native PDF viewer pointed at ``/papers/{id}/pdf``, which streams the
file from ``corpus_dir`` (the NFS mount on the cluster) using the
ref's cite_key (``Ref.slug``) and the ``precis watch`` shard layout
``<corpus_dir>/<letter>/<cite_key>.pdf``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse

from precis.errors import NotFound
from precis_web.deps import get_store, get_web_config, templates

router = APIRouter(prefix="/papers", tags=["papers"])


def _pdf_path(corpus_dir: Path, cite_key: str) -> Path:
    """Resolve a cite_key to its on-disk PDF path.

    Mirrors ``precis.cli.watch._move_to_corpus``: the shard letter is
    the lower-cased first alnum char of the cite_key, else ``_``.
    """
    letter = cite_key[0].lower() if cite_key and cite_key[0].isalnum() else "_"
    return corpus_dir / letter / f"{cite_key}.pdf"


def _paper_row(ref: Any) -> dict[str, Any]:
    return {
        "id": ref.id,
        "slug": ref.slug or "",
        "title": ref.title,
        "year": ref.year,
        "has_pdf": bool(ref.pdf_sha256),
    }


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(request: Request, q: str | None = None) -> HTMLResponse:
    """Search box + result list (recent papers when ``q`` is empty)."""
    store = get_store(request)
    if q and q.strip():
        hits = store.search_refs_lexical(q=q, kind="paper", limit=50)
        refs = [ref for ref, _score in hits]
    else:
        refs = store.list_refs(kind="paper", limit=50)
    return templates.TemplateResponse(
        request,
        "papers/index.html.j2",
        {
            "active_tab": "papers",
            "q": q or "",
            "papers": [_paper_row(r) for r in refs],
        },
    )


@router.get("/{ref_id}", response_class=HTMLResponse)
async def detail(request: Request, ref_id: int) -> HTMLResponse:
    """Paper detail: metadata + embedded PDF viewer."""
    store = get_store(request)
    refs = store.fetch_refs_by_ids([ref_id], include_deleted=False)
    ref = refs.get(ref_id)
    if ref is None or ref.kind != "paper":
        raise NotFound(f"paper id={ref_id} not found")
    cfg = get_web_config(request)
    cite_key = ref.slug or ""
    pdf_on_disk = bool(cite_key) and _pdf_path(cfg.corpus_dir, cite_key).is_file()
    return templates.TemplateResponse(
        request,
        "papers/detail.html.j2",
        {
            "active_tab": "papers",
            "paper": _paper_row(ref),
            "authors": ref.authors or [],
            "pdf_on_disk": pdf_on_disk,
        },
    )


@router.get("/{ref_id}/pdf")
async def pdf(request: Request, ref_id: int) -> FileResponse:
    """Stream the paper's PDF from ``corpus_dir`` (inline, for the viewer)."""
    store = get_store(request)
    refs = store.fetch_refs_by_ids([ref_id], include_deleted=False)
    ref = refs.get(ref_id)
    if ref is None or ref.kind != "paper":
        raise NotFound(f"paper id={ref_id} not found")
    cite_key = ref.slug or ""
    cfg = get_web_config(request)
    path = _pdf_path(cfg.corpus_dir, cite_key)
    if not cite_key or not path.is_file():
        raise NotFound(f"no PDF on disk for paper id={ref_id} (cite_key={cite_key!r})")
    return FileResponse(
        path,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{cite_key}.pdf"'},
    )
