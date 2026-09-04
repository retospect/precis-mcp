"""Shared "Links:" TOON sub-section render — F8, extracted for reuse.

``NumericRefHandler._render_links_section`` (the compact inline table
appended to every numeric-ref ``get()``) used to be the only kind of
handler that could render it — every ``Handler``-direct kind (paper,
draft, structure, cad, pcb, plan, pres, patent) had no equivalent, so
an agent reading e.g. a paper never saw its link graph — which also
blinded the inbound half of citation-chunk grounding (the
``inbound_chase`` citer sweep + ``_citer_sidecar`` verdict render).

:func:`render_links_section` is the free-standing extraction —
``NumericRefHandler`` delegates to it unchanged (pure refactor, no
behaviour change), and every ``Handler``-direct kind calls it directly
from a new ``view='links'`` arm, registered in that kind's view enum.
Kept free-standing (matches the ``_link_tag_ops`` / ``_slug_ref_shared``
style already used across handlers) rather than a mixin, since the only
shared state needed is ``store`` + the one ``ref``.

Relation hygiene (citation-chunk grounding): a chunk-scoped ``cites``
edge is *evidential* — citation-graph-confirmed, located to a chunk,
carrying a support verdict. Topical similarity ("close but doesn't
cite") must never ride ``cites``; it belongs on ``related-to`` +
``meta.note`` (a type-2 similarity pass — not built).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from precis.response import Response
from precis.utils import handle_registry

if TYPE_CHECKING:
    from precis.store import Link, Ref, Store


# F8: rel-name → inbound-passive-form. Symmetric rels (no passive form)
# map to themselves; unknown rels fall through to the ``<- <rel>``
# prefix rendering in :func:`render_links_section`.
_INVERSE_REL: dict[str, str] = {
    "related-to": "related-to",
    "cites": "cited by",
    "refutes": "refuted by",
    "supersedes": "superseded by",
    "supports": "supported by",
    "contradicts": "contradicted by",
    "cited-by": "cites",
    "retracted-by": "retracts",
}


# Change B: rel-priority order for ``priority=True`` — evidential
# relations (citation/support/contradiction/correction/retraction
# chains) surface before the catch-all ``related-to``, which always
# sorts last regardless of this list. Any relation not listed here
# (a plugin relation, say) sorts after every listed one but still
# ahead of ``related-to``.
_REL_PRIORITY: list[str] = [
    "cites",
    "cited-by",
    "supports",
    "supported-by",
    "contradicts",
    "contradicted-by",
    "refutes",
    "refuted-by",
    "corrects",
    "corrected-by",
    "retracts",
    "retracted-by",
    "derived-from",
    "supersedes",
    "superseded-by",
]
_REL_PRIORITY_INDEX: dict[str, int] = {rel: i for i, rel in enumerate(_REL_PRIORITY)}


#: gr311679: the shared bare-``get`` link-row cap. gr311344 first capped
#: the numeric-ref bare-``get`` append at a bare literal (``limit=12``,
#: matching what ``paper.py``'s overview append already used); named here
#: as a single constant so every capped callsite agrees on one number
#: instead of two literals drifting apart, and bumped to 20 — enough
#: headroom for ``priority=True`` to surface the full evidential set
#: (cites/supports/contradicts/…) on most refs before falling back to the
#: catch-all ``related-to`` bucket, while still keeping the response
#: bounded regardless of how many thousands of links a hot quest/gripe
#: accumulates. The full graph stays reachable via the overflow line's
#: ``view='links'`` pointer.
DEFAULT_LINK_ROW_CAP = 20


def _priority_sort_key(pair: tuple[Link, str]) -> tuple[int, int]:
    """Sort key for ``priority=True``: rel-priority bucket, then link.id."""
    link, _direction = pair
    rel = link.relation
    if rel == "related-to":
        bucket = len(_REL_PRIORITY) + 1
    else:
        bucket = _REL_PRIORITY_INDEX.get(rel, len(_REL_PRIORITY))
    return (bucket, link.id)


def render_links_section(
    store: Store,
    ref: Ref,
    *,
    limit: int | None = None,
    priority: bool = False,
) -> str:
    """F8: render the Links: TOON sub-section for a single-ref get.

    Three columns: ``{related to	keywords	how to get}``. Column 1
    holds ``<rel-marker> <target>`` — ``--`` for default ``related-to``
    (no semantic relation specified), the literal rel name otherwise.
    Inbound rows use the passive form via :data:`_INVERSE_REL`
    (``cites`` → ``cited by``); unknown inbound rels fall back to a
    ``<- <rel>`` prefix so direction stays visible.

    Returns an empty string when the ref has no links in either
    direction — the caller appends unconditionally, so the empty case
    must produce no output (not even a trailing newline).

    Teaser column = first ~60 chars of the target's title. The F8
    design called for "keywords" but the project doesn't yet expose a
    ``Store.keywords_for_ref`` helper; title is the portable fallback.
    Upgrade path: swap the call here when a keyword API lands.

    Args:
        limit: When set and the total link count exceeds it, render
            only the first ``limit`` rows (after sorting) and append
            an overflow line pointing at ``view='links'`` for the
            rest. ``None`` (default) renders every link — unchanged
            behaviour.
        priority: When ``True``, sort by evidential-relation priority
            (see :data:`_REL_PRIORITY`) instead of by link id — used
            by the Change B paper-overview append so the most
            load-bearing edges (cites, supports, contradicts, …)
            surface first under a hard cap. ``False`` (default)
            preserves the original link-id ordering — unchanged
            behaviour.

    Both kwargs default to a no-op so every existing callsite (the
    numeric-ref per-``get()`` append, ``render_links_view``) renders
    byte-identical output to before this signature grew.
    """
    out_links = store.links_for(ref.id, direction="out")
    in_links = store.links_for(ref.id, direction="in")
    if not out_links and not in_links:
        return ""

    endpoint_ids: set[int] = set()
    for link in out_links:
        endpoint_ids.add(link.dst_ref_id)
    for link in in_links:
        endpoint_ids.add(link.src_ref_id)
    endpoints = store.fetch_refs_by_ids(endpoint_ids)

    rows: list[dict[str, str]] = []
    combined = [(lnk, "out") for lnk in out_links] + [(lnk, "in") for lnk in in_links]
    if priority:
        combined.sort(key=_priority_sort_key)
    else:
        combined.sort(key=lambda pair: pair[0].id)

    total = len(combined)
    truncated = limit is not None and total > limit
    if truncated:
        assert limit is not None  # narrows for mypy; guarded by ``truncated``
        combined = combined[:limit]

    for link, direction in combined:
        if direction == "out":
            other_id, other_pos = link.dst_ref_id, link.dst_ord
            other_chunk_id = link.dst_chunk_id
            rel_marker = _format_outbound_rel(link.relation)
        else:
            other_id, other_pos = link.src_ref_id, link.src_ord
            other_chunk_id = link.src_chunk_id
            rel_marker = _format_inbound_rel(link.relation)
        target = _format_target_handle(other_id, other_pos, other_chunk_id, endpoints)
        teaser = _teaser_for(endpoints.get(other_id))
        get_call = _get_call_for(
            endpoints.get(other_id), other_id, pos=other_pos, chunk_id=other_chunk_id
        )
        rows.append(
            {
                "related to": f"{rel_marker} {target}".strip(),
                "keywords": teaser,
                "how to get": get_call,
            }
        )

    from precis.format import render_agent_table

    header = f"Links ({limit} of {total}):" if truncated else "Links:"
    out = f"\n\n{header}\n" + render_agent_table(
        rows, schema=["related to", "keywords", "how to get"]
    )
    if truncated:
        assert limit is not None  # narrows for mypy; guarded by ``truncated``
        n_more = total - limit
        handle = handle_registry.try_format(ref.kind, ref.id) or (
            ref.slug if ref.slug is not None else ref.id
        )
        out += f"\n+{n_more} more · get(kind={ref.kind!r}, id={handle!r}, view='links')"
    return out


def render_links_view(store: Store, ref: Ref, *, sense: str | None = None) -> Response:
    """``view='links'`` for a ``Handler``-direct kind (paper, draft, …).

    Wraps :func:`render_links_section`'s compact table with a header so
    it stands alone as a full ``Response`` — the shape every
    ``Handler``-direct kind's ``view='links'`` arm delegates to. Numeric-ref
    kinds don't need this: they get the section appended to every
    ``get()`` automatically (see ``NumericRefHandler.get``) plus their
    own richer ``view='links'`` (:meth:`NumericRefHandler._render_links_view`).
    """
    noun = sense or ref.kind
    section = render_links_section(store, ref)
    if not section:
        return Response(
            body=(
                f"# {noun} {ref.id} - links\n\n(no links)\n\n"
                f"add one with: link(kind={ref.kind!r}, id={ref.id}, "
                "target='kind:identifier', rel='related-to')"
            )
        )
    return Response(body=f"# {noun} {ref.id} - links" + section)


def _format_outbound_rel(relation: str) -> str:
    """``--`` for default ``related-to``; literal rel name otherwise."""
    if relation == "related-to":
        return "--"
    return relation


def _format_inbound_rel(relation: str) -> str:
    """Inverse-form for known rels; ``<- <rel>`` fallback."""
    if relation == "related-to":
        return "--"
    inv = _INVERSE_REL.get(relation)
    if inv is not None:
        return inv
    return f"<- {relation}"


def _format_target_handle(
    ref_id: int, pos: int | None, chunk_id: int | None, endpoints: dict[int, Ref]
) -> str:
    """Build the universal handle for the link row's endpoint.

    Chunk-level edge (``chunk_id`` set) → the *chunk* handle ``pc<id>`` /
    ``dc<id>`` (the granular, durable address that lets the citation tree
    resolve to the exact supporting passage). Ref-level edge → the record
    handle ``pa<id>`` / ``dr<id>``. A ``pos`` with no ``chunk_id`` (should
    not happen — ``pos`` derives from the chunk join) falls back to the
    legacy ``kind:slug~pos`` form.
    """
    ref = endpoints.get(ref_id)
    if ref is None:
        handle = f"<unknown ref {ref_id}>"
        if chunk_id is not None:
            handle += f" chunk {chunk_id}"
        elif pos is not None:
            handle += f"~{pos}"
        return handle
    if chunk_id is not None:
        handle = (
            handle_registry.try_format(ref.kind, chunk_id, chunk=True)
            or f"{ref.kind}:chunk:{chunk_id}"
        )
    elif pos is None:
        handle = handle_registry.try_format(ref.kind, ref.id) or (
            f"{ref.kind}:{ref.slug if ref.slug is not None else ref.id}"
        )
    else:
        ident = ref.slug if ref.slug is not None else str(ref.id)
        handle = f"{ref.kind}:{ident}~{pos}"
    if ref.retired_at is not None:
        handle += " (deleted)"
    return handle


def _teaser_for(ref: Ref | None) -> str:
    """First ~60 chars of the target's title — the keyword stand-in."""
    if ref is None or not ref.title:
        return ""
    title = ref.title.strip().replace("\n", " ")
    if len(title) > 60:
        return title[:60].rstrip() + "…"
    return title


#: Kinds whose chunks are addressed in ``get`` by their universal handles chunk
#: handle (``dc<id>``) rather than the paper-family ``slug~ord`` selector.
#: The draft/plan/figure/mermaid chunk-tree family (see
#: ``handlers/draft.py`` ``_is_draft_chunk_handle``); everything else
#: (paper/patent/…) reads a chunk via ``slug~pos``.
_CHUNK_HANDLE_GET_KINDS = frozenset({"draft", "plan", "figure", "mermaid"})


def _get_call_for(
    ref: Ref | None,
    fallback_id: int,
    *,
    pos: int | None = None,
    chunk_id: int | None = None,
) -> str:
    """Render the exact ``get(...)`` call to retrieve the link target.

    A chunk-level endpoint (``chunk_id`` set) renders the chunk-scoped
    retrieval, not the whole-document one: the draft family reads a chunk
    by its ``dc<id>`` handle; the paper family reads it by ``slug~ord``.
    ``ref is None`` (target row not fetched) or a chunk with no resolvable
    address falls back to the record-level get.
    """
    if ref is None:
        return f"get(id={fallback_id})"
    ident = ref.slug if ref.slug is not None else ref.id
    if chunk_id is not None:
        if ref.kind in _CHUNK_HANDLE_GET_KINDS:
            chunk_handle = handle_registry.try_format(ref.kind, chunk_id, chunk=True)
            if chunk_handle is not None:
                return f"get(kind={ref.kind!r}, id={chunk_handle!r})"
        elif pos is not None and ref.slug is not None:
            return f"get(kind={ref.kind!r}, id={f'{ref.slug}~{pos}'!r})"
    ident_repr = repr(ident) if isinstance(ident, str) else str(ident)
    return f"get(kind={ref.kind!r}, id={ident_repr})"


__all__ = ["DEFAULT_LINK_ROW_CAP", "render_links_section", "render_links_view"]
