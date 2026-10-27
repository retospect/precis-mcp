"""Dynamic TOC renderer over per-chunk KeyBERT keywords.

F20 (2026-06-05). Replaces the older ``ref_segments``-backed
renderer entirely. The new model:

* No precomputed segmentation. The per-chunk-keybert worker
  (:mod:`precis.workers.chunk_keywords`) populates
  ``chunks.keywords TEXT[]`` and ``chunks.keywords_meta JSONB``.
* At query time we fetch the chunks in scope, compute adjacent
  Jaccard distances on the keyword sets, feed them to the existing
  :func:`precis.utils.segmentation.segment_dp`, and label each
  resulting cluster from the union of its constituent chunks'
  keywords.

Output shape — TOON table, always ``(handle, keywords)``::

    # pa5 TOC — N chunks, K clusters

    Topics: shared keywords across ≥75% of clusters    (optional)

    {handle	keywords}
    pa5~0..14	keyword phrases for cluster 0
    pa5~15..29	keyword phrases for cluster 1
    …

    Next: drill a cluster for finer structure           (optional)
      get(kind='paper', id='pa5~15..29', view='toc')

The ``handle`` is the record's universal handle (``pa<id>``),
so every row + drill-in hint is a copy-pasteable ``get`` id. The
legacy ``slug~pos`` form is NOT emitted here — a cite-key slug
(``vaswani17``) round-trips on input, but the kind-prefixed
``paper:vaswani17`` did not.

The ``Topics:`` line is a lossless summary — it lists keywords that
appear in ≥75% of clusters; the per-row labels still include them,
so the line is a redundant overview, never a transformation.

The ``Next:`` block fires for any cluster that would itself re-cluster
if drilled (``> 2 * _DRILL_GROUP_SIZE`` chunks — the same predicate the
drill-down path uses). It hints the agent that a recursive
``view='toc'`` on that handle yields more structure.

Clustering is **scope-aware** so drilling walks a hierarchy rather
than bottoming out into a flat per-chunk dump. At the *top level* (no
scope) a short body (< ``_BUCKETING_THRESHOLD`` chunks) is read
directly — one row per chunk, nothing to drill. But a *drill-down*
(``scope`` set, e.g. double-clicking a group in the web reader)
re-clusters the sub-range into sub-groups whenever it can still split
(≥ ``2 * _MIN_CLUSTER_SIZE`` chunks), targeting a shallow branching
factor (``_DRILL_GROUP_SIZE``) instead of the log-scaled top-level
table. Papers have no heading tree, so this recursive keyword
clustering *is* the hierarchy. Only a range too small
to split further shows one row per chunk. For the actual chunk text,
call ``get(...)`` without ``view='toc'``.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from precis.format import render_agent_table
from precis.utils.segmentation import Segment, segment_dp
from precis.utils.text import strip_emphasis

if TYPE_CHECKING:
    from precis.store.protocols import ChunkListingStore

#: Below this chunk count, a *top-level* TOC (no scope) renders per-chunk
#: keywords directly instead of clustering — a short paper is scannable
#: as-is and there is nothing to drill into. A *drill-down* (scope set)
#: ignores this floor and keeps sub-clustering (see ``_should_cluster``).
_BUCKETING_THRESHOLD = 30

#: Target chunks-per-group when re-clustering a drilled sub-range. A
#: drill-down aims for a shallow branching factor (~this many chunks per
#: sub-group) rather than the log-scaled ~15-row table the top level
#: uses, so repeatedly drilling walks a legible hierarchy down to
#: individual chunks instead of dumping one flat per-chunk list.
_DRILL_GROUP_SIZE = 4

#: Hard cap on the cluster count. Keeps the rendered table skimmable
#: even on large ranges.
_BUCKET_MAX_COUNT = 15

#: Floor on cluster count once bucketing is active. Three rows is
#: the minimum that gives a useful sense of the range's shape.
_BUCKET_MIN_COUNT = 3

#: RAKE-style top-K keywords per cluster label.
_LABEL_TOP_K = 5

#: Minimum cluster size in the DP output. Smaller clusters get
#: absorbed into a neighbour by :func:`_collapse_singletons`.
#: 2 (was 3 pre-2026-06-05) — only true singletons collapse, so the
#: requested bucket count from :func:`_bucket_count` is preserved
#: more faithfully and the user gets the granularity they asked for.
_MIN_CLUSTER_SIZE = 2

#: Multiplier in the log-scaled bucket-count formula. 7 (was 5
#: pre-2026-06-05) — produces ~15 buckets at N≈150 instead of ~11,
#: matching the K_MAX ceiling at the sizes papers actually hit.
_BUCKET_MULTIPLIER = 7

#: Fraction of clusters a keyword must span to be promoted to the
#: "Topics:" header. ≥75% means it is pervasive enough to be the
#: paper-wide theme. Lower thresholds (≥50%) would hide *which*
#: half the keyword belongs to — see discussion in toc_db review.
_TOPICS_RATIO = 0.75

#: Cap on Topics-line keywords; same shape as per-row labels.
_TOPICS_TOP_K = 5

#: Fraction of in-scope chunks that must carry KeyBERT keywords before
#: the Jaccard clusterer is trusted. Below it the keyword graph
#: degenerates — empty sets contribute distance 0 to either side, so
#: ``segment_dp`` cuts arbitrarily and every label renders empty
#: (gr520232: every paper ingested since ~2026-09-11 sits at 0 coverage
#: while the ``chunk_keywords`` backlog drains). The TOC then falls back
#: to the ``chunks.section_path`` heading outline, which needs no worker.
_KEYWORD_COVERAGE_MIN = 0.25

#: A section title's display cap in the keywords column.
_SECTION_TITLE_CAP = 80

#: Leading section number — ``2.``, ``4.3.4.`` — grouping subsections
#: under their top-level number.
_SECTION_NUM_RE = re.compile(r"^(\d+)\.")

#: The full leading ``N.``/``N.M.`` run of a numbered section title —
#: its dot count is the title's depth, so a ``2. Methods`` heading
#: (depth 1) outranks ``2.1. …`` as a merged group's name.
_SECTION_DEPTH_RE = re.compile(r"^((?:\d+\.)+)")


def render_from_store(
    *,
    store: ChunkListingStore,
    ref_id: int,
    handle: str,
    kind: str,
    scope: tuple[int, int] | None = None,
    readiness_line: str | None = None,
) -> str:
    """Render the TOC body for ``ref_id``, optionally scoped to a range.

    ``handle`` is the record's universal handle (``pa<id>``);
    every rendered row + drill-in hint prefixes it so the output is a
    copy-pasteable ``get`` id. ``scope`` restricts to body chunks inside
    the inclusive ``(lo, hi)`` position range. Without it, the full body
    is clustered.

    ``readiness_line`` (read-for-question loop, slice 4) is a pre-rendered
    ``"readiness: embedded N/M · summarised N/M"`` line, threaded straight
    through into the header. Computed by the caller (typically only for
    the top-level, unscoped call — see ``PaperHandler._render_toc``) since
    this module's own store access is deliberately limited to
    ``ChunkListingStore`` (:mod:`precis.store.protocols`), which has no
    embed/summarize backlog query surface.

    Returns Markdown. First line is the kind-aware headline; the second
    (when ``readiness_line`` is given) is the readiness line; the rest is
    the TOON table, optionally preceded by a ``Topics:`` line and followed
    by a ``Next:`` drill-in block.
    """
    pos_range = scope
    blocks = store.chunks.list_chunks_for_ref(ref_id, pos_range=pos_range)
    if not blocks:
        return _empty_body(handle=handle, kind=kind, scope=scope)

    n = len(blocks)
    groups, mode = _clusters(blocks, scope=scope)
    if mode == "per-chunk":
        return _render_per_chunk(
            handle=handle, blocks=blocks, scope=scope, readiness_line=readiness_line
        )

    rows: list[dict[str, str]] = []
    row_keyword_sets: list[list[str]] = []
    fat_clusters: list[str] = []
    for bucket, label_terms in groups:
        lo_pos = bucket[0].ord
        hi_pos = bucket[-1].ord
        row_handle = (
            f"{handle}~{lo_pos}" if lo_pos == hi_pos else f"{handle}~{lo_pos}..{hi_pos}"
        )
        rows.append({"handle": row_handle, "keywords": ", ".join(label_terms)})
        row_keyword_sets.append(label_terms)
        # Hint a drill exactly when re-clustering this bucket would yield
        # sub-groups (same predicate the drill-down path uses), so the
        # agent surface only points at handles that actually go deeper.
        if lo_pos != hi_pos and _should_cluster(len(bucket), (lo_pos, hi_pos)):
            fat_clusters.append(row_handle)

    headline = _headline(handle=handle, n_chunks=n, n_clusters=len(rows), scope=scope)
    table = render_agent_table(rows, schema=["handle", "keywords"])

    parts: list[str] = [headline]
    if readiness_line:
        parts.append(readiness_line)
    # Sections mode means the keyword clusterer was not trusted — say
    # so, or the heading labels read as a new (and wrong) keyword shape
    # (gr520238). The pending count tells the caller the keyword labels
    # are coming rather than absent by design.
    if mode == "sections":
        pending = _pending_keyword_chunks(blocks)
        if pending:
            parts.append(
                f"_(labels: section headings — keywords pending for {pending} "
                "chunks; keyword labels appear as chunk_keywords drains)_"
            )
        else:
            parts.append("_(labels: section headings)_")
    parts.append("")
    # The Topics line is keyword-summary machinery — a section-label
    # column would only echo a heading that survived the merge.
    if mode == "keywords":
        topics = _topics_line(row_keyword_sets)
        if topics:
            parts.extend([f"Topics: {topics}", ""])
    parts.append(table)
    if fat_clusters:
        parts.extend(
            ["", "Next: drill a cluster for finer structure (recurses to chunks)"]
        )
        for row_handle in fat_clusters:
            parts.append(f"  get(kind='paper', id='{row_handle}', view='toc')")
    return "\n".join(parts)


def build_toc_segments(
    *,
    store: ChunkListingStore,
    ref_id: int,
    handle: str,
    scope: tuple[int, int] | None = None,
) -> list[dict[str, Any]]:
    """Structured TOC for clickable web nav — the data behind the prose.

    Same segmentation as :func:`render_from_store` (it reuses the very
    same helpers), but returns a list of segment dicts instead of a
    Markdown TOON table::

        [{"handle": "pa5~0..14", "lo": 0, "hi": 14,
          "keywords": ["x", "y", ...], "n": 15}, ...]

    ``handle`` is the record's universal handle (``pa<id>``); each
    segment's ``handle`` field prefixes it so the web row mirrors the
    copy-pasteable form the agent surface emits.

    A range that shouldn't split (:func:`_should_cluster`) yields one
    segment per chunk (``lo == hi``); a clusterable range — including any
    drill-down ``scope`` down to ``2 * _MIN_CLUSTER_SIZE`` chunks — yields
    multi-chunk sub-groups the reader can drill again. ``keywords`` is the
    per-segment label (top-K). The web layer adds the PDF page for each
    segment's ``lo`` chunk (a separate ``chunk_pages`` lookup) so a row
    click can jump the viewer. Returns ``[]`` for an empty range.
    """
    blocks = store.chunks.list_chunks_for_ref(ref_id, pos_range=scope)
    if not blocks:
        return []

    groups, _mode = _clusters(blocks, scope=scope)
    out: list[dict[str, Any]] = []
    for bucket, label_terms in groups:
        lo_pos = bucket[0].ord
        hi_pos = bucket[-1].ord
        seg_handle = (
            f"{handle}~{lo_pos}" if lo_pos == hi_pos else f"{handle}~{lo_pos}..{hi_pos}"
        )
        out.append(
            {
                "handle": seg_handle,
                "lo": lo_pos,
                "hi": hi_pos,
                "keywords": label_terms,
                "n": len(bucket),
            }
        )
    return out


def cluster_blocks(
    blocks: Sequence[Any],
    *,
    scope: tuple[int, int] | None = None,
) -> list[tuple[list[Any], list[str]]]:
    """Cluster a ref's ``blocks`` by adjacent keyword-Jaccard distance (F20),
    returning ``[(bucket_blocks, keyword_label)]`` in reading order.

    The same DP segmentation behind :func:`render_from_store` /
    ``view='toc'``, but handed back as **the Block objects themselves** (with
    their ``id``/``pos``/``text``) rather than a rendered table — so a caller
    that addresses chunks by their universal ``pc<id>`` handle (the fisheye
    eye render) can build handles off ``block.id`` instead of the legacy
    ``slug~pos`` form. A short or keyword-barren body that shouldn't cluster
    (:func:`_should_cluster`) yields one single-block cluster per block, so the
    contract is uniform regardless of size. ``keyword_label`` is the bucket's
    top-K label (possibly empty when no member carries keywords yet); under
    the keyword-coverage floor it is the dominant ``section_path`` heading
    instead — see :func:`_clusters`."""
    if not blocks:
        return []
    groups, _mode = _clusters(blocks, scope=scope)
    return groups


def _clusters(
    blocks: Sequence[Any], *, scope: tuple[int, int] | None = None
) -> tuple[list[tuple[list[Any], list[str]]], str]:
    """``([(bucket, label_terms)], mode)`` — one grouping pass shared by
    every entry point above, so the markdown table, the web nav segments
    and the fisheye map all render the same outline.

    Three modes:

    * ``"per-chunk"`` — a range that shouldn't cluster
      (:func:`_should_cluster`), or the fallback when neither signal
      yields structure: one singleton bucket per block, labelled by its
      own keywords.
    * ``"keywords"`` — the F20 adjacent-Jaccard DP, trusted once at
      least ``_KEYWORD_COVERAGE_MIN`` of the range carries keywords.
    * ``"sections"`` — below that floor (the ``chunk_keywords`` backlog
      drains, gr520232) the ``section_path`` heading outline takes over:
      consecutive runs grouped by major heading, labelled per
      :func:`_bucket_label`. Degenerate outlines (< 2 groups) fall back
      to per-chunk rather than render one unlabelled mega-row.
    """
    n = len(blocks)
    singles = [([b], _top_keywords([b], top_k=_LABEL_TOP_K)) for b in blocks]
    if not _should_cluster(n, scope):
        return singles, "per-chunk"
    if _keyword_coverage(blocks) < _KEYWORD_COVERAGE_MIN:
        groups = [(g, _bucket_label(g)) for g in _section_groups(blocks)]
        if len(groups) >= 2:
            return groups, "sections"
        return singles, "per-chunk"
    distances = _adjacent_jaccard_distances(blocks)
    if not distances:
        return singles, "per-chunk"
    raw_segments = segment_dp(distances, k=_target_k(n, scope))
    segments = _collapse_singletons(raw_segments, min_size=_MIN_CLUSTER_SIZE)
    out: list[tuple[list[Any], list[str]]] = []
    for seg in segments:
        bucket = list(blocks[seg.start : seg.end + 1])
        if bucket:
            out.append((bucket, _top_keywords(bucket, top_k=_LABEL_TOP_K)))
    if not out:
        return singles, "per-chunk"
    return out, "keywords"


def _keyword_coverage(blocks: Sequence[Any]) -> float:
    """Fraction of ``blocks`` carrying ``chunks.keywords`` — the signal
    the Jaccard clusterer needs. Below ``_KEYWORD_COVERAGE_MIN`` empty
    sets contribute distance 0 to either side, so ``segment_dp`` cuts
    arbitrarily and every label renders empty (gr520232)."""
    if not blocks:
        return 0.0
    keyed = sum(1 for b in blocks if getattr(b, "keywords", None))
    return keyed / len(blocks)


def _pending_keyword_chunks(blocks: Sequence[Any]) -> int:
    """In-scope blocks ``chunk_keywords`` still owes labels — the N in
    the sections-mode notice. A block counts when its ``keywords`` are
    unset and it clears the worker's own eligibility predicate
    (:func:`~precis.workers.chunk_keywords.is_keyword_eligible`):
    skip-kinds and stubs would *never* carry labels, so counting them
    would overstate the drain. ``getattr``-safe — a stub block without
    ``text``/``chunk_kind`` contributes 0.

    The worker is lazy-imported: this module sits under
    ``precis.utils`` and stays free of the worker import chain (the
    embedder client) at module load."""
    from precis.workers.chunk_keywords import is_keyword_eligible

    return sum(
        1 for b in blocks if not getattr(b, "keywords", None) and is_keyword_eligible(b)
    )


# ── helpers: section_path outline (the keyword-backlog fallback) ──────


def _section_path(block: Any) -> list[str]:
    """The chunk's ``meta['section_path']`` as a list of titles (the
    mapper stashes the TEXT[] column there); ``[]`` when absent.
    ``getattr``-safe: test stubs carry no ``meta``."""
    meta = getattr(block, "meta", None) or {}
    sp = meta.get("section_path")
    return [str(s) for s in sp] if isinstance(sp, (list, tuple)) else []


def _clean_section_title(raw: str) -> str:
    """A ``section_path`` element for grouping/display: ``**bold**`` /
    ``*italics*`` wrappers off, trimmed, capped at ``_SECTION_TITLE_CAP``."""
    t = strip_emphasis(str(raw)).strip()
    return t[:_SECTION_TITLE_CAP].rstrip() if len(t) > _SECTION_TITLE_CAP else t


def _section_key(block: Any) -> str | None:
    """The grouping key of a chunk's leaf section title: its top-level
    number for a numbered title (``2.1. Sensors`` and ``2.3. Other``
    both key section 2), the lowercased title otherwise; ``None`` when
    the chunk carries no usable path."""
    path = _section_path(block)
    if not path:
        return None
    title = _clean_section_title(path[-1])
    if not title:
        return None
    m = _SECTION_NUM_RE.match(title)
    if m:
        return m.group(1)
    return title.lower()


def _section_groups(blocks: Sequence[Any]) -> list[list[Any]]:
    """Consecutive runs grouped by :func:`_section_key`.

    A pathless chunk joins the open group (a leading pathless run opens
    its own, unlabelled, group). Past ``_BUCKET_MAX_COUNT`` groups the
    smallest merges into its smaller neighbour — a long paper with many
    tiny sections still fits the table."""
    groups: list[list[Any]] = []
    keys: list[str | None] = []
    for b in blocks:
        key = _section_key(b)
        if groups and (key is None or key == keys[-1]):
            groups[-1].append(b)
        else:
            groups.append([b])
            keys.append(key)
    while len(groups) > _BUCKET_MAX_COUNT:
        i = min(range(len(groups)), key=lambda j: len(groups[j]))
        if i == 0:
            j = 1
        elif i == len(groups) - 1:
            j = i - 1
        else:
            j = i - 1 if len(groups[i - 1]) <= len(groups[i + 1]) else i + 1
        groups[j].extend(groups[i])
        del groups[i]
    return groups


def _bucket_label(bucket: Sequence[Any]) -> list[str]:
    """A section-mode bucket's label: real keywords when present, else
    the dominant section title, else ``[]``."""
    kws = _top_keywords(bucket, top_k=_LABEL_TOP_K)
    if kws:
        return kws
    title = _dominant_section_title(bucket)
    return [title] if title else []


def _dominant_section_title(bucket: Sequence[Any]) -> str:
    """The title a merged group is named after: the *shallowest*
    numbered title (``2. Methods`` names a group spanning 2.1–2.4 better
    than the most frequent subsection), ties broken by frequency then
    reading order."""
    counter: Counter[str] = Counter()
    first_seen: dict[str, int] = {}
    for idx, b in enumerate(bucket):
        path = _section_path(b)
        if not path:
            continue
        title = _clean_section_title(path[-1])
        if not title:
            continue
        counter[title] += 1
        first_seen.setdefault(title, idx)
    if not counter:
        return ""

    def _rank(title: str) -> tuple[int, int, int]:
        m = _SECTION_DEPTH_RE.match(title)
        depth = m.group(1).count(".") if m else 99
        return (depth, -counter[title], first_seen[title])

    return min(counter, key=_rank)


# ── helpers: cluster shape ───────────────────────────────────────────


def _bucket_count(n_chunks: int) -> int:
    """Log-scaled cluster count: ~15 by N≈150, capped at 15.

    Floor at 3 keeps small-but-bucketable ranges interesting;
    ceiling at 15 keeps tables skimmable.
    """
    if n_chunks <= 1:
        return 1
    target = math.ceil(_BUCKET_MULTIPLIER * math.log10(max(2, n_chunks)))
    return max(_BUCKET_MIN_COUNT, min(_BUCKET_MAX_COUNT, target))


def _should_cluster(n: int, scope: tuple[int, int] | None) -> bool:
    """Whether a range of ``n`` chunks re-clusters into sub-groups.

    The bar depends on context: the *top-level* TOC only clusters a
    substantial body (``>= _BUCKETING_THRESHOLD``) — a short paper is
    scannable per-chunk with nothing to drill. A *drill-down* (``scope``
    set) sub-clusters whenever the range is bigger than a comfortably
    scannable leaf (``> 2 * _DRILL_GROUP_SIZE``), so double-clicking a
    group walks a hierarchy instead of flattening to singletons (the
    23-chunk sub-range that motivated this; papers have no heading tree,
    so hierarchy is recursive keyword clustering). Below
    that a drilled range is its own leaf — show its chunks directly.
    """
    if scope is None:
        return n >= _BUCKETING_THRESHOLD
    return n > 2 * _DRILL_GROUP_SIZE


def _target_k(n: int, scope: tuple[int, int] | None) -> int:
    """Requested cluster count for a range, context-aware.

    Top-level uses the log-scaled :func:`_bucket_count` (~15 rows for a
    big paper). A drill-down targets a shallow branching factor
    (``~_DRILL_GROUP_SIZE`` chunks per sub-group), clamped to at least two
    groups (so the split is real) and the same skimmable ceiling.
    """
    if scope is None:
        return _bucket_count(n)
    return max(2, min(_BUCKET_MAX_COUNT, round(n / _DRILL_GROUP_SIZE)))


def _adjacent_jaccard_distances(blocks: Sequence[Any]) -> list[float]:
    """Jaccard distance between adjacent chunks' keyword sets.

    Empty-keyword chunks (too short for KeyBERT, or non-content kind)
    contribute distance 0 to either side — the F20 "fold into the
    neighbour" rule. This keeps short interstitial chunks from
    spuriously cutting a cluster.
    """
    out: list[float] = []
    for i in range(len(blocks) - 1):
        a = blocks[i].keywords or []
        b = blocks[i + 1].keywords or []
        if not a or not b:
            out.append(0.0)
            continue
        sa = set(a)
        sb = set(b)
        union = sa | sb
        if not union:
            out.append(0.0)
            continue
        inter = sa & sb
        out.append(1.0 - (len(inter) / len(union)))
    return out


def _collapse_singletons(segments: list[Segment], *, min_size: int) -> list[Segment]:
    """Merge segments with fewer than ``min_size`` chunks into a neighbour.

    Greedy forward merge: a too-small segment is absorbed by the
    previous one. The last segment, if too small at the end, absorbs
    backwards into its predecessor.
    """
    if not segments or min_size <= 1:
        return segments
    out: list[Segment] = []
    for seg in segments:
        size = seg.end - seg.start + 1
        if size < min_size and out:
            out[-1] = Segment(out[-1].start, seg.end)
        else:
            out.append(seg)
    if len(out) >= 2:
        last = out[-1]
        if (last.end - last.start + 1) < min_size:
            prev = out[-2]
            out[-2] = Segment(prev.start, last.end)
            out.pop()
    return out


def _top_keywords(bucket: Sequence[Any], *, top_k: int) -> list[str]:
    """Top-K most-frequent keywords across the bucket's chunks.

    Frequency-ranked union. Ties broken by first-occurrence order.
    Empty-keyword chunks contribute nothing — the label comes from
    whichever members have keywords.
    """
    counter: Counter[str] = Counter()
    first_seen: dict[str, int] = {}
    for idx, block in enumerate(bucket):
        for kw in block.keywords or []:
            counter[kw] += 1
            first_seen.setdefault(kw, idx)
    if not counter:
        return []
    ordered = sorted(
        counter.items(),
        key=lambda kv: (-kv[1], first_seen[kv[0]]),
    )
    return [kw for kw, _ in ordered[:top_k]]


def _topics_line(row_keyword_sets: Sequence[Sequence[str]]) -> str:
    """Keywords present in ≥``_TOPICS_RATIO`` of clusters' row labels.

    Operates on the truncated per-cluster labels (what the user
    actually sees), so promotion mirrors the visible content. A
    keyword is counted at most once per cluster.

    Empty when no keyword crosses the threshold (e.g. an incoherent
    range across unrelated subjects).
    """
    k = len(row_keyword_sets)
    if k < 2:
        return ""
    threshold = math.ceil(k * _TOPICS_RATIO)
    counter: Counter[str] = Counter()
    first_seen: dict[str, int] = {}
    for idx, kws in enumerate(row_keyword_sets):
        for kw in set(kws):
            counter[kw] += 1
            first_seen.setdefault(kw, idx)
    shared = [(kw, c) for kw, c in counter.items() if c >= threshold]
    if not shared:
        return ""
    shared.sort(key=lambda kv: (-kv[1], first_seen[kv[0]]))
    return ", ".join(kw for kw, _ in shared[:_TOPICS_TOP_K])


# ── helpers: rendering ──────────────────────────────────────────────


def _headline(
    *,
    handle: str,
    n_chunks: int,
    n_clusters: int,
    scope: tuple[int, int] | None,
) -> str:
    if scope is not None:
        return (
            f"# {handle} sub-TOC ~{scope[0]}..{scope[1]} — "
            f"{n_chunks} chunks, {n_clusters} clusters"
        )
    return f"# {handle} TOC — {n_chunks} chunks, {n_clusters} clusters"


def _render_per_chunk(
    *,
    handle: str,
    blocks: Sequence[Any],
    scope: tuple[int, int] | None,
    readiness_line: str | None = None,
) -> str:
    """Short-range path: one row per chunk, per-chunk keywords as label.

    Same ``(handle, keywords)`` schema as the bucketed path — agents
    get a uniform contract regardless of range size. For the actual
    chunk text, use ``get(...)`` without ``view='toc'``.
    """
    rows: list[dict[str, str]] = []
    for block in blocks:
        rows.append(
            {
                "handle": f"{handle}~{block.ord}",
                "keywords": ", ".join(_top_keywords([block], top_k=_LABEL_TOP_K)),
            }
        )
    n_total = len(blocks)
    if scope is not None:
        head = f"# {handle} sub-TOC ~{scope[0]}..{scope[1]} — {n_total} chunks"
    else:
        head = f"# {handle} TOC — {n_total} chunks"
    if readiness_line:
        head = f"{head}\n{readiness_line}"
    table = render_agent_table(rows, schema=["handle", "keywords"])
    return f"{head}\n\n{table}"


def _empty_body(*, handle: str, kind: str, scope: tuple[int, int] | None) -> str:
    """No chunks in scope. Friendly placeholder + recovery hint."""
    if scope is not None:
        return (
            f"# {handle} — no chunks in scope ~{scope[0]}..{scope[1]}\n\n"
            f"Try widening the range or omit scope= for the full TOC."
        )
    return (
        f"# {handle} — no chunks yet\n\n"
        f"The chunker hasn't produced any body chunks for this {kind}."
    )


__all__ = ["build_toc_segments", "cluster_blocks", "render_from_store"]
