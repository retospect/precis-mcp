"""The fisheye neighborhood render (ADR 0051 §6).

The extent ladder separates *how much of the target* from *how much of the
surroundings* (§ C0). The content-only rungs render the node **alone** —
``kwd`` a bookmark (under its ancestor path), ``summary`` its gloss,
``verbatim`` its full text. The **neighborhood** appears only at the
``fisheye`` rung: the verbatim center over a graduated forward-biased span of
reading-order neighbours, under the **ancestor branch** (``section_path``) so
it never floats free of its heading. ``fisheye+1hop`` adds the reference ring
(``utils.refeye``). It is pure **assembly of existing data** (``reading_order``
+ ``block_views``: ``chunk_summaries`` / ``chunks.keywords``) — no new storage.

Scope: the DraftMixin-backed tree kinds (``draft`` / ``plan``), which share
``reading_order`` and ``block_views``. Non-tree kinds (``calc`` / ``web`` /
``memory``) have no neighborhood — the caller renders the ref itself. ``paper``
(a different chunk-op family) is a follow-up.

This module renders; it does **not** place or decay eyes (that is
``workers.working_set`` + the fisheye slice's decay machinery). It ships dark
until the render-loop wires it in.
"""

from __future__ import annotations

from typing import Any, Protocol

from precis.workers.working_set import Extent

#: Graduated forward-biased span for a ``fidelity`` eye (§6: "±5 full / ±10
#: summary / ±15 kwd, forward-biased"). Measured over reading-order neighbours
#: (not just siblings). Forward-biased: the backward reach is half the forward.
_FIDELITY_FULL = 5
_FIDELITY_SUMMARY = 10
_FIDELITY_KWD = 15

#: One-line gloss cap (§6): a toc / ancestor / skirt line stays a bookmark, not
#: a node's prose body. The verbatim body is what a ``full`` eye is for.
_GLOSS_CAP = 100


class _Chunk(Protocol):
    chunk_id: int
    ref_id: int
    handle: str
    chunk_kind: str
    text: str
    parent_chunk_id: int | None
    depth: int

    @property
    def dc(self) -> str: ...


def _gloss(chunk: _Chunk, views: dict[str, dict[str, str]]) -> str:
    """A one-line gloss for a node: summary → keywords → first text line,
    whitespace-collapsed and capped so a prose body can't spill the bookmark."""
    v = views.get(chunk.handle, {})
    g = v.get("summary") or v.get("keywords") or chunk.text or ""
    flat = " ".join(g.split())
    return flat if len(flat) <= _GLOSS_CAP else flat[: _GLOSS_CAP - 1].rstrip() + "…"


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


def _ancestors(by_id: dict[int, _Chunk], target: _Chunk) -> list[_Chunk]:
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
    store: Any,
    *,
    kind: str,
    handle: str,
    extent: Extent | str | int = Extent.FULL,
) -> str:
    """Assemble the fisheye neighborhood (§6) for ``handle`` in a
    ``draft``/``plan``. Returns the rendered text; raises ``ValueError`` if the
    handle does not resolve to a live node.

    - ``kwd`` — the ancestor branch + a one-line bookmark of the node.
    - ``summary`` / ``verbatim`` — the node **alone** (gloss vs full text), no
      surroundings.
    - ``fisheye`` — a graduated forward-biased span over reading-order
      neighbours (±5 full / ±10 summary / ±15 kwd), under the ancestor branch.
    - ``fisheye+1hop`` — the ``fisheye`` span **plus the reference ring**:
      everything the section points at one edge out (cited papers, cross-refs,
      linked notes/memories — ``utils.refeye``)."""
    ext = Extent.parse(extent)
    target = store.get_draft_chunk(handle, kind=kind)
    if target is None:
        raise ValueError(f"fisheye: no live {kind} node {handle!r}")
    chunks = store.reading_order(target.ref_id, kind=kind)
    views = store.block_views(target.ref_id)
    by_id = {c.chunk_id: c for c in chunks}

    # Content-only rungs (§ C0): the node *alone* — ``summary`` is its gloss,
    # ``verbatim`` its full text. No surroundings; the spatial ring is what the
    # ``fisheye`` rung is for.
    if ext is Extent.SUMMARY:
        return f"▸ {target.dc} [{target.chunk_kind}]\n{_summary_text(target, views)}"
    if ext is Extent.FULL:
        return f"▸ {target.dc} [{target.chunk_kind}]\n{target.text}"

    # Surroundings rungs share the ancestor branch (section_path), so the node
    # never floats free of its heading.
    lines: list[str] = []
    anc = _ancestors(by_id, target)
    if anc:
        crumb = " › ".join(_gloss(a, views) or a.dc for a in anc)
        lines.append(f"↑ {crumb}")

    # kwd (or a collapsed NONE) — a one-line bookmark under its ancestor path.
    if ext <= Extent.TOC:
        lines.append(f"▸ {target.dc}  {_gloss(target, views)}")
        return "\n".join(lines)

    # fisheye — verbatim center over a graduated spatial neighborhood; +1hop
    # appends the reference ring (everything the section points at, one edge out).
    lines.extend(_render_fidelity_span(chunks, target, views))
    if ext is Extent.HOP1:
        from precis.utils.refeye import render_reference_ring

        lines.append("")
        lines.append(render_reference_ring(store, target, chunks))
    return "\n".join(lines)


def _render_fidelity_span(
    chunks: list[_Chunk], target: _Chunk, views: dict[str, dict[str, str]]
) -> list[str]:
    """A graduated, forward-biased window over reading-order neighbours (§6):
    the center full, fanning out to summary then keyword lines, reaching
    further forward than back."""
    pos = next((i for i, c in enumerate(chunks) if c.chunk_id == target.chunk_id), 0)
    out: list[str] = []
    # Backward reach is half the forward reach (forward-biased).
    lo = max(0, pos - _FIDELITY_KWD // 2)
    hi = min(len(chunks), pos + _FIDELITY_KWD + 1)
    for i in range(lo, hi):
        c = chunks[i]
        dist = abs(i - pos)
        indent = "  " * c.depth
        if dist == 0:
            out.append(f"{indent}▸ {c.dc} [{c.chunk_kind}]\n{c.text}")
        elif dist <= _FIDELITY_FULL:
            out.append(f"{indent}{c.dc} [{c.chunk_kind}]\n{c.text}")
        elif dist <= _FIDELITY_SUMMARY:
            out.append(f"{indent}· {c.dc}  {_summary_text(c, views)}")
        else:
            out.append(f"{indent}· {c.dc}  {_gloss(c, views)}")
    return out
