"""Tests for `precis.handlers._paper_toc` — hierarchical TOC.

Pure logic. No DB, no IO.

Builds a synthetic list of `Block` rows (different heading patterns,
different orderings) and verifies the section tree + rendering.
"""

from __future__ import annotations

from datetime import UTC

from precis.handlers._paper_toc import (
    Section,
    build_toc,
    detect_heading,
    filter_toc_to_range,
    render_toc,
)
from precis.store.types import Block


def _make_block(pos: int, text: str) -> Block:
    """Construct a minimal Block for testing.

    All other Block fields are required by the dataclass but not by the
    TOC code; we fill plausible placeholders.
    """
    from datetime import datetime

    now = datetime.now(UTC)
    return Block(
        id=pos + 1,
        ref_id=1,
        pos=pos,
        slug=None,
        text=text,
        token_count=len(text.split()),
        embedding=None,
        density=None,
        meta={},
        created_at=now,
        updated_at=now,
    )


# ── heading detection ────────────────────────────────────────────────


class TestDetectHeading:
    def test_h1_acatome(self) -> None:
        h = detect_heading(_make_block(0, "■ **METHODS**"))
        assert h is not None
        assert h.title == "METHODS"
        assert h.level == 1

    def test_h1_with_amp(self) -> None:
        h = detect_heading(_make_block(0, "■ **RESULTS & DISCUSSION**"))
        assert h is not None
        assert h.title == "RESULTS & DISCUSSION"
        assert h.level == 1

    def test_h2_subsection(self) -> None:
        h = detect_heading(_make_block(5, "**Heterodiatomic Molecules**"))
        assert h is not None
        assert h.title == "Heterodiatomic Molecules"
        assert h.level == 2

    def test_h2_with_brackets(self) -> None:
        h = detect_heading(
            _make_block(5, "**Physics-Informed Program Synthesis [PIPS]**")
        )
        assert h is not None
        assert h.level == 2
        assert "PIPS" in h.title

    def test_md_h1(self) -> None:
        h = detect_heading(_make_block(0, "# Introduction"))
        assert h is not None
        assert h.title == "Introduction"
        assert h.level == 1

    def test_md_h2(self) -> None:
        h = detect_heading(_make_block(0, "## Methods"))
        assert h is not None
        assert h.title == "Methods"
        assert h.level == 2

    def test_body_text_not_heading(self) -> None:
        assert (
            detect_heading(
                _make_block(0, "We applied PIPS to a set of small molecules.")
            )
            is None
        )

    def test_multiline_block_not_heading(self) -> None:
        # Real headings are short single-line entries.
        assert (
            detect_heading(_make_block(0, "**Title**\nWith body text continuing"))
            is None
        )

    def test_empty_block_not_heading(self) -> None:
        assert detect_heading(_make_block(0, "")) is None
        assert detect_heading(_make_block(0, "   ")) is None

    def test_bold_text_inside_paragraph_not_heading(self) -> None:
        # `**Bold**` inside a longer line shouldn't match.
        assert (
            detect_heading(_make_block(0, "We tested **Bold** items in the data."))
            is None
        )


# ── TOC building ─────────────────────────────────────────────────────


class TestBuildToc:
    def test_empty(self) -> None:
        assert build_toc([]) == []

    def test_no_headings_one_implicit_section(self) -> None:
        blocks = [
            _make_block(0, "lorem"),
            _make_block(1, "ipsum"),
            _make_block(2, "dolor"),
        ]
        toc = build_toc(blocks)
        assert len(toc) == 1
        assert toc[0].title == ""
        assert toc[0].level == 0
        assert toc[0].start == 0
        assert toc[0].end == 2

    def test_simple_h1_only(self) -> None:
        blocks = [
            _make_block(0, "■ **INTRODUCTION**"),
            _make_block(1, "intro body 1"),
            _make_block(2, "intro body 2"),
            _make_block(3, "■ **METHODS**"),
            _make_block(4, "method body"),
        ]
        toc = build_toc(blocks)
        assert len(toc) == 2
        assert toc[0].title == "INTRODUCTION"
        assert (toc[0].start, toc[0].end) == (0, 2)
        assert toc[1].title == "METHODS"
        assert (toc[1].start, toc[1].end) == (3, 4)

    def test_h1_with_h2_children(self) -> None:
        blocks = [
            _make_block(0, "■ **METHODS**"),
            _make_block(1, "methods intro"),
            _make_block(2, "**Calculation Details**"),
            _make_block(3, "calc body 1"),
            _make_block(4, "**Heterodiatomic Molecules**"),
            _make_block(5, "het body"),
            _make_block(6, "■ **RESULTS**"),
            _make_block(7, "results body"),
        ]
        toc = build_toc(blocks)
        assert len(toc) == 2
        methods = toc[0]
        assert methods.title == "METHODS"
        assert (methods.start, methods.end) == (0, 5)
        assert len(methods.children) == 2
        assert methods.children[0].title == "Calculation Details"
        assert (methods.children[0].start, methods.children[0].end) == (2, 3)
        assert methods.children[1].title == "Heterodiatomic Molecules"
        assert (methods.children[1].start, methods.children[1].end) == (4, 5)
        results = toc[1]
        assert results.title == "RESULTS"
        assert (results.start, results.end) == (6, 7)

    def test_implicit_leading_section(self) -> None:
        """Blocks before the first heading become an untitled prelude."""
        blocks = [
            _make_block(0, "title page"),
            _make_block(1, "abstract"),
            _make_block(2, "■ **INTRODUCTION**"),
            _make_block(3, "intro body"),
        ]
        toc = build_toc(blocks)
        assert len(toc) == 2
        assert toc[0].title == ""
        assert toc[0].level == 0
        assert (toc[0].start, toc[0].end) == (0, 1)
        assert toc[1].title == "INTRODUCTION"

    def test_h2_only(self) -> None:
        """A paper with only H2s (no H1) — each H2 stays at top level."""
        blocks = [
            _make_block(0, "**Setup**"),
            _make_block(1, "setup body"),
            _make_block(2, "**Procedure**"),
            _make_block(3, "procedure body"),
        ]
        toc = build_toc(blocks)
        assert len(toc) == 2
        assert all(s.level == 2 for s in toc)
        assert toc[0].title == "Setup"
        assert toc[1].title == "Procedure"

    def test_real_paper_pattern(self) -> None:
        """Reproduce the structure observed live in acheson2026automated."""
        blocks = [
            _make_block(0, "title"),
            _make_block(8, "■ **INTRODUCTION**"),
            _make_block(21, "■ **THEORY**"),
            _make_block(41, "■ **METHODS**"),
            _make_block(43, "**Physics-Informed Program Synthesis [PIPS]**"),
            _make_block(54, "**Calculation Details**"),
            _make_block(74, "■ **RESULTS & DISCUSSION**"),
            _make_block(76, "**Application to Heterodiatomic Molecules**"),
            _make_block(97, "**Application to Alkanes**"),
            _make_block(117, "■ **CONCLUSIONS**"),
        ]
        toc = build_toc(blocks)
        # Expected top-level: untitled, INTRO, THEORY, METHODS,
        # RESULTS & DISCUSSION, CONCLUSIONS
        titles = [s.title for s in toc]
        assert titles == [
            "",
            "INTRODUCTION",
            "THEORY",
            "METHODS",
            "RESULTS & DISCUSSION",
            "CONCLUSIONS",
        ]
        # METHODS has two H2 children
        methods = next(s for s in toc if s.title == "METHODS")
        assert len(methods.children) == 2
        assert methods.children[0].title == "Physics-Informed Program Synthesis [PIPS]"
        # RESULTS & DISCUSSION has two H2 children
        results = next(s for s in toc if s.title == "RESULTS & DISCUSSION")
        assert len(results.children) == 2
        assert results.children[0].title == "Application to Heterodiatomic Molecules"


# ── range filter (drill-down) ────────────────────────────────────────


class TestFilterTocToRange:
    def _three_sections(self) -> list[Section]:
        # Start with a fresh-built TOC so children are populated.
        blocks = [
            _make_block(0, "■ **A**"),
            _make_block(1, "a body"),
            _make_block(10, "■ **B**"),
            _make_block(11, "b body 1"),
            _make_block(12, "**B1**"),
            _make_block(13, "b1 body"),
            _make_block(20, "■ **C**"),
            _make_block(21, "c body"),
        ]
        return build_toc(blocks)

    def test_filter_inside_one_section(self) -> None:
        toc = self._three_sections()
        scoped = filter_toc_to_range(toc, lo=11, hi=15)
        # Only B should remain (with its B1 child clipped to within range)
        assert len(scoped) == 1
        assert scoped[0].title == "B"
        assert (scoped[0].start, scoped[0].end) == (11, 15)
        assert len(scoped[0].children) == 1
        assert scoped[0].children[0].title == "B1"
        assert (scoped[0].children[0].start, scoped[0].children[0].end) == (12, 15)

    def test_filter_spanning_multiple(self) -> None:
        toc = self._three_sections()
        scoped = filter_toc_to_range(toc, lo=5, hi=21)
        assert [s.title for s in scoped] == ["A", "B", "C"]
        # A clipped to lo=5
        assert (scoped[0].start, scoped[0].end) == (5, 9)

    def test_filter_outside_returns_empty(self) -> None:
        toc = self._three_sections()
        scoped = filter_toc_to_range(toc, lo=100, hi=200)
        assert scoped == []


# ── rendering ────────────────────────────────────────────────────────


class TestRenderToc:
    def test_basic_render(self) -> None:
        blocks = [
            _make_block(0, "■ **METHODS**"),
            _make_block(1, "method body"),
            _make_block(2, "■ **RESULTS**"),
            _make_block(3, "result body"),
        ]
        toc = build_toc(blocks)
        out = render_toc(slug="test2026paper", toc=toc, total_blocks=4)
        # Header
        assert "# test2026paper — TOC (4 blocks, 2 sections)" in out
        # Section markers + ranges + counts
        assert "~0..1" in out
        assert "~2..3" in out
        assert "(2)" in out  # block count for each section
        assert "■ METHODS" in out
        assert "■ RESULTS" in out

    def test_subsection_indented(self) -> None:
        blocks = [
            _make_block(0, "■ **METHODS**"),
            _make_block(1, "method body"),
            _make_block(2, "**Calc**"),
            _make_block(3, "calc body"),
        ]
        toc = build_toc(blocks)
        out = render_toc(slug="x", toc=toc, total_blocks=4)
        # H2 rows are indented one level deeper than H1
        h1_line = next(line for line in out.splitlines() if "■ METHODS" in line)
        h2_line = next(
            line for line in out.splitlines() if "Calc" in line and "■" not in line
        )
        h1_indent = len(h1_line) - len(h1_line.lstrip())
        h2_indent = len(h2_line) - len(h2_line.lstrip())
        assert h2_indent > h1_indent

    def test_columns_align(self) -> None:
        """Ranges with different widths should still line up the count column."""
        blocks = [
            _make_block(0, "■ **A**"),  # short range
            _make_block(1, "a"),
            _make_block(2, "■ **B**"),
            *[_make_block(i, f"body {i}") for i in range(3, 200)],
        ]
        toc = build_toc(blocks)
        out = render_toc(slug="x", toc=toc, total_blocks=200)
        # The "(N)" count column should appear at the same column on
        # each top-level row.
        rows = [line for line in out.splitlines() if line.startswith("  ~")]
        count_positions = [r.index("(") for r in rows]
        assert len(set(count_positions)) == 1

    def test_drill_down_hint_when_multiple_sections(self) -> None:
        blocks = [
            _make_block(0, "■ **INTRO**"),
            _make_block(1, "i"),
            _make_block(10, "■ **METHODS**"),
            *[_make_block(i, "m") for i in range(11, 80)],
        ]
        toc = build_toc(blocks)
        out = render_toc(slug="x", toc=toc, total_blocks=80)
        # Largest section is METHODS (~10..79); hint should reference it.
        assert "Next:" in out
        assert "drill into METHODS" in out
        assert "~10..79/toc" in out

    def test_no_drill_hint_for_single_section(self) -> None:
        """Flat papers with one section don't get a drill-down hint."""
        blocks = [
            _make_block(0, "lorem ipsum"),
            _make_block(1, "dolor sit amet"),
        ]
        toc = build_toc(blocks)
        out = render_toc(slug="x", toc=toc, total_blocks=2)
        assert "drill into" not in out

    def test_range_label_in_header(self) -> None:
        blocks = [_make_block(i, f"body {i}") for i in range(5, 10)]
        toc = build_toc(blocks)
        out = render_toc(slug="x", toc=toc, total_blocks=100, range_label="~5..10")
        assert "~5..10" in out.splitlines()[0]

    def test_implicit_section_uses_preview(self) -> None:
        blocks = [
            _make_block(0, "Lorem ipsum dolor sit amet"),
            _make_block(1, "consectetur adipiscing"),
            _make_block(2, "■ **METHODS**"),
            _make_block(3, "method body"),
        ]
        toc = build_toc(blocks)
        out = render_toc(
            slug="x",
            toc=toc,
            total_blocks=4,
            blocks_by_pos={b.pos: b for b in blocks},
        )
        # The implicit leading section's row should include a preview
        # from block 0.
        assert "<untitled>" in out
        assert "Lorem ipsum" in out


# ── Sparse-fallback notice (no headings detected) ──────────────────


class TestSparseFallbackNotice:
    """Round-2 critic finding: a paper with no detectable headings used
    to render as a single useless TOC row labelled "<untitled>",
    silently misleading the agent into treating it as real structure.
    Render now prepends an explicit notice so the agent pivots to
    chunk-range reading.
    """

    def test_notice_present_when_no_headings_detected(self) -> None:
        """Pure body paragraphs → fallback notice + chunk-range hint."""
        blocks = [
            _make_block(i, f"body paragraph number {i}") for i in range(5)
        ]
        toc = build_toc(blocks)
        # Sanity: build_toc returned the implicit-untitled wrapper.
        assert len(toc) == 1
        assert toc[0].title == "" and toc[0].level == 0
        # Render should announce the fallback.
        out = render_toc(slug="opaque-paper", toc=toc, total_blocks=5)
        assert "no headings detected" in out
        assert "chunk range" in out

    def test_notice_absent_when_paper_has_real_headings(self) -> None:
        """A paper with even one real heading must NOT show the
        fallback notice — that would be a false-alarm and train the
        agent to ignore real structure cues."""
        blocks = [
            _make_block(0, "■ **INTRODUCTION**"),
            _make_block(1, "intro body"),
            _make_block(2, "■ **METHODS**"),
            _make_block(3, "methods body"),
        ]
        toc = build_toc(blocks)
        out = render_toc(slug="x", toc=toc, total_blocks=4)
        assert "no headings detected" not in out

    def test_notice_absent_when_paper_has_one_heading_plus_untitled_lead(
        self,
    ) -> None:
        """An implicit "untitled" leading section (blocks before the
        first heading) plus at least one real heading must not trigger
        the fallback. There IS structure — only some content is above
        the first heading."""
        blocks = [
            _make_block(0, "preamble"),
            _make_block(1, "■ **METHODS**"),
            _make_block(2, "methods body"),
        ]
        toc = build_toc(blocks)
        # The TOC has 2 sections (untitled + METHODS), so the
        # structural check rules out the sparse case.
        assert len(toc) == 2
        out = render_toc(slug="x", toc=toc, total_blocks=3)
        assert "no headings detected" not in out

    def test_notice_absent_on_drilled_down_single_section(self) -> None:
        """``filter_toc_to_range`` can leave a single section in the
        result when the agent drills into one specific section. That
        section came from a real heading so we do NOT show the
        fallback notice — the user is looking at structure, just
        narrowly scoped."""
        blocks = [
            _make_block(0, "■ **METHODS**"),
            _make_block(1, "methods body"),
            _make_block(2, "■ **RESULTS**"),
            _make_block(3, "results body"),
        ]
        full_toc = build_toc(blocks)
        # Drill into the METHODS range only.
        clipped = filter_toc_to_range(full_toc, lo=0, hi=1)
        assert len(clipped) == 1
        assert clipped[0].title == "METHODS"
        out = render_toc(
            slug="x", toc=clipped, total_blocks=4, range_label="~0..1"
        )
        assert "no headings detected" not in out

    def test_notice_includes_chunk_range_pivot_hint(self) -> None:
        """The notice must point the caller at a concrete recovery
        path (``Read by chunk range``) — a bare "no headings" message
        is information without a remedy."""
        blocks = [_make_block(i, f"body {i}") for i in range(3)]
        toc = build_toc(blocks)
        out = render_toc(slug="x", toc=toc, total_blocks=3)
        assert "Read by chunk range" in out
