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

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from precis.errors import NotFound
from precis_web.deps import await_dispatch, get_store, get_web_config, templates

router = APIRouter(prefix="/papers", tags=["papers"])

#: Cap on the abstract length shown in the hover card (chars).
_ABSTRACT_PREVIEW = 900

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _pdf_candidates(corpus_dirs: tuple[Path, ...], cite_key: str) -> list[Path]:
    """All on-disk PDF paths to try for a cite_key, one per corpus root.

    Mirrors ``precis.cli.watch._move_to_corpus``: the shard letter is
    the lower-cased first alnum char of the cite_key, else ``_``. One
    candidate per configured root so a per-host NFS mount difference
    is searched rather than fatal.
    """
    if not cite_key:
        return []
    letter = cite_key[0].lower() if cite_key[0].isalnum() else "_"
    return [root / letter / f"{cite_key}.pdf" for root in corpus_dirs]


def _resolve_pdf(corpus_dirs: tuple[Path, ...], cite_key: str) -> Path | None:
    """First existing PDF path across the corpus roots, or ``None``."""
    for path in _pdf_candidates(corpus_dirs, cite_key):
        if path.is_file():
            return path
    return None


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


def _links_from_ids(ids: dict[str, str]) -> dict[str, str]:
    """Build verification links from a ref's external identifiers.

    ``ids`` is the ``{scheme: value}`` map from
    ``store.identifiers_for_refs``. We surface DOI and arXiv as
    clickable URLs (the two an operator uses to verify a paper at a
    glance); other schemes are left for the detail page.
    """
    doi = ids.get("doi", "")
    arxiv = ids.get("arxiv", "")
    return {
        "doi": doi,
        "doi_url": f"https://doi.org/{doi}" if doi else "",
        "arxiv": arxiv,
        "arxiv_url": f"https://arxiv.org/abs/{arxiv}" if arxiv else "",
    }


def _paper_row(ref: Any) -> dict[str, Any]:
    return {
        "id": ref.id,
        "slug": ref.slug or "",
        "title": ref.title,
        "year": ref.year,
        "has_pdf": bool(ref.pdf_sha256),
        "authors": _authors_str(ref),
        "abstract": _abstract_str(ref),
        "links": {"doi": "", "doi_url": "", "arxiv": "", "arxiv_url": ""},
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
    # DOI / arXiv links for the hover card, fetched in one batched
    # query so quick verification doesn't cost N round-trips.
    ids_map = store.identifiers_for_refs([row["id"] for row in rows])
    for row in rows:
        row["links"] = _links_from_ids(ids_map.get(row["id"], {}))
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
    found = _resolve_pdf(cfg.corpus_dirs, cite_key)
    paper = _paper_row(ref)
    paper["links"] = _links_from_ids(
        store.identifiers_for_refs([ref_id]).get(ref_id, {})
    )
    return templates.TemplateResponse(
        request,
        "papers/detail.html.j2",
        {
            "active_tab": "papers",
            "paper": paper,
            "authors": ref.authors or [],
            "pdf_on_disk": found is not None,
            # Diagnostics for the "file expected but missing" case (a
            # held paper whose corpus roots / mount are misconfigured,
            # or a paper with no cite_key to address the file by): list
            # every path we tried so it's self-diagnosing.
            "cite_key": cite_key,
            "pdf_lookup_paths": [
                str(p) for p in _pdf_candidates(cfg.corpus_dirs, cite_key)
            ],
            "corpus_dirs": [str(p) for p in cfg.corpus_dirs],
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
    path = _resolve_pdf(cfg.corpus_dirs, cite_key)
    if path is None:
        tried = [str(p) for p in _pdf_candidates(cfg.corpus_dirs, cite_key)]
        raise NotFound(
            f"no PDF on disk for paper id={ref_id} (cite_key={cite_key!r}); "
            f"looked at {tried or '(no cite_key to address a file)'}. "
            "If the file exists elsewhere, add its root to PRECIS_CORPUS_DIR "
            "(os.pathsep-separated) for the web process and restart."
        )
    return FileResponse(
        path,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{cite_key}.pdf"'},
    )


# ---- Edit + delete ----------------------------------------------------
#
# Both routes flow through ``runtime.dispatch(edit / delete)`` so the
# handler's validation, ref-events log, and tree guards stay
# single-sourced (web + MCP behave the same).


@router.post("/{ref_id}/edit", response_model=None)
async def edit(
    request: Request,
    ref_id: int,
    title: str = Form(""),
    year: str = Form(""),
    doi: str = Form(""),
    arxiv: str = Form(""),
    abstract: str = Form(""),
    authors: str = Form(""),
) -> RedirectResponse | HTMLResponse:
    """Update editable paper metadata.

    Empty fields are NOT sent (so an unset value doesn't overwrite the
    existing one). Authors come in as a newline- or comma-separated
    string and get split + re-shaped into the ``[{family, given}, ...]``
    list shape the schema stores.
    """
    payload: dict[str, Any] = {"kind": "paper", "id": ref_id}
    if title.strip():
        payload["title"] = title.strip()
    if year.strip():
        try:
            payload["year"] = int(year.strip())
        except ValueError:
            pass
    if doi.strip():
        payload["doi"] = doi.strip()
    if arxiv.strip():
        payload["arxiv"] = arxiv.strip()
    if abstract.strip():
        payload["abstract"] = abstract.strip()
    if authors.strip():
        # Split on newlines first, then commas — operator likely paste
        # the list with one author per line or "Lastname, F.; Other, A.".
        raw = [a.strip() for a in authors.replace(";", "\n").splitlines()]
        cleaned = [a for a in raw if a]
        # Shape into family/given when a comma's present; otherwise treat
        # the whole entry as family.
        shaped: list[dict[str, str]] = []
        for a in cleaned:
            if "," in a:
                family, _, given = a.partition(",")
                shaped.append(
                    {"family": family.strip(), "given": given.strip()}
                )
            else:
                shaped.append({"family": a, "given": ""})
        if shaped:
            payload["authors"] = shaped

    body, is_error = await await_dispatch(request, "edit", payload)
    if is_error:
        # Render the error inline rather than redirect — operator needs
        # to see why the edit didn't take.
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {"active_tab": "papers", "body": body, "is_error": True},
            status_code=400,
        )
    return RedirectResponse(url=f"/papers/{ref_id}", status_code=303)


@router.post("/{ref_id}/delete", response_model=None)
async def delete(
    request: Request,
    ref_id: int,
) -> RedirectResponse | HTMLResponse:
    """Soft-delete this paper (sets ``refs.deleted_at = now()``).

    The `delete` verb is reversible at the DB level (toggle deleted_at
    back to NULL), but the UX presents it as a one-way removal. The
    redirect lands on the papers list, not the (now-404) detail page.
    """
    body, is_error = await await_dispatch(
        request, "delete", {"kind": "paper", "id": ref_id}
    )
    if is_error:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {"active_tab": "papers", "body": body, "is_error": True},
            status_code=400,
        )
    return RedirectResponse(url="/papers", status_code=303)
