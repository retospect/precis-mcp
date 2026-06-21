"""Papers tab — search the corpus and read PDFs in-browser.

List / search read off the DB (``store.list_refs`` /
``store.search_refs_lexical``). The detail page embeds the browser's
native PDF viewer pointed at ``/papers/{id}/pdf``, which streams the
file from ``corpus_dir`` (the NFS mount on the cluster) using the
ref's cite_key (``Ref.slug``) and the ``precis watch`` shard layout
``<corpus_dir>/<letter>/<cite_key>.pdf``.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    Response,
)

from precis.errors import BadInput, NotFound
from precis.utils.authors import author_names
from precis_web.deps import (
    await_dispatch,
    get_store,
    get_web_config,
    redirect_or_error,
    templates,
)

router = APIRouter(prefix="/papers", tags=["papers"])

#: Open tag marking a paper whose metadata automation couldn't recover —
#: the triage queue works this set (set by ``precis fix-metadata``).
_TRIAGE_TAG = "needs-triage"

#: Cap on the abstract length shown in the hover card (chars).
_ABSTRACT_PREVIEW = 900

#: Rows per page on the recent-papers list (matches the Refs tab).
_PAGE_SIZE = 50

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

#: Matches the cross-ref identifier-uniqueness error raised by
#: ``Store.set_ref_identifier`` (``_identifiers_ops.py``) so a failed
#: metadata edit can render the duplicate resolver — links to the owner
#: + a delete button — instead of a raw 400.
_ID_CONFLICT_RE = re.compile(
    r"(?P<field>\w+)=(?P<value>'[^']*'|\S+) already belongs to ref id=(?P<owner>\d+)"
)


def _parse_identifier_conflict(body: str) -> dict[str, Any] | None:
    """Pull ``(field, value, owner_id)`` out of the duplicate-identifier 400.

    Scoped to ``doi`` / ``arxiv`` — those are the "same paper held twice"
    case the delete-resolver is for. A ``cite_key`` clash is a different
    problem (pick another handle), handled inline by the rename path, so
    it falls through to the generic error page here. Returns ``None`` for
    any other error too.
    """
    m = _ID_CONFLICT_RE.search(body or "")
    if m is None or m.group("field") not in ("doi", "arxiv"):
        return None
    return {
        "field": m.group("field"),
        "value": m.group("value").strip("'"),
        "owner_id": int(m.group("owner")),
    }


_SLUG_RE = re.compile(r"[a-z0-9]+")


def _suggest_slug(store: Any, ref: Any, prefill: dict[str, Any] | None) -> str:
    """A free ``cite_key`` suggestion from the paper's author + year.

    Uses the S2 ``prefill`` (author/year the operator is about to save)
    when present, else the ref's stored values. Returns ``""`` when the
    inputs are too thin to beat the ``anon`` placeholder, or on any error
    (a suggestion must never 500 the detail page).
    """
    if prefill:
        raw = str(prefill.get("authors") or "")
        authors: Any = [
            ln.strip() for ln in raw.replace(";", "\n").splitlines() if ln.strip()
        ]
        yr = str(prefill.get("year") or "").strip()
        year: int | None = int(yr) if yr.isdigit() else None
    else:
        authors = ref.authors or []
        year = ref.year
    if not authors:
        return ""
    try:
        return store.suggest_cite_key(authors, year, exclude_ref_id=ref.id)
    except Exception:
        return ""


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
    """Authors joined for a single-line display (natural reading order).

    Shape-tolerance lives in :mod:`precis.utils.authors` — this is just
    the inline-join wrapper.
    """
    return ", ".join(author_names(getattr(ref, "authors", None)))


def _author_edit_lines(ref: Any) -> list[str]:
    """Editor prefill: one author per line in ``Family, Given`` order,
    which the edit handler round-trips back to the stored shape."""
    return author_names(getattr(ref, "authors", None), order="sortable")


def _abstract_str(ref: Any) -> str:
    """Plain-text abstract preview for the hover card.

    The publisher abstract in ``refs.meta['abstract']`` is often
    JATS/HTML-wrapped; strip tags and collapse whitespace, then cap to
    a preview length so the tooltip stays bounded.
    """
    text = _abstract_full(ref)
    if len(text) > _ABSTRACT_PREVIEW:
        text = text[:_ABSTRACT_PREVIEW].rstrip() + "…"
    return text


def _abstract_full(ref: Any) -> str:
    """Full publisher abstract (tag-stripped, NOT truncated) for the
    editable form. Distinct from :func:`_abstract_str` — feeding the
    editor the preview would persist the truncation on save."""
    meta = getattr(ref, "meta", None) or {}
    raw = meta.get("abstract")
    if not raw:
        return ""
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", str(raw))).strip()


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
        # Filled in by the index route from a batched existence query.
        "has_chunks": False,
        "authors": _authors_str(ref),
        "abstract": _abstract_str(ref),
        "links": {"doi": "", "doi_url": "", "arxiv": "", "arxiv_url": ""},
    }


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    q: str | None = None,
    has_pdf: int = 0,
    has_chunks: int = 0,
    page: int = 1,
) -> HTMLResponse:
    """Search box + result list (recent papers when ``q`` is empty).

    ``has_pdf`` / ``has_chunks`` are 0/1 toggles. On the recent-list
    path they push down into ``store.list_refs`` (SQL-side, so the
    page cap counts only matching papers). On the lexical-search
    path they post-filter the ranked hits (the lexical query can't
    take the extra predicates), so a query + toggle may show fewer
    than a full page even when more match — acceptable for a triage
    filter. The recent-list path pages via ``?page=N`` (offset-based,
    one-extra-row probe for "has next"); the ranked-search path shows
    the top window only (relevance ordering doesn't page cleanly).
    """
    store = get_store(request)
    want_pdf = bool(has_pdf)
    want_chunks = bool(has_chunks)
    page = max(1, page)
    offset = (page - 1) * _PAGE_SIZE
    if q and q.strip():
        hits = store.search_refs_lexical(q=q, kind="paper", limit=_PAGE_SIZE)
        refs = [ref for ref, _score in hits]
        if want_pdf:
            refs = [r for r in refs if r.pdf_sha256]
        if want_chunks:
            survivors = store.ref_ids_with_chunks([r.id for r in refs])
            refs = [r for r in refs if r.id in survivors]
        has_next = False
    else:
        refs = store.list_refs(
            kind="paper",
            has_pdf=True if want_pdf else None,
            has_chunks=True if want_chunks else None,
            limit=_PAGE_SIZE + 1,  # one extra row probes "has next page"
            offset=offset,
        )
        has_next = len(refs) > _PAGE_SIZE
        refs = refs[:_PAGE_SIZE]
    rows = [_paper_row(r) for r in refs]
    # Chunk-presence badge for every row, one batched query. (When
    # ``want_chunks`` is set every row is True by construction, but the
    # round-trip is cheap and keeps the badge correct otherwise.)
    chunked = store.ref_ids_with_chunks([row["id"] for row in rows])
    for row in rows:
        row["has_chunks"] = row["id"] in chunked
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
            "has_pdf": want_pdf,
            "has_chunks": want_chunks,
            "papers": rows,
            "page": page,
            "has_next": has_next,
            "paged": not (q and q.strip()),
        },
    )


@router.get("/triage", response_class=HTMLResponse)
async def triage_queue(request: Request, page: int = 1) -> HTMLResponse:
    """Queue of papers tagged ``needs-triage`` (metadata automation gave up).

    Registered before ``/{ref_id}`` so the literal ``triage`` segment
    isn't swallowed by the int path param. Each row links to the paper
    detail with ``?triage=1`` so the detail page opens the triage panel.
    """
    store = get_store(request)
    page = max(1, page)
    offset = (page - 1) * _PAGE_SIZE
    refs = store.list_refs(
        kind="paper",
        tags=[_TRIAGE_TAG],
        limit=_PAGE_SIZE + 1,
        offset=offset,
    )
    has_next = len(refs) > _PAGE_SIZE
    refs = refs[:_PAGE_SIZE]
    total = store.count_refs(kind="paper", tags=[_TRIAGE_TAG])
    total_pages = max(1, -(-total // _PAGE_SIZE))  # ceil-div
    rows = [_paper_row(r) for r in refs]
    ids_map = store.identifiers_for_refs([row["id"] for row in rows])
    for row in rows:
        row["links"] = _links_from_ids(ids_map.get(row["id"], {}))
    return templates.TemplateResponse(
        request,
        "papers/triage.html.j2",
        {
            "active_tab": "triage",
            "papers": rows,
            "page": page,
            "has_next": has_next,
            "total": total,
            "total_pages": total_pages,
            "offset": offset,
        },
    )


def _render_detail(
    request: Request,
    ref: Any,
    *,
    triage: bool = False,
    prefill: dict[str, Any] | None = None,
    triage_msg: str = "",
    cited: dict[str, Any] | None = None,
) -> HTMLResponse:
    """Render the paper detail page. Shared by ``detail`` and the triage
    lookup so an S2 result can re-render the page with the edit form
    pre-filled (``prefill``) without duplicating the context build."""
    store = get_store(request)
    cfg = get_web_config(request)
    ref_id = ref.id
    cite_key = ref.slug or ""
    found = _resolve_pdf(cfg.corpus_dirs, cite_key)
    paper = _paper_row(ref)
    paper["links"] = _links_from_ids(
        store.identifiers_for_refs([ref_id]).get(ref_id, {})
    )
    has_triage = triage or store.has_tag(ref_id, "OPEN", _TRIAGE_TAG)
    stamps = store.ingest_timestamps(ref_id)
    # Suggest a real cite_key from the (fixed) author + year. Pre-fill the
    # field with it only when the current handle is the anon placeholder —
    # otherwise default to the existing handle so a save is a no-op unless
    # the operator opts in. The suggestion is always shown as a hint.
    suggested_slug = _suggest_slug(store, ref, prefill)
    if suggested_slug and (not cite_key or cite_key.startswith("anon")):
        slug_default = suggested_slug
    else:
        slug_default = cite_key
    return templates.TemplateResponse(
        request,
        "papers/detail.html.j2",
        {
            "active_tab": "papers",
            "paper": paper,
            "authors_display": _authors_str(ref),
            "author_lines": _author_edit_lines(ref),
            "abstract": _abstract_full(ref),
            "ingested": stamps,
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
            # Triage panel state (paste-title -> S2 lookup -> pre-filled edit).
            "is_triage": has_triage,
            "prefill": prefill,
            "triage_msg": triage_msg,
            # Editable short handle (cite_key) + a free suggestion.
            "slug_default": slug_default,
            "suggested_slug": suggested_slug,
            # Cited passage (from a ``?chunk=N`` citation click) — rendered
            # as a highlighted card so the reader lands on "the relevant
            # thing", with a PDF-page link for the full context.
            "cited": cited,
        },
    )


def _cited_chunk(store: Any, ref_id: int, chunk: str | None) -> dict[str, Any] | None:
    """Resolve a ``?chunk=N`` (or ``N..M``) citation to the cited chunk's
    verbatim text + PDF page, for the highlighted "cited passage" card.
    ``pN`` page-jumps and missing chunks return ``None``."""
    if not chunk:
        return None
    m = re.match(r"^(\d+)(?:\.\.\d+)?$", chunk)
    if m is None:  # e.g. ``p23`` — a page jump, no chunk text
        return None
    ord_ = int(m.group(1))
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT text, page_first FROM chunks WHERE ref_id = %s AND ord = %s",
            (ref_id, ord_),
        ).fetchone()
    if row is None or not row[0]:
        return None
    return {"ord": ord_, "text": row[0], "page": row[1]}


@router.get("/{ref_id}", response_class=HTMLResponse)
async def detail(
    request: Request, ref_id: int, triage: int = 0, chunk: str | None = None
) -> HTMLResponse:
    """Paper detail: metadata + embedded PDF viewer. ``?chunk=N`` (a
    citation click) surfaces that chunk's text as a highlighted card."""
    store = get_store(request)
    refs = store.fetch_refs_by_ids([ref_id], include_deleted=False)
    ref = refs.get(ref_id)
    if ref is None or ref.kind != "paper":
        raise NotFound(f"paper id={ref_id} not found")
    return _render_detail(
        request, ref, triage=bool(triage), cited=_cited_chunk(store, ref_id, chunk)
    )


@router.post("/{ref_id}/triage-lookup", response_model=None)
async def triage_lookup(
    request: Request, ref_id: int, title: str = Form("")
) -> HTMLResponse:
    """Look the operator-supplied title up on Semantic Scholar and re-render
    the detail page with the edit form pre-filled from the best match.

    Read-only: it never writes. The operator reviews the candidate and
    commits via the normal Save (the ``edit`` POST), which also clears the
    ``needs-triage`` tag. A miss just re-opens the panel with a message.
    """
    store = get_store(request)
    refs = store.fetch_refs_by_ids([ref_id], include_deleted=False)
    ref = refs.get(ref_id)
    if ref is None or ref.kind != "paper":
        raise NotFound(f"paper id={ref_id} not found")

    query = title.strip()
    if not query:
        return _render_detail(
            request, ref, triage=True, triage_msg="Enter a title to look up."
        )

    from precis.ingest.lookup import lookup_title

    result = lookup_title(query, s2_key=os.environ.get("SEMANTIC_SCHOLAR_API_KEY", ""))
    if not result or not result.get("title"):
        return _render_detail(
            request,
            ref,
            triage=True,
            triage_msg=f"No Semantic Scholar match for {query!r}. "
            "Edit the fields by hand below.",
        )

    names = author_names(result.get("authors") or [])
    prefill = {
        "title": result.get("title", ""),
        "year": result.get("year") or "",
        "doi": result.get("doi") or "",
        "arxiv": result.get("arxiv_id") or "",
        "abstract": result.get("abstract") or "",
        "authors": "\n".join(names),
    }
    return _render_detail(
        request,
        ref,
        triage=True,
        prefill=prefill,
        triage_msg=f"Found on Semantic Scholar: {result['title']!r} — "
        "review and Save to apply (clears needs-triage).",
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
# ``edit`` flows through ``runtime.dispatch(edit)`` so the handler's
# validation, ref-events log, and tree guards stay single-sourced (web +
# MCP behave the same). ``delete`` deliberately does NOT: it calls the
# store directly so paper deletion stays a web-only affordance and is not
# exposed on the agent MCP surface (``PaperHandler`` keeps
# ``supports_delete=False``).


def _render_edit_conflict(
    request: Request, ref_id: int, conflict: dict[str, Any]
) -> HTMLResponse:
    """Render the duplicate-identifier resolver for a failed paper edit.

    Loads the conflicting owner so the template can link to its detail +
    PDF; degrades gracefully (``owner=None``) if it can't be fetched.
    """
    store = get_store(request)
    owner_id = conflict["owner_id"]
    owner_ref = store.fetch_refs_by_ids([owner_id], include_deleted=False).get(owner_id)
    owner: dict[str, Any] | None = None
    owner_pdf = False
    if owner_ref is not None and owner_ref.kind == "paper":
        owner = _paper_row(owner_ref)
        owner["links"] = _links_from_ids(
            store.identifiers_for_refs([owner_id]).get(owner_id, {})
        )
        cfg = get_web_config(request)
        owner_pdf = _resolve_pdf(cfg.corpus_dirs, owner_ref.slug or "") is not None
    return templates.TemplateResponse(
        request,
        "papers/edit_conflict.html.j2",
        {
            "active_tab": "papers",
            "ref_id": ref_id,
            "field": conflict["field"],
            "value": conflict["value"],
            "owner_id": owner_id,
            "owner": owner,
            "owner_pdf": owner_pdf,
        },
        status_code=409,
    )


def _safe_papers_redirect(return_to: str) -> str:
    """Constrain a ``return_to`` to a local ``/papers`` path (no open redirect)."""
    return return_to if return_to.startswith("/papers") else "/papers"


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
    cite_key: str = Form(""),
) -> RedirectResponse | HTMLResponse:
    """Update editable paper metadata.

    Empty fields are NOT sent (so an unset value doesn't overwrite the
    existing one). Authors come in as a newline- or comma-separated
    string and get split + re-shaped into the ``[{family, given}, ...]``
    list shape the schema stores. ``cite_key`` is the short handle: when
    it differs from the current slug the paper is re-slugged (and its PDF
    moved on disk) — see :func:`_rename_slug`.
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
        # One author per line (or ';'-separated). Forward the cleaned
        # lines as-is; the paper edit handler canonicalises them to the
        # stored ``{"name": …}`` shape (see precis.utils.authors), so
        # the web layer no longer hand-shapes family/given.
        lines = [a.strip() for a in authors.replace(";", "\n").splitlines()]
        cleaned = [a for a in lines if a]
        if cleaned:
            payload["authors"] = cleaned

    store = get_store(request)
    current_slug = ""
    ref = store.fetch_refs_by_ids([ref_id], include_deleted=False).get(ref_id)
    if ref is not None:
        current_slug = ref.slug or ""
    new_slug = cite_key.strip().lower()
    slug_changed = bool(new_slug) and new_slug != current_slug
    has_meta = len(payload) > 2  # anything beyond kind + id

    if not has_meta and not slug_changed:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "title": "Edit error",
                "detail": "Nothing to change — edit a field or the handle first.",
                "status": 400,
            },
            status_code=400,
        )

    if has_meta:
        body, is_error = await await_dispatch(request, "edit", payload)
        if is_error:
            conflict = _parse_identifier_conflict(body)
            if conflict is not None:
                # Duplicate identifier: the DOI / arXiv id being assigned
                # already belongs to another paper (the two are almost
                # always the same paper held twice). Render the resolver —
                # it links to the owner's detail + PDF (open in a new tab to
                # inspect) and offers to delete this redundant copy.
                return _render_edit_conflict(request, ref_id, conflict)
            # Any other error: render inline rather than redirect — the
            # operator needs to see why the edit didn't take.
            return templates.TemplateResponse(
                request,
                "error.html.j2",
                {"title": "Edit error", "detail": body, "status": 400},
                status_code=400,
            )

    if slug_changed:
        err = await asyncio.to_thread(
            _rename_slug, request, ref_id, current_slug, new_slug
        )
        if err is not None:
            return templates.TemplateResponse(
                request,
                "error.html.j2",
                {"title": "Rename error", "detail": err, "status": 400},
                status_code=400,
            )

    # A successful edit that lands a real title resolves a triaged paper —
    # clear the needs-triage tag so it leaves the queue. Idempotent: the
    # tag verb's remove is a no-op when the tag isn't present.
    if store.has_tag(ref_id, "OPEN", _TRIAGE_TAG):
        await await_dispatch(
            request,
            "tag",
            {"kind": "paper", "id": ref_id, "remove": [_TRIAGE_TAG]},
        )
    return RedirectResponse(url=f"/papers/{ref_id}", status_code=303)


def _rename_slug(
    request: Request, ref_id: int, old_slug: str, new_slug: str
) -> str | None:
    """Re-slug a paper: replace its ``cite_key`` and move the PDF on disk.

    Web-only (a direct store + filesystem op, not dispatched). Returns an
    error string for the caller to surface, or ``None`` on success.

    The on-disk PDF is named ``<cite_key>.pdf`` under a sharded corpus
    root, so the rename must move it too or the in-browser viewer 404s.
    The old path is resolved *before* the DB change (so we still have the
    handle to it), then the identifier is swapped (which raises on a
    cross-ref clash), then the file is moved best-effort.
    """
    if not _SLUG_RE.fullmatch(new_slug):
        return (
            f"handle {new_slug!r} is invalid — use lowercase letters and "
            "digits only (e.g. piela07)."
        )
    store = get_store(request)
    cfg = get_web_config(request)
    old_pdf = _resolve_pdf(cfg.corpus_dirs, old_slug) if old_slug else None
    try:
        store.set_ref_identifier(ref_id, "cite_key", new_slug, source="web-edit")
    except BadInput as exc:
        return str(exc)
    if old_pdf is not None:
        letter = new_slug[0].lower() if new_slug[0].isalnum() else "_"
        new_pdf = old_pdf.parent.parent / letter / f"{new_slug}.pdf"
        try:
            new_pdf.parent.mkdir(parents=True, exist_ok=True)
            if not new_pdf.exists():
                old_pdf.rename(new_pdf)
        except OSError:
            # DB is updated; the file move is best-effort. The detail page's
            # "file expected but missing" panel will self-diagnose if the
            # move didn't land.
            pass
    return None


@router.post("/{ref_id}/delete", response_model=None)
async def delete(
    request: Request,
    ref_id: int,
    return_to: str = Form("/papers"),
) -> RedirectResponse | HTMLResponse:
    """Soft-delete this paper (sets ``refs.deleted_at = now()``).

    Web-only by policy: the call goes straight to the store rather than
    through ``runtime.dispatch`` so paper deletion is NOT exposed on the
    agent MCP surface. Soft delete is reversible at the DB level (toggle
    ``deleted_at`` back to NULL); the UX presents it as a one-way removal.
    ``return_to`` lands the operator back where they were (triage queue /
    duplicate resolver), constrained to ``/papers*`` to avoid an open
    redirect; it defaults to the papers list, not the (now-404) detail.
    """
    store = get_store(request)
    ref = store.fetch_refs_by_ids([ref_id], include_deleted=False).get(ref_id)
    if ref is None or ref.kind != "paper":
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "title": "Delete error",
                "detail": f"paper id={ref_id} not found",
                "status": 404,
            },
            status_code=404,
        )
    try:
        await asyncio.to_thread(store.soft_delete_ref, ref_id)
    except NotFound as exc:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {"title": "Delete error", "detail": str(exc), "status": 400},
            status_code=400,
        )
    return RedirectResponse(url=_safe_papers_redirect(return_to), status_code=303)


@router.post("/{ref_id}/untriage", response_model=None)
async def untriage(
    request: Request,
    ref_id: int,
    return_to: str = Form("/papers/triage"),
) -> Response:
    """Manually clear the ``needs-triage`` tag (dismiss from the queue).

    A successful metadata edit clears the tag automatically, but a paper
    that's actually fine, one fixed by hand outside the S2 flow, or one
    whose fix failed on a duplicate identifier stays tagged. This is the
    explicit operator control. Idempotent: the tag remove is a no-op when
    the tag isn't present.

    A thin named preset over the generic ``tag`` verb (the same dispatch
    the ``/refs/{kind}/{ref_id}/tags`` endpoint uses) routed through the
    shared :func:`redirect_or_error` so a failed dispatch renders the
    handler's message instead of silently redirecting — the original bug
    here was a swallowed ``NotFound`` that made the button look like it
    worked while the tag survived.
    """
    return await redirect_or_error(
        request,
        "tag",
        {"kind": "paper", "id": ref_id, "remove": [_TRIAGE_TAG]},
        redirect=_safe_papers_redirect(return_to),
        error_title="Untriage error",
    )
