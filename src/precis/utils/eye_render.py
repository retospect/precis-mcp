"""Per-kind eye render (ADR 0051 §6) — an eye's *neighborhood* depends on its
kind, so the ladder generalizes but its shape does not:

- **Tree kinds** (``draft`` / ``plan``): reading-order neighborhood — the
  :func:`precis.utils.fisheye.render_fisheye` span + reference ring.
- **Non-tree refs** (``memory`` / ``finding`` / ``paper`` / ``patent`` /
  ``web`` / …): the ref renders as its document at the extent (title → gist →
  body), and at ``fisheye+1hop`` it grows its **link neighborhood** — every ref
  linked to it, **either direction**, shown with its **relation type**
  (``supports:`` / ``related-to:`` / …). Links are symmetric, so a note linked
  to a paper (or a paper *chunk* ``pc``) surfaces when you fisheye the paper,
  and the paper surfaces when you fisheye the note. (The graduated reading-order
  span over paper/web chunks is a follow-up; today the body renders flat.)

``skill`` is file-backed (``resolve_handle`` returns ``None``), so an eye on a
skill needs the handler, not this store-only path — a documented follow-up.

Ships dark; the composer (:func:`precis.utils.working_set_render`) dispatches
non-tree eyes here.
"""

from __future__ import annotations

from typing import Any

from precis.utils import handle_registry
from precis.utils.refeye import SEMANTIC_RELATIONS
from precis.workers.working_set import Extent

#: Reading-order tree kinds — routed to the spatial fisheye.
_TREE_KINDS: frozenset[str] = frozenset({"draft", "plan"})

_SUMMARY_CAP = 300
_VERBATIM_CAP = 4000
_NEIGHBOR_TITLE_CAP = 80


def render_eye(store: Any, handle: str, extent: Extent | str | int) -> str:
    """Render one eye by its kind's neighborhood strategy. Raises ``ValueError``
    if the handle does not resolve to a live ref."""
    kind = handle_registry.parse(handle)[0]
    ext = Extent.parse(extent)
    if kind in _TREE_KINDS:
        from precis.utils.fisheye import render_fisheye

        return render_fisheye(store, kind=kind, handle=handle, extent=ext)
    return _render_ref_eye(store, handle, kind, ext)


def _resolve_ref(store: Any, handle: str) -> Any:
    r = store.resolve_handle(handle)
    if r is None:
        return None
    rid = int(r.ref_id)
    return store.fetch_refs_by_ids([rid]).get(rid)


def _ordered_body(store: Any, ref_id: int, *, cap: int) -> str:
    """The ref's body — its ord≥0 chunks in order, capped."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND ord >= 0 ORDER BY ord",
            (ref_id,),
        ).fetchall()
    body = "\n".join(str(r[0]) for r in rows if r[0]).strip()
    return body if len(body) <= cap else body[: cap - 1].rstrip() + "…"


def _head(ref: Any, kind: str) -> str:
    hid = handle_registry.format_handle(kind, int(ref.id))
    title = " ".join((getattr(ref, "title", None) or "").split())
    return f"{hid} [{kind}] {title}".rstrip()


def _render_ref_eye(store: Any, handle: str, kind: str, ext: Extent) -> str:
    """A non-tree ref (memory / paper / patent / web / …): the doc at its extent
    (title → gist → body), and at ``fisheye+1hop`` its **link neighborhood** —
    every ref linked to it, *either direction*, with its relation type. So a note
    linked to a paper (or a paper *chunk* ``pc``) surfaces when you fisheye the
    paper, and the paper surfaces when you fisheye the note. (For a memory the
    body *is* the note and the links are its point; for a paper the body is the
    doc — the graduated reading-order span over paper chunks is a follow-up.)"""
    ref = _resolve_ref(store, handle)
    if ref is None or getattr(ref, "deleted_at", None) is not None:
        raise ValueError(f"eye: no live {kind} ref for {handle!r}")
    if ext <= Extent.TOC:
        return f"· {_head(ref, kind)}"
    cap = _SUMMARY_CAP if ext is Extent.SUMMARY else _VERBATIM_CAP
    body = _ordered_body(store, int(ref.id), cap=cap)
    block = f"{_head(ref, kind)}\n{body}" if body else _head(ref, kind)
    if ext < Extent.HOP1:
        return block
    neighbors = _link_neighbors(store, int(ref.id))
    return f"{block}\n\n{neighbors}" if neighbors else block


def _link_neighbors(store: Any, ref_id: int) -> str:
    """The ref's one-hop link neighborhood, grouped by relation type — the
    ``fisheye+1hop`` layer for a non-tree eye. Follows meaning edges
    (`SEMANTIC_RELATIONS`), **both directions** (``links_for`` matches either
    endpoint, incl. chunk-level edges since they carry the ref id); the neighbor
    is the *other* end of each edge."""
    links = store.links_for(ref_id, direction="both")
    edges: list[tuple[str, int]] = []
    ids: set[int] = set()
    for link in links:
        rel = getattr(link, "relation", None)
        if rel not in SEMANTIC_RELATIONS:
            continue
        other = (
            int(link.dst_ref_id)
            if int(link.src_ref_id) == ref_id
            else int(link.src_ref_id)
        )
        if other == ref_id:
            continue
        edges.append((str(rel), other))
        ids.add(other)
    if not edges:
        return ""
    refs = store.fetch_refs_by_ids(list(ids))
    lines = ["— linked (1 hop) —"]
    for rel, oid in edges:
        r = refs.get(oid)
        if r is None or getattr(r, "deleted_at", None) is not None:
            continue
        oh = handle_registry.format_handle(getattr(r, "kind", "?"), oid)
        title = " ".join((getattr(r, "title", None) or "").split())
        if len(title) > _NEIGHBOR_TITLE_CAP:
            title = title[: _NEIGHBOR_TITLE_CAP - 1].rstrip() + "…"
        lines.append(f"  {rel}: {oh} — {title}" if title else f"  {rel}: {oh}")
    return "\n".join(lines) if len(lines) > 1 else ""
