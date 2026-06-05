"""TextTiling-style sequential segmentation on embedding sequences.

Given an ordered list of N chunk embeddings, return ``[(start, end),
...]`` segment boundaries (inclusive index ranges). The algorithm:

1. Compute **gap[i]** = ``1 - cos(e[i], e[i+1])`` for each adjacent
   pair — high gap = topic shift.
2. Smooth gaps with a 3-wide sliding window so single-chunk
   semantic noise doesn't inflate spurious shifts in dense text.
3. For each candidate boundary i, compute its **depth score** —
   how much of a valley it is relative to the maxima on either
   side. Depth = ``(max(gap[<i]) - gap[i]) + (max(gap[>i]) - gap[i])``.
4. Filter candidates by ``depth > mean(depth) + 0.5·std(depth)``
   (TextTiling noise floor).
5. **Knee-point on survivors**: sort surviving depths descending,
   find the largest consecutive gap, cut there → that's K-1
   boundaries.
6. Clamp K to ``[K_MIN, K_MAX]`` (=[3, 9]). When fewer than
   ``K_MIN`` candidates survive, take the top ``K_MIN`` by raw
   depth regardless of the noise filter.
7. Special-case N < K_MIN: no segmentation, return one segment
   per chunk (caller renders as flat list).

Deterministic — same input gives same output. Pure stdlib + math.
Versioned via :data:`SEGMENTATION_VERSION` so caches and persisted
outputs invalidate cleanly when the algorithm evolves.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

#: Bump when the algorithm changes shape. Used today as a cache key
#: for the in-process skill-TOC clustering in :mod:`precis.utils.toc`;
#: F20 retired the persistent ``ref_segments`` rows that used to
#: include this version, but the in-memory cache still keys on it so
#: an algorithm tweak invalidates the cache on process restart.
#:
#: 1.1 (2026-06-04, F18) — raised K_MAX from 9 to 18, dropped target
#: chunks-per-segment from 20 to 12, added singleton merge post-DP.
SEGMENTATION_VERSION: Final[str] = "1.1"

K_MIN: Final[int] = 3
K_MAX: Final[int] = 18
DEPTH_THRESHOLD_MULTIPLIER: Final[float] = 0.5
SMOOTHING_WINDOW: Final[int] = 3  # 3-wide centred moving average


@dataclass(frozen=True)
class Segment:
    """One contiguous segment of chunks.

    ``start`` and ``end`` are inclusive indices into the original
    chunk sequence. ``length`` is ``end - start + 1``.
    """

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1


def segment_embeddings(
    embeddings: Sequence[Sequence[float]],
    *,
    k_min: int = K_MIN,
    k_max: int = K_MAX,
) -> list[Segment]:
    """Segment an ordered embedding sequence into contiguous clusters.

    Returns a list of :class:`Segment` covering ``[0, N-1]`` exactly
    (no gaps, no overlaps). Always returns at least one segment for
    any non-empty input; never returns more than ``k_max`` segments.

    For very short inputs (``N < k_min``) returns one segment per
    chunk so the caller can render a flat list rather than
    pseudo-clusters that would each contain only one chunk anyway.
    """
    n = len(embeddings)
    if n == 0:
        return []
    if n == 1:
        return [Segment(0, 0)]
    if n < k_min:
        # Too few chunks to segment meaningfully — one per row.
        return [Segment(i, i) for i in range(n)]

    # Step 1: adjacent gaps. ``1 - cos(a, b)`` for L2-normalised
    # bge-m3 vectors. We compute cosine even if vectors aren't
    # normalised so the algorithm doesn't silently misbehave on a
    # different embedder; the cost is negligible.
    gaps: list[float] = [
        1.0 - _cosine(embeddings[i], embeddings[i + 1]) for i in range(n - 1)
    ]

    # Step 2: smooth — centred moving average over adjacent gaps to
    # damp single-chunk semantic noise in dense text. Skip smoothing
    # when there are few enough gaps that adjacent topic shifts would
    # blur into one indistinct peak (the failure mode on short
    # fixtures: 3 sharp topic shifts across 6 chunks get averaged
    # into a single shoulder). Threshold of 8 gaps = 9 chunks chosen
    # so any paper short enough to have densely-packed topics gets
    # raw peak detection.
    smoothed = _smooth(gaps, window=SMOOTHING_WINDOW) if len(gaps) > 8 else list(gaps)

    # Step 3: depth scores at every candidate boundary.
    depths = _depth_scores(smoothed)

    # Step 4-6: select K-1 boundaries.
    boundary_count = _choose_k_minus_1(depths, k_min=k_min, k_max=k_max)
    if boundary_count == 0:
        # Genuinely uniform sequence — return one segment.
        return [Segment(0, n - 1)]

    # Pick the top ``boundary_count`` boundaries by depth, then sort
    # by position so the resulting segments are contiguous.
    ranked = sorted(range(len(depths)), key=lambda i: depths[i], reverse=True)
    chosen = sorted(ranked[:boundary_count])

    # Convert boundary indices (gap-between-i-and-i+1) into segment
    # ranges. ``chosen[k]`` means "split between chunk chosen[k] and
    # chosen[k]+1".
    out: list[Segment] = []
    prev = 0
    for boundary in chosen:
        out.append(Segment(prev, boundary))
        prev = boundary + 1
    out.append(Segment(prev, n - 1))
    return out


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity in ``[-1, 1]``. Returns 0.0 for zero vectors."""
    dot = 0.0
    a_norm_sq = 0.0
    b_norm_sq = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        a_norm_sq += x * x
        b_norm_sq += y * y
    if a_norm_sq == 0.0 or b_norm_sq == 0.0:
        return 0.0
    return dot / math.sqrt(a_norm_sq * b_norm_sq)


def _smooth(values: list[float], *, window: int) -> list[float]:
    """Centred moving average. Edges keep their original value when
    the window would extend past the sequence — simpler than padding
    with means and the edge cases there always look like topic shifts
    anyway (intro / conclusion boundaries)."""
    if window < 2 or len(values) <= window:
        return list(values)
    half = window // 2
    out = list(values)
    for i in range(half, len(values) - half):
        out[i] = sum(values[i - half : i + half + 1]) / window
    return out


def _depth_scores(gaps: list[float]) -> list[float]:
    """For each gap position ``i``, compute its peak prominence.

    ``gaps[i]`` is ``1 - cos(e[i], e[i+1])`` — high values are topic
    shifts (low cosine similarity). We want to cut at the peaks of
    this signal, so depth here is *peak prominence*: how much
    higher this gap is than the lowest values on either side.

    Boundaries at the very ends have only one side; we use the
    available side's contribution doubled so they're directly
    comparable to interior depths.
    """
    if not gaps:
        return []
    n = len(gaps)
    depths: list[float] = [0.0] * n
    for i in range(n):
        left_min = min(gaps[:i]) if i > 0 else None
        right_min = min(gaps[i + 1 :]) if i < n - 1 else None
        if left_min is None and right_min is None:
            depths[i] = 0.0
        elif left_min is None:
            depths[i] = 2.0 * max(0.0, gaps[i] - right_min)  # type: ignore[operator]
        elif right_min is None:
            depths[i] = 2.0 * max(0.0, gaps[i] - left_min)
        else:
            depths[i] = max(0.0, gaps[i] - left_min) + max(0.0, gaps[i] - right_min)
    return depths


def _choose_k_minus_1(depths: list[float], *, k_min: int, k_max: int) -> int:
    """Pick the number of boundaries given the depth profile.

    Strategy:

    1. Compute the noise floor: ``mean(depths) + 0.5·std(depths)``.
    2. Count boundaries with depth above the floor.
    3. If fewer than ``k_min - 1`` survive, take the top
       ``k_min - 1`` by depth instead — every paper gets at least
       ``k_min`` segments unless N is genuinely tiny.
    4. Knee-point on the surviving sorted depths: find the largest
       consecutive gap; cut there.
    5. Clamp the survivor count to ``[k_min - 1, k_max - 1]``.
    """
    n = len(depths)
    if n == 0:
        return 0
    target_min = max(0, k_min - 1)
    target_max = max(target_min, k_max - 1)

    # Step 1 + 2: noise-floor filter.
    mean = sum(depths) / n
    var = sum((d - mean) ** 2 for d in depths) / n
    std = math.sqrt(var)
    floor = mean + DEPTH_THRESHOLD_MULTIPLIER * std
    survivors = [d for d in depths if d > floor]

    if len(survivors) < target_min:
        # Step 3: not enough survivors — take top-K by raw depth.
        # Sort all depths descending; pick the top target_min that
        # are strictly positive (zero-depth boundaries on a uniform
        # sequence don't deserve to be cuts).
        sorted_all = sorted(depths, reverse=True)
        positive = [d for d in sorted_all if d > 0.0]
        return min(target_min, len(positive))

    # Step 4: knee-point. Sort survivors descending and find the
    # largest consecutive gap.
    sorted_survivors = sorted(survivors, reverse=True)
    if len(sorted_survivors) <= target_min:
        return len(sorted_survivors)

    largest_gap = 0.0
    knee_idx = len(sorted_survivors)
    for i in range(len(sorted_survivors) - 1):
        gap = sorted_survivors[i] - sorted_survivors[i + 1]
        if gap > largest_gap:
            largest_gap = gap
            knee_idx = i + 1  # cut after position i (keep i+1 boundaries)

    # Step 5: clamp.
    return max(target_min, min(target_max, knee_idx))


# ─── DP-uniform-cost segmenter ──────────────────────────────────────


def segment_dp(
    distances: Sequence[float],
    *,
    k: int,
) -> list[Segment]:
    """Optimal K-segmentation minimising sum of intra-segment dispersion.

    Standard DP for sequential clustering. Given adjacent-pair
    distances (``distances[i]`` = 1 - cos(e[i], e[i+1]) — same input
    the TextTiling-style :func:`segment_embeddings` consumes), find
    the segmentation into exactly ``k`` contiguous segments that
    minimises the total within-segment dispersion.

    Dispersion of a segment ``[a..b]`` is the sum of adjacent-pair
    distances *inside* it (``distances[a..b-1]``). Single-chunk
    segments have dispersion 0, so the algorithm naturally avoids
    creating them unless K forces it.

    Complexity: O(N²K). For N≤500 and K≤9 this is microseconds —
    cheap enough to run on every TOC view, easily cacheable.

    Returns segments covering ``[0, N]`` (N = len(distances) + 1)
    exactly, no gaps, no overlaps. Deterministic.

    Raises ``ValueError`` for k < 1 or k > N.
    """
    n_chunks = len(distances) + 1
    if k < 1:
        raise ValueError(f"k must be ≥ 1, got {k}")
    if k > n_chunks:
        raise ValueError(f"k={k} exceeds chunk count {n_chunks}")
    if n_chunks == 0:
        return []
    if k == 1:
        return [Segment(0, n_chunks - 1)]
    if k == n_chunks:
        # Each chunk its own segment.
        return [Segment(i, i) for i in range(n_chunks)]

    # Prefix sum so cost(a, b) — sum of distances[a:b] — is O(1).
    prefix: list[float] = [0.0]
    for d in distances:
        prefix.append(prefix[-1] + d)

    def cost(a: int, b: int) -> float:
        """Intra-segment dispersion for chunks [a..b] (inclusive).

        Equals the sum of adjacent-pair distances inside the
        segment: ``distances[a..b-1]``. A 1-chunk segment costs 0.
        """
        if a >= b:
            return 0.0
        return prefix[b] - prefix[a]

    # DP table. D[seg][last_chunk_idx] = minimum total cost to
    # cover chunks [0..last_chunk_idx] with exactly ``seg`` segments.
    INF = float("inf")
    D = [[INF] * n_chunks for _ in range(k + 1)]
    # back[seg][last] = the chunk index where the LAST segment starts.
    back: list[list[int]] = [[0] * n_chunks for _ in range(k + 1)]

    # Base case: 1 segment covering [0..last] has cost cost(0, last).
    for last in range(n_chunks):
        D[1][last] = cost(0, last)
        back[1][last] = 0

    # Fill k = 2..K.
    for seg in range(2, k + 1):
        for last in range(seg - 1, n_chunks):
            # Try every start position for this segment: [start..last].
            best = INF
            best_start = seg - 1
            for start in range(seg - 1, last + 1):
                prev_cost = D[seg - 1][start - 1]
                if prev_cost == INF:
                    continue
                total = prev_cost + cost(start, last)
                if total < best:
                    best = total
                    best_start = start
            D[seg][last] = best
            back[seg][last] = best_start

    # Reconstruct boundaries by tracing back from D[k][n_chunks-1].
    boundaries: list[tuple[int, int]] = []
    last = n_chunks - 1
    for seg in range(k, 0, -1):
        start = back[seg][last]
        boundaries.append((start, last))
        last = start - 1
    boundaries.reverse()
    return [Segment(s, e) for s, e in boundaries]


__all__ = [
    "K_MAX",
    "K_MIN",
    "SEGMENTATION_VERSION",
    "Segment",
    "segment_dp",
    "segment_embeddings",
]
