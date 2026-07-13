"""Compose a whole working set into one context (ADR 0051 §6, deduper).

Renders N eyes — possibly across several documents — as **one deduplicated
context** rather than N independent fisheyes. The core is a per-chunk
**demanded-extent map**: each chunk is shown once, at the *highest* extent any
eye asks for it (a multi-focus fisheye). Overlapping neighborhoods collapse;
shared reference-ring entries merge by ``ref_id``.

**Gap closing** (§ discussion): a short run of undemanded chunks *between* two
demanded ones is bridged — a one-chunk hole in a passage is probably relevant,
so fill it rather than show a jarring break. Bridged chunks take the **lesser**
of the two shoulders' extents (never richer than either side). A heading inside
a gap is already demanded (an ancestor breadcrumb at ``kwd``), so it stays a
heading and the section boundary shows itself — no special-casing needed.
Longer gaps collapse to a visible ``⋯ N more ⋯`` marker (no silent omission).

Ships dark; the render-loop (phase B/C) will drive it. Single-sources the
neighborhood bands + gloss helpers from :mod:`precis.utils.fisheye`.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Any

from precis.utils import handle_registry
from precis.utils.eye_render import _TREE_KINDS, render_eye
from precis.utils.fisheye import (
    _FIDELITY_FULL,
    _FIDELITY_KWD,
    _FIDELITY_SUMMARY,
    _ancestors,
    _gloss,
    _summary_text,
)
from precis.utils.refeye import _RING_CAP, collect_ring, render_ring_groups
from precis.utils.section_keywords import rollup_label
from precis.workers.working_set import Extent, WorkingSet

#: Bridge a gap of at most this many undemanded chunks between two demanded
#: chunks (§ gap closing) — a small hole in a passage is "probably relevant."
_BRIDGE_GAP_MAX = 2


def eye_demand(
    chunks: list[Any],
    idx_by_id: dict[int, int],
    target: Any,
    ext: Extent,
) -> dict[int, Extent]:
    """The extent each chunk is demanded at by **one** eye. Content rungs
    (``kwd``/``summary``/``verbatim``) demand only the target (+ its ancestors
    as a ``kwd`` breadcrumb); ``fisheye``/``fisheye+1hop`` demand a graduated
    forward-biased span (mirrors ``fisheye._render_fidelity_span``'s bands)."""
    demand: dict[int, Extent] = {}
    by_id = {c.chunk_id: c for c in chunks}
    for a in _ancestors(by_id, target):
        demand[a.chunk_id] = Extent.TOC
    if ext <= Extent.FULL:
        demand[target.chunk_id] = max(demand.get(target.chunk_id, Extent.NONE), ext)
        return demand
    pos = idx_by_id[target.chunk_id]
    lo = max(0, pos - _FIDELITY_KWD // 2)
    hi = min(len(chunks), pos + _FIDELITY_KWD + 1)
    for i in range(lo, hi):
        d = abs(i - pos)
        e = (
            Extent.FULL
            if d <= _FIDELITY_FULL
            else Extent.SUMMARY
            if d <= _FIDELITY_SUMMARY
            else Extent.TOC
        )
        cid = chunks[i].chunk_id
        demand[cid] = max(demand.get(cid, Extent.NONE), e)
    return demand


def _close_gaps(
    chunks: list[Any], demand: dict[int, Extent], *, max_gap: int = _BRIDGE_GAP_MAX
) -> None:
    """Bridge small gaps in place: a run of ``≤ max_gap`` undemanded chunks
    between two demanded ones fills at the **lesser** of the two shoulders'
    extents. Headings in the gap are already demanded (ancestor ``kwd``), so
    they are untouched and keep dividing the sections."""
    demanded = [
        i
        for i, c in enumerate(chunks)
        if demand.get(c.chunk_id, Extent.NONE) > Extent.NONE
    ]
    for a, b in pairwise(demanded):
        if 0 < (b - a - 1) <= max_gap:
            fill = min(demand[chunks[a].chunk_id], demand[chunks[b].chunk_id])
            for i in range(a + 1, b):
                cid = chunks[i].chunk_id
                if demand.get(cid, Extent.NONE) <= Extent.NONE:
                    demand[cid] = fill


def _render_doc(
    ref: Any,
    chunks: list[Any],
    demand: dict[int, Extent],
    cursor: str | None,
    views: dict[str, dict[str, str]],
) -> str:
    """Render one document from its (gap-closed) demand map — each demanded
    chunk once, at its extent; undemanded runs collapse to a ``⋯ N more ⋯``
    marker."""
    slug = getattr(ref, "slug", None) or getattr(ref, "id", "?")
    title = getattr(ref, "title", None) or slug
    lines = [f"# {title}  ({slug})"]
    prev_i: int | None = None
    for i, c in enumerate(chunks):
        e = demand.get(c.chunk_id, Extent.NONE)
        if e <= Extent.NONE:
            continue
        if prev_i is not None and i - prev_i > 1:
            # Self-describing collapse (no bare counts): roll the skipped run's
            # keywords into the marker so a gap says *what* it hides, not just
            # how much. Falls back to the bare count when the run has none.
            label = rollup_label(views, chunks[prev_i + 1 : i], top_k=4)
            gap = i - prev_i - 1
            lines.append(
                f"  ⋯ {gap} more · {label} ⋯" if label else f"  ⋯ {gap} more ⋯"
            )
        prev_i = i
        indent = "  " * c.depth
        mark = "▸ " if c.dc == cursor else ""
        if e >= Extent.FULL:
            lines.append(f"{indent}{mark}{c.dc} [{c.chunk_kind}]\n{c.text}")
        elif e is Extent.SUMMARY:
            lines.append(f"{indent}{mark}{c.dc}  {_summary_text(c, views)}")
        else:  # TOC / kwd bookmark
            lines.append(f"{indent}{mark}· {c.dc}  {_gloss(c, views)}")
    return "\n".join(lines)


def render_working_set(
    store: Any,
    ws: WorkingSet,
    *,
    cap: int = _RING_CAP,
    marks: dict[str, str] | None = None,
) -> str:
    """Render the whole working set as **one** deduplicated context: each
    document rendered once from the merged demand map (eyes on the same doc
    share it), with the cursor's document first and a single merged reference
    ring for all ``fisheye+1hop`` eyes.

    ``marks`` (handle → prefix line) folds an out-of-band *role* into the render
    of a **flat** (non-tree) eye — source-backfill uses it to stamp a source
    paper eye ``★ cited  ← <citing section>`` or a recall hit ``○ candidate``,
    so the working set is self-describing without cross-referencing the appended
    lists. It never touches the tree docs (the draft under construction is not a
    source), so with ``marks=None`` the output is byte-identical to before.
    """
    docs: dict[int, dict[str, Any]] = {}
    ring_merged: dict[str, dict[int, str]] = {}
    flat_eyes: list[tuple[str, Any]] = []  # non-tree eyes (memory/paper/…)

    for handle, eye in ws.eyes.items():
        kind = handle_registry.parse(handle)[0]
        # Only draft/plan share the reading-order demand-map dedup; every other
        # kind's neighborhood is a different shape (link graph / doc) — render it
        # standalone via the per-kind dispatcher.
        if kind not in _TREE_KINDS:
            flat_eyes.append((handle, eye))
            continue
        target = store.get_draft_chunk(handle, kind=kind)
        if target is None:
            continue
        ref_id = int(target.ref_id)
        d = docs.get(ref_id)
        if d is None:
            chunks = store.reading_order(ref_id, kind=kind)
            d = docs[ref_id] = {
                "kind": kind,
                "chunks": chunks,
                "idx": {c.chunk_id: i for i, c in enumerate(chunks)},
                "demand": {},
            }
        for cid, e in eye_demand(d["chunks"], d["idx"], target, eye.extent).items():
            d["demand"][cid] = max(d["demand"].get(cid, Extent.NONE), e)
        if eye.extent is Extent.HOP1:
            for group, items in collect_ring(store, target, d["chunks"]).items():
                for rid, label in items:
                    ring_merged.setdefault(group, {})[rid] = label

    if not docs and not flat_eyes:
        return "— empty working set —"

    # The cursor's document leads (only a tree-kind cursor resolves to a doc;
    # a cursor on a non-tree eye just doesn't reorder the docs).
    cursor = ws.cursor
    cursor_ref: int | None = None
    if cursor is not None and handle_registry.parse(cursor)[0] in _TREE_KINDS:
        ct = store.get_draft_chunk(cursor, kind=handle_registry.parse(cursor)[0])
        cursor_ref = int(ct.ref_id) if ct is not None else None
    order = sorted(docs, key=lambda r: (r != cursor_ref, r))

    blocks: list[str] = []
    for ref_id in order:
        d = docs[ref_id]
        _close_gaps(d["chunks"], d["demand"])
        views = store.block_views(ref_id)
        ref = store.fetch_refs_by_ids([ref_id]).get(ref_id)
        blocks.append(_render_doc(ref, d["chunks"], d["demand"], cursor, views))

    # Non-tree eyes (memory link-graph, paper/web doc, …) render standalone via
    # the per-kind dispatcher; a bad one degrades to a marker, never the whole set.
    for handle, eye in flat_eyes:
        try:
            block = render_eye(store, handle, eye.extent)
        except Exception:
            block = f"({handle}: unrenderable)"
        if block.strip():
            mark = "▸ " if handle == cursor else ""
            role = marks.get(handle) if marks else None
            block = f"{role}\n{mark}{block}" if role else f"{mark}{block}"
            blocks.append(block)

    out = "\n\n".join(blocks)

    if any(ring_merged.values()):
        groups = {g: list(m.items()) for g, m in ring_merged.items()}
        out += "\n\n" + render_ring_groups(
            groups, cap=cap, header="— referenced (1 hop), merged across eyes —"
        )
    return out
