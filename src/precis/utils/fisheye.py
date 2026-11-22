"""The fisheye neighborhood render.

The extent ladder separates *how much of the target* from *how much of the
surroundings*. The content-only rungs render the node **alone** — ``kwd`` a
bookmark (under its ancestor path), ``summary`` a summary of it, ``verbatim``
its full text. The **neighborhood** appears only at the ``fisheye`` rung: the
verbatim center over a graduated forward-biased span of reading-order
neighbours, under the **ancestor branch** (``section_path``) so it never
floats free of its heading. ``fisheye+1hop`` adds the reference ring
(``utils.refeye``). It is pure **assembly of existing data** (``reading_order``
+ ``block_views``: ``chunk_summaries`` / ``chunks.keywords``) — no new storage.

Scope: the DraftStore-backed tree kinds (``draft`` / ``plan``), which share
``reading_order`` and ``block_views``, and — via the store-free core
:func:`render_fisheye_span` — the skill kind's H2 sections (gr568475: a
skill *is* an H2 tree, so ``sk:<slug>~N`` gets the same graduated spatial
ladder through an adapter of ``chunk_by_h2`` sections; the store-backed
``render_fisheye`` resolves draft chunks and delegates). Non-tree kinds
(``calc`` / ``web`` / ``memory``) have no neighborhood — the caller renders
the ref itself. ``paper`` (a different chunk-op family) is a follow-up.

This module renders; it does **not** place or decay eyes (that is
``workers.working_set`` + the fisheye slice's decay machinery).

Wired for ``draft``: ``src/precis/handlers/draft.py::DraftHandler.get``'s
``extent=`` kwarg dispatches through ``utils.eye_render.render_eye`` to
:func:`render_fisheye`. Not yet wired for ``plan`` — ``PlanHandler.get`` has
no ``extent=`` kwarg and silently ignores one if passed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Protocol

from precis.workers.working_set import Extent

if TYPE_CHECKING:
    from precis.store.store import Store

#: Graduated forward-biased span for a fidelity eye: ±5 full / ±10 summary /
#: ±15 kwd, measured over reading-order neighbours (not just siblings). The
#: backward reach is half the forward reach.
_FIDELITY_FULL = 5
_FIDELITY_SUMMARY = 10
_FIDELITY_KWD = 15

#: One-line summary cap: a toc/ancestor/skirt line stays a bookmark, not a
#: node's prose body. The verbatim body is what a ``full`` eye is for.
_SUMMARY_LINE_CAP = 100


class _Chunk(Protocol):
    # Read-only properties, not plain attributes — frozen dataclasses
    # (DraftChunk) don't satisfy a mutable-attribute protocol.
    @property
    def chunk_id(self) -> int: ...
    @property
    def ref_id(self) -> int: ...
    @property
    def handle(self) -> str: ...
    @property
    def chunk_kind(self) -> str: ...
    @property
    def text(self) -> str: ...
    @property
    def parent_chunk_id(self) -> int | None: ...
    @property
    def depth(self) -> int: ...
    @property
    def dc(self) -> str: ...


def _summary_line(chunk: _Chunk, views: dict[str, dict[str, str]]) -> str:
    """A one-line summary for a node: summary → keywords → first text line,
    whitespace-collapsed and capped so a prose body can't spill the bookmark."""
    v = views.get(chunk.handle, {})
    g = v.get("summary") or v.get("keywords") or chunk.text or ""
    flat = " ".join(g.split())
    return (
        flat
        if len(flat) <= _SUMMARY_LINE_CAP
        else flat[: _SUMMARY_LINE_CAP - 1].rstrip() + "…"
    )


def _summary_text(chunk: _Chunk, views: dict[str, dict[str, str]]) -> str:
    """The summary body for a ``summary`` render — the llm-v1 summary, else a
    keyword line, else the (possibly truncated) first paragraph."""
    v = views.get(chunk.handle, {})
    if v.get("summary"):
        return v["summary"]
    if v.get("keywords"):
        return f"[keywords] {v['keywords']}"
    line = chunk.text.strip().splitlines()[0] if chunk.text.strip() else ""
    return line[:300]


def _ancestors(by_id: Mapping[int, _Chunk], target: _Chunk) -> list[_Chunk]:
    """The ancestor branch root→…→parent (the ``section_path``), by walking
    ``parent_chunk_id`` up. Excludes the target."""
    chain: list[_Chunk] = []
    pid = target.parent_chunk_id
    seen: set[int] = set()
    while pid is not None and pid in by_id and pid not in seen:
        seen.add(pid)
        node = by_id[pid]
        chain.append(node)
        pid = node.parent_chunk_id
    chain.reverse()
    return chain


def render_fisheye(
    store: Store,
    *,
    kind: str,
    handle: str,
    extent: Extent | str | int = Extent.FULL,
) -> str:
    """Assemble the fisheye neighborhood for ``handle`` in a
    ``draft``/``plan``. Returns the rendered text; raises ``ValueError`` if the
    handle does not resolve to a live node.

    The store-backed front door: resolves the draft chunk and its
    reading-order neighbours + block views, then delegates to the shared
    store-free core :func:`render_fisheye_span` (which the skill eye also
    uses, over its H2-section adapter)."""
    target = store.drafts.get_draft_chunk(handle, kind=kind)
    if target is None:
        raise ValueError(f"fisheye: no live {kind} node {handle!r}")
    chunks = store.drafts.reading_order(target.ref_id, kind=kind)
    views = store.drafts.block_views(target.ref_id)
    ext = Extent.parse(extent)
    from precis.utils.eye_render import _head, _head_chips, _inline_links, _rings

    inline = _inline_links(store, target.ref_id) if ext >= Extent.HOP1 else {}
    shown: set[int] = set()

    def _ring() -> str:
        from precis.utils.refeye import collect_ring, render_ring_groups

        sections = _rings(
            store,
            target.ref_id,
            kind,
            ext,
            recall=False,
            expand=None,
            ring=None,
            handle=handle,
            inline_chunks=set(inline) & (shown | {None}),
        )
        mined = collect_ring(store, target, chunks, include_inbound=False)
        sections.append(render_ring_groups(mined))
        return "\n\n".join(sections)

    body = render_fisheye_span(
        chunks, target, views, ext, ring=_ring, inline=inline, shown=shown
    )
    if ext >= Extent.HOP1:
        ref = store.fetch_refs_by_ids([target.ref_id]).get(target.ref_id)
        if ref is not None:
            head = _head(ref, kind, _head_chips(store, ref, kind))
            if inline.get(None):
                head += "\n\n" + inline[None]
            body = head + "\n\n" + body
    return body


def render_fisheye_span(
    chunks: Sequence[_Chunk],
    target: _Chunk,
    views: dict[str, dict[str, str]],
    extent: Extent | str | int = Extent.FULL,
    *,
    ring: Callable[[], str] | None = None,
    inline: dict[int | None, str] | None = None,
    shown: set[int] | None = None,
) -> str:
    """The shared tree-kind eye core — render ``target`` over its
    reading-order ``chunks`` at ``extent``.

    Store-free on purpose: ``draft``/``plan`` sections arrive from
    :func:`render_fisheye`, skill H2 sections from the adapter in
    ``utils.eye_render`` (gr568475) — one graduated-span renderer for every
    H2-tree kind instead of a per-kind copy. ``views`` maps a chunk's
    ``handle`` to its optional ``summary``/``keywords`` gloss; absent views
    degrade to the chunk's first text line. ``ring`` renders the
    ``fisheye+1hop`` neighbourhood (a draft's reference ring, a skill's
    wikilink ring) and is appended whenever the rung asks for it and it
    returns text. ``inline`` maps attachment chunk ids to rendered links;
    ``shown`` records displayed chunks before the ring callback runs, so
    it can omit links already printed under those chunks.

    - ``kwd`` — the ancestor branch + a one-line bookmark of the node.
    - ``summary`` / ``verbatim`` — the node **alone** (short summary vs full
      text), no surroundings.
    - ``fisheye`` — a graduated forward-biased span over reading-order
      neighbours (±5 full / ±10 summary / ±15 kwd).
    - ``fisheye+1hop`` and up — the span **plus the ring**."""
    ext = Extent.parse(extent)

    # Content-only rungs: the node *alone* — ``summary`` renders a summary of
    # it, ``verbatim`` its full text. No surroundings; the spatial ring is
    # what the ``fisheye`` rung is for.
    if ext is Extent.SUMMARY:
        return f"▸ {target.dc} [{target.chunk_kind}]\n{_summary_text(target, views)}"
    if ext is Extent.FULL:
        return f"▸ {target.dc} [{target.chunk_kind}]\n{target.text}"

    # Surroundings rungs share the ancestor branch (section_path), so the node
    # never floats free of its heading.
    lines: list[str] = []
    by_id = {c.chunk_id: c for c in chunks}
    anc = _ancestors(by_id, target)
    if anc:
        crumb = " › ".join(_summary_line(a, views) or a.dc for a in anc)
        lines.append(f"↑ {crumb}")

    # kwd (or a collapsed NONE) — a one-line bookmark under its ancestor path.
    if ext <= Extent.TOC:
        lines.append(f"▸ {target.dc}  {_summary_line(target, views)}")
        return "\n".join(lines)

    # fisheye — verbatim center over a graduated spatial neighborhood; the
    # hop rungs append the ring (draft: reference ring; skill: wikilinks).
    lines.extend(
        _render_fidelity_span(chunks, target, views, inline=inline, shown=shown)
    )
    if ext >= Extent.HOP1 and ring is not None:
        ring_text = ring()
        if ring_text:
            lines.append("")
            lines.append(ring_text)
    return "\n".join(lines)


def _render_fidelity_span(
    chunks: Sequence[_Chunk],
    target: _Chunk,
    views: dict[str, dict[str, str]],
    *,
    inline: dict[int | None, str] | None = None,
    shown: set[int] | None = None,
) -> list[str]:
    """A graduated, forward-biased window over reading-order neighbours: the
    center full, fanning out to summary then keyword lines, reaching further
    forward than back. ``inline`` supplies links below each attachment;
    ``shown`` records the displayed chunk ids for ring exclusion. With
    inline links the focus leads, followed by reading-order neighbours."""
    pos = next((i for i, c in enumerate(chunks) if c.chunk_id == target.chunk_id), 0)
    out: list[str] = []
    # Backward reach is half the forward reach (forward-biased).
    lo = max(0, pos - _FIDELITY_KWD // 2)
    hi = min(len(chunks), pos + _FIDELITY_KWD + 1)
    indices = list(range(lo, hi))
    if inline is not None:
        indices.sort(key=lambda i: i != pos)
    for i in indices:
        c = chunks[i]
        if shown is not None:
            shown.add(c.chunk_id)
        if inline is not None and out:
            out.append("")
        dist = abs(i - pos)
        indent = "  " * c.depth
        if dist == 0:
            out.append(f"{indent}▸ {c.dc} [{c.chunk_kind}]\n{c.text}")
        elif dist <= _FIDELITY_FULL:
            out.append(f"{indent}{c.dc} [{c.chunk_kind}]\n{c.text}")
        elif dist <= _FIDELITY_SUMMARY:
            out.append(
                f"{indent}· {c.dc}  {_summary_text(c, views)}{_chunk_size_cell(c)}"
            )
        else:
            out.append(
                f"{indent}· {c.dc}  {_summary_line(c, views)}{_chunk_size_cell(c)}"
            )
        if inline and inline.get(c.chunk_id):
            out.append(inline[c.chunk_id])
    return out


def _chunk_size_cell(c: _Chunk) -> str:
    """`` · <n>kB`` — the chunk's own text size at ≥1 kB (text is loaded)."""
    from precis._result_budget import size_cell

    sz = size_cell(len((c.text or "").encode("utf-8")))
    return f" · {sz}" if sz else ""
