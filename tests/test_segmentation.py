"""TextTiling segmentation contract tests.

Pin the algorithm's shape so changes to depth thresholding, knee
selection, K bounds, or smoothing fail loudly in CI before
silently re-segmenting every paper in the corpus.

Inputs are synthetic embedding sequences with known topic-shift
structure so we can assert specific boundary positions. Real
bge-m3 embeddings are not used here — that's an integration
concern.
"""

from __future__ import annotations

import math

import pytest

from precis.utils.segmentation import (
    K_MAX,
    K_MIN,
    SEGMENTATION_VERSION,
    Segment,
    segment_dp,
    segment_embeddings,
)


def _unit(*xs: float) -> list[float]:
    """L2-normalise a small vector for cosine testing."""
    norm = math.sqrt(sum(x * x for x in xs))
    return [x / norm for x in xs]


# ── trivial cases ────────────────────────────────────────────────────


class TestTrivial:
    def test_empty_input_yields_empty_list(self) -> None:
        assert segment_embeddings([]) == []

    def test_single_chunk_yields_one_segment(self) -> None:
        segs = segment_embeddings([_unit(1, 0, 0)])
        assert segs == [Segment(0, 0)]

    def test_below_k_min_yields_one_segment_per_chunk(self) -> None:
        # 2 chunks < K_MIN (=3): caller would render as a flat list.
        e = [_unit(1, 0, 0), _unit(0, 1, 0)]
        assert segment_embeddings(e) == [Segment(0, 0), Segment(1, 1)]


# ── known-topic-shift fixtures ──────────────────────────────────────


class TestKnownShifts:
    def test_two_clear_topics_split_at_boundary(self) -> None:
        """5 chunks: 0..2 are topic A (all aligned with [1,0,0]),
        3..4 are topic B (aligned with [0,1,0]). The shift is at
        gap[2]. With K_MIN=3 the algorithm should still return at
        least 3 segments — but the cleanest 2-segment split is at
        the topic boundary. We assert the topic boundary appears in
        the chosen cuts."""
        e = [
            _unit(1, 0, 0.01),
            _unit(1, 0, 0.02),
            _unit(1, 0.01, 0),
            _unit(0, 1, 0.01),
            _unit(0, 1, 0.02),
        ]
        segs = segment_embeddings(e)
        # The boundary between chunk 2 and chunk 3 must be a cut —
        # i.e. some segment ends at 2 and the next starts at 3.
        ends = {s.end for s in segs}
        starts = {s.start for s in segs}
        assert 2 in ends and 3 in starts, (
            f"expected cut between chunk 2 and 3; got segments {segs!r}"
        )

    def test_three_topics_yield_three_segments_minimum(self) -> None:
        """6 chunks across 3 distinct topics. With K_MIN=3 we expect
        exactly 3 segments at the topic boundaries."""
        e = [
            _unit(1, 0, 0),
            _unit(1, 0.01, 0),
            _unit(0, 1, 0),
            _unit(0, 1, 0.01),
            _unit(0, 0, 1),
            _unit(0.01, 0, 1),
        ]
        segs = segment_embeddings(e)
        assert len(segs) >= K_MIN
        # Cuts should land at positions 1->2 and 3->4 (the topic
        # shifts). Allow extra cuts within topics; just verify the
        # topic boundaries are honoured.
        ends = {s.end for s in segs}
        assert 1 in ends and 3 in ends, (
            f"expected cuts at chunk-1->2 and chunk-3->4; got {segs!r}"
        )

    def test_uniform_sequence_yields_one_segment(self) -> None:
        """All chunks identical — no topic shift, should collapse
        to a single segment regardless of K_MIN (covers the
        'genuinely uniform sequence' branch)."""
        e = [_unit(1, 0, 0) for _ in range(8)]
        segs = segment_embeddings(e)
        # Either one segment covering everything, or the K_MIN
        # fallback splits arbitrarily — both are defensible. Pin
        # the simpler case explicitly: uniform → single segment.
        assert len(segs) == 1
        assert segs[0] == Segment(0, 7)


# ── coverage invariants ──────────────────────────────────────────────


class TestInvariants:
    def test_segments_cover_full_range_with_no_gaps(self) -> None:
        e = [_unit(math.cos(i), math.sin(i), 0) for i in range(12)]
        segs = segment_embeddings(e)
        # First segment starts at 0; last segment ends at N-1;
        # consecutive segments are adjacent.
        assert segs[0].start == 0
        assert segs[-1].end == len(e) - 1
        for i in range(len(segs) - 1):
            assert segs[i].end + 1 == segs[i + 1].start, (
                f"segments {i} and {i + 1} not adjacent: {segs[i]!r} {segs[i + 1]!r}"
            )

    def test_segment_count_within_k_bounds(self) -> None:
        """For typical inputs (N >= K_MIN), segment count stays in
        ``[K_MIN, K_MAX]``."""
        e = [_unit(math.cos(i * 0.5), math.sin(i * 0.5), 0) for i in range(50)]
        segs = segment_embeddings(e)
        assert K_MIN <= len(segs) <= K_MAX

    def test_segment_length_positive(self) -> None:
        e = [_unit(math.cos(i), math.sin(i), 0) for i in range(15)]
        for s in segment_embeddings(e):
            assert s.length >= 1


# ── determinism ──────────────────────────────────────────────────────


def test_segmentation_is_deterministic() -> None:
    e = [_unit(math.cos(i), math.sin(i), 0.1 * i) for i in range(20)]
    assert segment_embeddings(e) == segment_embeddings(e)


def test_version_string_is_pinned() -> None:
    """Tests + cache callers rely on the version string being a
    stable identifier. Bumping it is intentional; silent drift
    isn't. Pin the current value here."""
    assert SEGMENTATION_VERSION == "1.1"


# ── K bounds parameterised ───────────────────────────────────────────


@pytest.mark.parametrize("n", [3, 4, 5, 10, 20, 50, 100])
def test_n_chunks_always_produces_at_least_one_segment(n: int) -> None:
    e = [_unit(math.cos(i), math.sin(i), 0) for i in range(n)]
    segs = segment_embeddings(e)
    assert len(segs) >= 1
    assert len(segs) <= K_MAX


# ── DP-uniform-cost segmenter ───────────────────────────────────────


class TestSegmentDp:
    def test_k_equals_1_returns_single_segment(self) -> None:
        distances = [0.1, 0.2, 0.3, 0.4]  # 5 chunks
        segs = segment_dp(distances, k=1)
        assert segs == [Segment(0, 4)]

    def test_k_equals_n_returns_singletons(self) -> None:
        distances = [0.1, 0.2, 0.3]  # 4 chunks
        segs = segment_dp(distances, k=4)
        assert segs == [Segment(0, 0), Segment(1, 1), Segment(2, 2), Segment(3, 3)]

    def test_k_picks_largest_gaps(self) -> None:
        # 7 chunks, two clear topic shifts (large gaps) between
        # positions 1-2 and 4-5. DP with K=3 should cut there.
        distances = [0.01, 0.99, 0.01, 0.01, 0.99, 0.01]
        segs = segment_dp(distances, k=3)
        # Boundary indices are at positions 1 and 4 (gaps with 0.99).
        # Resulting segments: [0..1], [2..4], [5..6].
        assert segs == [Segment(0, 1), Segment(2, 4), Segment(5, 6)]

    def test_balanced_when_no_clear_boundary(self) -> None:
        # Uniform tiny gaps. DP minimises total cost; for uniform
        # distances the cost is the same no matter where boundaries
        # land, so we just verify segments cover the full range
        # contiguously.
        n_chunks = 12
        distances = [0.1] * (n_chunks - 1)
        segs = segment_dp(distances, k=4)
        assert len(segs) == 4
        # Full coverage.
        assert segs[0].start == 0
        assert segs[-1].end == n_chunks - 1
        # Contiguous, no gaps.
        for i in range(len(segs) - 1):
            assert segs[i].end + 1 == segs[i + 1].start

    def test_avoids_singleton_segments_when_possible(self) -> None:
        # 8 chunks, one significant gap between 4-5. K=2 should put the
        # boundary at gap index 4 (between chunks 4 and 5), NOT at the
        # ends.
        distances = [0.05, 0.05, 0.05, 0.05, 0.9, 0.05, 0.05]
        segs = segment_dp(distances, k=2)
        # Best is to put boundary at the big gap.
        assert segs == [Segment(0, 4), Segment(5, 7)]

    def test_deterministic(self) -> None:
        distances = [0.1, 0.5, 0.2, 0.4, 0.3, 0.6, 0.1]
        a = segment_dp(distances, k=3)
        b = segment_dp(distances, k=3)
        assert a == b

    def test_k_out_of_range_raises(self) -> None:
        distances = [0.1, 0.2]  # 3 chunks
        with pytest.raises(ValueError):
            segment_dp(distances, k=0)
        with pytest.raises(ValueError):
            segment_dp(distances, k=4)  # > N

    def test_isolates_outlier_at_tail(self) -> None:
        """One giant outlier gap at the end (back-matter) gets its
        own segment under DP, freeing the rest of K-1 boundaries to
        be placed elsewhere.

        Note: when body gaps are truly uniform there's no
        information for the DP to balance on — every partition has
        identical cost. Real papers don't have uniform body gaps;
        they have meaningful local peaks the DP will find.
        Verifying: (a) the spike chunk gets isolated, (b) remaining
        segments cover the body contiguously.
        """
        n_chunks = 20
        distances = [0.1] * (n_chunks - 2) + [0.9]  # tail spike
        segs = segment_dp(distances, k=4)
        assert len(segs) == 4
        # The last segment should be just chunk 19 (the spike isolates it).
        assert segs[-1] == Segment(19, 19)
        # The other 3 segments cover [0..18] contiguously, no overlaps.
        body_segs = segs[:-1]
        assert body_segs[0].start == 0
        assert body_segs[-1].end == 18
        for i in range(len(body_segs) - 1):
            assert body_segs[i].end + 1 == body_segs[i + 1].start

    def test_realistic_non_uniform_signal_covers_fully(self) -> None:
        """When distances have local structure DP finds genuine
        partitions. Coverage + contiguity guaranteed; individual
        segment sizes depend on where the largest gaps land —
        adjacent large gaps can produce isolated singletons by
        design (that's the optimal cut).
        """
        n_chunks = 12
        distances = [0.1, 0.15, 0.2, 0.25, 0.3, 0.25, 0.2, 0.15, 0.1, 0.15, 0.2]
        segs = segment_dp(distances, k=3)
        assert len(segs) == 3
        assert segs[0].start == 0
        assert segs[-1].end == n_chunks - 1
        for i in range(len(segs) - 1):
            assert segs[i].end + 1 == segs[i + 1].start
