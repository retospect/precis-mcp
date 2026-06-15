"""Papers tab — search the corpus and read PDFs in-browser.

List / search read off the DB (``store.list_refs`` /
``store.search_refs_lexical``). The detail page embeds the browser's
native PDF viewer pointed at ``/papers/{id}/pdf``, which streams the
file from ``corpus_dir`` (the NFS mount on the cluster) using the
ref's cite_key (``Ref.slug``) and the ``precis watch`` shard layout
``<corpus_dir>/<letter>/<cite_key>.pdf``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse

from precis.errors import NotFound
from precis_web.deps import get_store, get_web_config, templates

router = APIRouter(prefix="/papers", tags=["papers"])

#: Cap on the abstract length shown in the hover card (chars).
_ABSTRACT_PREVIEW = 900

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _pdf_path(corpus_dir: Path, cite_key: str) -> Path:
    """Resolve a cite_key to its on-disk PDF path.

    Mirrors ``precis.cli.watch._move_to_corpus``: the shard letter is
    the lower-cased first alnum char of the cite_key, else ``_``.
    """
    letter = cite_key[0].lower() if cite_key and cite_key[0].isalnum() else "_"
    return corpus_dir / letter / f"{cite_key}.pdf"


def _authors_str(ref: Any) -> str:
    """Join an authors list (dicts with family/given) into a string.

    Tolerant of the various author shapes in ``refs.authors`` — dicts
    with ``family``/``given``, plain strings, or missing entirely.
    """
    authors = getattr(ref, "authors", None) or []
    names: list[str] = []
    for a in authors:
        if isinstance(a, dict):
            name = (a.get("family") or a.get("name") or "").strip()
            given = (a.get("given") or "").strip()
            if name and given:
                name = f"{given} {name}"
            elif not name:
                name = given
        else:
            name = str(a).strip()
        if name:
            names.append(name)
    return ", ".join(names)


def _abstract_str(ref: Any) -> str:
    """Plain-text abstract for the hover card.

    The publisher abstract in ``refs.meta['abstract']`` is often
    JATS/HTML-wrapped; strip tags and collapse whitespace, then cap to
    a preview length so the tooltip stays bounded.
    """
    meta = getattr(ref, "meta", None) or {}
    raw = meta.get("abstract")
    if not raw:
        return ""
    text = _WS_RE.sub(" ", _TAG_RE.sub(" ", str(raw))).strip()
    if len(text) > _ABSTRACT_PREVIEW:
        text = text[:_ABSTRACT_PREVIEW].rstrip() + "…"
    return text


def _paper_row(ref: Any) -> dict[str, Any]:
    return {
        "id": ref.id,
        "slug": ref.slug or "",
        "title": ref.title,
        "year": ref.year,
        "has_pdf": bool(ref.pdf_sha256),
        "authors": _authors_str(ref),
        "abstract": _abstract_str(ref),
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
    rows = [_paper_row(r) for r in refs]
    # Most papers have no publisher abstract in meta; backfill the
    # hover-card text from the leading body chunks in one batched query.
    missing = [row for row in rows if not row["abstract"]]
    if missing:
        previews = store.abstract_previews([row["id"] for row in missing])
        for row in missing:
            row["abstract"] = previews.get(row["id"], "")
    return templates.TemplateResponse(
        request,
        "papers/index.html.j2",
        {
            "active_tab": "papers",
            "q": q or "",
            "papers": rows,
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
    lookup_path = _pdf_path(cfg.corpus_dir, cite_key) if cite_key else None
    pdf_on_disk = lookup_path is not None and lookup_path.is_file()
    return templates.TemplateResponse(
        request,
        "papers/detail.html.j2",
        {
            "active_tab": "papers",
            "paper": _paper_row(ref),
            "authors": ref.authors or [],
            "pdf_on_disk": pdf_on_disk,
            # Diagnostics for the "file expected but missing" case (a
            # held paper whose corpus_dir / mount is misconfigured, or
            # a paper with no cite_key to address the file by): show
            # exactly where we looked so it's self-diagnosing.
            "cite_key": cite_key,
            "pdf_lookup_path": str(lookup_path) if lookup_path else "",
            "corpus_dir": str(cfg.corpus_dir),
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
        raise NotFound(
            f"no PDF on disk for paper id={ref_id} (cite_key={cite_key!r}); "
            f"looked at {str(path)!r} under corpus_dir={str(cfg.corpus_dir)!r}. "
            "If the file exists elsewhere, set PRECIS_CORPUS_DIR for the web process."
        )
    return FileResponse(
        path,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{cite_key}.pdf"'},
    )
