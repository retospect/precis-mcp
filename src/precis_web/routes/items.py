"""``/items`` — retired into the unified Drive surface (WS1a).

The cross-kind search + facet + presenter engine this module built
(Slice-3 of ``docs/proposals/unified-item-view.md``) is now served at
``/drive`` (``routes/drive.py``), grafted onto Drive's folder tree +
CRUD + per-row actions per
``docs/proposals/web-ui-rationalization.md``'s Workstream 1. This
module keeps the ``/items`` path alive as a redirect (old bookmarks,
saved searches, in-flight links) — WS1b/WS4 own its final retirement,
not this slice.

The query-independent helpers below (``_DEFAULT_SOURCE_KINDS``,
``_parse_date``, ``_run_search``, ``_recent_rows``, ``_folder_options``,
``_tag_filter_string``, ``_PAGE_SIZE``) are unit-tested directly and
imported by ``routes/drive.py`` as the reusable engine — kept here
rather than duplicated.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from precis.store._mappers import SEMANTIC_DISTANCE_FLOOR
from precis_web.deps import get_store
from precis_web.item_view import item_row
from precis_web.routes.flags import FLAG_NAMESPACE, FLAG_VALUE_LIST

router = APIRouter(prefix="/items", tags=["items"])
log = logging.getLogger(__name__)


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
    *,
    has_chunks: bool | None = None,
    deleted: bool = False,
) -> tuple[list[dict[str, Any]], bool]:
    """The no-query landing: most-recently-added source items, newest
    first, optionally narrowed by the tag chips, the stub filter
    (``has_pdf=False`` → only stubs, the "papers to get" queue), the
    ingested/chunk-less filter (``has_chunks`` — the "chunked"/"unchunked"
    state facet), the folder facet (``folder_id`` — one folder's direct
    children; only artifact kinds carry a ``parent_id``, so this is a no-op
    for pure source rows), and ``deleted`` (the "show deleted" toggle —
    soft-deleted refs instead of live ones). Rows carry no preview (no
    query) — name, kind, when-added, badges, tags, links, flags. Returns
    ``(rows, has_next)`` via the same over-fetch-one-extra probe as
    :func:`_run_search`.
    """
    refs = store.recent_refs(
        kinds,
        tags=tags or None,
        has_pdf=has_pdf,
        has_chunks=has_chunks,
        parent_id=folder_id,
        deleted=deleted,
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
    # Guards a corrupted/cyclic parent_id chain (a folder that is its own
    # ancestor) from recursing forever and stack-overflowing the whole
    # /drive page — mirrors the visited-set style of drive._breadcrumb's
    # parent walk.
    seen: set[int] = set()

    def walk(parent: int | None, depth: int) -> None:
        for ref_id, title in by_parent.get(parent, []):
            if ref_id in seen:
                log.warning(
                    "folder %s revisits an ancestor in its own parent chain; "
                    "skipping cyclic branch",
                    ref_id,
                )
                continue
            seen.add(ref_id)
            out.append({"id": ref_id, "label": ("— " * depth) + (title or "")})
            walk(ref_id, depth + 1)

    walk(None, 0)
    return out


@router.get("/tags/suggest")
async def tags_suggest(request: Request, q: str = "") -> JSONResponse:
    """Autocomplete backend for the tag-filter chips — substring tag
    matches as JSON ``[{label, tag}]`` (``tag`` is the filter string to
    submit). Empty/1-char queries return nothing. Kept at both this
    legacy path and ``/drive/tags/suggest`` (same function, two routes —
    see ``routes/drive.py``)."""
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


@router.get("")
@router.get("/")
async def index(request: Request) -> Response:
    """Redirect to ``/drive``, preserving every filter verbatim — the
    merged surface (WS1a of ``docs/proposals/web-ui-rationalization.md``).
    """
    suffix = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(url=f"/drive{suffix}")
