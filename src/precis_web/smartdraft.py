"""Smartdraft — the fisheye rail's LLM-free relevance engine (design:
``docs/proposals/draft-reader-fisheye-rail.md``).

The whole surface is one primitive: **a prioritized list rendered at fidelity ∝
priority, capped by a budget.** Priority is *eye-pressure* — how much a chunk
wants to be noticed *relative to the current focus* — computed with **no LLM**,
from signals we already store:

- **keyword overlap** (`chunks.keywords`, KeyBERT) — literal,
- **reading proximity** (distance in reading order) — structural,
- **status boost** — a pin / lock / pending need pokes through regardless of
  topical relevance (so "what needs you" is never collapsed away).

Pressure is embedding-free on purpose: loading every vector + a python cosine
blocked the page for seconds on a 10k-chunk draft. The **semantic** search
signal comes from the HNSW index at query time (`semantic_ranks`), not a scan.

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

import time
from dataclasses import dataclass, field
from typing import Any

from precis.store._draft_ops import content_sha

# ── pressure weights (tune later; env-overridable is a follow-up) ────────
_W_KEYWORD = 1.0
_W_PROX = 0.6
#: A pin/lock/need adds this so a marked chunk always clears the keep bar.
_STATUS_BOOST = 10.0
#: Body chunks below this pressure collapse into a ``⋯ N ⋯`` run (headings and
#: status-boosted chunks never collapse — the outline + needs always show).
_KEEP_THRESHOLD = 0.18
#: Middle reading window around the focus (forward-biased, sized to fill a
#: typical viewport). A true measure-and-fill is a client-side follow-up.
_MID_BACK = 4
_MID_FWD = 6
#: Verbatim cap for the ±1 neighbours (truncated toward the focus edge so the
#: text reads continuously into/out of the focus).
_NEIGHBOR_CAP = 400
#: How many high-pressure *non-neighbour* chunks to surface as "relevant
#: elsewhere" under the middle window.
_ELSEWHERE_K = 4

#: RRF fusion constant (standard ~60) + per-signal weights. A tag is
#: human-curated attention, so it outweighs the machine literal signals.
_RRF_K = 60
_SEARCH_W: dict[str, float] = {"v": 1.0, "k": 1.0, "t": 2.0, "s": 1.0}
#: Only the top-N most-similar chunks count as a *semantic* match (below that,
#: cosine is baseline noise — including it would make everything a hit).
_SEM_TOPN = 20


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
    #: content_sha of ``text`` — the optimistic-concurrency token the inline
    #: editor passes to ``POST /drafts/{id}/text`` (a stale one 409s).
    sha: str = ""
    #: chunk tags (``chunk_tags.value``) — the ``T`` search signal.
    tags: list[str] = field(default_factory=list)
    pinned: bool = False
    locked: bool = False

    @property
    def is_heading(self) -> bool:
        return self.chunk_kind == "heading"

    @property
    def editable(self) -> bool:
        """Only free-text body chunks are inline-editable here (a heading is
        text too; a table/figure needs its own editor — a follow-up)."""
        return self.chunk_kind in ("paragraph", "heading")

    @property
    def has_status(self) -> bool:
        return self.pinned or self.locked


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / len(a | b) if inter else 0.0


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


# ── base-node cache ──────────────────────────────────────────────────────
# Building the nodes is 4 serial DB round-trips (reading-order + blocks + views
# + tags) plus an O(N) construction — ~0.35s on a 9.8k-chunk draft, and it's
# *identical* across navigations within the same draft (only the focus, a query
# param, changes). So cache the base nodes (pins/locks NOT baked in — those are
# a cheap per-request overlay) keyed by ref_id, invalidated by a cheap content
# version (chunk count + max id, which changes on any DELETE+INSERT body edit)
# with a TTL backstop for out-of-band drift (worker re-summarize/keyword). This
# is what makes click-around instant: the first focus pays the build, the rest
# read the cache + re-run only the ~7ms assemble_view.
#: {ref_id: (monotonic_stamp, version_token, base_nodes)}
_NODE_CACHE: dict[int, tuple[float, str, list[ChunkNode]]] = {}
#: Rebuild after this many seconds regardless of version — heals drift a worker
#: made without minting a new chunk_id (summary/keyword rewrites, tag edits from
#: outside the smartdraft write path, which calls :func:`invalidate` directly).
_NODE_TTL = 45.0


def _cache_version(store: Any, ref_id: int) -> str | None:
    """A cheap content token for a draft — ``chunks:max(chunk_id):tags`` over its
    live chunks. Body edits DELETE+INSERT (a new chunk_id) so the first two
    change on any text edit; the ``chunk_tags`` count changes on any tag add /
    remove — so the token self-invalidates for tag writes from *any* source (the
    smartdraft route, the MCP ``tag`` verb, a worker), not just the route that
    calls :func:`invalidate`. One round-trip. Returns ``None`` if the store can't
    answer (a FakeStore in tests, a pool-less handle) — the caller then skips the
    cache entirely, preserving the pre-cache always-rebuild behaviour exactly."""
    try:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT "
                " (SELECT count(*) FROM chunks WHERE ref_id = %s AND retired_at IS NULL), "
                " (SELECT coalesce(max(chunk_id), 0) FROM chunks "
                "    WHERE ref_id = %s AND retired_at IS NULL), "
                " (SELECT count(*) FROM chunk_tags ct JOIN chunks c "
                "    ON c.chunk_id = ct.chunk_id WHERE c.ref_id = %s)",
                (ref_id, ref_id, ref_id),
            ).fetchone()
    except Exception:
        return None
    return f"{row[0]}:{row[1]}:{row[2]}" if row else None


def invalidate(ref_id: int) -> None:
    """Drop a draft's cached base nodes — call from any smartdraft write path
    (tag add/remove) so the change shows on the very next render, not after the
    TTL. Body-text edits self-invalidate via :func:`_cache_version`."""
    _NODE_CACHE.pop(ref_id, None)


def _apply_marks(nodes: list[ChunkNode], marks: dict[str, Any] | None) -> None:
    """Stamp pin/lock status onto (cached) nodes from ``marks`` — a cheap
    per-request overlay so marks needn't invalidate the cache. Sets *both* flags
    on *every* node each call (clearing a prior request's overlay), so a shared
    cached list stays consistent. Safe because the reader route runs the build →
    render span synchronously (no ``await`` yields inside it)."""
    pins = set((marks or {}).get("pens") or []) | set((marks or {}).get("eyes") or {})
    locked = set((marks or {}).get("locks") or [])
    for n in nodes:
        n.pinned = n.dc in pins
        n.locked = n.dc in locked


def build_nodes(
    store: Any, ref_id: int, *, marks: dict[str, Any] | None = None
) -> list[ChunkNode]:
    """Assemble the draft's chunks into `ChunkNode`s (cached per draft; see
    :data:`_NODE_CACHE`). ``marks`` stamps pin/lock status as a per-request
    overlay on top of the cached base."""
    ver = _cache_version(store, ref_id)
    now = time.monotonic()
    ent = _NODE_CACHE.get(ref_id)
    if ver is not None and ent and ent[1] == ver and now - ent[0] < _NODE_TTL:
        nodes = ent[2]
    else:
        nodes = _build_nodes_uncached(store, ref_id)
        if ver is not None:
            _NODE_CACHE[ref_id] = (now, ver, nodes)
    _apply_marks(nodes, marks)
    return nodes


def _build_nodes_uncached(store: Any, ref_id: int) -> list[ChunkNode]:
    """The actual build — one join over reading-order (structure) +
    `list_blocks_for_ref` (keywords) + `block_views` (llm summary) + chunk tags.
    Pins/locks are left False; :func:`_apply_marks` overlays them per request."""
    chunks = store.reading_order(ref_id)
    # NB: do NOT load embeddings here — for a 10k-chunk draft that fetches ~10M
    # floats and (with a python cosine) blocks the page for seconds. Semantic is
    # served by the HNSW index at query time (`semantic_ranks`), not a full scan.
    blocks = {b.id: b for b in store.list_blocks_for_ref(ref_id)}
    views = store.block_views(ref_id)
    tag_map = _load_chunk_tags(store, ref_id)
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
                sha=content_sha(c.text or ""),
                tags=tag_map.get(c.chunk_id, []),
            )
        )
    return nodes


def _kw_from_view(v: dict[str, str]) -> list[str]:
    raw = v.get("keywords") or ""
    return [k.strip() for k in raw.split(",") if k.strip()]


def _load_chunk_tags(store: Any, ref_id: int) -> dict[int, list[str]]:
    """``{chunk_id: [tag value, …]}`` for a ref's chunks (``chunk_tags`` — the
    ``T`` search signal). Best-effort: a store without a raw pool degrades to
    no tags rather than raising."""
    out: dict[int, list[str]] = {}
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT ct.chunk_id, t.value FROM chunk_tags ct "
                "JOIN tags t ON t.tag_id = ct.tag_id "
                "JOIN chunks c ON c.chunk_id = ct.chunk_id "
                "WHERE c.ref_id = %s",
                (ref_id,),
            ).fetchall()
    except Exception:
        return out
    for cid, val in rows:
        out.setdefault(int(cid), []).append(str(val))
    return out


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
        prox = 1.0 / (1.0 + abs(n.idx - focus_idx))
        p = _W_KEYWORD * kw + _W_PROX * prox
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
    #: True when this chunk shares ≥1 keyword with the focus — a first-class
    #: keep+highlight reason (distal shared-keyword paras surface in the map,
    #: not just spatial/embedding neighbours).
    shared: bool = False
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
    #: All nodes (reading order) — the route searches these (`search_chunks`).
    nodes: list[ChunkNode] = field(default_factory=list)


def _left_toc(
    nodes: list[ChunkNode],
    pres: dict[int, float],
    *,
    relevance: bool,
    shared_idx: set[int] | None = None,
    keep_dcs: set[str] | None = None,
) -> list[TocRow]:
    """The fisheye TOC. ``relevance=False`` → the plain full outline (every
    chunk). ``relevance=True`` → keep headings + status + **keyword-shared** +
    high-pressure chunks; collapse quiet-irrelevant runs to a ``⋯ n ⋯`` marker
    (order never reshuffles — only expand/collapse tracks the focus).
    ``keep_dcs`` (search hits) are always kept — the in-TOC search view shows
    every match, uncollapsed."""
    shared_idx = shared_idx or set()
    keep_dcs = keep_dcs or set()
    rows: list[TocRow] = []
    run: list[ChunkNode] = []

    def flush() -> None:
        if not run:
            return
        if len(run) == 1:  # a lone quiet chunk — show it, collapsing saves nothing
            n = run[0]
            rows.append(TocRow(node=n, pressure=pres.get(n.idx, 0.0)))
        else:
            rows.append(TocRow(collapsed_nodes=list(run)))
        run.clear()

    for n in nodes:
        is_shared = n.idx in shared_idx
        keep = (
            not relevance
            or n.is_heading
            or n.has_status
            or is_shared
            or n.dc in keep_dcs
            or pres.get(n.idx, 0.0) >= _KEEP_THRESHOLD
        )
        if keep:
            flush()
            rows.append(TocRow(node=n, pressure=pres.get(n.idx, 0.0), shared=is_shared))
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
    keep_dcs: set[str] | None = None,
) -> SmartView:
    """Build the nodes for a draft and assemble its view (the store-backed entry;
    the route uses :func:`build_nodes` + :func:`assemble_view` directly so it can
    search the same nodes)."""
    nodes = build_nodes(store, ref_id, marks=marks)
    return assemble_view(
        nodes, ref_id=ref_id, focus_dc=focus_dc, relevance=relevance, keep_dcs=keep_dcs
    )


def assemble_view(
    nodes: list[ChunkNode],
    *,
    ref_id: int = 0,
    focus_dc: str | None = None,
    relevance: bool = True,
    keep_dcs: set[str] | None = None,
) -> SmartView:
    """Assemble the three-pane view from pre-built nodes. ``keep_dcs`` (search
    hits) are always shown in the TOC. Pure — the same object an MCP `focus` verb
    would serialize to text."""
    if not nodes:
        return SmartView(ref_id=ref_id, focus=None)
    fi = focus_index(nodes, focus_dc)
    pres = pressures(nodes, fi)
    # Chunks that share ≥1 keyword with the focus (the focus itself excluded) —
    # a first-class keep+highlight so distal shared-keyword paras surface.
    focus_kw = set(nodes[fi].keywords)
    shared_idx = (
        {n.idx for n in nodes if n.idx != fi and focus_kw & set(n.keywords)}
        if focus_kw
        else set()
    )
    toc = _left_toc(
        nodes, pres, relevance=relevance, shared_idx=shared_idx, keep_dcs=keep_dcs
    )

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
        nodes=nodes,
    )


# ── search: multi-signal RRF fusion (V / K / T / semantic) ───────────────


@dataclass(slots=True)
class SearchHit:
    """One search result. ``v``/``k``/``t`` are the literal/keyword/tag matches
    (shown as badges *regardless* of whether the signal is active — an off
    signal renders greyed). ``s_rank`` is the semantic rank (1-based) when the
    chunk is in the top-N most-similar, else ``None``. ``score`` counts only the
    **active** signals (RRF)."""

    node: ChunkNode
    v: bool
    k: bool
    t: bool
    s_rank: int | None
    score: float


def search_chunks(
    nodes: list[ChunkNode],
    query: str,
    *,
    active: set[str],
    semantic_ranks: dict[int, int] | None = None,
    weights: dict[str, float] | None = None,
) -> list[SearchHit]:
    """Fuse the four signals by **Reciprocal Rank Fusion** (the same fusion the
    corpus search uses): each active signal contributes ``w / (k + rank)``.
    Literal/keyword/tag are boolean (rank 1 when matched); semantic contributes
    by ``semantic_ranks`` (``{chunk_id: rank}`` from the HNSW top-N, computed once
    in SQL — never a python scan over every vector) — so a semantic-only hit still
    surfaces and a strong-semantic tie-breaks. A chunk with no *active* match
    scores 0 and is dropped. Results are sorted by score desc."""
    q = (query or "").strip().lower()
    if not q:
        return []
    w = weights or _SEARCH_W
    sranks = semantic_ranks or {}

    hits: list[SearchHit] = []
    for n in nodes:
        v = q in (n.text or "").lower()
        km = any(q in kw.lower() for kw in n.keywords)
        tm = any(q in tag.lower() for tag in n.tags)
        sr = sranks.get(n.chunk_id)
        score = 0.0
        if v and "v" in active:
            score += w["v"] / (_RRF_K + 1)
        if km and "k" in active:
            score += w["k"] / (_RRF_K + 1)
        if tm and "t" in active:
            score += w["t"] / (_RRF_K + 1)
        if sr is not None and "s" in active:
            score += w["s"] / (_RRF_K + sr)
        if score > 0:
            hits.append(SearchHit(node=n, v=v, k=km, t=tm, s_rank=sr, score=score))
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits


def semantic_ranks(
    store: Any, ref_id: int, query_vec: list[float] | None, *, k: int = _SEM_TOPN
) -> dict[int, int]:
    """``{chunk_id: rank}`` for a query's top-``k`` semantically-nearest chunks in
    a ref — computed by the **HNSW index** (pgvector ``<=>``), not a python scan
    over every vector. Empty on no query vector / any failure (semantic degrades
    to lexical, never 500s)."""
    if not query_vec:
        return {}
    lit = "[" + ",".join(repr(float(x)) for x in query_vec) + "]"
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT c.chunk_id FROM chunks c "
                "JOIN chunk_embeddings ce ON ce.chunk_id = c.chunk_id "
                "WHERE c.ref_id = %s AND c.ord >= 0 "
                "ORDER BY ce.vector <=> %s::vector LIMIT %s",
                (ref_id, lit, k),
            ).fetchall()
    except Exception:
        return {}
    return {int(cid): rank for rank, (cid,) in enumerate(rows, start=1)}
