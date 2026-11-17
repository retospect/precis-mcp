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

from typing import TYPE_CHECKING, Any

from precis import profiles
from precis._result_budget import body_bytes, row_size_cell
from precis.errors import BadInput, ResultBudgetExceeded
from precis.handlers._capped_section import DEFAULT_CHILD_ROW_CAP, render_capped_section
from precis.response import Response
from precis.utils import handle_registry
from precis.utils.link_tag_ops import link_why_suffix
from precis.utils.surface_scope import masked_ref_placeholder

if TYPE_CHECKING:
    from collections.abc import Callable

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


# Change B: rel-priority order for ``priority=True`` — the claim-graph
# relations first (gr627629: ``establishes``/``corroborates``/``disputes``/
# ``contradicts``/``refines`` are the edges a paper's link rollup exists
# to surface, and inbound-citation floods were burying them inside the
# capped bare-get preview), then citation/support/contradiction/
# correction/retraction chains, then the catch-all ``related-to``, which
# always sorts last regardless of this list. Any relation not listed here
# (a plugin relation, say) sorts after every listed one but still
# ahead of ``related-to``.
_REL_PRIORITY: list[str] = [
    "establishes",
    "corroborates",
    "disputes",
    "contradicts",
    "contradicted-by",
    "refines",
    "cites",
    "cited-by",
    "supports",
    "supported-by",
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
DEFAULT_LINK_ROW_CAP = DEFAULT_CHILD_ROW_CAP


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
    rel: str | None = None,
    of_kind: str | None = None,
    inbound_only: bool = False,
) -> str:
    """F8: render the Links: TOON sub-section for a single-ref get.

    Columns: ``{related to	keywords	how to get	size [kB]
    (default 1kB)}``. Column 1
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
        rel: When set, keep only edges whose stored relation equals it
            (matched on the raw ``links.relation`` in *both* directions —
            ``rel='touched'`` keeps outbound AND inbound touched edges,
            so "edges of this relation touching me" is one call). The
            filter exists because a heavily-linked ref's full view can
            run to hundreds of KB (gr514335).
        of_kind: When set, keep only links whose *other* endpoint is a
            ref of that kind — the same other-endpoint filter
            ``NumericRefHandler._render_links_view`` already exposes as
            ``args={'of_kind': …}`` (gr472990).
        inbound_only: When ``True``, render only the ``<-`` rows — the
            dispatch-level default-read section for kinds whose own
            ``get`` prints no Links block (gr702986).

    Both kwargs default to a no-op so every existing callsite (the
    numeric-ref per-``get()`` append, ``render_links_view``) renders
    byte-identical output to before this signature grew.
    """
    prepared = _select_link_pairs(
        store, ref, rel=rel, of_kind=of_kind, inbound_only=inbound_only
    )
    if prepared is None:
        return ""
    combined, endpoints, ref_sizes, chunk_sizes = prepared

    def render(selected: list[tuple[Link, str]], total: int) -> str:
        rows = [
            _link_row(
                lnk,
                d,
                ref=ref,
                endpoints=endpoints,
                ref_sizes=ref_sizes,
                chunk_sizes=chunk_sizes,
            )
            for lnk, d in selected
        ]
        truncated = len(selected) < total

        from precis.format import render_agent_table

        header = f"Links ({limit} of {total}):" if truncated else "Links:"
        out = f"\n\n{header}\n" + render_agent_table(
            rows,
            schema=["related to", "keywords", "how to get", "size [kB] (default 1kB)"],
        )
        return out

    handle = handle_registry.try_format(ref.kind, ref.id) or (
        ref.slug if ref.slug is not None else ref.id
    )
    return render_capped_section(
        combined,
        limit=limit,
        key=_priority_sort_key if priority else lambda pair: pair[0].id,
        render=render,
        overflow=lambda n: (
            f"\n+{n} more · get(kind={ref.kind!r}, id={handle!r}, view='links')"
        ),
    )


def _select_link_pairs(
    store: Store,
    ref: Ref,
    *,
    rel: str | None = None,
    of_kind: str | None = None,
    inbound_only: bool = False,
) -> (
    tuple[list[tuple[Link, str]], dict[int, Ref], dict[int, int], dict[int, int]] | None
):
    """The ref's ``(link, 'out'|'in')`` pairs after profile/rel/kind
    filters, plus the render context (endpoint refs, ref body sizes,
    chunk text sizes) — or ``None`` when no row renders. Shared by
    :func:`render_links_section` and :func:`render_links_view` so the
    inline section and the standalone view draw one row set."""
    out_links = [] if inbound_only else store.links_for(ref.id, direction="out")
    in_links = store.links_for(ref.id, direction="in")
    if not out_links and not in_links:
        return None

    endpoint_ids: set[int] = set()
    for link in out_links:
        endpoint_ids.add(link.dst_ref_id)
    for link in in_links:
        endpoint_ids.add(link.src_ref_id)
    endpoints = store.fetch_refs_by_ids(endpoint_ids)
    # Another owner's (or a deleted) profile is no row at all (gr490266).
    hidden = profiles.hidden_ids(store, endpoints.values())
    if hidden:
        out_links = [lk for lk in out_links if lk.dst_ref_id not in hidden]
        in_links = [lk for lk in in_links if lk.src_ref_id not in hidden]
        if not out_links and not in_links:
            return None

    if rel is not None:
        out_links = [lk for lk in out_links if lk.relation == rel]
        in_links = [lk for lk in in_links if lk.relation == rel]
    if of_kind is not None:
        out_links = [
            lk
            for lk in out_links
            if getattr(endpoints.get(lk.dst_ref_id), "kind", None) == of_kind
        ]
        in_links = [
            lk
            for lk in in_links
            if getattr(endpoints.get(lk.src_ref_id), "kind", None) == of_kind
        ]
    if not out_links and not in_links:
        return None

    combined = [(lnk, "out") for lnk in out_links] + [(lnk, "in") for lnk in in_links]

    # Row sizes from stored lengths only — one bulk read over the
    # endpoint refs' body bytes plus one over the chunk-handle rows'
    # own text (the fetchable thing each "how to get" names).
    ref_sizes = store.chunks.body_byte_counts(endpoint_ids)
    chunk_ids = {
        c
        for lnk, d in combined
        for c in ((lnk.dst_chunk_id if d == "out" else lnk.src_chunk_id),)
        if c is not None
    }
    chunk_sizes: dict[int, int] = {}
    if chunk_ids:
        with store.pool.connection() as conn:
            for cid, nbytes in conn.execute(
                "SELECT chunk_id, octet_length(text) FROM chunks "
                "WHERE chunk_id = ANY(%s)",
                (list(chunk_ids),),
            ).fetchall():
                chunk_sizes[int(cid)] = int(nbytes)
    return combined, endpoints, ref_sizes, chunk_sizes


def _link_row(
    link: Link,
    direction: str,
    *,
    ref: Ref,
    endpoints: dict[int, Ref],
    ref_sizes: dict[int, int],
    chunk_sizes: dict[int, int],
) -> dict[str, str]:
    """One ``{related to, keywords, how to get, size [kB] (default 1kB)}``
    table row.

    The ``related to`` cell names the far endpoint, then ``via`` the
    focus's own chunk when the edge pins one (gr627629 — a chunk-grounded
    edge used to prove *where on the neighbour* it lands while hiding
    *which of this ref's passages* carries the claim), then the edge's
    why."""
    if direction == "out":
        other_id, other_pos = link.dst_ref_id, link.dst_ord
        other_chunk_id = link.dst_chunk_id
        own_chunk_id, own_ord = link.src_chunk_id, link.src_ord
        rel_marker = _format_outbound_rel(link.relation)
    else:
        other_id, other_pos = link.src_ref_id, link.src_ord
        other_chunk_id = link.src_chunk_id
        own_chunk_id, own_ord = link.dst_chunk_id, link.dst_ord
        rel_marker = _format_inbound_rel(link.relation)
    # gr555998 round 3: an endpoint this surface may not read —
    # an out-of-scope conv or a ref of a gated kind — collapses
    # to the same opaque placeholder the eye rings print; the
    # row still shows the edge exists but carries no handle,
    # title, size or get-call.
    placeholder = masked_ref_placeholder(endpoints.get(other_id))
    if placeholder is not None:
        return {
            "related to": f"{rel_marker} {placeholder}".strip(),
            "keywords": "",
            "how to get": "",
            "size [kB] (default 1kB)": "",
        }
    target = _format_target_handle(other_id, other_pos, other_chunk_id, endpoints)
    teaser = _teaser_for(endpoints.get(other_id))
    get_call = _get_call_for(
        endpoints.get(other_id),
        other_id,
        pos=other_pos,
        chunk_id=other_chunk_id,
    )
    n_bytes = (
        chunk_sizes.get(other_chunk_id, 0)
        if other_chunk_id is not None
        else ref_sizes.get(other_id, 0)
    )
    return {
        "related to": (
            f"{rel_marker} {target}"
            f"{_via_suffix(ref, own_chunk_id, own_ord)}"
            f"{link_why_suffix(link.meta)}"
        ).strip(),
        "keywords": teaser,
        "how to get": get_call,
        # Every row names a size — sub-kB bodies report decimal
        # kB (gr568219, gr573411); the column header names the
        # unit and the hidden ~1kB default.
        "size [kB] (default 1kB)": row_size_cell(n_bytes),
    }


def _via_suffix(ref: Ref, chunk_id: int | None, ord_: int | None) -> str:
    """`` via pc<id> ~<ord>`` — the focus's own chunk handle + position a
    chunk-pinned edge grounds at on *this* side. Empty for a ref-level
    edge or a focus kind with no chunk code (memory chunks are not
    agent-addressable)."""
    if chunk_id is None:
        return ""
    handle = handle_registry.try_format(ref.kind, chunk_id, chunk=True)
    if handle is None:
        return ""
    return f" via {handle} ~{ord_}" if ord_ is not None else f" via {handle}"


#: Rows a ``view='links'`` page renders by default (gr627629): before the
#: cap the unfiltered view grew until the response byte budget truncated
#: mid-table, with no exact continuation; a row cap keeps the default
#: call inside the budget and the footer's ``cursor`` resumes exactly
#: where the page ended.
_LINKS_VIEW_PAGE = 100


def render_links_view(
    store: Store,
    ref: Ref,
    *,
    sense: str | None = None,
    rel: str | None = None,
    of_kind: str | None = None,
    limit: Any = None,
    cursor: str | None = None,
) -> Response:
    """``view='links'`` for a ``Handler``-direct kind (paper, draft, …).

    Wraps the same compact table :func:`render_links_section` draws with
    a header so it stands alone as a full ``Response`` — the shape every
    ``Handler``-direct kind's ``view='links'`` arm delegates to. Numeric-ref
    kinds don't need this: they get the section appended to every
    ``get()`` automatically (see ``NumericRefHandler.get``) plus their
    own richer ``view='links'`` (:meth:`NumericRefHandler._render_links_view`).

    ``rel`` / ``of_kind`` narrow the view (a hot ref's unfiltered graph
    can run to hundreds of KB — gr514335); the header echoes the active
    filter so a partial graph never reads as the whole graph. Rows page
    at ``limit`` (``args={'limit': N}``, default
    :data:`_LINKS_VIEW_PAGE`); when a page leaves rows unshown the footer
    carries ``next:`` with the exact ``args={'cursor':
    '<bucket>:<link_id>'}`` continuation — outbound edges first, each
    bucket in link-id order, so paging covers every row once (gr627629).
    The footer reports ``shown S of T`` and ends ``complete`` on the last
    page: a partial response never implies full coverage.
    """
    noun = sense or ref.kind
    filt = ""
    if rel is not None:
        filt += f" rel={rel!r}"
    if of_kind is not None:
        filt += f" kind={of_kind!r}"
    if filt:
        filt = f" ({filt.strip()})"
    prepared = _select_link_pairs(store, ref, rel=rel, of_kind=of_kind)
    if prepared is None:
        if filt and (
            store.links_for(ref.id, direction="out")
            or store.links_for(ref.id, direction="in")
        ):
            # The ref HAS edges — the filter hid them all; say so or
            # "(no links)" reads as an empty graph.
            return Response(
                body=(
                    f"# {noun} {ref.id} - links{filt}\n\n"
                    f"(no links matching{filt})\n\n"
                    f"drop the filter: get(kind={ref.kind!r}, id={ref.id}, "
                    "view='links')"
                )
            )
        return Response(
            body=(
                f"# {noun} {ref.id} - links\n\n(no links)\n\n"
                f"add one with: link(kind={ref.kind!r}, id={ref.id}, "
                "target='kind:identifier', rel='related-to')"
            )
        )
    combined, endpoints, ref_sizes, chunk_sizes = prepared
    ordered = sorted(combined, key=lambda pair: (pair[1] != "out", pair[0].id))
    total = len(ordered)
    page_size = _links_page_size(limit, ref)
    start = _links_page_start(ordered, cursor, ref)
    page = ordered[start : start + page_size]
    rows = [
        _link_row(
            lnk,
            d,
            ref=ref,
            endpoints=endpoints,
            ref_sizes=ref_sizes,
            chunk_sizes=chunk_sizes,
        )
        for lnk, d in page
    ]
    from precis.format import render_agent_table

    handle = handle_registry.try_format(ref.kind, ref.id) or (
        ref.slug if ref.slug is not None else ref.id
    )

    def page_body(shown: int) -> str:
        """The page body carrying exactly ``shown`` table rows (``1`` to
        ``len(page)``) — the indivisible unit ``budget_fit`` cuts on. The
        header, ``shown`` count and ``next:`` cursor are recomputed for
        the cut so a byte-fitted page still resumes exactly where its
        last rendered row ended."""
        end = start + shown
        table_head = (
            "Links:" if start == 0 and end == total else f"Links ({shown} of {total}):"
        )
        out = f"# {noun} {ref.id} - links{filt}\n\n{table_head}\n"
        out += render_agent_table(
            rows[:shown],
            schema=["related to", "keywords", "how to get", "size [kB] (default 1kB)"],
        )
        if end < total:
            next_args: dict[str, Any] = {
                "cursor": f"{page[shown - 1][1]}:{page[shown - 1][0].id}"
            }
            if limit is not None:
                next_args["limit"] = page_size
            if rel is not None:
                next_args["rel"] = rel
            if of_kind is not None:
                next_args["of_kind"] = of_kind
            out += (
                f"\nshown {end} of {total} — next: "
                f"get(kind={ref.kind!r}, id={handle!r}, view='links', "
                f"args={next_args!r})"
            )
        else:
            out += f"\nshown {end} of {total} — complete"
        return out

    body = page_body(len(page))
    return Response(
        body=body,
        result_count=len(page),
        budget_fit=_links_view_fit(
            page_body, full_body=body, page_rows=len(page), ref=ref, handle=handle
        ),
    )


def _links_view_fit(
    render_page: Callable[[int], str],
    *,
    full_body: str,
    page_rows: int,
    ref: Ref,
    handle: str | int,
) -> Callable[[int], str]:
    """The ``budget_fit`` for an over-cap ``view='links'`` page.

    The row cap keeps a default page under ``PRECIS_MAX_BODY_BYTES`` in
    the common case, but a page of wide rows (CJK titles and reasons
    count 3 bytes a character) can still overrun it — and the generic
    line-fit used to cut the footer, dropping the ``next:`` cursor and
    restoring the search hint (review td681248). Instead the page cuts
    at a whole-row boundary, keeps its ``shown S of T — next:`` tail,
    and the cursor points at the last row that survived the cut, so
    following it still covers every link once."""
    total = body_bytes(full_body)

    def fit(cap: int) -> str:
        if total <= cap:
            return full_body
        lo, hi = 0, page_rows
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if body_bytes(render_page(mid)) <= cap:
                lo = mid
            else:
                hi = mid - 1
        if lo == 0:
            raise ResultBudgetExceeded(
                f"get: links page is {total} bytes and even one link row "
                f"does not fit under the {cap}-byte result budget",
                next=(
                    f"get(kind={ref.kind!r}, id={handle!r}, view='links', "
                    "args={'limit': 1})"
                ),
            )
        return render_page(lo)

    return fit


def _links_page_size(limit: Any, ref: Ref) -> int:
    """``args={'limit': N}`` for ``view='links'`` — a non-integer or
    non-positive value is a caller error, never a silent full dump (the
    dump is what overflowed the byte cap, gr627629)."""
    if limit is None:
        return _LINKS_VIEW_PAGE
    try:
        n = int(str(limit))
    except (TypeError, ValueError):
        n = 0
    if n < 1:
        raise BadInput(
            f"links view limit must be a positive integer, got {limit!r}",
            next=f"get(kind={ref.kind!r}, id={ref.id}, view='links')",
        )
    return n


def _links_page_start(
    ordered: list[tuple[Link, str]], cursor: str | None, ref: Ref
) -> int:
    """First row index strictly past the ``<bucket>:<link_id>`` cursor —
    resuming *after* the named row keeps the union of pages equal to the
    whole set with no repeats. A cursor whose edge was deleted or
    filtered out since still resumes at its sorted position rather than
    rewinding to page one."""
    if cursor is None:
        return 0
    bucket, sep, raw = str(cursor).partition(":")
    link_id: int | None = None
    if sep and bucket in ("out", "in"):
        try:
            link_id = int(raw.strip())
        except ValueError:
            link_id = None
    if link_id is None:
        raise BadInput(
            "links cursor must be '<bucket>:<link_id>' with bucket 'out' "
            f"or 'in', got {cursor!r}",
            next=f"get(kind={ref.kind!r}, id={ref.id}, view='links')",
        )
    mark = (bucket != "out", link_id)
    for i, (lnk, d) in enumerate(ordered):
        if (d != "out", lnk.id) > mark:
            return i
    return len(ordered)


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
