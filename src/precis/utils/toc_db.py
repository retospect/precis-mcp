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

Output shape — TOON table::

    # slug TOC — N chunks, K clusters

    {handle	keywords}
    slug~0..14	keyword phrases for cluster 0
    slug~15..29	keyword phrases for cluster 1
    …

When the requested range is small enough to read directly,
clustering is skipped and the chunks render as a per-chunk preview.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from typing import Any

from precis.format import render_agent_table
from precis.utils.segmentation import Segment, segment_dp

#: Below this chunk count, render per-chunk preview rather than
#: clustering. Below ~30 chunks the agent can scan them directly.
_BUCKETING_THRESHOLD = 30

#: Hard cap on the cluster count. Keeps the rendered table skimmable
#: even on large ranges.
_BUCKET_MAX_COUNT = 15

#: Floor on cluster count once bucketing is active. Three rows is
#: the minimum that gives a useful sense of the range's shape.
_BUCKET_MIN_COUNT = 3

#: RAKE-style top-K keywords per cluster label.
_LABEL_TOP_K = 5

#: First-N chars of chunk text shown in the per-chunk preview
#: fallback for short ranges.
_FALLBACK_PREVIEW_CHARS = 60

#: Row cap for the per-chunk preview fallback.
_FALLBACK_ROW_CAP = 30

#: Minimum cluster size in the DP output. Smaller clusters get
#: absorbed into a neighbour by :func:`_collapse_singletons`.
_MIN_CLUSTER_SIZE = 3


def render_from_store(
    *,
    store: Any,
    ref_id: int,
    slug: str,
    kind: str,
    scope: tuple[int, int] | None = None,
) -> str:
    """Render the TOC body for ``ref_id``, optionally scoped to a range.

    ``scope`` restricts to body chunks inside the inclusive ``(lo, hi)``
    position range. Without it, the full body is clustered.

    Returns Markdown. First line is the kind-aware headline; the rest
    is the TOON table.
    """
    pos_range = scope
    blocks = store.list_blocks_for_ref(ref_id, pos_range=pos_range)
    if not blocks:
        return _empty_body(slug=slug, kind=kind, scope=scope)

    n = len(blocks)
    if n < _BUCKETING_THRESHOLD:
        return _render_chunk_preview(slug=slug, blocks=blocks, scope=scope)

    target_k = _bucket_count(n)
    distances = _adjacent_jaccard_distances(blocks)
    if not distances:
        return _render_chunk_preview(slug=slug, blocks=blocks, scope=scope)

    raw_segments = segment_dp(distances, k=target_k)
    segments = _collapse_singletons(raw_segments, min_size=_MIN_CLUSTER_SIZE)

    rows: list[dict[str, str]] = []
    for seg in segments:
        bucket = blocks[seg.start : seg.end + 1]
        if not bucket:
            continue
        lo_pos = bucket[0].pos
        hi_pos = bucket[-1].pos
        handle = (
            f"{slug}~{lo_pos}" if lo_pos == hi_pos else f"{slug}~{lo_pos}..{hi_pos}"
        )
        rows.append({"handle": handle, "keywords": _label_cluster(bucket)})

    headline = _headline(slug=slug, n_chunks=n, n_clusters=len(rows), scope=scope)
    table = render_agent_table(rows, schema=["handle", "keywords"])
    return f"{headline}\n\n{table}"


# ── helpers: cluster shape ───────────────────────────────────────────


def _bucket_count(n_chunks: int) -> int:
    """Log-scaled cluster count: ~10 at N=170, capped at 15 around N=1000+.

    Matches the F16 v2 formula. Floor at 3 keeps small-but-bucketable
    ranges interesting; ceiling at 15 keeps tables skimmable.
    """
    if n_chunks <= 1:
        return 1
    target = math.ceil(5 * math.log10(max(2, n_chunks)))
    return max(_BUCKET_MIN_COUNT, min(_BUCKET_MAX_COUNT, target))


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


def _label_cluster(bucket: Sequence[Any]) -> str:
    """Top-K most-frequent keywords across the cluster's chunks.

    Frequency-ranked union. Ties broken by first-occurrence order.
    Empty-keyword chunks contribute nothing — the cluster's label
    comes from whichever members have keywords.
    """
    counter: Counter[str] = Counter()
    first_seen: dict[str, int] = {}
    for idx, block in enumerate(bucket):
        for kw in block.keywords or []:
            counter[kw] += 1
            first_seen.setdefault(kw, idx)
    if not counter:
        return ""
    # Sort by (-count, first_seen) for stable, frequency-first order.
    ordered = sorted(
        counter.items(),
        key=lambda kv: (-kv[1], first_seen[kv[0]]),
    )
    return ", ".join(kw for kw, _ in ordered[:_LABEL_TOP_K])


# ── helpers: rendering ──────────────────────────────────────────────


def _headline(
    *,
    slug: str,
    n_chunks: int,
    n_clusters: int,
    scope: tuple[int, int] | None,
) -> str:
    if scope is not None:
        return (
            f"# {slug} sub-TOC ~{scope[0]}..{scope[1]} — "
            f"{n_chunks} chunks, {n_clusters} clusters"
        )
    return f"# {slug} TOC — {n_chunks} chunks, {n_clusters} clusters"


def _render_chunk_preview(
    *,
    slug: str,
    blocks: Sequence[Any],
    scope: tuple[int, int] | None,
) -> str:
    """Short-range fallback: one row per chunk with text preview."""
    n_total = len(blocks)
    truncated = blocks[:_FALLBACK_ROW_CAP]
    rows: list[dict[str, str]] = []
    for block in truncated:
        text = (block.text or "").strip().replace("\n", " ")
        if len(text) > _FALLBACK_PREVIEW_CHARS:
            preview = text[:_FALLBACK_PREVIEW_CHARS].rstrip() + "…"
        else:
            preview = text
        rows.append(
            {
                "handle": f"{slug}~{block.pos}",
                "preview": preview,
            }
        )
    if scope is not None:
        head = (
            f"# {slug} sub-TOC ~{scope[0]}..{scope[1]} — "
            f"{n_total} chunks (per-chunk preview)"
        )
    else:
        head = f"# {slug} TOC — {n_total} chunks (per-chunk preview)"
    table = render_agent_table(rows, schema=["handle", "preview"])
    body = f"{head}\n\n{table}"
    if n_total > _FALLBACK_ROW_CAP:
        lo = blocks[_FALLBACK_ROW_CAP].pos
        hi = blocks[-1].pos
        body += (
            f"\n\n…{n_total - _FALLBACK_ROW_CAP} more chunks. "
            f"Continue: get(kind='paper', id='{slug}~{lo}..{hi}')"
        )
    return body


def _empty_body(*, slug: str, kind: str, scope: tuple[int, int] | None) -> str:
    """No chunks in scope. Friendly placeholder + recovery hint."""
    if scope is not None:
        return (
            f"# {slug} — no chunks in scope ~{scope[0]}..{scope[1]}\n\n"
            f"Try widening the range or omit scope= for the full TOC."
        )
    return (
        f"# {slug} — no chunks yet\n\n"
        f"The chunker hasn't produced any body chunks for this {kind}."
    )


__all__ = ["render_from_store"]
