"""Smartdraft — the fisheye rail's LLM-free relevance engine.

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

import re
import time
from dataclasses import dataclass, field
from typing import Any

from precis.store._draft_ops import content_sha
from precis.utils.figure_source import RenderSpec, resolve_figure_source
from precis.utils.table_data import table_payload

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
#: Full-document (📄, relevance-off) mode renders ±this-many chunks around the
#: focus verbatim server-side; everything else is a lazily-hydrated `skel`
#: placeholder. Keeps the initial full-doc page O(window), not O(N), on a huge
#: draft — the client fills the rest on scroll (`/blocks`). Fisheye mode is
#: already bounded by `_TOC_BUDGET`; this bounds the one unbounded reading path.
_FULLDOC_WINDOW = 40
#: Verbatim cap for the ±1 neighbours (truncated toward the focus edge so the
#: text reads continuously into/out of the focus).
_NEIGHBOR_CAP = 400
#: How many high-pressure *non-neighbour* chunks to surface as "relevant
#: elsewhere" under the middle window.
_ELSEWHERE_K = 4
#: The fisheye's keep budget — the max number of *soft* keeps (keyword-shared /
#: above-threshold) rendered as TOC rows; the rest collapse into ``⋯ n ⋯`` runs.
#: Headings, status (pin/lock), and search hits are *hard* keeps and never count
#: against it. Without this, a large draft whose focus shares a common keyword
#: kept thousands of rows — defeating the fisheye and bloating the page to
#: megabytes. This makes the render O(budget), not O(N). (The module's stated
#: principle: "fidelity ∝ priority, capped by a budget" — this is the cap.)
_TOC_BUDGET = 160

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
    #: Recovered ``{header, rows, caption}`` for a ``chunk_kind='table'``
    #: chunk (:func:`precis.utils.table_data.table_payload`), else ``None``.
    #: Feeds the ``⊞ edit table`` grid editor (shared ``draft_editors.
    #: draft_table_editor`` — gripe 56746) in the focus pane.
    table: dict[str, Any] | None = None
    #: Medium-aware render spec for a ``chunk_kind='figure'`` chunk
    #: (:func:`precis.utils.figure_source.resolve_figure_source`, ADR
    #: 0034/0057/0058), else ``None``. Feeds the shared ``draft_figures.
    #: figure_media`` macro (gripe 56668) in the focus pane.
    figure_render: RenderSpec | None = None
    #: ``meta.figure.origin`` (``original``/``own_graph``/``third_party``) —
    #: the clearance-badge chip. ``None`` for a non-figure chunk.
    figure_origin: str | None = None
    #: Whether the figure is cleared to ship (medium-aware — an asset-less
    #: figure reads uncleared, a drawn canvas cleared). ``None`` for a
    #: non-figure chunk.
    figure_cleared: bool | None = None
    #: ``meta.figure.permission`` — the third-party publisher paper-trail
    #: (publisher / permission_id / status / dates / …), else ``None``.
    figure_permission: dict[str, Any] | None = None
    #: ``meta.short`` for a ``chunk_kind='term'`` leaf (ADR 0052) — the
    #: term's primary label (may itself be the long descriptive form, e.g.
    #: ``'stereolithography'``). ``None`` for a non-term chunk.
    term_short: str | None = None
    #: ``meta.abbrev`` (gripe 56690) — a dedicated acronym surface, distinct
    #: from ``term_short``/``surface_forms``. ``None`` for a non-term chunk
    #: or a term without one.
    term_abbrev: str | None = None
    #: ``meta.surface_forms`` — extra aliases the leaf also hover-resolves
    #: under (ADR 0052 §4). Empty for a non-term chunk.
    term_surface_forms: list[str] = field(default_factory=list)
    #: Grounding provenance for a ``chunk_kind='paragraph'`` node —
    #: ``"sourced"`` / ``"pending"`` / ``"unsourced"``
    #: (:func:`precis_web.routes.drafts.provenance_state`), the reader's
    #: per-paragraph colour marker. ``""`` for a non-prose node (heading /
    #: figure / table / term), which has no citation surface.
    provenance: str = ""

    @property
    def is_heading(self) -> bool:
        return self.chunk_kind == "heading"

    @property
    def is_table(self) -> bool:
        return self.chunk_kind == "table"

    @property
    def is_figure(self) -> bool:
        return self.chunk_kind == "figure"

    @property
    def is_term(self) -> bool:
        return self.chunk_kind == "term"

    @property
    def editable(self) -> bool:
        """Only free-text body chunks are inline-editable via the plain-text
        editor here (a heading is text too). A table gets its own grid editor
        (``is_table`` / ``table`` — gripe 56746); a figure gets its own
        medium-aware image render + clearance badge (``is_figure`` /
        ``figure_render`` — gripe 56668) — neither has a free-text edit
        path, so both stay outside ``editable``."""
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
    # Lazy import: `routes.drafts` imports FROM this module (top-level
    # `precis_web.smartdraft`) via `routes.smartdraft` — an eager module-level
    # import here would risk a load-order cycle. Cheap (no heavy work at
    # import time) and called once per (cache-missed) build.
    from precis_web.routes.drafts import provenance_state

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
        is_table = c.chunk_kind == "table"
        table = table_payload(getattr(c, "meta", None), c.text) if is_table else None
        is_figure = c.chunk_kind == "figure"
        fig_meta = (
            (getattr(c, "meta", None) or {}).get("figure", {}) if is_figure else {}
        )
        fsrc = resolve_figure_source(store, c) if is_figure else None
        is_term = c.chunk_kind == "term"
        term_meta = (getattr(c, "meta", None) or {}) if is_term else {}
        is_paragraph = c.chunk_kind == "paragraph"
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
                table=table,
                figure_render=fsrc.render if fsrc else None,
                figure_origin=fig_meta.get("origin") if is_figure else None,
                figure_cleared=fsrc.cleared if fsrc else None,
                figure_permission=fig_meta.get("permission") if is_figure else None,
                term_short=term_meta.get("short") if is_term else None,
                term_abbrev=term_meta.get("abbrev") if is_term else None,
                term_surface_forms=list(term_meta.get("surface_forms") or [])
                if is_term
                else [],
                provenance=provenance_state(c.text or "") if is_paragraph else "",
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
    chunk (else 0). A missing/stale handle degrades to the default.

    ``focus_dc`` may be the universal ``dc<id>`` handle OR the legacy base58
    anchor (``chunks.handle``, optionally ``¶``/``c-`` prefixed): the app-wide
    ``/c/<handle>`` and agentlog deep links carry base58, so accepting both
    lets every ``¶``/``§`` citation click land on the right chunk here."""
    if focus_dc:
        base = focus_dc.lstrip("¶")
        if base.startswith("c-"):
            base = base[2:]
        for n in nodes:
            if n.dc == focus_dc or n.base58 == base:
                return n.idx
    for n in nodes:
        if not n.is_heading:
            return n.idx
    return 0


def _term_surfaces(term: ChunkNode) -> list[str]:
    """The string surfaces a term chunk is known by — its ``short``, its
    dedicated ``abbrev`` (gripe 56690), and each ``surface_forms`` alias —
    longest first (mirrors :func:`precis_web.linkify._highlight_abbrevs`'s
    matching, so ``RNA-seq`` beats ``RNA``). Deliberately excludes ``text``:
    per :meth:`precis.store._draft_ops.PapersMixin.defined_terms`, a term
    leaf's ``text`` is its DEFINITION prose, never a lookup surface — a
    paragraph merely containing the definition wording is not an
    "occurrence" of the term any more than it would get a live
    ``<abbr class="pa">`` highlight in the reader. A glossary term has no
    ``mpn`` (that's the manufacturing-part surface), so it's omitted here."""
    surfaces = {
        s.strip()
        for s in (term.term_short, term.term_abbrev, *term.term_surface_forms)
        if s and s.strip()
    }
    return sorted(surfaces, key=len, reverse=True)


def term_occurrences(nodes: list[ChunkNode], term: ChunkNode) -> list[ChunkNode]:
    """Every *other* chunk in ``nodes`` (already-loaded — no DB scan) whose
    text mentions one of ``term``'s surfaces — the "occurs in N places"
    backlink list for a focused glossary/registry term (gripe 56690). Mirrors
    :func:`precis_web.linkify._highlight_abbrevs` EXACTLY — same surface set
    (excludes the definition ``text``, see :func:`_term_surfaces`), same
    longest-surface-first + word-boundary + plural/possessive inflection
    pattern, and the same case-SENSITIVE matching (no ``re.IGNORECASE``) —
    so this count equals the number of paragraphs that actually get a live
    highlight, not a superset that also catches definition-prose mentions or
    case variants the reader never highlights. A chunk matching more than
    one surface (e.g. both ``STL`` and ``stereolithography``) is counted
    once, not per surface. Reading-order-preserving."""
    surfaces = _term_surfaces(term)
    if not surfaces:
        return []
    pat = re.compile(
        r"(?<![\w-])(" + "|".join(re.escape(s) for s in surfaces) + r")"
        r"(?:s|es|'s|’s)?(?!\w)"
    )
    return [n for n in nodes if n.dc != term.dc and n.text and pat.search(n.text)]


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
class OutlineSection:
    """A node in the heading-outline tree — a heading plus the fisheye TOC rows
    that fall *directly* under it (kept body chunks + collapsed runs, before the
    first sub-heading) and its child sections. Rendered as a foldable
    ``<details>`` (CSS-only, no JS). ``open`` is the server-decided fold state:
    the focus's ancestor path (and any search-hit's path) ships open, the rest
    ship folded. ``heading=None`` is the synthetic root for any body that
    precedes the first heading."""

    heading: ChunkNode | None
    body: list[TocRow] = field(default_factory=list)
    children: list[OutlineSection] = field(default_factory=list)
    open: bool = False


def build_outline(
    toc_rows: list[TocRow], *, reveal_dcs: set[str] | None = None
) -> list[OutlineSection]:
    """Fold the flat fisheye TOC (headings + kept body + collapsed runs, in
    reading order) into a heading tree, nesting by ``depth``. Cheap — a single
    stack walk over the already-built rows, no DB. Sections on the path to any
    ``reveal_dcs`` (the focus + search hits) are opened; roots open by default
    when nothing is revealed. The heading tree is clean by construction (a
    heading's parent is always another heading — verified in prod), so depth is
    a faithful nesting key."""
    reveal_dcs = reveal_dcs or set()
    roots: list[OutlineSection] = []
    stack: list[OutlineSection] = []

    def open_path() -> None:
        for s in stack:
            s.open = True

    for row in toc_rows:
        n = row.node
        if n is not None and n.is_heading:
            sec = OutlineSection(heading=n)
            while stack and (
                stack[-1].heading is None or stack[-1].heading.depth >= n.depth
            ):
                stack.pop()
            (stack[-1].children if stack else roots).append(sec)
            stack.append(sec)
            if n.dc in reveal_dcs:
                open_path()
        else:
            if not stack:  # body before the first heading — synthetic root
                stack.append(OutlineSection(heading=None))
                roots.append(stack[-1])
            stack[-1].body.append(row)
            if n is not None and n.dc in reveal_dcs:
                open_path()

    if not reveal_dcs:  # no focus/hits to reveal → show the top level
        for r in roots:
            r.open = True
    return roots


def _est_px(n: ChunkNode) -> int:
    """A rough rendered-height estimate (px) for a full-doc ``skel`` spacer, so
    the un-hydrated document has a sane scroll height/scrollbar before its
    distant chunks are lazily filled. Deliberately approximate — the placeholder
    is replaced by the real block (its true height) the moment it nears the
    viewport; the estimate only has to keep the scrollbar from lurching wildly."""
    if n.is_heading:
        return 34
    if n.is_figure:
        return 240
    if n.is_table:
        return 180
    text = n.text or ""
    lines = (len(text) // 90) + text.count("\n") + 1
    return min(40 + lines * 22, 1200)


@dataclass(slots=True)
class MidRow:
    """A middle-pane row. ``mode`` grades the fidelity by distance to focus:
    ``full`` (focus) · ``tail``/``head`` (±1 verbatim, truncated toward the
    focus) · ``summary`` (±2). In full-document mode a distant chunk is a
    ``skel`` placeholder (``est`` px tall) the client hydrates lazily on scroll
    via ``/smartdraft/{ident}/blocks``. ``display`` is the text to render."""

    node: ChunkNode
    is_focus: bool
    mode: str = "summary"
    display: str = ""
    est: int = 0  # skel-mode height estimate (px) for the un-hydrated spacer


@dataclass(slots=True)
class SmartView:
    ref_id: int
    focus: ChunkNode | None
    toc: list[TocRow] = field(default_factory=list)
    #: The heading-outline tree (foldable ``<details>`` in the reader) built from
    #: ``toc`` — the left pane's fisheye-mode backbone.
    outline: list[OutlineSection] = field(default_factory=list)
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
    budget: int | None = None,
) -> list[TocRow]:
    """The fisheye TOC. ``relevance=False`` → the plain full outline (every
    chunk). ``relevance=True`` → keep headings + status + **keyword-shared** +
    high-pressure chunks; collapse quiet-irrelevant runs to a ``⋯ n ⋯`` marker
    (order never reshuffles — only expand/collapse tracks the focus).
    ``keep_dcs`` (search hits) are always kept — the in-TOC search view shows
    every match, uncollapsed. ``budget`` caps the *soft* keeps (shared /
    above-threshold) to the top-N by pressure so the TOC is O(budget), not O(N),
    on a huge draft; headings / status / hits are hard keeps, uncapped."""
    shared_idx = shared_idx or set()
    keep_dcs = keep_dcs or set()
    # Under a budget, pre-select which soft keeps survive: the highest-pressure
    # `budget` of the shared/above-threshold candidates. The rest collapse.
    soft_ok: set[int] | None = None
    if relevance and budget is not None:
        cands = [
            n.idx
            for n in nodes
            if not n.is_heading
            and not n.has_status
            and n.dc not in keep_dcs
            and (n.idx in shared_idx or pres.get(n.idx, 0.0) >= _KEEP_THRESHOLD)
        ]
        cands.sort(key=lambda i: pres.get(i, 0.0), reverse=True)
        soft_ok = set(cands[:budget])
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
        if soft_ok is not None:
            soft = n.idx in soft_ok
        else:
            soft = is_shared or pres.get(n.idx, 0.0) >= _KEEP_THRESHOLD
        keep = not relevance or n.is_heading or n.has_status or n.dc in keep_dcs or soft
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
        nodes,
        pres,
        relevance=relevance,
        shared_idx=shared_idx,
        keep_dcs=keep_dcs,
        budget=_TOC_BUDGET,
    )
    # Fold the flat TOC into the foldable heading-outline tree. Reveal (open) the
    # focus's section path + any search hit's path so what matters is unfolded.
    reveal = {nodes[fi].dc} | (keep_dcs or set())
    outline = build_outline(toc, reveal_dcs=reveal)

    middle: list[MidRow] = []
    if not relevance:
        # Full / uncompressed document. Only a window around the focus renders
        # verbatim server-side; distant chunks are `skel` placeholders (sized by
        # `_est_px`) the client hydrates lazily on scroll (via `/blocks`), so a
        # 10k-chunk draft costs a screenful of real nodes at load, not 10k. The
        # focus stays framed. The Fisheye⇄Full toggle drives both panes.
        lo = max(0, fi - _FULLDOC_WINDOW)
        hi = min(len(nodes), fi + _FULLDOC_WINDOW + 1)
        for i, n in enumerate(nodes):
            if i == fi:
                middle.append(
                    MidRow(node=n, is_focus=True, mode="full", display=n.text)
                )
            elif lo <= i < hi:
                middle.append(
                    MidRow(node=n, is_focus=False, mode="doc", display=n.text)
                )
            else:
                middle.append(
                    MidRow(
                        node=n, is_focus=False, mode="skel", display="", est=_est_px(n)
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
        outline=outline,
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
