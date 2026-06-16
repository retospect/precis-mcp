"""Tests for the universal search-merge primitive.

Coverage:
- ``SearchHit.handle`` — slug/pos/ref_id fallbacks.
- ``merge_and_render`` priority mode — preserves stream order,
  drops cross-stream dedup_key collisions, allows intra-stream
  duplicates (patent's local block hits sharing a ref slug).
- ``merge_and_render`` rrf mode — reciprocal rank fusion sums
  contributions across streams; raw score breaks ties; insertion
  order is the final tiebreak so output is deterministic.
- ``block_hits_to_search_hits`` — block dedupe vs ref-level
  dedupe; preview truncation; extra_lines callable; source label.
- ``ref_hits_to_search_hits`` — ref-level dedupe by slug or ref_id.
- Empty streams render the empty body, with custom override.
"""

from __future__ import annotations

from dataclasses import dataclass

from precis.utils.search_merge import (
    SearchHit,
    block_hits_to_search_hits,
    merge_and_render,
    ref_hits_to_search_hits,
)

# ---------------------------------------------------------------------------
# SearchHit.handle
# ---------------------------------------------------------------------------


def test_handle_uses_slug_and_pos() -> None:
    h = SearchHit(score=0.0, kind="paper", title="t", preview="p", slug="abc", pos=5)
    assert h.handle == "abc~5"


def test_handle_uses_slug_only_when_no_pos() -> None:
    h = SearchHit(score=0.0, kind="oracle", title="t", preview="p", slug="abc")
    assert h.handle == "abc"


def test_handle_falls_back_to_ref_id() -> None:
    h = SearchHit(score=0.0, kind="memory", title="t", preview="p", ref_id=42)
    assert h.handle == "#42"


def test_handle_last_resort_question_mark() -> None:
    h = SearchHit(score=0.0, kind="thing", title="t", preview="p")
    assert h.handle == "?"


# ---------------------------------------------------------------------------
# Priority-mode merge
# ---------------------------------------------------------------------------


def _hit(
    *,
    kind: str = "paper",
    slug: str | None = None,
    pos: int | None = None,
    score: float = 0.0,
    source: str | None = None,
    dedupe_key: str | None = None,
    title: str = "T",
    preview: str = "P",
) -> SearchHit:
    return SearchHit(
        score=score,
        kind=kind,
        title=title,
        preview=preview,
        slug=slug,
        pos=pos,
        source=source,
        dedupe_key=dedupe_key,
    )


def test_priority_preserves_stream_order_and_drops_cross_stream_dups() -> None:
    s1 = [
        _hit(slug="a", pos=1, dedupe_key="paper:a"),
        _hit(slug="a", pos=2, dedupe_key="paper:a"),
    ]
    s2 = [
        _hit(slug="a", pos=99, dedupe_key="paper:a"),  # collides with s1
        _hit(slug="b", pos=1, dedupe_key="paper:b"),
    ]
    out = merge_and_render([s1, s2], page_size=10, mode="priority")
    body = out.body
    # Both s1 hits keep (intra-stream collisions allowed).
    assert "a~1" in body
    assert "a~2" in body
    # s2's first hit (paper:a) drops via cross-stream dedup.
    assert "a~99" not in body
    # s2's second hit (paper:b) survives.
    assert "b~1" in body


def test_priority_no_dedupe_key_means_no_collapse() -> None:
    s1 = [_hit(slug="a", pos=1, dedupe_key=None)]
    s2 = [_hit(slug="a", pos=1, dedupe_key=None)]
    out = merge_and_render([s1, s2], page_size=10, mode="priority")
    # Both hits render — without a dedupe_key, no merge happens.
    assert out.body.count("a~1") == 2


# ---------------------------------------------------------------------------
# RRF-mode merge
# ---------------------------------------------------------------------------


def test_rrf_fuses_streams_by_rank() -> None:
    # Three streams; "x" appears at rank 1 in two streams → highest
    # cumulative score. "y" appears once at rank 1; "z" appears
    # twice at ranks 2 and 3.
    s1 = [
        _hit(slug="x", dedupe_key="paper:x", score=0.9),
        _hit(slug="z", dedupe_key="paper:z", score=0.1),
    ]
    s2 = [
        _hit(slug="x", dedupe_key="paper:x", score=0.5),
        _hit(slug="z", dedupe_key="paper:z", score=0.4),
    ]
    s3 = [_hit(slug="y", dedupe_key="paper:y", score=0.6)]
    out = merge_and_render([s1, s2, s3], page_size=10, mode="rrf")
    body = out.body
    # x must outrank y (two stream contributions vs one).
    x_idx = body.find("paper:x")  # via dedupe_key only used internally;
    # use the citation handle markers instead.
    assert "1. x" in body  # rank 1 line in the rendered output


def test_rrf_no_dedupe_key_keeps_all_singletons() -> None:
    s1 = [_hit(slug="a", pos=1)]
    s2 = [_hit(slug="a", pos=1)]
    out = merge_and_render([s1, s2], page_size=10, mode="rrf")
    # Without dedupe_key, every hit is its own document.
    assert out.body.count("## ") == 2


# ---------------------------------------------------------------------------
# Empty-body
# ---------------------------------------------------------------------------


def test_empty_streams_default_message() -> None:
    out = merge_and_render([], page_size=10, mode="priority", header_noun="patent hit")
    assert "no patent hit matches" in out.body


def test_empty_streams_custom_message() -> None:
    out = merge_and_render(
        [],
        page_size=10,
        mode="priority",
        empty_body="no patents match 'foo'",
    )
    assert out.body == "no patents match 'foo'"


def test_empty_streams_with_query_mentions_query() -> None:
    out = merge_and_render([], page_size=10, query="foo", header_noun="match")
    assert "foo" in out.body


# ---------------------------------------------------------------------------
# Per-hit rendering
# ---------------------------------------------------------------------------


def test_renders_handle_title_and_preview() -> None:
    s1 = [_hit(slug="a", pos=3, title="A title", preview="A preview")]
    out = merge_and_render([s1], page_size=10, mode="priority")
    assert "## 1. a~3" in out.body
    assert "_A title_" in out.body
    assert "A preview" in out.body


def test_show_label_false_drops_bracket() -> None:
    s1 = [_hit(slug="a", pos=3, source="local")]
    out = merge_and_render([s1], page_size=10, mode="priority", show_label=False)
    assert "[local]" not in out.body


def test_show_label_uses_kind_when_no_source() -> None:
    s1 = [_hit(slug="a", pos=3, kind="paper")]
    out = merge_and_render([s1], page_size=10, mode="priority")
    assert "[paper]" in out.body


def test_show_label_prefers_source_over_kind() -> None:
    s1 = [_hit(slug="a", pos=3, kind="paper", source="local")]
    out = merge_and_render([s1], page_size=10, mode="priority")
    assert "[local]" in out.body
    assert "[paper]" not in out.body


def test_page_size_caps_rendered_hits() -> None:
    s1 = [_hit(slug=f"x{i}", pos=0, dedupe_key=f"paper:x{i}") for i in range(5)]
    out = merge_and_render([s1], page_size=2, mode="priority")
    assert "## 1. x0" in out.body
    assert "## 2. x1" in out.body
    assert "## 3. x2" not in out.body


# ---------------------------------------------------------------------------
# block_hits_to_search_hits adapter
# ---------------------------------------------------------------------------


@dataclass
class FakeBlock:
    text: str
    pos: int


@dataclass
class FakeRef:
    title: str
    slug: str
    id: int = 1


def test_block_helper_default_dedupe_uses_block_handle() -> None:
    triples = [(FakeBlock("hello world", 5), FakeRef("title", "abc"), 0.9)]
    [hit] = block_hits_to_search_hits(triples, kind="paper")
    assert hit.dedupe_key == "paper:abc~5"


def test_block_helper_ref_level_dedupe_collapses_to_slug() -> None:
    triples = [
        (FakeBlock("first", 0), FakeRef("title", "abc"), 0.9),
        (FakeBlock("second", 1), FakeRef("title", "abc"), 0.8),
    ]
    hits = block_hits_to_search_hits(triples, kind="patent", ref_level_dedupe=True)
    # Both hits get the same ref-level dedupe key.
    assert all(h.dedupe_key == "patent:abc" for h in hits)


def test_block_helper_truncates_preview() -> None:
    long_text = "x" * 500
    triples = [(FakeBlock(long_text, 0), FakeRef("title", "abc"), 0.5)]
    [hit] = block_hits_to_search_hits(triples, kind="paper", excerpt=50)
    assert len(hit.preview) <= 50
    assert hit.preview.endswith("…")


def test_block_helper_extra_lines_callable() -> None:
    triples = [(FakeBlock("body", 1), FakeRef("title", "abc"), 0.5)]
    [hit] = block_hits_to_search_hits(
        triples,
        kind="paper",
        extra_lines_for=lambda b, r: (f"slug={r.slug}",),
    )
    assert hit.extra_lines == ("slug=abc",)


def test_block_helper_source_overrides_label() -> None:
    triples = [(FakeBlock("body", 0), FakeRef("title", "abc"), 0.5)]
    [hit] = block_hits_to_search_hits(triples, kind="patent", source="local")
    assert hit.source == "local"
    assert hit.kind == "patent"


# ---------------------------------------------------------------------------
# ref_hits_to_search_hits adapter
# ---------------------------------------------------------------------------


def test_ref_helper_dedupe_by_slug() -> None:
    pairs = [(FakeRef("Title", "foo", id=7), 0.5)]
    [hit] = ref_hits_to_search_hits(pairs, kind="oracle")
    assert hit.dedupe_key == "oracle:foo"


def test_ref_helper_falls_back_to_id_for_numeric_kind() -> None:
    @dataclass
    class NumericRef:
        title: str
        id: int
        slug: str | None = None

    pairs = [(NumericRef("Title", 99), 0.5)]
    [hit] = ref_hits_to_search_hits(pairs, kind="memory")
    assert hit.dedupe_key == "memory:#99"


def test_ref_helper_default_preview_truncates_title() -> None:
    long_title = "y" * 300
    pairs = [(FakeRef(long_title, "foo"), 0.5)]
    [hit] = ref_hits_to_search_hits(pairs, kind="oracle", excerpt=50)
    assert len(hit.preview) <= 50
    assert hit.preview.endswith("…")


def test_ref_helper_preview_callable() -> None:
    pairs = [(FakeRef("Title", "foo"), 0.5)]
    [hit] = ref_hits_to_search_hits(
        pairs, kind="oracle", preview_for=lambda r: f"custom: {r.title}"
    )
    assert hit.preview == "custom: Title"


# ---------------------------------------------------------------------------
# view='keywords' — compact id|kind|keywords TOON shape (T10.1)
# ---------------------------------------------------------------------------


@dataclass
class _FakeBlockKw:
    text: str
    pos: int
    keywords: tuple[str, ...] = ()


def test_block_adapter_propagates_block_keywords_to_hit() -> None:
    """The adapter must pass ``block.keywords`` through to the SearchHit
    so the keywords renderer has something to project."""
    block = _FakeBlockKw(text="body", pos=3, keywords=("photocatalysis", "tio2"))
    ref = FakeRef("Title", "abc", id=10)
    [hit] = block_hits_to_search_hits([(block, ref, 0.5)], kind="paper")
    assert hit.keywords == ("photocatalysis", "tio2")


def test_block_adapter_defaults_to_empty_keywords_tuple() -> None:
    """Producers that don't populate keywords still produce valid hits;
    the keywords cell renders empty."""
    block = _FakeBlockKw(text="body", pos=3)
    ref = FakeRef("Title", "abc", id=10)
    [hit] = block_hits_to_search_hits([(block, ref, 0.5)], kind="paper")
    assert hit.keywords == ()


def test_keywords_shape_renders_compact_table() -> None:
    """``output_shape='keywords'`` swaps the renderer for a 3-col table
    with no preview body — that's the whole point of the shape."""
    s1 = [
        SearchHit(
            score=0.9,
            kind="paper",
            title="Long-title-here that should NOT appear in the cells",
            preview="this preview SHOULD NOT appear",
            slug="acheson26",
            pos=5,
            keywords=("photocatalysis", "tio2"),
        )
    ]
    s2 = [
        SearchHit(
            score=0.8,
            kind="memory",
            title="An idea",
            preview="don't surface this",
            ref_id=42,
            keywords=("attention", "dreaming"),
        )
    ]
    out = merge_and_render(
        [s1, s2],
        page_size=10,
        query="topics",
        mode="rrf",
        output_shape="keywords",
    )
    body = out.body
    # Headline line first, then the TOON table.
    assert "match" in body.lower()
    # Cells: ids + keywords present.
    assert "acheson26~5" in body
    assert "42" in body
    assert "photocatalysis" in body
    assert "attention" in body
    # Preview / title bodies absent — that's the whole compaction.
    assert "should NOT appear" not in body
    assert "don't surface this" not in body


def test_keywords_shape_renders_empty_cell_when_no_keywords() -> None:
    """A hit with no keywords still surfaces in the table (the agent
    sees the ref exists) but the keywords cell is empty."""
    s1 = [
        SearchHit(
            score=0.9,
            kind="paper",
            title="t",
            preview="p",
            slug="foo",
            pos=1,
            keywords=(),
        )
    ]
    out = merge_and_render(
        [s1],
        page_size=10,
        query="topics",
        mode="rrf",
        output_shape="keywords",
    )
    assert "foo~1" in out.body
    assert "paper" in out.body
