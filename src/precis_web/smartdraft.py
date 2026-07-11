"""Smartdraft — the fisheye rail's LLM-free relevance engine (design:
``docs/proposals/draft-reader-fisheye-rail.md``).

The whole surface is one primitive: **a prioritized list rendered at fidelity ∝
priority, capped by a budget.** Priority is *eye-pressure* — how much a chunk
wants to be noticed *relative to the current focus* — computed with **no LLM**,
from signals we already store:

- **keyword overlap** (`chunks.keywords`, KeyBERT) — literal,
- **embedding similarity** (`chunk_embeddings`, bge-m3) — semantic,
- **reading proximity** (distance in reading order) — structural,
- **status boost** — a pin / lock / pending need pokes through regardless of
  topical relevance (so "what needs you" is never collapsed away).

The focus is a chunk (the current para) *or* a query (search is just "focus =
these keywords"). Rank once; three panes read the same ranking at three
densities — the left TOC (whole map, thin, quiet runs collapsed), the middle
(the top few, thick), the right (urgency-sorted). This module is the pure engine
+ view-model; the route (`routes/smartdraft.py`) serializes it to HTML and the
same ranking is what an MCP `focus` verb would serialize to text.

Ships parallel to `/drafts` (a new route, same data) so it never touches the
working reader — dark by construction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

# ── pressure weights (tune later; env-overridable is a follow-up) ────────
_W_KEYWORD = 1.0
_W_EMBED = 1.0
_W_PROX = 0.6
#: A pin/lock/need adds this so a marked chunk always clears the keep bar.
_STATUS_BOOST = 10.0
#: Body chunks below this pressure collapse into a ``⋯ N ⋯`` run (headings and
#: status-boosted chunks never collapse — the outline + needs always show).
_KEEP_THRESHOLD = 0.18
#: Middle reading window around the focus (forward-biased).
_MID_BACK = 2
_MID_FWD = 2
#: Verbatim cap for the ±1 neighbours (truncated toward the focus edge so the
#: text reads continuously into/out of the focus).
_NEIGHBOR_CAP = 400
#: How many high-pressure *non-neighbour* chunks to surface as "relevant
#: elsewhere" under the middle window.
_ELSEWHERE_K = 4


@dataclass(slots=True)
class ChunkNode:
    """One draft chunk with everything the render needs, joined from
    reading-order (structure) + blocks (keywords/embedding) + views (summary)."""

    idx: int  # position in reading order
    dc: str  # universal handle (dc<id>)
    base58: str  # legacy anchor — the reader scrolls to #c-<base58>
    chunk_id: int
    depth: int
    chunk_kind: str
    text: str
    summary: str
    keywords: list[str]
    embedding: list[float] | None
    pinned: bool = False
    locked: bool = False

    @property
    def is_heading(self) -> bool:
        return self.chunk_kind == "heading"

    @property
    def has_status(self) -> bool:
        return self.pinned or self.locked


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / len(a | b) if inter else 0.0


def _cosine(a: list[float] | None, b: list[float] | None) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _first_line(text: str, cap: int = 140) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= cap else flat[: cap - 1].rstrip() + "…"


def _trunc_head(text: str, cap: int) -> str:
    """Keep the START (drop the end) — for the +1 neighbour, whose beginning
    flows out of the focus."""
    t = (text or "").strip()
    return t if len(t) <= cap else t[: cap - 1].rstrip() + "…"


def _trunc_tail(text: str, cap: int) -> str:
    """Keep the END (drop the beginning) — for the −1 neighbour, whose tail
    leads into the focus."""
    t = (text or "").strip()
    return t if len(t) <= cap else "…" + t[-(cap - 1) :].lstrip()


def build_nodes(
    store: Any, ref_id: int, *, marks: dict[str, Any] | None = None
) -> list[ChunkNode]:
    """Assemble the draft's chunks into `ChunkNode`s — one join over
    reading-order (structure) + `list_blocks_for_ref` (keywords + embedding) +
    `block_views` (llm summary). ``marks`` stamps pin/lock status."""
    chunks = store.reading_order(ref_id)
    blocks = {b.id: b for b in store.list_blocks_for_ref(ref_id, with_embedding=True)}
    views = store.block_views(ref_id)
    pins = set((marks or {}).get("pens") or [])
    eyes = set((marks or {}).get("eyes") or {})
    locked = set((marks or {}).get("locks") or [])  # forward-compat; unused v1
    nodes: list[ChunkNode] = []
    for i, c in enumerate(chunks):
        b = blocks.get(c.chunk_id)
        v = views.get(c.handle, {}) or {}
        kws = list(b.keywords) if (b and b.keywords) else _kw_from_view(v)
        summary = v.get("summary") or _first_line(c.text)
        nodes.append(
            ChunkNode(
                idx=i,
                dc=c.dc,
                base58=c.handle,
                chunk_id=c.chunk_id,
                depth=c.depth,
                chunk_kind=c.chunk_kind,
                text=c.text or "",
                summary=summary,
                keywords=kws,
                embedding=list(b.embedding) if (b and b.embedding) else None,
                pinned=(c.dc in pins or c.dc in eyes),
                locked=(c.dc in locked),
            )
        )
    return nodes


def _kw_from_view(v: dict[str, str]) -> list[str]:
    raw = v.get("keywords") or ""
    return [k.strip() for k in raw.split(",") if k.strip()]


def focus_index(nodes: list[ChunkNode], focus_dc: str | None) -> int:
    """The reading-order index of the focus chunk, defaulting to the first body
    chunk (else 0). A missing/stale handle degrades to the default."""
    if focus_dc:
        for n in nodes:
            if n.dc == focus_dc:
                return n.idx
    for n in nodes:
        if not n.is_heading:
            return n.idx
    return 0


def pressures(nodes: list[ChunkNode], focus_idx: int) -> dict[int, float]:
    """Eye-pressure of every chunk relative to the focus (§ module docstring).
    The focus itself is 1.0; everything else is the weighted signal sum + any
    status boost."""
    if not nodes:
        return {}
    f = nodes[focus_idx]
    fk = set(f.keywords)
    out: dict[int, float] = {}
    for n in nodes:
        if n.idx == focus_idx:
            out[n.idx] = 1.0
            continue
        kw = _jaccard(fk, set(n.keywords))
        emb = max(0.0, _cosine(f.embedding, n.embedding))
        prox = 1.0 / (1.0 + abs(n.idx - focus_idx))
        p = _W_KEYWORD * kw + _W_EMBED * emb + _W_PROX * prox
        if n.has_status:
            p += _STATUS_BOOST
        out[n.idx] = p
    return out


# ── view model: the three panes over one ranking ─────────────────────────


@dataclass(slots=True)
class TocRow:
    """A left-pane row: either a kept chunk (relevant / heading / status) or a
    collapsed run of ≥2 quiet chunks (``collapsed_nodes`` carries them, so the
    marker can hover-list their summaries and click to open). A *single* quiet
    chunk is never collapsed — the ``⋯ 1 para ⋯`` marker saves no space."""

    node: ChunkNode | None = None
    pressure: float = 0.0
    collapsed_nodes: list[ChunkNode] = field(default_factory=list)


@dataclass(slots=True)
class MidRow:
    """A middle-pane row. ``mode`` grades the fidelity by distance to focus:
    ``full`` (focus) · ``tail``/``head`` (±1 verbatim, truncated toward the
    focus) · ``summary`` (±2). ``display`` is the text to render at that mode."""

    node: ChunkNode
    is_focus: bool
    mode: str = "summary"
    display: str = ""


@dataclass(slots=True)
class SmartView:
    ref_id: int
    focus: ChunkNode | None
    toc: list[TocRow] = field(default_factory=list)
    middle: list[MidRow] = field(default_factory=list)
    elsewhere: list[ChunkNode] = field(default_factory=list)
    #: ``[dc, depth]`` per chunk in reading order — the keyboard nav sequence.
    #: Up/down step linearly; indent/outdent walk the depth (parent/child).
    order: list[list[Any]] = field(default_factory=list)


def _left_toc(
    nodes: list[ChunkNode], pres: dict[int, float], *, relevance: bool
) -> list[TocRow]:
    """The fisheye TOC. ``relevance=False`` → the plain full outline (every
    chunk). ``relevance=True`` → keep headings + status + high-pressure chunks;
    collapse quiet-irrelevant runs to a ``⋯ n ⋯`` marker (order never
    reshuffles — only expand/collapse tracks the focus)."""
    rows: list[TocRow] = []
    run: list[ChunkNode] = []

    def flush() -> None:
        if not run:
            return
        if len(run) == 1:  # a lone quiet chunk — show it, collapsing saves nothing
            rows.append(TocRow(node=run[0], pressure=pres.get(run[0].idx, 0.0)))
        else:
            rows.append(TocRow(collapsed_nodes=list(run)))
        run.clear()

    for n in nodes:
        keep = (
            not relevance
            or n.is_heading
            or n.has_status
            or pres.get(n.idx, 0.0) >= _KEEP_THRESHOLD
        )
        if keep:
            flush()
            rows.append(TocRow(node=n, pressure=pres.get(n.idx, 0.0)))
        else:
            run.append(n)
    flush()
    return rows


def build_view(
    store: Any,
    ref_id: int,
    *,
    focus_dc: str | None = None,
    relevance: bool = True,
    marks: dict[str, Any] | None = None,
) -> SmartView:
    """Assemble the whole three-pane view for one focus. Pure over the store's
    read methods — the route just renders it (and an MCP `focus` verb would
    serialize the same object to text)."""
    nodes = build_nodes(store, ref_id, marks=marks)
    if not nodes:
        return SmartView(ref_id=ref_id, focus=None)
    fi = focus_index(nodes, focus_dc)
    pres = pressures(nodes, fi)
    toc = _left_toc(nodes, pres, relevance=relevance)

    middle: list[MidRow] = []
    if not relevance:
        # Full / uncompressed document: every chunk verbatim (the focus is still
        # framed). The Fisheye⇄Full toggle drives both panes from one flag.
        for i, n in enumerate(nodes):
            middle.append(
                MidRow(
                    node=n,
                    is_focus=(i == fi),
                    mode=("full" if i == fi else "doc"),
                    display=n.text,
                )
            )
    else:
        lo = max(0, fi - _MID_BACK)
        hi = min(len(nodes), fi + _MID_FWD + 1)
        for i in range(lo, hi):
            n = nodes[i]
            dist = i - fi
            if dist == 0:
                mode, display = "full", n.text
            elif dist == -1:
                mode, display = "tail", _trunc_tail(n.text, _NEIGHBOR_CAP)
            elif dist == 1:
                mode, display = "head", _trunc_head(n.text, _NEIGHBOR_CAP)
            else:
                mode, display = "summary", n.summary
            middle.append(
                MidRow(node=n, is_focus=(dist == 0), mode=mode, display=display)
            )

    # "relevant elsewhere" — highest-pressure chunks outside the fisheye window
    # (empty in full-doc mode, where everything is already shown). Kept on the
    # model for a future TOC-hover surfacing.
    elsewhere: list[ChunkNode] = []
    if relevance:
        near = {m.node.idx for m in middle}
        ranked = sorted(
            (n for n in nodes if n.idx not in near and not n.is_heading),
            key=lambda n: pres.get(n.idx, 0.0),
            reverse=True,
        )
        elsewhere = [
            n for n in ranked[:_ELSEWHERE_K] if pres.get(n.idx, 0.0) >= _KEEP_THRESHOLD
        ]

    return SmartView(
        ref_id=ref_id,
        focus=nodes[fi],
        toc=toc,
        middle=middle,
        elsewhere=elsewhere,
        order=[[n.dc, n.depth] for n in nodes],
    )
