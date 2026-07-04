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

The ``handle`` is the record's ADR-0036 universal handle (``pa<id>``),
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
clustering *is* the hierarchy (ADR-0018/F20). Only a range too small
to split further shows one row per chunk. For the actual chunk text,
call ``get(...)`` without ``view='toc'``.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from typing import Any

from precis.format import render_agent_table
from precis.utils.segmentation import Segment, segment_dp

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


def render_from_store(
    *,
    store: Any,
    ref_id: int,
    handle: str,
    kind: str,
    scope: tuple[int, int] | None = None,
) -> str:
    """Render the TOC body for ``ref_id``, optionally scoped to a range.

    ``handle`` is the record's universal handle (``pa<id>``, ADR 0036);
    every rendered row + drill-in hint prefixes it so the output is a
    copy-pasteable ``get`` id. ``scope`` restricts to body chunks inside
    the inclusive ``(lo, hi)`` position range. Without it, the full body
    is clustered.

    Returns Markdown. First line is the kind-aware headline; the rest
    is the TOON table, optionally preceded by a ``Topics:`` line and
    followed by a ``Next:`` drill-in block.
    """
    pos_range = scope
    blocks = store.list_blocks_for_ref(ref_id, pos_range=pos_range)
    if not blocks:
        return _empty_body(handle=handle, kind=kind, scope=scope)

    n = len(blocks)
    if not _should_cluster(n, scope):
        return _render_per_chunk(handle=handle, blocks=blocks, scope=scope)

    target_k = _target_k(n, scope)
    distances = _adjacent_jaccard_distances(blocks)
    if not distances:
        return _render_per_chunk(handle=handle, blocks=blocks, scope=scope)

    raw_segments = segment_dp(distances, k=target_k)
    segments = _collapse_singletons(raw_segments, min_size=_MIN_CLUSTER_SIZE)

    rows: list[dict[str, str]] = []
    row_keyword_sets: list[list[str]] = []
    fat_clusters: list[str] = []
    for seg in segments:
        bucket = blocks[seg.start : seg.end + 1]
        if not bucket:
            continue
        lo_pos = bucket[0].pos
        hi_pos = bucket[-1].pos
        row_handle = (
            f"{handle}~{lo_pos}" if lo_pos == hi_pos else f"{handle}~{lo_pos}..{hi_pos}"
        )
        label_kws = _top_keywords(bucket, top_k=_LABEL_TOP_K)
        rows.append({"handle": row_handle, "keywords": ", ".join(label_kws)})
        row_keyword_sets.append(label_kws)
        # Hint a drill exactly when re-clustering this bucket would yield
        # sub-groups (same predicate the drill-down path uses), so the
        # agent surface only points at handles that actually go deeper.
        if lo_pos != hi_pos and _should_cluster(len(bucket), (lo_pos, hi_pos)):
            fat_clusters.append(row_handle)

    headline = _headline(handle=handle, n_chunks=n, n_clusters=len(rows), scope=scope)
    table = render_agent_table(rows, schema=["handle", "keywords"])

    parts: list[str] = [headline, ""]
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
    store: Any,
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
    blocks = store.list_blocks_for_ref(ref_id, pos_range=scope)
    if not blocks:
        return []

    n = len(blocks)
    if not _should_cluster(n, scope):
        return [
            {
                "handle": f"{handle}~{b.pos}",
                "lo": b.pos,
                "hi": b.pos,
                "keywords": _top_keywords([b], top_k=_LABEL_TOP_K),
                "n": 1,
            }
            for b in blocks
        ]

    distances = _adjacent_jaccard_distances(blocks)
    if not distances:
        return [
            {
                "handle": f"{handle}~{b.pos}",
                "lo": b.pos,
                "hi": b.pos,
                "keywords": _top_keywords([b], top_k=_LABEL_TOP_K),
                "n": 1,
            }
            for b in blocks
        ]

    raw_segments = segment_dp(distances, k=_target_k(n, scope))
    segments = _collapse_singletons(raw_segments, min_size=_MIN_CLUSTER_SIZE)

    out: list[dict[str, Any]] = []
    for seg in segments:
        bucket = blocks[seg.start : seg.end + 1]
        if not bucket:
            continue
        lo_pos = bucket[0].pos
        hi_pos = bucket[-1].pos
        seg_handle = (
            f"{handle}~{lo_pos}" if lo_pos == hi_pos else f"{handle}~{lo_pos}..{hi_pos}"
        )
        out.append(
            {
                "handle": seg_handle,
                "lo": lo_pos,
                "hi": hi_pos,
                "keywords": _top_keywords(bucket, top_k=_LABEL_TOP_K),
                "n": len(bucket),
            }
        )
    return out


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
    so hierarchy is recursive keyword clustering, ADR-0018/F20). Below
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
                "handle": f"{handle}~{block.pos}",
                "keywords": ", ".join(_top_keywords([block], top_k=_LABEL_TOP_K)),
            }
        )
    n_total = len(blocks)
    if scope is not None:
        head = f"# {handle} sub-TOC ~{scope[0]}..{scope[1]} — {n_total} chunks"
    else:
        head = f"# {handle} TOC — {n_total} chunks"
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


__all__ = ["build_toc_segments", "render_from_store"]
