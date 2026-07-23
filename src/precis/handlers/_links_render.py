"""Shared "Links:" TOON sub-section render — F8, extracted for reuse.

``NumericRefHandler._render_links_section`` (the compact inline table
appended to every numeric-ref ``get()``) used to be the only kind of
handler that could render it — every ``Handler``-direct kind (paper,
draft, structure, cad, pcb, plan, pres, patent) had no equivalent, so
an agent reading e.g. a paper never saw its link graph
(``OPEN-ITEMS.md`` graph-completeness audit item 1; blocks the inbound
half of ``docs/design/citation-chunk-grounding.md``).

:func:`render_links_section` is the free-standing extraction —
``NumericRefHandler`` delegates to it unchanged (pure refactor, no
behaviour change), and every ``Handler``-direct kind calls it directly
from a new ``view='links'`` arm, registered in that kind's view enum.
Kept free-standing (matches the ``_link_tag_ops`` / ``_slug_ref_shared``
style already used across handlers) rather than a mixin, since the only
shared state needed is ``store`` + the one ``ref``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from precis.response import Response
from precis.utils import handle_registry

if TYPE_CHECKING:
    from precis.store import Ref, Store


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


def render_links_section(store: Store, ref: Ref) -> str:
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
    combined.sort(key=lambda pair: pair[0].id)
    for link, direction in combined:
        if direction == "out":
            other_id, other_pos = link.dst_ref_id, link.dst_pos
            rel_marker = _format_outbound_rel(link.relation)
        else:
            other_id, other_pos = link.src_ref_id, link.src_pos
            rel_marker = _format_inbound_rel(link.relation)
        target = _format_target_handle(other_id, other_pos, endpoints)
        teaser = _teaser_for(endpoints.get(other_id))
        get_call = _get_call_for(endpoints.get(other_id), other_id)
        rows.append(
            {
                "related to": f"{rel_marker} {target}".strip(),
                "keywords": teaser,
                "how to get": get_call,
            }
        )

    from precis.format import render_agent_table

    return "\n\nLinks:\n" + render_agent_table(
        rows, schema=["related to", "keywords", "how to get"]
    )


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
    ref_id: int, pos: int | None, endpoints: dict[int, Ref]
) -> str:
    """Build ``kind:identifier[~pos]`` for the link row."""
    ref = endpoints.get(ref_id)
    if ref is None:
        handle = f"<unknown ref {ref_id}>"
    else:
        ident = ref.slug if ref.slug is not None else str(ref.id)
        # ADR 0036: ref-level → record universal handle; block-level keeps
        # the legacy ``kind:slug~pos`` (valid input; chunk_id unavailable).
        if pos is None:
            handle = handle_registry.try_format(ref.kind, ref.id) or (
                f"{ref.kind}:{ident}"
            )
        else:
            handle = f"{ref.kind}:{ident}~{pos}"
        if ref.deleted_at is not None:
            handle += " (deleted)"
        return handle
    if pos is not None:
        handle += f"~{pos}"
    return handle


def _teaser_for(ref: Ref | None) -> str:
    """First ~60 chars of the target's title — the keyword stand-in."""
    if ref is None or not ref.title:
        return ""
    title = ref.title.strip().replace("\n", " ")
    if len(title) > 60:
        return title[:60].rstrip() + "…"
    return title


def _get_call_for(ref: Ref | None, fallback_id: int) -> str:
    """Render the exact ``get(...)`` call to retrieve the link target."""
    if ref is None:
        return f"get(id={fallback_id})"
    ident = ref.slug if ref.slug is not None else ref.id
    ident_repr = repr(ident) if isinstance(ident, str) else str(ident)
    return f"get(kind={ref.kind!r}, id={ident_repr})"


__all__ = ["render_links_section", "render_links_view"]
