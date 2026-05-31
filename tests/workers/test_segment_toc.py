"""Integration tests for the segment_toc worker.

Exercises the discovery-layer write path end-to-end: from a seeded
``refs`` + ``chunks`` + ``chunk_embeddings`` state through to
``ref_segments`` + ``ref_segment_sentences`` rows.

Mock embedder keeps the test deterministic and fast — the worker
itself never sees the real bge-m3.
"""

from __future__ import annotations

import math

import pytest

from precis.embedder import MockEmbedder
from precis.utils.toc import ChunksForToc
from precis.workers.segment_toc import (
    EXTRACTOR_VERSION,
    build_segments,
    claim_refs_without_segments,
)
from tests.workers._helpers import make_mock_bge_m3, seed_chunk, seed_ref


# ── fixtures ────────────────────────────────────────────────────────


def _body_text(idx: int) -> str:
    """Long-enough body text to escape the boilerplate position-0
    classifier (>1500 chars) while embedding cleanly."""
    return (
        f"chunk {idx} body text "
        + ("metal organic framework synthesis characterization " * 80)
        + f" marker {idx}."
    )


def _unit(x: float, y: float) -> tuple[float, ...]:
    """L2-normalised 1024-dim vector (mostly zeros, two non-zeros).

    Lets us shape segment-cluster boundaries deterministically: two
    adjacent chunks with the same direction land in one segment;
    a direction change suggests a boundary.
    """
    norm = math.sqrt(x * x + y * y)
    if norm == 0:
        norm = 1.0
    vec = [0.0] * 1024
    vec[0] = x / norm
    vec[1] = y / norm
    return tuple(vec)


def _seed_paper_with_body(
    store,
    *,
    n_body: int = 6,
) -> tuple[int, list[int]]:
    """Insert one ref + n_body paragraph chunks with embeddings.

    Embeddings split halfway into two distinct directions so the DP
    segmenter has a real boundary to find. Returns ``(ref_id,
    chunk_ids)``.
    """
    ref_id = seed_ref(store)
    chunk_ids: list[int] = []
    for i in range(n_body):
        chunk_id = seed_chunk(
            store, ref_id=ref_id, ord=i, chunk_kind="paragraph",
            text=_body_text(i),
        )
        chunk_ids.append(chunk_id)
    # Two-cluster embedding shape: first half points at (1, 0), second
    # half at (0, 1). 1 - cos(half[N], half[N+1]) = 1 - 0 = 1.0, the
    # largest possible gap, so the DP segmenter must put the boundary
    # there.
    half = n_body // 2
    embedder = make_mock_bge_m3()
    with store.pool.connection() as conn:
        for i, chunk_id in enumerate(chunk_ids):
            if i < half:
                vec = list(_unit(1.0, 0.0))
            else:
                vec = list(_unit(0.0, 1.0))
            conn.execute(
                "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status) "
                "VALUES (%s, %s, %s, 'ok')",
                (chunk_id, embedder.model, vec),
            )
        conn.commit()
    return ref_id, chunk_ids


def _adapter_for(store, ref_id: int) -> ChunksForToc:
    """Build a minimal ChunksForToc from the seeded state."""
    blocks = store.list_blocks_for_ref(ref_id, with_embedding=True)
    blocks = sorted(blocks, key=lambda b: b.pos)
    return ChunksForToc(
        chunks_text=tuple(b.text for b in blocks),
        embeddings=tuple(tuple(b.embedding) for b in blocks),
        h2_boundaries=(),
        positions=tuple(b.pos for b in blocks),
        chunker_version="test-1.0",
        embedder_name=make_mock_bge_m3().model,
        embedder=make_mock_bge_m3(),
    )


# ── core happy path ─────────────────────────────────────────────────


class TestBuildSegmentsHappyPath:
    def test_writes_segments_and_sentences(self, store):
        ref_id, _ = _seed_paper_with_body(store, n_body=6)
        adapter = _adapter_for(store, ref_id)
        with store.pool.connection() as conn:
            n_seg = build_segments(conn, ref_id=ref_id, adapter=adapter)
            conn.commit()
        assert n_seg >= 1

        segs = store.list_segments_for_ref(ref_id)
        assert len(segs) == n_seg
        for s in segs:
            assert s.pos_lo <= s.pos_hi
            assert s.mode in ("h2", "embedding")
            assert s.segmentation_version
            assert s.extractor_version == EXTRACTOR_VERSION
            assert s.embedder_name == make_mock_bge_m3().model
            # Forms array surfaces every keyword's long+short form for GIN
            # lookup. We don't pin the exact terms (depends on RAKE
            # output on the synthetic text) but the count is non-zero
            # when keywords are non-empty.
            if s.keywords:
                assert s.forms

        # Sentence rows exist for each segment.
        for s in segs:
            sents = store.top_sentences_for_segment(s.segment_id, limit=20)
            assert isinstance(sents, list)
            for sent in sents:
                assert sent.text
                assert sent.chunk_pos >= 0
                assert sent.char_offset >= 0
                assert math.isfinite(sent.centroid_score)

    def test_idempotent_rerun_overwrites(self, store):
        ref_id, _ = _seed_paper_with_body(store, n_body=6)
        adapter = _adapter_for(store, ref_id)
        with store.pool.connection() as conn:
            n_first = build_segments(conn, ref_id=ref_id, adapter=adapter)
            conn.commit()
            n_second = build_segments(conn, ref_id=ref_id, adapter=adapter)
            conn.commit()
        assert n_first == n_second
        # Exactly n_second rows survive (no duplicate ord conflicts).
        segs = store.list_segments_for_ref(ref_id)
        assert len(segs) == n_second


# ── boundary cases ──────────────────────────────────────────────────


class TestBuildSegmentsEdgeCases:
    def test_requires_embedder_and_embeddings(self, store):
        ref_id, _ = _seed_paper_with_body(store, n_body=3)
        # Strip embedder + embeddings — the worker requires both.
        adapter = ChunksForToc(
            chunks_text=(_body_text(0), _body_text(1), _body_text(2)),
            embeddings=None,
            h2_boundaries=(),
            embedder=None,
        )
        with store.pool.connection() as conn:
            with pytest.raises(ValueError):
                build_segments(conn, ref_id=ref_id, adapter=adapter)

    def test_empty_chunks_writes_nothing(self, store):
        ref_id = seed_ref(store)
        adapter = ChunksForToc(
            chunks_text=(),
            embeddings=(),
            h2_boundaries=(),
            embedder=make_mock_bge_m3(),
        )
        with store.pool.connection() as conn:
            n = build_segments(conn, ref_id=ref_id, adapter=adapter)
            conn.commit()
        assert n == 0
        assert store.list_segments_for_ref(ref_id) == []


# ── claim queue ─────────────────────────────────────────────────────


class TestClaimRefsWithoutSegments:
    def test_returns_refs_with_body_chunks_but_no_segments(self, store):
        ref_a, _ = _seed_paper_with_body(store, n_body=4)
        ref_b, _ = _seed_paper_with_body(store, n_body=4)
        with store.pool.connection() as conn:
            claimed = claim_refs_without_segments(conn, limit=10)
        assert ref_a in claimed
        assert ref_b in claimed

    def test_excludes_refs_with_existing_segments(self, store):
        ref_a, _ = _seed_paper_with_body(store, n_body=4)
        adapter = _adapter_for(store, ref_a)
        with store.pool.connection() as conn:
            build_segments(conn, ref_id=ref_a, adapter=adapter)
            conn.commit()
            claimed = claim_refs_without_segments(conn, limit=10)
        assert ref_a not in claimed

    def test_excludes_refs_with_only_card_chunks(self, store):
        # Ref with only ord<0 cards (no body chunks) — segment_toc
        # has nothing to work with, so claim_refs_without_segments
        # should skip it.
        ref_id = seed_ref(store)
        seed_chunk(
            store, ref_id=ref_id, ord=-1, chunk_kind="card_combined",
            text="title + abstract",
        )
        with store.pool.connection() as conn:
            claimed = claim_refs_without_segments(conn, limit=10)
        assert ref_id not in claimed

    def test_excludes_refs_with_only_references_chunks(self, store):
        # References chunks don't qualify as body for segmentation.
        ref_id = seed_ref(store)
        seed_chunk(
            store, ref_id=ref_id, ord=0, chunk_kind="references",
            text="[1] Smith 2020",
        )
        with store.pool.connection() as conn:
            claimed = claim_refs_without_segments(conn, limit=10)
        assert ref_id not in claimed

    def test_respects_limit(self, store):
        for _ in range(5):
            _seed_paper_with_body(store, n_body=4)
        with store.pool.connection() as conn:
            claimed = claim_refs_without_segments(conn, limit=2)
        assert len(claimed) == 2

    def test_zero_limit_rejected(self, store):
        with store.pool.connection() as conn:
            with pytest.raises(ValueError):
                claim_refs_without_segments(conn, limit=0)
