"""Unified item view (``/items``) — one cross-kind search surface.

The Slice-3 front-end over the Slice-2 primitive
(``Store.search_chunks_across_kinds``): one query box that searches the
chunks of a *set* of kinds at once (semantic + lexical, RRF-fused),
shows one best-matching chunk per ref, and orders by relevance or
recency — the human twin of what the LLM gets from the ``search`` verb.
Each row carries the reading-intent flag buttons, a hover peek + optional
thumbnail (the ``ItemPresenter`` contract, ``precis_web/item_view.py``),
and a click-through to the kind's own reader. ``page=`` pages past the
30-item window; the kind chips split into a "Source" facet and an
"Author" facet (``role='artifact'`` kinds); a folder facet narrows the
no-query landing to one folder's direct children.

Additive: this retires nothing yet — none of ``/drive`` /
``/papers-needed`` / ``/papers/triage`` / ``/refs`` / ``/tags/refs``
reduce to a clean filter-preset here without losing functionality this
page doesn't have yet (folder CRUD, per-row quick-actions, watch-dir
info, deleted-ref visibility — see ``OPEN-ITEMS.md``). Read-only.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from precis.store._mappers import SEMANTIC_DISTANCE_FLOOR
from precis_web.deps import get_runtime, get_store, templates
from precis_web.item_view import artifact_kinds, item_row
from precis_web.routes.flags import FLAG_DEFS, FLAG_NAMESPACE, FLAG_VALUE_LIST

router = APIRouter(prefix="/items", tags=["items"])


def _tag_filter_string(ns: str, value: str) -> str:
    """Canonical tag-filter string for the search verb — ``OPEN`` tags are
    bare, closed axes are ``NAMESPACE:value`` (what ``build_tag_filter``
    parses)."""
    return value if ns == "OPEN" else f"{ns}:{value}"


#: Default kind set when the query doesn't name any — the block-searchable
#: kinds: ingested documents, cached external answers, and the authored /
#: reflective notes (``memory`` — the reviewer digests and the dream
#: ``DREAM:*`` speculations, whose ``memory_body`` chunk is embedded like a
#: source doc). Kinds with no embedded chunks contribute nothing, so an
#: over-broad list is harmless; the coupled taxonomy audit will formalise
#: this set.
_DEFAULT_SOURCE_KINDS: tuple[str, ...] = (
    "paper",
    "patent",
    "datasheet",
    "cfp",
    "pres",
    "web",
    "wikipedia",
    "youtube",
    "perplexity-reasoning",
    "perplexity-research",
    "websearch",
    "semanticscholar",
    "oracle",
    "math",
    "memory",
)

#: Results per page.
_PAGE_SIZE = 30


def _parse_date(raw: str) -> datetime | None:
    """Parse a ``since=``/``until=`` box into a tz-aware datetime, or None.

    Invalid input degrades to None (the filter is simply not applied) —
    a browse box shouldn't 500 on a half-typed date.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _run_search(
    store: Any,
    embedder: Any,
    *,
    kinds: list[str],
    q: str,
    sort: str,
    since: datetime | None,
    until: datetime | None,
    tags: list[str],
    offset: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Blocking search + row-build; runs in a worker thread.

    Embeds the query once (degrading to lexical if the embedder is
    absent or warming), runs the cross-kind primitive filtered by the
    selected ``tags``, then batches the flag/tag state for the whole page.
    Over-fetches one extra hit past ``_PAGE_SIZE`` to probe "has next
    page" without a separate count query; returns ``(rows, has_next)``.
    """
    query_vec = None
    if embedder is not None:
        try:
            query_vec = embedder.embed_one(q)
        except Exception:
            query_vec = None
    hits = store.search_chunks_across_kinds(
        kinds=kinds,
        q=q,
        query_vec=query_vec,
        sort=sort,
        since=since,
        until=until,
        tags=tags or None,
        limit=_PAGE_SIZE + 1,
        offset=offset,
        max_distance=SEMANTIC_DISTANCE_FLOOR,
    )
    has_next = len(hits) > _PAGE_SIZE
    hits = hits[:_PAGE_SIZE]
    ref_ids = [ref.id for _, ref, _ in hits]
    flag_state = store.ref_tag_values(ref_ids, FLAG_NAMESPACE, FLAG_VALUE_LIST)
    tags_bulk = store.ref_tags_bulk(ref_ids)
    idents = store.paper_identifiers(ref_ids)
    # A search hit matched a chunk, so the ref is ingested by definition.
    rows = [
        item_row(
            ref,
            block,
            score,
            flag_state.get(ref.id, set()),
            has_chunks=True,
            tags=tags_bulk.get(ref.id),
            identifier=idents.get(ref.id),
        )
        for block, ref, score in hits
    ]
    return rows, has_next


def _recent_rows(
    store: Any,
    kinds: list[str],
    tags: list[str],
    has_pdf: bool | None,
    folder_id: int | None,
    offset: int,
) -> tuple[list[dict[str, Any]], bool]:
    """The no-query landing: most-recently-added source items, newest
    first, optionally narrowed by the tag chips, the stub filter
    (``has_pdf=False`` → only stubs, the "papers to get"), and the
    folder facet (``folder_id`` — one folder's direct children; only
    artifact kinds carry a ``parent_id``, so this is a no-op for pure
    source rows). Rows carry no preview (no query) — name, kind,
    when-added, badges, tags, links, flags. Returns ``(rows, has_next)``
    via the same over-fetch-one-extra probe as :func:`_run_search`.
    """
    refs = store.recent_refs(
        kinds,
        tags=tags or None,
        has_pdf=has_pdf,
        parent_id=folder_id,
        limit=_PAGE_SIZE + 1,
        offset=offset,
    )
    has_next = len(refs) > _PAGE_SIZE
    refs = refs[:_PAGE_SIZE]
    ref_ids = [r.id for r in refs]
    flag_state = store.ref_tag_values(ref_ids, FLAG_NAMESPACE, FLAG_VALUE_LIST)
    ingested = store.refs_with_body_chunks(ref_ids)
    tags_bulk = store.ref_tags_bulk(ref_ids)
    idents = store.paper_identifiers(ref_ids)
    rows = [
        item_row(
            r,
            None,
            0.0,
            flag_state.get(r.id, set()),
            has_chunks=r.id in ingested,
            tags=tags_bulk.get(r.id),
            identifier=idents.get(r.id),
        )
        for r in refs
    ]
    return rows, has_next


def _folder_options(store: Any) -> list[dict[str, Any]]:
    """Flat, indented folder list for the folder-facet ``<select>`` —
    the raw ``list_folders()`` edges walked depth-first (mirrors
    ``/drive``'s own tree flatten, kept separate so this router doesn't
    couple to Drive's richer per-folder child-count view)."""
    edges = store.list_folders()
    by_parent: dict[int | None, list[tuple[int, str]]] = {}
    for ref_id, title, parent_id in edges:
        by_parent.setdefault(parent_id, []).append((ref_id, title))

    out: list[dict[str, Any]] = []

    def walk(parent: int | None, depth: int) -> None:
        for ref_id, title in by_parent.get(parent, []):
            out.append({"id": ref_id, "label": ("— " * depth) + (title or "")})
            walk(ref_id, depth + 1)

    walk(None, 0)
    return out


@router.get("/tags/suggest")
async def tags_suggest(request: Request, q: str = "") -> JSONResponse:
    """Autocomplete backend for the tag-filter chips — substring tag
    matches as JSON ``[{label, tag}]`` (``tag`` is the filter string to
    submit). Empty/1-char queries return nothing."""
    q = (q or "").strip()
    if len(q) < 2:
        return JSONResponse([])
    store = get_store(request)
    rows = await asyncio.to_thread(store.suggest_tags, q, limit=10)
    return JSONResponse(
        [
            {"label": _tag_filter_string(ns, val), "tag": _tag_filter_string(ns, val)}
            for ns, val, _n in rows
        ]
    )


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    q: str = "",
    sort: str = "relevance",
    since: str = "",
    until: str = "",
    k: list[str] = Query(default_factory=list),
    tag: list[str] = Query(default_factory=list),
    state: str = "all",
    folder: str = "",
    page: int = 1,
    submitted: str = "",
) -> HTMLResponse:
    """Unified cross-kind search over the source + author kinds.

    ``q=`` runs the search; ``k=`` (repeated, one per checked kind)
    narrows the set — both the default "Source" chips and the "Author"
    facet (artifact kinds: draft/cad/structure/…, per ``KindSpec.role``);
    ``tag=`` (repeated) are the tag-filter chips; ``state=stub`` shows
    only stubs (papers still to get — they behave like a to-do);
    ``folder=`` (a folder ``ref_id``) narrows the no-query landing to one
    folder's direct children (the Drive-style facet); ``sort=recency``
    orders newest-first; ``since=`` / ``until=`` bound the date window;
    ``page=`` pages past the ``_PAGE_SIZE`` cap. With no ``q`` the landing
    shows the recent list.

    Kind selection persists in an ``items_kinds`` cookie: an explicit
    submit (``submitted=1``) sets it; a fresh visit reads it (or defaults
    to every source kind). This is the "remembered checkboxes" behaviour.
    """
    store = get_store(request)
    q = (q or "").strip()

    # Resolve the kind set: an explicit submit uses exactly the checked
    # boxes (empty = none); a fresh visit uses the cookie, else all.
    if submitted:
        selected_kinds = [x.strip() for x in k if x.strip()]
    else:
        cookie = request.cookies.get("items_kinds", "")
        selected_kinds = [x for x in cookie.split(",") if x] or list(
            _DEFAULT_SOURCE_KINDS
        )
    tags = [t.strip() for t in tag if t.strip()]
    sort = "recency" if (sort or "").strip().lower() == "recency" else "relevance"
    state = (state or "all").strip().lower()
    # ``state=stub`` → only PDF-less papers (the "to get" queue). Stubs have
    # no chunks, so this only shapes the recent/browse view, not search.
    has_pdf = False if state == "stub" else None
    since_dt = _parse_date(since)
    until_dt = _parse_date(until)
    folder_raw = (folder or "").strip()
    folder_id = int(folder_raw) if folder_raw.isdigit() else None
    page = max(1, page)
    offset = (page - 1) * _PAGE_SIZE

    runtime = get_runtime(request)
    hub = getattr(runtime, "hub", None)
    artifact_kind_defs = artifact_kinds(hub)

    rows: list[dict[str, Any]] = []
    recent: list[dict[str, Any]] = []
    has_next = False
    if q:
        embedder = getattr(hub, "embedder", None)
        rows, has_next = await asyncio.to_thread(
            _run_search,
            store,
            embedder,
            kinds=selected_kinds,
            q=q,
            sort=sort,
            since=since_dt,
            until=until_dt,
            tags=tags,
            offset=offset,
        )
    else:
        # Default landing: the recent list (narrowed by the tag chips,
        # the stub filter, and the folder facet) under the search
        # apparatus.
        recent, has_next = await asyncio.to_thread(
            _recent_rows, store, selected_kinds, tags, has_pdf, folder_id, offset
        )

    # Where a flag toggle bounces back to — this exact search.
    return_to = request.url.path + (
        f"?{request.url.query}" if request.url.query else ""
    )

    # Pager links preserve every filter, only ``page`` changes.
    _pager_params: list[tuple[str, str]] = [("submitted", "1")]
    if q:
        _pager_params.append(("q", q))
    _pager_params.append(("sort", sort))
    if since:
        _pager_params.append(("since", since))
    if until:
        _pager_params.append(("until", until))
    if state != "all":
        _pager_params.append(("state", state))
    if folder_raw:
        _pager_params.append(("folder", folder_raw))
    for kk in selected_kinds:
        _pager_params.append(("k", kk))
    for t in tags:
        _pager_params.append(("tag", t))

    def _page_url(n: int) -> str:
        return "/items?" + urlencode([*_pager_params, ("page", n)])

    resp = templates.TemplateResponse(
        request,
        "items/index.html.j2",
        {
            "active_tab": "items",
            "q": q,
            "kind_defs": list(_DEFAULT_SOURCE_KINDS),
            "artifact_kind_defs": artifact_kind_defs,
            "selected_kinds": selected_kinds,
            "tags": tags,
            "sort": sort,
            "since": since,
            "until": until,
            "state": state,
            "folder": folder_raw,
            "folder_options": await asyncio.to_thread(_folder_options, store),
            "rows": rows,
            "recent": recent,
            "flag_defs": FLAG_DEFS,
            "return_to": return_to,
            "page": page,
            "has_next": has_next,
            "prev_url": _page_url(page - 1) if page > 1 else None,
            "next_url": _page_url(page + 1) if has_next else None,
        },
    )
    if submitted:
        # Remember the kind selection for the next visit (90 days).
        resp.set_cookie("items_kinds", ",".join(selected_kinds), max_age=90 * 24 * 3600)
    return resp
