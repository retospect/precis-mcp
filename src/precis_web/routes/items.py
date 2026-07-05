"""Unified item view (``/items``) — one cross-kind search surface.

The Slice-3 front-end over the Slice-2 primitive
(``Store.search_chunks_across_kinds``): one query box that searches the
chunks of a *set* of kinds at once (semantic + lexical, RRF-fused),
shows one best-matching chunk per ref, and orders by relevance or
recency — the human twin of what the LLM gets from the ``search`` verb.
Each row carries the reading-intent flag buttons and a click-through to
the kind's own reader.

Additive: this retires nothing yet. Retiring ``/drive`` /
``/papers-needed`` / triage / ``/refs`` / ``/tags/refs`` into filters on
this page is later Slice-3 work (see the proposal). Read-only.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from precis.store._mappers import SEMANTIC_DISTANCE_FLOOR
from precis_web.deps import get_runtime, get_store, templates
from precis_web.item_view import item_row
from precis_web.routes.flags import FLAG_DEFS, FLAG_NAMESPACE, FLAG_VALUE_LIST

router = APIRouter(prefix="/items", tags=["items"])

#: Default kind set when the query doesn't name any — the block-searchable
#: *source* kinds (ingested documents + cached external answers). Kinds
#: with no embedded chunks contribute nothing, so an over-broad list is
#: harmless; the coupled taxonomy audit will formalise this set.
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
)

#: Results per page.
_PAGE_SIZE = 30

#: Machine / structural namespaces kept out of the tag cloud — they're
#: high-cardinality control tags (``STATUS:running``, ``DREAM:*``) that
#: would swamp the topical vocabulary a browsing human wants to see.
_CLOUD_EXCLUDE_NS: frozenset[str] = frozenset(
    {"STATUS", "DREAM", "PRIO", "SRC", "CACHE", "EMBED", "LLM", "ROLE3", "CLASSIFY"}
)

#: How many tags the cloud shows, and the font-size buckets (smallest →
#: largest) it maps usage counts onto.
_CLOUD_SIZE = 40
_CLOUD_FONTS = ("text-xs", "text-sm", "text-base", "text-lg", "text-xl")


def _tag_cloud(store: Any) -> list[dict[str, Any]]:
    """Top topical tags sized by usage — a browse-by-vocabulary entry.

    Pulls the most-used tags (excluding machine namespaces), buckets
    each count onto a font size, and links to the existing ``/tags/refs``
    pivot. Degrades to empty on any store hiccup — a cloud is a nicety,
    never a page-breaker.
    """
    try:
        raw = store.list_all_tags(page_size=_CLOUD_SIZE * 3)
    except Exception:
        return []
    tags = [
        (ns, val, n) for (ns, val, n) in raw if ns not in _CLOUD_EXCLUDE_NS and n > 0
    ][:_CLOUD_SIZE]
    if not tags:
        return []
    top = max(n for _, _, n in tags)
    out: list[dict[str, Any]] = []
    for ns, val, n in sorted(tags, key=lambda t: (t[0], t[1])):
        # Bucket the count onto a font size (linear over the range).
        idx = min(len(_CLOUD_FONTS) - 1, (n * len(_CLOUD_FONTS)) // (top + 1))
        label = val if ns == "OPEN" else f"{ns}:{val}"
        out.append(
            {
                "label": label,
                "href": f"/tags/refs?namespace={ns}&value={val}",
                "count": n,
                "font": _CLOUD_FONTS[idx],
            }
        )
    return out


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
) -> list[dict[str, Any]]:
    """Blocking search + row-build; runs in a worker thread.

    Embeds the query once (degrading to lexical if the embedder is
    absent or warming), runs the cross-kind primitive, then batches the
    flag state for the whole page so the toggle buttons render active.
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
        limit=_PAGE_SIZE,
        max_distance=SEMANTIC_DISTANCE_FLOOR,
    )
    ref_ids = [ref.id for _, ref, _ in hits]
    flag_state = store.ref_tag_values(ref_ids, FLAG_NAMESPACE, FLAG_VALUE_LIST)
    tags_bulk = store.ref_tags_bulk(ref_ids)
    # A search hit matched a chunk, so the ref is ingested by definition.
    return [
        item_row(
            ref,
            block,
            score,
            flag_state.get(ref.id, set()),
            has_chunks=True,
            tags=tags_bulk.get(ref.id),
        )
        for block, ref, score in hits
    ]


def _recent_rows(store: Any, kinds: list[str]) -> list[dict[str, Any]]:
    """The no-query landing: most-recently-added source items, newest
    first. No matching chunk (there's no query), so rows carry no preview
    — just name, kind, when-added, the stub/ingested badges, and flags."""
    refs = store.recent_refs(kinds, limit=_PAGE_SIZE)
    ref_ids = [r.id for r in refs]
    flag_state = store.ref_tag_values(ref_ids, FLAG_NAMESPACE, FLAG_VALUE_LIST)
    ingested = store.refs_with_body_chunks(ref_ids)
    tags_bulk = store.ref_tags_bulk(ref_ids)
    return [
        item_row(
            r,
            None,
            0.0,
            flag_state.get(r.id, set()),
            has_chunks=r.id in ingested,
            tags=tags_bulk.get(r.id),
        )
        for r in refs
    ]


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    q: str = "",
    kinds: str = "",
    sort: str = "relevance",
    since: str = "",
    until: str = "",
) -> HTMLResponse:
    """Unified cross-kind search over the source kinds.

    ``q=`` runs the search; ``kinds=`` (comma-list) narrows the set;
    ``sort=recency`` orders newest-first; ``since=`` / ``until=`` (ISO
    date) bound the date window. With no ``q`` the page is just the
    search form.
    """
    store = get_store(request)
    q = (q or "").strip()
    kind_list = [k.strip() for k in kinds.split(",") if k.strip()] or list(
        _DEFAULT_SOURCE_KINDS
    )
    sort = "recency" if (sort or "").strip().lower() == "recency" else "relevance"
    since_dt = _parse_date(since)
    until_dt = _parse_date(until)

    rows: list[dict[str, Any]] = []
    recent: list[dict[str, Any]] = []
    cloud: list[dict[str, Any]] = []
    if q:
        runtime = get_runtime(request)
        embedder = getattr(getattr(runtime, "hub", None), "embedder", None)
        rows = await asyncio.to_thread(
            _run_search,
            store,
            embedder,
            kinds=kind_list,
            q=q,
            sort=sort,
            since=since_dt,
            until=until_dt,
        )
    else:
        # Default landing: a browse-by-vocabulary tag cloud + recent things
        # under the search apparatus.
        cloud = await asyncio.to_thread(_tag_cloud, store)
        recent = await asyncio.to_thread(_recent_rows, store, kind_list)

    # Where a flag toggle bounces back to — this exact search.
    return_to = request.url.path + (
        f"?{request.url.query}" if request.url.query else ""
    )

    return templates.TemplateResponse(
        request,
        "items/index.html.j2",
        {
            "active_tab": "items",
            "q": q,
            "kinds": kinds,
            "sort": sort,
            "since": since,
            "until": until,
            "rows": rows,
            "recent": recent,
            "cloud": cloud,
            "flag_defs": FLAG_DEFS,
            "return_to": return_to,
        },
    )
