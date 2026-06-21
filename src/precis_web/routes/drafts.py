"""Drafts tab — a read-first viewer/editor for the ``draft`` kind (ADR 0033).

Tier-A surface (the document is *steered*, not hand-typed). The reader is
a **per-block row grid**: one row per chunk in DFS reading order, each row
three columns —

  ┌ content (raw source via linkify_refs + KaTeX, hierarchy-indented,
  │          headings collapse their subtree)
  ├ meta    (terse: the refs this block makes + in-flight change-requests)
  └ change  (a per-block "around here…" box → an anchored todo)

Routes:

* ``GET /drafts`` — list drafts.
* ``GET /drafts/{ident}`` — the reader (slug or numeric id).
* ``GET /draft/{ident}`` — singular convenience alias → 303 to the reader.
* ``POST /drafts/{ident}/request`` — file a change request (anchored todo
  parented on the draft's project; flows into the todo tree → dispatch).
* ``GET /c/{handle}`` — resolve a ``¶`` handle → redirect into the reader
  at ``#c-<handle>`` (the click target of every ``¶`` anchor).
* ``GET /preview/chunk/{handle}`` — hover-popover fragment for a ``¶``.
* ``GET /drafts/{ident}/row/{handle}`` — one rendered row (the fragment
  the future live-refresh poll/websocket swaps in place).
* ``GET /drafts/{ident}/version`` — a monotone version token (max
  ``chunk_events.event_id``) the future poll compares against.

Rendering is **raw source** (Tier A); the resolution pass that computes
§-numbers / resolves cross-refs is the export engine (Tier B), shared
across HTML/LaTeX/Word targets. KaTeX renders ``$…$`` client-side.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)

from precis.utils import draft_markup, mentions
from precis.utils.embed_query import embed_query
from precis_web.deps import get_runtime, get_store, redirect_or_error, templates
from precis_web.linkify import popover_chip

router = APIRouter(tags=["drafts"])

log = logging.getLogger(__name__)


def _draft_ref(store: Any, ident: str) -> Any:
    """Resolve a draft by slug or numeric ref_id (``get_ref`` handles
    both). Returns the live ``Ref`` or ``None``."""
    key: int | str = int(ident) if ident.lstrip("#").isdigit() else ident
    if isinstance(key, str) and key.startswith("#"):
        key = int(key[1:])
    return store.get_ref(kind="draft", id=key)


def _project_id(store: Any, ref_id: int) -> int | None:
    """The draft's owning project todo (the ``draft-of`` target)."""
    for link in store.links_for(ref_id, direction="out", relation="draft-of"):
        return int(link.dst_ref_id)
    return None


def _ancestor_headings(chunk_objs: list[Any]) -> dict[str, list[str]]:
    """Each chunk's ancestor *heading* handles (root→nearest), walking
    ``parent_chunk_id``. Drives client-side collapse: a row hides when any
    of its ancestor headings is collapsed; a heading owns exactly the
    chunks that carry it in this list."""
    by_id = {c.chunk_id: c for c in chunk_objs}
    out: dict[str, list[str]] = {}
    for c in chunk_objs:
        anc: list[str] = []
        pid = c.parent_chunk_id
        while pid is not None and pid in by_id:
            p = by_id[pid]
            if p.chunk_kind == "heading":
                anc.append(p.handle)
            pid = p.parent_chunk_id
        out[c.handle] = list(reversed(anc))
    return out


def _ref_chips(text: str) -> list[Any]:
    """The references a block makes, as terse hover-preview chips — the
    superset grammar (bracket/sigil forms ∪ bare ``kind:ref``), deduped
    by their navigate target so ``§kong24~2`` and ``paper:kong24~2`` (the
    same chunk) collapse to one chip. Each chip carries the cited quote
    on hover (``popover_chip``). Reuses the shared parser/grammar (DRY)."""
    seen: set[str] = set()
    chips: list[Any] = []

    def add(label: str, href: str, preview: str | None) -> None:
        if href in seen:
            return
        seen.add(href)
        chips.append(popover_chip(label, href, preview))

    def paper(slug: str, chunk: str | None, label: str) -> None:
        # chunk here is the regex group incl. leading ``~`` (or None).
        suffix = f"?chunk={chunk[1:]}" if chunk else ""
        add(label, f"/r/paper/{slug}{suffix}", f"/preview/paper/{slug}{suffix}")

    for ref in draft_markup.parse_references(text):
        if ref.cls == draft_markup.XREF:
            h = ref.target.lstrip("¶")
            add(ref.surface or ref.target, f"/c/{h}", f"/preview/chunk/{h}")
        elif ref.cls == draft_markup.CITE:
            m = mentions.DRAFT_CITE_PATTERN.fullmatch(ref.target)
            if m:
                paper(m.group("slug"), m.group("chunk"), ref.surface or ref.target)
        elif ref.cls == draft_markup.WEB:
            add(ref.surface or ref.target, ref.target, None)
        else:  # AUTHORING — a bracketed [[kind:id]]
            m = mentions.REF_PATTERN.fullmatch(ref.target)
            if m and m.group("kind") in mentions.LINKIFY_KINDS:
                k, i = m.group("kind"), m.group("id").lstrip("#")
                add(ref.surface or ref.target, f"/r/{k}/{i}", f"/preview/{k}/{i}")
    for kind, ident, chunk in mentions.extract_handles(text):
        i = ident.lstrip("#")
        if kind == "paper":  # collapse with the § form (same target)
            paper(i, chunk, f"{kind}:{ident}{chunk or ''}")
            continue
        suffix = f"?chunk={chunk[1:]}" if chunk else ""
        add(
            f"{kind}:{ident}{chunk or ''}",
            f"/r/{kind}/{i}{suffix}",
            f"/preview/{kind}/{i}{suffix}",
        )
    return chips


#: Request lifecycle ordering for the per-block list: active first, then
#: done/abandoned (which now *persist* so you can click in and debug the
#: LLM run, rather than vanishing on completion).
_REQUEST_ORDER = {"open": 0, "scheduled": 1, "doing": 2, "paused": 3}


def _requests_by_handle(
    store: Any, handles: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """ALL change-request todos anchored at each chunk (``meta.anchor =
    '¶<handle>'``), grouped by handle — including **done / won't-do**, so a
    finished request hangs around to click into (its ``plan_tick`` job's
    captured LLM transcript is the debugging surface). Active requests
    sort first; ``started`` (a job minted) gates the X-cancel."""
    if not handles:
        return {}
    anchors = [f"¶{h}" for h in handles]
    sql = (
        "SELECT r.ref_id, r.title, r.meta->>'anchor' AS anchor, "
        "  (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
        "    WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1) AS status, "
        "  EXISTS (SELECT 1 FROM refs j WHERE j.parent_id = r.ref_id "
        "          AND j.kind = 'job') AS started, "
        "  (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
        "    WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN' "
        "      AND t.value LIKE 'ask-user:%%' LIMIT 1) AS asking, "
        "  EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
        "    WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN' "
        "      AND t.value LIKE 'child-failed:%%') AS failed "
        "FROM refs r "
        "WHERE r.kind = 'todo' AND r.deleted_at IS NULL "
        "  AND r.meta->>'anchor' = ANY(%s)"
    )
    out: dict[str, list[dict[str, Any]]] = {}
    with store.pool.connection() as conn:
        rows = conn.execute(sql, (anchors,)).fetchall()
    for ref_id, title, anchor, status, started, asking, failed in rows:
        status = status or "open"
        handle = (anchor or "").lstrip("¶")
        # ``OPEN:ask-user:<slug>`` → a human-ish question ("see-chunk-0" →
        # "see chunk 0"). The slug is terse — the full reasoning is in the
        # job transcript the chip links to.
        ask = (asking or "").split("ask-user:", 1)[-1].replace("-", " ").strip()
        out.setdefault(handle, []).append(
            {
                "ref_id": ref_id,
                "title": (title or "").split("\n", 1)[0][:60],
                "status": status,
                "done": status in ("done", "won't-do"),
                # "started" = a plan_tick (or other) job minted; the
                # X-to-cancel only shows before that.
                "started": bool(started),
                # attention: waiting on the user, or a failed child job.
                "asking": ask,
                "failed": bool(failed),
            }
        )
    for reqs in out.values():
        reqs.sort(key=lambda r: _REQUEST_ORDER.get(r["status"], 9))
    return out


def _block_views(store: Any, ref_id: int) -> dict[str, dict[str, str]]:
    """Per-block keyword + llm-summary text for the view slider (body /
    summary / keywords). Populated by the chunk_keywords + llm_summarize
    workers; empty for a chunk they haven't reached yet (→ first-line
    fallback in the row)."""
    out: dict[str, dict[str, str]] = {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT c.handle, c.keywords, "
            "  (SELECT s.text FROM chunk_summaries s WHERE s.chunk_id = c.chunk_id "
            "    AND s.summarizer = 'llm-v1' LIMIT 1) AS summary "
            "FROM chunks c WHERE c.ref_id = %s AND c.retired_at IS NULL "
            "  AND c.pos IS NOT NULL AND c.ord >= 0",
            (ref_id,),
        ).fetchall()
    for handle, kws, summary in rows:
        out[handle] = {
            "keywords": ", ".join((kws or [])[:12]),
            "summary": (summary or "").strip(),
        }
    return out


def _rows_for(store: Any, ref: Any) -> list[dict[str, Any]]:
    """Per-block row context for the whole draft (content + ancestors +
    ref chips + requests + summary/keywords for the view slider)."""
    chunk_objs = store.reading_order(ref.id)
    anc = _ancestor_headings(chunk_objs)
    requests = _requests_by_handle(store, [c.handle for c in chunk_objs])
    views = _block_views(store, ref.id)
    # Recall highlight: every occurrence of a defined abbreviation gets a
    # hover-definition in the reader (one dict for the whole draft).
    abbrevs = store.defined_abbrevs(ref.id)
    rows: list[dict[str, Any]] = []
    for c in chunk_objs:
        v = views.get(c.handle, {})
        first_line = ((c.text or "").splitlines() or [""])[0][:140]
        rows.append(
            {
                "handle": c.handle,
                "chunk_kind": c.chunk_kind,
                "text": c.text,
                "depth": c.depth,
                "is_heading": c.chunk_kind == "heading",
                "ancestors": anc.get(c.handle, []),
                "abbrevs": abbrevs,
                "refs": _ref_chips(c.text),
                "requests": requests.get(c.handle, []),
                # view slider: summary falls back to keywords → first line;
                # keywords falls back to first line.
                "summary": v.get("summary") or v.get("keywords") or first_line,
                "keywords": v.get("keywords") or first_line,
            }
        )
    return rows


def _ref_view(ref: Any) -> dict[str, Any]:
    return {
        "ident": ref.slug or ref.id,
        "slug": ref.slug,
        "title": ref.title,
        "id": ref.id,
    }


@router.get("/drafts", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    store = get_store(request)
    refs = store.list_refs(kind="draft", limit=200)
    drafts = [
        {
            "ident": r.slug or r.id,
            "title": (r.title or r.slug or "untitled").split("\n", 1)[0],
            "slug": r.slug,
        }
        for r in refs
    ]
    return templates.TemplateResponse(
        request,
        "drafts/index.html.j2",
        {"active_tab": "drafts", "drafts": drafts},
    )


@router.get("/draft/{ident}")
async def reader_alias(ident: str) -> RedirectResponse:
    """Singular ``/draft/<id>`` → the canonical plural reader."""
    return RedirectResponse(url=f"/drafts/{ident}", status_code=303)


@router.get("/drafts/{ident}", response_class=HTMLResponse)
async def reader(request: Request, ident: str) -> Response:
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "active_tab": "drafts",
                "title": "Draft not found",
                "status": 404,
                "detail": f"no draft {ident!r}",
            },
            status_code=404,
        )
    return templates.TemplateResponse(
        request,
        "drafts/detail.html.j2",
        {
            "active_tab": "drafts",
            "ref": _ref_view(ref),
            "rows": _rows_for(store, ref),
        },
    )


@router.get("/drafts/{ident}/row/{handle}", response_class=HTMLResponse)
async def reader_row(request: Request, ident: str, handle: str) -> HTMLResponse:
    """One rendered row — the fragment a future live-refresh poll swaps in
    place (the page is composed from this same macro, so no rewrite)."""
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return HTMLResponse("", status_code=404)
    row = next((r for r in _rows_for(store, ref) if r["handle"] == handle), None)
    if row is None:
        return HTMLResponse("", status_code=404)
    return templates.TemplateResponse(
        request,
        "drafts/_row_fragment.html.j2",
        {"r": row, "ref": _ref_view(ref)},
    )


@router.get("/drafts/{ident}/rows", response_class=HTMLResponse)
async def reader_rows(request: Request, ident: str) -> HTMLResponse:
    """Just the rows (no page chrome) — what the live-refresh poll swaps
    into ``#doc`` when the version token bumps and nobody's mid-edit."""
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return HTMLResponse("", status_code=404)
    return templates.TemplateResponse(
        request,
        "drafts/_rows.html.j2",
        {"rows": _rows_for(store, ref), "ref": _ref_view(ref)},
    )


@router.get("/drafts/{ident}/version")
async def version(request: Request, ident: str) -> JSONResponse:
    """Monotone version token = max ``chunk_events.event_id`` over the
    draft's chunks. The future poll refetches changed rows when it bumps."""
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return JSONResponse({"version": 0})
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(ce.event_id), 0) FROM chunk_events ce "
            "JOIN chunks c ON c.chunk_id = ce.chunk_id WHERE c.ref_id = %s",
            (ref.id,),
        ).fetchone()
    return JSONResponse({"version": int(row[0]) if row else 0})


@router.get("/drafts/{ident}/find")
async def find(
    request: Request, ident: str, q: str = "", mode: str = "verbatim"
) -> JSONResponse:
    """In-draft find — the user's reader-side search bar.

    Returns the matching chunk handles, in the order the find bar
    cycles them with ‹ ›:

    * ``mode='verbatim'`` — case-insensitive substring over each live
      block's source text, in **document order** (a plain Ctrl-F over
      the prose, the deterministic path that needs no embedder).
    * ``mode='semantic'`` — cosine ranked (best-first) over the draft's
      chunk embeddings, scoped to this draft. Degrades to verbatim when
      the embedder is unavailable or the query won't embed.

    The client highlights/scrolls to each handle and cycles next/prev
    starting from whichever chunk is currently in view.
    """
    store = get_store(request)
    ref = _draft_ref(store, ident)
    q = q.strip()
    if ref is None or not q:
        return JSONResponse({"handles": [], "mode": mode})

    chunks = store.reading_order(ref.id)
    m = (mode or "verbatim").strip().lower()

    if m == "semantic":
        hub = getattr(get_runtime(request), "hub", None)
        embedder = getattr(hub, "embedder", None)
        vec = embed_query(embedder, q)
        if vec is not None:
            by_id = {c.chunk_id: c.handle for c in chunks}
            hits = store.search_blocks_semantic(
                query_vec=vec,
                scope_ref_id=ref.id,
                limit=200,
                max_distance=None,
            )
            handles = [by_id[b.id] for b, _ref, _d in hits if b.id in by_id]
            return JSONResponse({"handles": handles, "mode": "semantic"})
        m = "verbatim"  # no vector → degrade to a literal find

    needle = q.lower()
    handles = [c.handle for c in chunks if needle in (c.text or "").lower()]
    return JSONResponse({"handles": handles, "mode": "verbatim"})


@router.post("/drafts/{ident}/request")
async def request_change(
    request: Request,
    ident: str,
    handle: str = Form(...),
    text: str = Form(...),
) -> Response:
    """File a change request anchored at a chunk: a ``todo`` parented on
    the draft's project, carrying ``meta.anchor='¶<handle>'``. Flows
    through the normal todo tree → dispatch → jobs."""
    store = get_store(request)
    ref = _draft_ref(store, ident)
    back = f"/drafts/{ident}#c-{handle}"
    if ref is None or not text.strip():
        return RedirectResponse(url=back, status_code=303)
    project = _project_id(store, ref.id)
    args: dict[str, Any] = {
        "kind": "todo",
        "text": text.strip(),
        "meta": {"anchor": f"¶{handle}"},
    }
    if project is not None:
        args["parent_id"] = project
    return await redirect_or_error(
        request, "put", args, redirect=back, error_title="Change request error"
    )


#: Reviewer briefs for the per-heading "review ▾" dropdown. Each files an
#: anchored review-todo (→ plan_tick), scoped to the heading's subtree.
#: ``all`` files one todo that tells the planner to fan out sequentially.
_REVIEW_BRIEFS: dict[str, str] = {
    "structural": (
        "Structural review of the draft section under ¶{h}. Check it against "
        "the project brief: drift, contradictions with sibling sections, gaps, "
        "depth/fanout problems, weak or missing topic sentences. File concrete "
        "change requests (anchored at the offending chunks) for what to fix."
    ),
    "deep_review": (
        "Deep review of the draft section under ¶{h}. Scrutinise the rigor of "
        "every claim and citation — does each cited passage actually and "
        "strongly support its claim? Prune redundancy, rebalance, and flag "
        "anything overstated. File concrete change requests."
    ),
    "all": (
        "Review the draft section under ¶{h} thoroughly. Do this as SEQUENTIAL "
        "subtasks: (1) a structural review (drift, contradictions, gaps, topic-"
        "sentence structure), then (2) a deep review (claim/citation rigor, "
        "redundancy, overstatement). File concrete change requests from each."
    ),
}


@router.post("/drafts/{ident}/review")
async def review_block(
    request: Request,
    ident: str,
    handle: str = Form(...),
    reviewer: str = Form(...),
) -> Response:
    """Run a standard reviewer on a heading's subtree — files an anchored
    review-todo (parented on the draft's project) that runs as a plan_tick,
    showing up as an in-flight request on the block. ``reviewer`` is
    ``structural`` | ``deep_review`` | ``all``."""
    store = get_store(request)
    ref = _draft_ref(store, ident)
    back = f"/drafts/{ident}#c-{handle}"
    brief = _REVIEW_BRIEFS.get(reviewer)
    if ref is None or brief is None:
        return RedirectResponse(url=back, status_code=303)
    args: dict[str, Any] = {
        "kind": "todo",
        "text": brief.format(h=handle),
        "meta": {"anchor": f"¶{handle}", "review": reviewer},
    }
    project = _project_id(store, ref.id)
    if project is not None:
        args["parent_id"] = project
    return await redirect_or_error(
        request, "put", args, redirect=back, error_title="Review error"
    )


@router.post("/drafts/{ident}/todo/{todo_id}/delete")
async def delete_change_request(request: Request, ident: str, todo_id: int) -> Response:
    """Cancel a change-request todo anchored in this draft (the X on a
    not-yet-started in-flight chip). Soft-deletes via the todo handler."""
    back = f"/drafts/{ident}"
    return await redirect_or_error(
        request,
        "delete",
        {"kind": "todo", "id": todo_id},
        redirect=back,
        error_title="Delete change request error",
    )


@router.get("/c/{handle}")
async def goto_chunk(request: Request, handle: str) -> Response:
    """Resolve an opaque ``¶`` handle → redirect into its draft reader,
    anchored at the chunk. The click target of every ``¶`` anchor."""
    store = get_store(request)
    chunk = store.get_draft_chunk(handle)
    if chunk is None:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "active_tab": "drafts",
                "title": "Chunk not found",
                "status": 404,
                "detail": f"no chunk ¶{handle}",
            },
            status_code=404,
        )
    ref = store.get_ref(kind="draft", id=int(chunk.ref_id))
    ident = ref.slug if ref and ref.slug else chunk.ref_id
    return RedirectResponse(url=f"/drafts/{ident}#c-{handle}", status_code=303)


@router.get("/preview/chunk/{handle}", response_class=HTMLResponse)
async def preview_chunk(request: Request, handle: str) -> HTMLResponse:
    """Hover-popover fragment for a ``¶`` chunk anchor — peer of the
    ``/preview/{kind}/{id}`` route, reusing the same popover template."""
    store = get_store(request)
    chunk = store.get_draft_chunk(handle)
    if chunk is None:
        return templates.TemplateResponse(
            request,
            "preview/popover.html.j2",
            {"kind": "chunk", "label": f"¶{handle}", "missing": True},
        )
    # Show the chunk's verbatim text (≤ ~20 lines) as the quote — the
    # "what does ¶handle actually say?" a hover should answer.
    text = chunk.text or ""
    lines = text.splitlines()
    quote = "\n".join(lines[:20]) + ("\n…" if len(lines) > 20 else "")
    if len(quote) > 1600:
        quote = quote[:1600].rstrip() + "…"
    return templates.TemplateResponse(
        request,
        "preview/popover.html.j2",
        {
            "kind": chunk.chunk_kind,
            "label": f"¶{handle}",
            "ref_id": handle,
            "title": f"¶{handle}",
            "quote": quote.strip() or "(empty)",
            "chunk_label": "",
            "body_preview": "",
            "deleted": False,
            "missing": False,
        },
    )
