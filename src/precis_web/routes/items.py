"""``/items`` — retired into the unified Drive surface (WS1a).

The cross-kind search + facet + presenter engine this module built
(the unified item view) is now served at
``/drive`` (``routes/drive.py``), grafted onto Drive's folder tree +
CRUD + per-row actions per
the web-UI rationalization's Drive workstream. This
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
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from precis.store._mappers import SEMANTIC_DISTANCE_FLOOR
from precis_web.deps import get_store
from precis_web.item_view import COMPONENT_BADGE_SPECS, item_row
from precis_web.routes.flags import FLAG_NAMESPACE, FLAG_VALUE_LIST

if TYPE_CHECKING:
    from precis.store.store import Store

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
#: source doc), and the captured conversations (``conv`` — the Discord /
#: Slack bridge threads and AI follow-up discussions, each turn an embedded
#: body chunk). Kinds with no embedded chunks contribute nothing, so an
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
    "news",
    "youtube",
    "perplexity-reasoning",
    "perplexity-research",
    "websearch",
    "semanticscholar",
    "oracle",
    "math",
    "memory",
    "conv",
)

#: Design/artifact kinds that predate ``KindSpec.placement`` and so are
#: declared ``placement='stream'`` by default (component/material/pcb —
#: see ``KindSpec.placement`` in ``protocol.py``), even though in every
#: sense that matters to a human browsing Drive they're an authored
#: design the operator made, not a collected source. ``se``/``structure``/
#: ``figure`` already carry ``placement='artifact'`` and reach Drive
#: through the live-hub ``artifact_kinds()`` facet (``item_view.py``) —
#: this is the small hand-curated remainder placement introspection
#: alone won't catch. Deliberately *not* folded into
#: ``_DEFAULT_SOURCE_KINDS`` above, which stays the literal "Source"
#: facet row (collected/ingested docs) — ``routes/drive.py`` unions this
#: list into the *default search scope* instead, keeping the Source row
#: semantically honest. The coupled taxonomy audit (see that constant's
#: own docstring) will want a real placement bucket for this; noting the
#: gap here rather than solving it structurally.
_DESIGN_KINDS: tuple[str, ...] = ("pcb", "component", "material")

#: Results per page — shared by the /drive + /items browse and search
#: lists. Large by design: this is a self-hosted daily-use tool where
#: scanning a long queue in one page (e.g. the "Stubs (to get)" download
#: queue) beats clicking through many short pages. Per-row work is all
#: batched (one query each for flags/chunks/tags/identifiers), so a wide
#: page stays cheap.
_PAGE_SIZE = 100


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


def _se_summaries_bulk(store: Store, ref_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Per-``se``-design level/block/bound-structure summary, batched over
    ``ref_ids`` (the page's ``se`` rows only) in one query — the same
    columns ``routes/design.py``'s ``_levels`` reads per block (never
    geometry), aggregated to one row per design since a Drive row has
    space for a badge, not a tree. Empty input skips the query."""
    if not ref_ids:
        return {}
    sql = """
        SELECT ref_id,
               count(*)                                                AS n_blocks,
               bool_or(envelope IS NOT NULL)                            AS has_l1,
               bool_or(dof IS NOT NULL OR objectives IS NOT NULL
                       OR process_overrides IS NOT NULL)                AS has_l2,
               bool_or(bound_kind IS NOT NULL)                          AS has_l3,
               count(*) FILTER (WHERE bound_kind = 'structure')         AS n_bound
          FROM se_blocks
         WHERE ref_id = ANY(%s) AND retired_at IS NULL
         GROUP BY ref_id
    """
    with store.pool.connection() as conn:
        rows = conn.execute(sql, (ref_ids,)).fetchall()
    out: dict[int, dict[str, Any]] = {}
    for ref_id, n_blocks, has_l1, has_l2, has_l3, n_bound in rows:
        level = "L3" if has_l3 else "L2" if has_l2 else "L1" if has_l1 else "L0"
        out[int(ref_id)] = {
            "level": level,
            "blocks": int(n_blocks),
            "bound_structures": int(n_bound),
        }
    return out


def _structure_summaries_bulk(
    store: Store, ref_ids: list[int]
) -> dict[int, dict[str, Any]]:
    """Per-``structure`` atom/run/energy-ladder summary, batched over
    ``ref_ids`` — the same three facts ``routes/structure.py``'s
    ``_list_rows`` shows per row, three small queries here restricted to
    this page's ids instead of one query per every live structure. Empty
    input skips all three queries."""
    if not ref_ids:
        return {}
    with store.pool.connection() as conn:
        atom_rows = conn.execute(
            "SELECT ref_id, count(*) FROM struct_atoms "
            "WHERE ref_id = ANY(%s) AND retired_version IS NULL "
            "GROUP BY ref_id",
            (ref_ids,),
        ).fetchall()
        run_rows = conn.execute(
            "SELECT ref_id, count(*) FROM struct_runs "
            "WHERE ref_id = ANY(%s) GROUP BY ref_id",
            (ref_ids,),
        ).fetchall()
        last_rows = conn.execute(
            "SELECT DISTINCT ON (ref_id) ref_id, energy, fidelity "
            "FROM struct_runs "
            "WHERE ref_id = ANY(%s) AND status = 'succeeded' "
            "AND energy IS NOT NULL "
            "ORDER BY ref_id, id DESC",
            (ref_ids,),
        ).fetchall()
    out: dict[int, dict[str, Any]] = {
        int(rid): {"atoms": 0, "runs": 0, "last_energy": None, "last_fidelity": None}
        for rid in ref_ids
    }
    for rid, n in atom_rows:
        out[int(rid)]["atoms"] = int(n)
    for rid, n in run_rows:
        out[int(rid)]["runs"] = int(n)
    for rid, energy, fidelity in last_rows:
        out[int(rid)]["last_energy"] = float(energy)
        out[int(rid)]["last_fidelity"] = fidelity
    return out


def _component_summaries_bulk(
    store: Store, ref_ids: list[int]
) -> dict[int, dict[str, Any]]:
    """Current value of the Drive-row badge specs
    (:data:`precis_web.item_view.COMPONENT_BADGE_SPECS`) per component,
    batched over ``ref_ids`` — one ``component_spec_values`` query,
    ``DISTINCT ON`` per ``(component_ref_id, spec_id)`` picking the current
    value the same way ``store/_component_ops.py``'s ``_CURRENT_ORDER``
    does for a single component. Empty input skips the query."""
    if not ref_ids:
        return {}
    sql = """
        SELECT DISTINCT ON (component_ref_id, spec_id)
               component_ref_id, spec_id, value_text
          FROM component_spec_values
         WHERE component_ref_id = ANY(%s) AND spec_id = ANY(%s)
         ORDER BY component_ref_id, spec_id, as_of DESC NULLS LAST, created_at DESC
    """
    with store.pool.connection() as conn:
        rows = conn.execute(sql, (ref_ids, list(COMPONENT_BADGE_SPECS))).fetchall()
    out: dict[int, dict[str, Any]] = {}
    for rid, spec_id, value_text in rows:
        out.setdefault(int(rid), {})[spec_id] = value_text
    return out


def _design_facts_bulk(store: Store, refs: list[Any]) -> dict[int, dict[str, Any]]:
    """Batched per-kind design facts for the ``se``/``structure``/
    ``component`` Drive-row presenters (``item_view.py``'s
    ``SePresenter``/``StructurePresenter``/``ComponentPresenter``) — one
    query per kind actually present among ``refs`` (never fired for a page
    with none of the three), keyed by ref id so :func:`item_row` can pass
    each ref its own facts dict. ``pcb``/``material``/``figure`` need no
    query at all (their facts live on ``ref.meta``, already loaded per
    ref), so they're not in here."""
    by_kind: dict[str, list[int]] = {}
    for r in refs:
        by_kind.setdefault(getattr(r, "kind", ""), []).append(r.id)
    out: dict[int, dict[str, Any]] = {}
    out.update(_se_summaries_bulk(store, by_kind.get("se", [])))
    out.update(_structure_summaries_bulk(store, by_kind.get("structure", [])))
    out.update(_component_summaries_bulk(store, by_kind.get("component", [])))
    return out


def _run_search(
    store: Store,
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
    hits = store.chunks.search_chunks_across_kinds(
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
    summaries = store.chunks.chunk_summaries_bulk(
        [(ref.id, block.ord) for block, ref, _ in hits]
    )
    design_facts = _design_facts_bulk(store, [ref for _, ref, _ in hits])
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
            summary=summaries.get((ref.id, block.ord)),
            design=design_facts.get(ref.id),
        )
        for block, ref, score in hits
    ]
    return rows, has_next


def _recent_rows(
    store: Store,
    kinds: list[str],
    tags: list[str],
    has_pdf: bool | None,
    folder_id: int | None,
    offset: int,
    *,
    has_chunks: bool | None = None,
    has_schedule: bool | None = None,
    has_external_id: bool | None = None,
    unfiled_only: bool = False,
    ref_ids: list[int] | None = None,
    deleted: bool = False,
    oldest: bool = False,
    untried: bool = False,
    downloadable_first: bool = False,
) -> tuple[list[dict[str, Any]], bool]:
    """The no-query landing: most-recently-*edited* source items, newest
    first (``recent_refs`` orders by ``updated_at``, so a re-worked draft
    bubbles up; or least-recently-edited first when ``oldest`` — the
    ``sort=oldest`` facet; or untried-attempts-first when ``untried`` — the
    ``sort=untried`` facet / the downloads queue's default), optionally
    narrowed by the tag chips, the stub
    filter (``has_pdf=False`` → only stubs, the "papers to get" queue), the
    fetchable-id filter (``has_external_id=True`` → only refs with a
    DOI/arXiv/S2 — paired with ``has_pdf=False`` by the "Stubs (to get)" queue
    so id-less, non-fetchable papers don't crowd it), the
    ingested/chunk-less filter (``has_chunks`` — the "chunked"/"unchunked"
    state facet), the folder facet (``folder_id`` — one folder's direct
    children; only artifact kinds carry a ``parent_id``, so this is a no-op
    for pure source rows), ``unfiled_only`` (the default top-level view — hide
    anything filed into a folder, so a folder's contents live only inside it),
    the ``ref_ids`` allow-list (the ``/drive?cited_by=<draft>`` scope — a
    draft's papers-to-fetch set; an empty list restricts to nothing), and
    ``deleted`` (the "show deleted" toggle — soft-deleted refs instead of live
    ones). Rows carry no preview (no query) — name, kind, when-edited, badges,
    tags, links, flags. Returns
    ``(rows, has_next)`` via the same over-fetch-one-extra probe as
    :func:`_run_search`.
    """
    refs = store.recent_refs(
        kinds,
        tags=tags or None,
        has_pdf=has_pdf,
        has_chunks=has_chunks,
        has_schedule=has_schedule,
        has_external_id=has_external_id,
        parent_id=folder_id,
        unfiled_only=unfiled_only,
        ref_ids=ref_ids,
        deleted=deleted,
        oldest=oldest,
        untried=untried,
        downloadable_first=downloadable_first,
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
    design_facts = _design_facts_bulk(store, refs)
    rows = [
        item_row(
            r,
            None,
            0.0,
            flag_state.get(r.id, set()),
            has_chunks=r.id in ingested,
            tags=tags_bulk.get(r.id),
            identifier=idents.get(r.id),
            design=design_facts.get(r.id),
        )
        for r in refs
    ]
    return rows, has_next


# store stays Any: tests pass a hand-rolled fake narrower than Store
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
    merged Drive surface.
    """
    suffix = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(url=f"/drive{suffix}")
