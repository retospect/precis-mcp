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

This is now the **sole** draft reader: the classic virtual-scroll `/drafts`
reader was retired and every draft deep link 30x-redirects into `/smartdraft`
(see the ``precis_web`` package docstring). `routes/drafts.py` remains only as the
shared editing/export/figure/lifecycle backend this reader reuses.
Full-document mode (relevance off) is the default and stays O(window), not
O(N): ±`_FULLDOC_WINDOW` chunks render verbatim, the rest are inert `skel`
placeholders lazily hydrated on scroll. :func:`focus_index` accepts both the
universal ``dc<id>`` handle and the legacy base58 form.

Review status: the reader's per-chunk marks read the ``chunk_review``
watermark ledger (migration 0086) via `routes/drafts.py::
_review_status_by_chunk` — lens namespace ``flow``/``cites``/``structure``/
``adversarial``/``toc`` (`precis.quest.review_fanout`) plus ``human`` as the
fixed point. Fanout is incremental (only stale chunks re-mint) at prio 2,
and a lens row is written back only by a clean, non-resumed tick that
concluded ``verdict: done`` (`executors/claude_inproc.py`) — a false
approval would hide an unreviewed section behind a green ✓.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC
from typing import TYPE_CHECKING, Any

from precis.quest.review_fanout import ALL_LENSES, DOC_LENSES
from precis.store._draft_ops import content_sha
from precis.utils.figure_source import RenderSpec, resolve_figure_source
from precis.utils.table_data import table_payload
from precis.utils.wordcount import PROSE_CHUNK_KINDS

if TYPE_CHECKING:
    from precis.store.store import Store

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
    #: (:func:`precis.utils.figure_source.resolve_figure_source`),
    #: else ``None``. Feeds the shared ``draft_figures.
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
    #: Whether this figure carries a ``meta.figure.data_package`` snapshot
    #: (a ``precis quest figure`` mint — schema 1, source/columns/rows) it
    #: can be re-rendered from — gates the "↻ refresh" button. A cheap
    #: truthiness check kept consistent with (but not importing)
    #: :func:`precis.export._data_package.collect_entry`, the export
    #: appendix's single source of truth for the same question. ``False``
    #: for a non-figure chunk.
    figure_has_data_package: bool = False
    #: ``meta.short`` for a ``chunk_kind='term'`` leaf — the
    #: term's primary label (may itself be the long descriptive form, e.g.
    #: ``'stereolithography'``). ``None`` for a non-term chunk.
    term_short: str | None = None
    #: ``meta.abbrev`` (gripe 56690) — a dedicated acronym surface, distinct
    #: from ``term_short``/``surface_forms``. ``None`` for a non-term chunk
    #: or a term without one.
    term_abbrev: str | None = None
    #: ``meta.surface_forms`` — extra aliases the leaf also hover-resolves
    #: under. Empty for a non-term chunk.
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
    def is_prose(self) -> bool:
        """Whether this chunk is one of ``PROSE_CHUNK_KINDS`` (paragraph/
        aside/callout/claim) — the review widget's gate for the
        ``flow``/``cites`` run-lens buttons (item 3): those two lenses only
        ever mint on prose chunks (:func:`precis.quest.review_fanout.
        _lenses_for_kind`), so offering them on a table/figure/term/equation
        block would silently no-op the click."""
        return self.chunk_kind in PROSE_CHUNK_KINDS

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
# version (a digest over each live chunk's identity, position and content_sha)
# with a TTL backstop for out-of-band drift (worker re-summarize/keyword). This
# is what makes click-around instant: the first focus pays the build, the rest
# read the cache + re-run only the ~7ms assemble_view.
#: {ref_id: (monotonic_stamp, version_token, base_nodes)}
_NODE_CACHE: dict[int, tuple[float, str, list[ChunkNode]]] = {}
#: Guards :data:`_NODE_CACHE` — request handlers run on FastAPI's threadpool
#: (sync routes) or concurrently under async, so two requests can race a
#: read-check-write on the same ref's entry without this (a `dict` mutation
#: isn't atomic across the check-then-set in :func:`build_nodes`).
_NODE_CACHE_LOCK = threading.Lock()
#: Rebuild after this many seconds regardless of version — heals drift a worker
#: made without minting a new chunk_id (summary/keyword rewrites, tag edits from
#: outside the smartdraft write path, which calls :func:`invalidate` directly).
_NODE_TTL = 45.0


def _cache_version(store: Store, ref_id: int) -> str | None:
    """A cheap content token for a draft — ``digest:tags`` over its live chunks.

    The digest folds in every input :func:`_build_nodes_uncached` reads off the
    chunk row itself: ``chunk_id`` (add / retire), ``pos`` and
    ``parent_chunk_id`` (move, re-parent), and ``content_sha`` (text). Hashing
    the *content* rather than counting rows is load-bearing — the live edit path
    (``store.drafts.edit_text``) UPDATEs a chunk **in place**, so a token built from
    ``count(*) + max(chunk_id)`` never moved on an edit and the reader served
    stale text until the TTL fired. The ``chunk_tags`` count covers tag add /
    remove.

    That makes the token self-invalidating for writes from *any* source (the
    smartdraft route, the MCP ``edit``/``tag`` verbs, a worker), not just the
    routes that call :func:`invalidate`. Derived data a worker rewrites without
    touching the chunk row (summaries, keywords) is still the TTL's job.

    One round-trip. Returns ``None`` if the store can't answer (a FakeStore in
    tests, a pool-less handle) — the caller then skips the cache entirely,
    preserving the pre-cache always-rebuild behaviour exactly."""
    try:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT "
                " (SELECT coalesce(md5(string_agg("
                "      chunk_id || ':' || coalesce(pos::text, '')"
                "      || ':' || coalesce(parent_chunk_id::text, '')"
                "      || ':' || coalesce(content_sha, ''), ',' ORDER BY chunk_id"
                "    )), '') FROM chunks WHERE ref_id = %s AND retired_at IS NULL), "
                " (SELECT count(*) FROM chunk_tags ct JOIN chunks c "
                "    ON c.chunk_id = ct.chunk_id WHERE c.ref_id = %s)",
                (ref_id, ref_id),
            ).fetchone()
    except Exception:
        return None
    return f"{row[0]}:{row[1]}" if row else None


def invalidate(ref_id: int) -> None:
    """Drop a draft's cached base nodes — call from any smartdraft write path
    (tag add/remove) so the change shows on the very next render, not after the
    TTL. Body-text edits self-invalidate via :func:`_cache_version`."""
    with _NODE_CACHE_LOCK:
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
    store: Store, ref_id: int, *, marks: dict[str, Any] | None = None
) -> list[ChunkNode]:
    """Assemble the draft's chunks into `ChunkNode`s (cached per draft; see
    :data:`_NODE_CACHE`). ``marks`` stamps pin/lock status as a per-request
    overlay on top of the cached base."""
    ver = _cache_version(store, ref_id)
    now = time.monotonic()
    with _NODE_CACHE_LOCK:
        ent = _NODE_CACHE.get(ref_id)
    if ver is not None and ent and ent[1] == ver and now - ent[0] < _NODE_TTL:
        nodes = ent[2]
    else:
        nodes = _build_nodes_uncached(store, ref_id)
        if ver is not None:
            with _NODE_CACHE_LOCK:
                _NODE_CACHE[ref_id] = (now, ver, nodes)
    _apply_marks(nodes, marks)
    return nodes


def _build_nodes_uncached(store: Store, ref_id: int) -> list[ChunkNode]:
    """The actual build — one join over reading-order (structure) +
    `list_blocks_for_ref` (keywords) + `block_views` (llm summary) + chunk tags.
    Pins/locks are left False; :func:`_apply_marks` overlays them per request."""
    # Lazy import: `routes.drafts` imports FROM this module (top-level
    # `precis_web.smartdraft`) via `routes.smartdraft` — an eager module-level
    # import here would risk a load-order cycle. Cheap (no heavy work at
    # import time) and called once per (cache-missed) build.
    from precis_web.routes.drafts import provenance_state

    chunks = store.drafts.reading_order(ref_id)
    # NB: do NOT load embeddings here — for a 10k-chunk draft that fetches ~10M
    # floats and (with a python cosine) blocks the page for seconds. Semantic is
    # served by the HNSW index at query time (`semantic_ranks`), not a full scan.
    blocks = {b.id: b for b in store.blocks.list_blocks_for_ref(ref_id)}
    views = store.drafts.block_views(ref_id)
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
        dp = fig_meta.get("data_package") if is_figure else None
        has_data_package = bool(
            isinstance(dp, dict)
            and dp.get("schema") == 1
            and "columns" in dp
            and "rows" in dp
        )
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
                figure_has_data_package=has_data_package,
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


def _load_chunk_tags(store: Store, ref_id: int) -> dict[int, list[str]]:
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
    store: Store,
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
    store: Store, ref_id: int, query_vec: list[float] | None, *, k: int = _SEM_TOPN
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


# ── review status ───────────────────────────────────────────────────────────
# The per-block indicator, its dropdown, and the toolbar
# rollup all derive from ONE whole-draft ledger fetch
# (``routes/drafts.py::_review_status_by_chunk``, itself one
# ``Store.review_status_for_draft`` query) — everything below turns that
# chunk_id-keyed map into the per-node render payload the template needs,
# plus the read-time (never sha-pinned) citation-integrity flag (item 5c).
# Human sign-off supersedes machine state by design (proposal's "churn/
# termination model" decision): a chunk approved by ``human`` at its
# current sha is DONE regardless of what the machine lenses say.

#: The per-chunk machine lenses that gate a PROSE block's own state.
_MACHINE_LENSES: tuple[str, ...] = ("flow", "cites")
#: The per-chunk machine lenses that gate a HEADING's own state, and a
#: prose block's *section* state (via its nearest heading ancestor).
_SECTION_LENSES: tuple[str, ...] = ("structure", "adversarial")
_STATUS_SYMBOL: dict[str, str] = {"current": "✓", "stale": "⚠", "never": "–"}


def _age_str(at: Any) -> str:
    """A terse ``"2h ago"``/``"3d ago"`` for the tooltip — ``""`` when
    ``at`` is absent or unparseable (a synthetic never-reviewed row, or a
    test fixture that doesn't bother with a real timestamp)."""
    if not at:
        return ""
    try:
        from datetime import datetime

        ts = datetime.fromisoformat(at) if isinstance(at, str) else at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        secs = (datetime.now(UTC) - ts).total_seconds()
    except Exception:
        return ""
    if secs < 3600:
        return f"{max(1, int(secs // 60))}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _matrix_row(
    checker: str, entry: dict[str, Any] | None, *, via_section: bool
) -> dict[str, Any]:
    """One tooltip-matrix line — ``✓ current`` / ``⚠ stale`` / ``– never``,
    with the checker's verdict + age once it has ever run; section lenses
    (item 2's "via section" imprecision) get an explicit suffix so the
    tooltip never reads as if the paragraph itself carries that lens."""
    if entry is None:
        status = "never"
    elif entry.get("dirty"):
        status = "stale"
    else:
        status = "current"
    label = checker + (" (via section)" if via_section else "")
    bits = [f"{_STATUS_SYMBOL[status]} {label}"]
    if entry is not None:
        if entry.get("verdict"):
            bits.append(str(entry["verdict"]))
        age = _age_str(entry.get("at"))
        if age:
            bits.append(age)
    return {
        "checker": checker,
        "via_section": via_section,
        "status": status,
        "line": " · ".join(bits),
    }


def review_indicator(
    chunk_id: int, chunk_kind: str, status_by_chunk: dict[int, dict[str, Any]]
) -> dict[str, Any] | None:
    """Derive one block's 4-state review indicator from the whole-draft
    ledger map (item 6). ``None`` when the chunk is absent from the map
    (not reviewable — no ``content_sha`` / retired / unordered, see
    ``_review_status_by_chunk``'s docstring).

    ``state`` is one of:

    * ``"empty"`` — checks outstanding (grey).
    * ``"machine"`` — every relevant machine lens approved at the current
      sha, human still pending (hollow/blue). A PROSE block's relevant
      lenses are its own ``flow``/``cites`` PLUS its enclosing heading's
      ``structure``/``adversarial`` ("via section" — the nearest heading
      ancestor, ``_section_chunk_id``); a HEADING's are its own
      ``structure``/``adversarial`` (plus ``toc`` when the ledger map
      carries a ``toc`` row here — only the document's first chunk ever
      does).
    * ``"human"`` — ``human`` approved at the current sha (green).
    * ``"dirty"`` — was human-approved, edited since (amber).

    ``matrix`` is the tooltip's per-checker list (:func:`_matrix_row`), in
    lens → human display order."""
    own = status_by_chunk.get(chunk_id)
    if own is None:
        return None
    is_heading = chunk_kind == "heading"
    matrix: list[dict[str, Any]] = []
    machine_seen = False
    machine_ok = True

    def _gate(entry: dict[str, Any] | None) -> None:
        nonlocal machine_seen, machine_ok
        if entry is None or entry.get("dirty"):
            machine_ok = False
        else:
            machine_seen = True

    if is_heading:
        for lens in _SECTION_LENSES:
            entry = own.get(lens)
            matrix.append(_matrix_row(lens, entry, via_section=False))
            _gate(entry)
    else:
        for lens in _MACHINE_LENSES:
            entry = own.get(lens)
            matrix.append(_matrix_row(lens, entry, via_section=False))
            _gate(entry)
        section_id = own.get("_section_chunk_id")
        section = status_by_chunk.get(section_id) if section_id is not None else None
        for lens in _SECTION_LENSES:
            entry = section.get(lens) if section is not None else None
            matrix.append(_matrix_row(lens, entry, via_section=True))
            _gate(entry)
    if "toc" in own:  # rides on whichever chunk is document-first (item 10)
        entry = own.get("toc")
        matrix.append(_matrix_row("toc", entry, via_section=False))
        _gate(entry)

    human = own.get("human")
    matrix.append(_matrix_row("human", human, via_section=False))

    if human is not None and not human.get("dirty"):
        state = "human"
    elif human is not None and human.get("dirty"):
        state = "dirty"
    elif machine_seen and machine_ok:
        state = "machine"
    else:
        state = "empty"

    return {
        "state": state,
        "human": human,
        "matrix": matrix,
        "tooltip": "\n".join(r["line"] for r in matrix),
    }


def cite_integrity_ok(store: Store, text: str, cache: dict[int, bool]) -> bool:
    """``True`` unless ``text`` carries a cite token that fails to resolve
    (a dead/merged-away ``[pc<id>]``) or whose cited paper isn't held (a
    stub with zero body blocks — the same "to-fetch" signal
    ``handlers/_citations_view.py`` partitions on) — item 5c. Deliberately
    read-time, NOT sha-pinned: a paper can vanish from the corpus without
    the paragraph's own text changing, so a ledger checker would rot
    silently — this is recomputed on every render instead, never stored.
    Reuses ``_citations_view``'s token scanner rather than re-parsing the
    cite grammar; ``cache`` (shared across one render's blocks) avoids a
    repeat store hit for a paper cited from several paragraphs."""
    from precis.handlers._citations_view import _iter_chunk_tokens
    from precis.utils import handle_registry

    for _raw, tag, payload in _iter_chunk_tokens(text or ""):
        if tag != "handle":
            continue  # a pub_id placeholder either resolves or is
            # accidental base32-looking prose (_citations_view's own
            # phrase) — neither is an integrity problem this flag raises.
        parsed = handle_registry.parse(payload)
        if parsed is None:
            continue  # a well-formed handle of some other kind — not a cite
        kind, is_chunk, pk = parsed
        if kind != "paper":
            continue
        if is_chunk:
            resolved = store.resolve_handle(payload)
            if resolved is None or resolved.kind != "paper":
                return False  # dead/merged-away chunk cite
            ref_id = resolved.ref_id
        else:
            ref_id = pk
        held = cache.get(ref_id)
        if held is None:
            held = store.blocks.count_blocks(ref_id) > 0
            cache[ref_id] = held
        if not held:
            return False  # cited paper isn't held (a stub)
    return True


def claim_trust_for_block(
    # `Any`, not `Store`: unit tests call this with a bare `object()` —
    # the two store-touching calls (`resolve_head_ref_id`/`claim_trust`)
    # are monkeypatched away in every test, so the sentinel never resolves
    # a real attribute (precedent: `briefing_cast._lane_quest`).
    store: Any,
    text: str,
    cache: dict[str, Any],
) -> dict[str, Any] | None:
    """Worst-of claim trust across ``text``'s distinct cite heads —
    the trust-surfaces editor badges, the
    ``claim_trust`` counterpart to :func:`cite_integrity_ok`. ``None`` when
    the block cites nothing shaky (no cite heads, or every resolved head is
    ``clean``). A non-clean head keeps its label — the softer ``abstract``
    (Ⓐ) / ``vouched`` (✍) an ``unacquirable_override`` folds an unverified
    claim to, or ``unverified`` / ``unsupported`` — and the block badge is
    the worst-of (:func:`~precis.taproot.trust.worse_trust`). A head that
    resolves ``clean`` is skipped; one that doesn't resolve to a *finding*
    (a bare paper
    cite, or prose that merely looks like a cite head) is skipped —
    that's ``cite_integrity_ok``'s domain, not trust's.

    ``cache`` (shared across one render's blocks, exactly like
    ``cite_integrity_ok``'s own cache) maps a head to its resolved
    :class:`~precis.taproot.trust.TrustState`, or ``None`` for a head that
    doesn't resolve to a finding — so a claim cited from several blocks in
    this render costs one ``claim_trust`` store round-trip, not N."""
    if "[" not in (text or ""):
        return None  # cheap pre-check: no bracket token, no cite head possible
    from precis.taproot.trust import claim_trust
    from precis_web.claim_render import cite_heads_in, resolve_head_ref_id

    offenders: list[dict[str, Any]] = []
    for head in cite_heads_in(text):
        if head not in cache:
            ref_id = resolve_head_ref_id(store, head)
            cache[head] = claim_trust(store, ref_id) if ref_id is not None else None
        state = cache[head]
        if state is None or state.label == "clean":
            continue
        offenders.append({"head": head, "label": state.label, "note": state.note})
    if not offenders:
        return None
    from precis.taproot.trust import worse_trust

    label = "clean"
    for o in offenders:
        label = worse_trust(label, o["label"])
    return {"label": label, "heads": offenders}


def _prepopulate_trust_cache(store: Store, nodes: list[ChunkNode]) -> dict[str, Any]:
    """Bulk-resolve every distinct cite head across ``nodes`` into
    :func:`claim_trust_for_block`'s cache shape up front — the batch
    counterpart to that function's own lazy per-head ``claim_trust`` call.

    A render window with many distinct hub cites used to cost one FULL
    ``claim_trust`` derivation (~7 round trips once a hub's supporters are
    counted) PER distinct head; pre-warming the shared cache with
    :func:`~precis.taproot.trust.claim_trust_bulk` collapses all of them
    into a handful of bulk queries regardless of head count
    (OPEN-ITEMS.md's "/smartdraft reader" O(all-hubs) TTFB fix). A head
    that never resolves to a finding is simply absent here — the per-node
    loop's lazy ``if head not in cache`` fallback still handles it (one
    no-op resolve, no store hit for an ``fi``-shaped head)."""
    from precis.taproot.trust import claim_trust_bulk
    from precis_web.claim_render import cite_heads_in, resolve_head_ref_id

    head_ref: dict[str, int] = {}
    for n in nodes:
        for head in cite_heads_in(n.text or ""):
            if head in head_ref:
                continue
            ref_id = resolve_head_ref_id(store, head)
            if ref_id is not None:
                head_ref[head] = ref_id
    if not head_ref:
        return {}
    states = claim_trust_bulk(store, head_ref.values())
    return {head: states.get(ref_id) for head, ref_id in head_ref.items()}


def review_payloads_for(
    nodes: list[ChunkNode],
    status_by_chunk: dict[int, dict[str, Any]],
    store: Store,
) -> dict[str, dict[str, Any]]:
    """``{dc: payload}`` for every reviewable node in ``nodes`` — scoped to
    the per-request RENDERED set (the middle pane, or one ``/blocks``
    hydration window), never the whole draft, because both the integrity
    and claim-trust flags can hit the store (``no per-block DB hits`` means
    no per-RENDER-set blowup either — this shares one ``integrity_cache``
    AND one ``trust_cache`` across the whole call, so a paper/finding
    cited from several of these blocks costs one store hit, not N; the
    trust cache is additionally pre-warmed in bulk up front, see
    :func:`_prepopulate_trust_cache`). Each payload is :func:`review_indicator`'s
    dict plus ``integrity_ok`` (5c) and ``claim_trust`` (finding-trust-
    surfaces §3 — ``None`` / ``{"label": "unverified"|"unsupported",
    "heads": [...]}``, worst-of across the block's cite heads)."""
    integrity_cache: dict[int, bool] = {}
    trust_cache: dict[str, Any] = _prepopulate_trust_cache(store, nodes)
    out: dict[str, dict[str, Any]] = {}
    for n in nodes:
        ind = review_indicator(n.chunk_id, n.chunk_kind, status_by_chunk)
        if ind is None:
            continue
        ind["integrity_ok"] = cite_integrity_ok(store, n.text, integrity_cache)
        ind["claim_trust"] = claim_trust_for_block(store, n.text, trust_cache)
        out[n.dc] = ind
    return out


def checker_rollup(
    status_by_chunk: dict[int, dict[str, Any]],
) -> dict[str, dict[str, int]]:
    """``{checker: {"current", "stale", "never"}}`` over every chunk each
    checker could ever apply to — the toolbar dropdown's per-checker
    breakdown (item 8), derived purely from the one whole-draft status
    fetch (no extra query). ``flow``/``cites``/``human`` scope to PROSE
    chunks (``human`` mirrors the ``N/M`` rollup's own prose-only
    denominator — decision 2026-08-02); ``structure``/``adversarial``
    scope to heading chunks; ``toc`` scopes to wherever the map's
    synthetic/real toc row rides (the document's first chunk — always
    exactly one, see ``Store.review_status_for_draft``)."""
    order = (*ALL_LENSES, "human", *DOC_LENSES)
    counts: dict[str, dict[str, int]] = {
        c: {"current": 0, "stale": 0, "never": 0} for c in order
    }
    for entry in status_by_chunk.values():
        kind = entry.get("_chunk_kind")
        applicable: list[str] = []
        if kind in PROSE_CHUNK_KINDS:
            applicable = ["flow", "cites", "human"]
        elif kind == "heading":
            applicable = ["structure", "adversarial"]
        if "toc" in entry:
            applicable.append("toc")
        for checker in applicable:
            row = entry.get(checker)
            if row is None:
                counts[checker]["never"] += 1
            elif row.get("dirty"):
                counts[checker]["stale"] += 1
            else:
                counts[checker]["current"] += 1
    return counts
