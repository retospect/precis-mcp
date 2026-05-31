"""Contract tests for :func:`precis.utils.toc_db.render_from_store`.

Stubs the store so we can pin output shape without DB. End-to-end
behaviour (worker write → renderer read) is covered by the
integration tests in ``tests/workers/test_segment_toc.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from precis.store._segments_ops import SegmentRow, SentenceRow
from precis.utils.toc_db import render_from_store


# ── stub reader ─────────────────────────────────────────────────────


@dataclass
class _StubReader:
    """Tiny fake of the :class:`SegmentsMixin` read surface."""

    segments: list[SegmentRow]
    sentences_by_seg: dict[int, list[SentenceRow]]

    def list_segments_for_ref(self, ref_id: int) -> list[SegmentRow]:
        return list(self.segments)

    def top_sentences_for_segment(
        self,
        segment_id: int,
        *,
        limit: int = 2,
        query_embedding: list[float] | None = None,
    ) -> list[SentenceRow]:
        return list(self.sentences_by_seg.get(segment_id, []))[:limit]


def _make_segment(
    *,
    segment_id: int,
    idx: int,
    pos_lo: int,
    pos_hi: int,
    heading: str | None = None,
    mode: str = "embedding",
    keywords: list[dict[str, Any]] | None = None,
) -> SegmentRow:
    return SegmentRow(
        segment_id=segment_id,
        segment_idx=idx,
        pos_lo=pos_lo,
        pos_hi=pos_hi,
        heading=heading,
        mode=mode,
        section_class=None,
        segmentation_version="1.0",
        extractor_version="test",
        embedder_name="mock",
        keywords=keywords or [],
        forms=[],
        status="ok",
    )


def _make_sentence(
    *,
    segment_id: int,
    idx: int,
    text: str,
    chunk_pos: int,
    score: float = 0.5,
) -> SentenceRow:
    return SentenceRow(
        sentence_id=segment_id * 100 + idx,
        segment_id=segment_id,
        sentence_idx=idx,
        text=text,
        chunk_pos=chunk_pos,
        char_offset=0,
        centroid_score=score,
    )


# ── empty / placeholder paths ───────────────────────────────────────


class TestPlaceholder:
    def test_no_segments_yields_placeholder(self) -> None:
        reader = _StubReader(segments=[], sentences_by_seg={})
        out = render_from_store(
            store=reader, ref_id=42, slug="foo", kind="paper"
        )
        assert "segments not yet computed" in out
        assert "foo" in out

    def test_scope_with_no_matching_segments(self) -> None:
        reader = _StubReader(
            segments=[_make_segment(segment_id=1, idx=0, pos_lo=0, pos_hi=5)],
            sentences_by_seg={},
        )
        out = render_from_store(
            store=reader, ref_id=42, slug="foo", kind="paper",
            scope=(20, 30),
        )
        assert "no segments in scope ~20..30" in out


# ── headline + table shape ──────────────────────────────────────────


class TestHeadline:
    def test_embedding_mode_headline(self) -> None:
        reader = _StubReader(
            segments=[
                _make_segment(segment_id=1, idx=0, pos_lo=0, pos_hi=5),
                _make_segment(segment_id=2, idx=1, pos_lo=6, pos_hi=10),
            ],
            sentences_by_seg={},
        )
        out = render_from_store(
            store=reader, ref_id=42, slug="foo", kind="paper"
        )
        assert "embedding clustering" in out
        assert "2 segments" in out

    def test_h2_mode_headline(self) -> None:
        reader = _StubReader(
            segments=[
                _make_segment(
                    segment_id=1, idx=0, pos_lo=0, pos_hi=5,
                    heading="Introduction", mode="h2",
                ),
                _make_segment(
                    segment_id=2, idx=1, pos_lo=6, pos_hi=10,
                    heading="Methods", mode="h2",
                ),
            ],
            sentences_by_seg={},
        )
        out = render_from_store(
            store=reader, ref_id=42, slug="foo", kind="paper"
        )
        assert "H2 sections" in out

    def test_scope_headline(self) -> None:
        reader = _StubReader(
            segments=[_make_segment(segment_id=1, idx=0, pos_lo=5, pos_hi=7)],
            sentences_by_seg={},
        )
        out = render_from_store(
            store=reader, ref_id=42, slug="foo", kind="paper",
            scope=(5, 7),
        )
        assert "sub-TOC ~5..7" in out


# ── excerpt sub-lines ────────────────────────────────────────────────


class TestExcerpt:
    def test_excerpt_attached_to_segment(self) -> None:
        seg = _make_segment(
            segment_id=1, idx=0, pos_lo=3, pos_hi=5,
            keywords=[{"long": "Cu-MOF", "short": "MOF", "aliases": [],
                       "score": 0.8}],
        )
        reader = _StubReader(
            segments=[seg],
            sentences_by_seg={
                1: [_make_sentence(
                    segment_id=1, idx=0,
                    text="We observed 12% FE for CO2 reduction at -0.3 V.",
                    chunk_pos=4,
                )],
            },
        )
        out = render_from_store(
            store=reader, ref_id=42, slug="foo", kind="paper"
        )
        # Tabular row with the keyword (short form preferred).
        assert "foo~3..5" in out
        assert "MOF" in out
        # Excerpt sub-line indented + quoted + chunk-handle annotated.
        assert 'excerpt @ ~4: "We observed 12% FE' in out
        assert "  - excerpt" in out  # 2-space indent + dash bullet

    def test_no_excerpt_when_no_sentences(self) -> None:
        reader = _StubReader(
            segments=[
                _make_segment(
                    segment_id=1, idx=0, pos_lo=0, pos_hi=3,
                    keywords=[{"long": "topic", "short": None, "aliases": [],
                               "score": 0.5}],
                ),
            ],
            sentences_by_seg={},  # no sentences for segment 1
        )
        out = render_from_store(
            store=reader, ref_id=42, slug="foo", kind="paper"
        )
        # No excerpt prefix when sentences are absent — silence is
        # the right signal per the design discussion.
        assert "excerpt @" not in out

    def test_multiline_sentence_flattened(self) -> None:
        # Stored sentences sometimes contain newlines (multi-line OCR
        # blocks). Render must keep the excerpt on one line per the
        # 2026-05-31 design (LLM-friendly, terminal soft-wraps).
        reader = _StubReader(
            segments=[_make_segment(segment_id=1, idx=0, pos_lo=0, pos_hi=2)],
            sentences_by_seg={
                1: [_make_sentence(
                    segment_id=1, idx=0,
                    text="line one\nline two\n   line three",
                    chunk_pos=1,
                )],
            },
        )
        out = render_from_store(
            store=reader, ref_id=42, slug="foo", kind="paper"
        )
        # Sub-line collapses inner whitespace into single spaces.
        assert 'excerpt @ ~1: "line one line two line three"' in out


# ── keyword display ─────────────────────────────────────────────────


class TestKeywordDisplay:
    def test_prefers_short_over_long_when_available(self) -> None:
        seg = _make_segment(
            segment_id=1, idx=0, pos_lo=0, pos_hi=2,
            keywords=[
                {"long": "Metal-Organic Framework", "short": "MOF",
                 "aliases": [], "score": 0.9},
                {"long": "Density Functional Theory", "short": "DFT",
                 "aliases": [], "score": 0.7},
            ],
        )
        reader = _StubReader(segments=[seg], sentences_by_seg={})
        out = render_from_store(
            store=reader, ref_id=42, slug="foo", kind="paper"
        )
        assert "MOF" in out and "DFT" in out
        # When short forms are present we don't display the long form
        # next to them — short wins. The long stays available via
        # the JSONB on the row for verifiers / external callers.
        assert "Metal-Organic Framework" not in out

    def test_uses_long_when_no_short(self) -> None:
        seg = _make_segment(
            segment_id=1, idx=0, pos_lo=0, pos_hi=2,
            keywords=[
                {"long": "lithium battery anode", "short": None,
                 "aliases": [], "score": 0.9},
            ],
        )
        reader = _StubReader(segments=[seg], sentences_by_seg={})
        out = render_from_store(
            store=reader, ref_id=42, slug="foo", kind="paper"
        )
        assert "lithium battery anode" in out
