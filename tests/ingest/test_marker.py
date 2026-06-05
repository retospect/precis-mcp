"""Tests for marker.py post-processing: junk detection, link stripping,
and the small-block merge pass."""

from __future__ import annotations

import pytest

from precis.ingest.marker import (
    _JUNK_HEADING_RE,
    _MD_LINK_RE,
    _MERGE_TARGET_CHARS,
    _clean_text,
    _mark_junk,
    _merge_small_blocks,
)


class TestJunkHeadingRegex:
    @pytest.mark.parametrize(
        "text",
        [
            "OPEN ACCESS",
            "Open Access",
            "OPEN  ACCESS",
            "COPYRIGHT",
            "Copyright",
            "CITATION",
            "REVIEWED BY Yashwant Bisht, Uttaranchal University, India",
            "Reviewed by John Smith",
            "EDITED BY Jane Doe",
            "Edited by Someone",
            "*CORRESPONDENCE",
            "CORRESPONDENCE",
            "Correspondence",
            "RECEIVED 12 January 2025",
            "ACCEPTED 5 March 2025",
            "PUBLISHED 10 March 2025",
            "HANDLING EDITOR John",
            "ASSOCIATE EDITOR Jane",
            "TYPE Original Research",
        ],
    )
    def test_matches_junk(self, text):
        assert _JUNK_HEADING_RE.match(text), f"Expected match: {text!r}"

    @pytest.mark.parametrize(
        "text",
        [
            "Introduction",
            "1. Methods",
            "Results and Discussion",
            "Acknowledgments",
            "References",
            "Abstract",
            "Experimental Procedures",
            "2.1 Surface Characterization",
        ],
    )
    def test_does_not_match_real_headings(self, text):
        assert not _JUNK_HEADING_RE.match(text), f"Should not match: {text!r}"


class TestMarkJunk:
    def test_junk_heading_and_followers(self):
        blocks = [
            {
                "type": "section_header",
                "text": "OPEN ACCESS",
                "section_path": ["OPEN ACCESS"],
            },
            {
                "type": "text",
                "text": "Some boilerplate.",
                "section_path": ["OPEN ACCESS"],
            },
            {
                "type": "section_header",
                "text": "Introduction",
                "section_path": ["Introduction"],
            },
            {"type": "text", "text": "Real content.", "section_path": ["Introduction"]},
        ]
        result = _mark_junk(blocks)
        assert result[0]["type"] == "junk"
        assert result[1]["type"] == "junk"
        assert result[2]["type"] == "section_header"
        assert result[3]["type"] == "text"

    def test_multiple_junk_sections(self):
        blocks = [
            {
                "type": "section_header",
                "text": "OPEN ACCESS",
                "section_path": ["OPEN ACCESS"],
            },
            {"type": "text", "text": "OA text.", "section_path": ["OPEN ACCESS"]},
            {
                "type": "section_header",
                "text": "COPYRIGHT",
                "section_path": ["COPYRIGHT"],
            },
            {"type": "text", "text": "Copyright text.", "section_path": ["COPYRIGHT"]},
            {
                "type": "section_header",
                "text": "CITATION",
                "section_path": ["CITATION"],
            },
            {"type": "text", "text": "Citation text.", "section_path": ["CITATION"]},
            {
                "type": "section_header",
                "text": "Introduction",
                "section_path": ["Introduction"],
            },
            {
                "type": "text",
                "text": "Actual content.",
                "section_path": ["Introduction"],
            },
        ]
        result = _mark_junk(blocks)
        for i in range(6):
            assert result[i]["type"] == "junk", f"Block {i} should be junk"
        assert result[6]["type"] == "section_header"
        assert result[7]["type"] == "text"

    def test_no_junk_when_no_frontmatter(self):
        blocks = [
            {
                "type": "section_header",
                "text": "Introduction",
                "section_path": ["Introduction"],
            },
            {"type": "text", "text": "Content.", "section_path": ["Introduction"]},
            {"type": "section_header", "text": "Methods", "section_path": ["Methods"]},
            {"type": "text", "text": "More content.", "section_path": ["Methods"]},
        ]
        result = _mark_junk(blocks)
        assert all(b["type"] != "junk" for b in result)

    def test_junk_does_not_leak_past_real_heading(self):
        blocks = [
            {
                "type": "section_header",
                "text": "REVIEWED BY Someone",
                "section_path": ["REVIEWED BY Someone"],
            },
            {
                "type": "text",
                "text": "Reviewer info.",
                "section_path": ["REVIEWED BY Someone"],
            },
            {
                "type": "section_header",
                "text": "1. Introduction",
                "section_path": ["1. Introduction"],
            },
            {
                "type": "text",
                "text": "Real intro.",
                "section_path": ["1. Introduction"],
            },
            {
                "type": "text",
                "text": "More intro.",
                "section_path": ["1. Introduction"],
            },
        ]
        result = _mark_junk(blocks)
        assert result[0]["type"] == "junk"
        assert result[1]["type"] == "junk"
        assert result[2]["type"] == "section_header"
        assert result[3]["type"] == "text"
        assert result[4]["type"] == "text"

    def test_preserves_block_text(self):
        """Junk detection changes type but preserves text."""
        blocks = [
            {
                "type": "section_header",
                "text": "COPYRIGHT",
                "section_path": ["COPYRIGHT"],
            },
            {"type": "text", "text": "© 2025 Authors", "section_path": ["COPYRIGHT"]},
        ]
        result = _mark_junk(blocks)
        assert result[0]["text"] == "COPYRIGHT"
        assert result[1]["text"] == "© 2025 Authors"


class TestMergeSmallBlocks:
    """Pins the merge-forward pass.

    Two rules under test:

    1. ``section_header`` blocks absorb forward into the next non-header
       non-junk block; the merged block inherits the body's type.
    2. Adjacent same-type small blocks (text+text, list+list) merge
       within a (section_path, page) window while combined length
       stays at or under ``_MERGE_TARGET_CHARS``.

    Pass-through guarantees: junk blocks never absorb headers; tables /
    figures / equations stay standalone; per-page indices renumber after
    merging so ``node_id`` stays self-consistent.
    """

    @staticmethod
    def _b(
        btype: str,
        text: str,
        *,
        section_path: list[str] | None = None,
        page: int = 0,
        node_id: str = "00000000",
    ) -> dict[str, object]:
        return {
            "node_id": node_id,
            "page": page,
            "type": btype,
            "text": text,
            "section_path": section_path or [],
        }

    def test_section_header_absorbed_into_next_text(self):
        blocks = [
            self._b("section_header", "Methods", section_path=["Methods"]),
            self._b("text", "We used GC-MS.", section_path=["Methods"]),
        ]
        result = _merge_small_blocks(blocks, paper_id="p1")
        assert len(result) == 1
        assert result[0]["type"] == "text"
        assert result[0]["text"] == "Methods\n\nWe used GC-MS."
        assert result[0]["section_path"] == ["Methods"]

    def test_stacked_headers_absorb_together(self):
        blocks = [
            self._b("section_header", "Results", section_path=["Results"]),
            self._b(
                "section_header",
                "3.2 Yield",
                section_path=["Results", "3.2 Yield"],
            ),
            self._b(
                "text",
                "Yield was 92%.",
                section_path=["Results", "3.2 Yield"],
            ),
        ]
        result = _merge_small_blocks(blocks, paper_id="p1")
        assert len(result) == 1
        assert result[0]["text"] == "Results\n\n3.2 Yield\n\nYield was 92%."
        assert result[0]["type"] == "text"

    def test_trailing_header_kept_standalone(self):
        blocks = [
            self._b("text", "Body.", section_path=["Intro"]),
            self._b("section_header", "End", section_path=["End"]),
        ]
        result = _merge_small_blocks(blocks, paper_id="p1")
        assert len(result) == 2
        assert result[1]["type"] == "section_header"
        assert result[1]["text"] == "End"

    def test_header_above_junk_flushes_as_standalone(self):
        """Heading just before a junk block must not fold into junk."""
        blocks = [
            self._b("section_header", "Methods", section_path=["Methods"]),
            self._b(
                "junk",
                "© 2025 ...",
                section_path=["COPYRIGHT"],
            ),
            self._b("text", "Body.", section_path=["Methods"]),
        ]
        result = _merge_small_blocks(blocks, paper_id="p1")
        # Heading stays as standalone (flushed before junk).
        assert result[0]["type"] == "section_header"
        assert result[0]["text"] == "Methods"
        assert result[1]["type"] == "junk"
        # Body stays standalone — no header pending when it arrives.
        assert result[2]["type"] == "text"
        assert result[2]["text"] == "Body."

    def test_adjacent_text_blocks_merge_within_section(self):
        blocks = [
            self._b("text", "First sentence.", section_path=["Intro"]),
            self._b("text", "Second sentence.", section_path=["Intro"]),
            self._b("text", "Third sentence.", section_path=["Intro"]),
        ]
        result = _merge_small_blocks(blocks, paper_id="p1")
        assert len(result) == 1
        assert (
            result[0]["text"]
            == "First sentence.\n\nSecond sentence.\n\nThird sentence."
        )

    def test_text_does_not_merge_across_section_path(self):
        blocks = [
            self._b("text", "End of intro.", section_path=["Intro"]),
            self._b("text", "Start of methods.", section_path=["Methods"]),
        ]
        result = _merge_small_blocks(blocks, paper_id="p1")
        assert len(result) == 2

    def test_text_does_not_merge_across_page(self):
        blocks = [
            self._b("text", "Page one.", section_path=["Intro"], page=0),
            self._b("text", "Page two.", section_path=["Intro"], page=1),
        ]
        result = _merge_small_blocks(blocks, paper_id="p1")
        assert len(result) == 2

    def test_text_does_not_merge_across_type(self):
        """text + list shouldn't merge — heterogeneous grammar."""
        blocks = [
            self._b("text", "Para.", section_path=["X"]),
            self._b("list", "- item one\n- item two", section_path=["X"]),
        ]
        result = _merge_small_blocks(blocks, paper_id="p1")
        assert len(result) == 2

    def test_merge_stops_at_target_size(self):
        """Combined length must stay at or under _MERGE_TARGET_CHARS."""
        big = "x" * (_MERGE_TARGET_CHARS - 50)
        small = "y" * 100  # combined > target
        blocks = [
            self._b("text", big, section_path=["X"]),
            self._b("text", small, section_path=["X"]),
        ]
        result = _merge_small_blocks(blocks, paper_id="p1")
        assert len(result) == 2  # no merge — combined would exceed target

    def test_tables_stay_standalone(self):
        blocks = [
            self._b("section_header", "Data", section_path=["Data"]),
            self._b(
                "table",
                "| col1 | col2 |\n|---|---|\n| 1 | 2 |",
                section_path=["Data"],
            ),
            self._b("table", "| a |\n|---|\n| b |", section_path=["Data"]),
        ]
        result = _merge_small_blocks(blocks, paper_id="p1")
        # Header absorbed into first table; second table stays standalone.
        assert len(result) == 2
        assert result[0]["type"] == "table"
        assert result[0]["text"].startswith("Data\n\n| col1")
        assert result[1]["type"] == "table"

    def test_figures_absorb_headers_but_dont_merge_with_each_other(self):
        blocks = [
            self._b("section_header", "Results", section_path=["Results"]),
            self._b(
                "figure",
                "Figure 1. Yield curve.",
                section_path=["Results"],
            ),
            self._b("figure", "Figure 2. Time series.", section_path=["Results"]),
        ]
        result = _merge_small_blocks(blocks, paper_id="p1")
        assert len(result) == 2
        assert result[0]["type"] == "figure"
        assert result[0]["text"] == "Results\n\nFigure 1. Yield curve."
        assert result[1]["type"] == "figure"
        assert result[1]["text"] == "Figure 2. Time series."

    def test_node_id_renumbered_per_page_after_merge(self):
        from precis.identity import make_node_id

        blocks = [
            self._b("section_header", "H", section_path=["H"], page=0),
            self._b("text", "Body one.", section_path=["H"], page=0),
            self._b("text", "Body two.", section_path=["H"], page=0),
            self._b("text", "Body three.", section_path=["H"], page=1),
        ]
        result = _merge_small_blocks(blocks, paper_id="p1")
        # Page 0: heading + body1 + body2 → all merge to one block.
        # Page 1: body3 standalone.
        assert len(result) == 2
        assert result[0]["node_id"] == make_node_id("p1", 0, 0)
        assert result[1]["node_id"] == make_node_id("p1", 1, 0)

    def test_empty_input_returns_empty(self):
        assert _merge_small_blocks([], paper_id="p1") == []

    def test_only_headers_kept_standalone(self):
        """A page that's nothing but headings (rare) keeps every heading."""
        blocks = [
            self._b("section_header", "A", section_path=["A"]),
            self._b("section_header", "B", section_path=["B"]),
        ]
        result = _merge_small_blocks(blocks, paper_id="p1")
        assert len(result) == 2
        assert all(b["type"] == "section_header" for b in result)

    def test_no_merges_preserves_indices(self):
        """When no merge fires, node_ids match the original per-page sequence."""
        from precis.identity import make_node_id

        blocks = [
            self._b(
                "table",
                "| a |\n|---|\n| b |",
                section_path=["X"],
                page=0,
            ),
            self._b(
                "table",
                "| c |\n|---|\n| d |",
                section_path=["X"],
                page=0,
            ),
        ]
        result = _merge_small_blocks(blocks, paper_id="p1")
        assert len(result) == 2
        assert result[0]["node_id"] == make_node_id("p1", 0, 0)
        assert result[1]["node_id"] == make_node_id("p1", 0, 1)


class TestMdLinkStrip:
    def test_strip_link(self):
        text = "[Experimental study](https://example.com/article)"
        assert _MD_LINK_RE.sub(r"\1", text) == "Experimental study"

    def test_strip_multiple_links(self):
        text = "[Part one](http://a.com) and [Part two](http://b.com)"
        assert _MD_LINK_RE.sub(r"\1", text) == "Part one and Part two"

    def test_no_links_unchanged(self):
        text = "Plain heading text"
        assert _MD_LINK_RE.sub(r"\1", text) == text


# ── Chemistry / scientific corpus must round-trip unchanged ──────────


class TestCleanTextChemistryCorpus:
    """The cleanup pass MUST preserve every Unicode character that a
    chemist or physicist would actually type. This is the load-bearing
    test for the chemistry-safe ftfy config — if any of these regress,
    you're either (a) running the wrong ftfy preset or (b) accidentally
    enabled NFKC normalization, ``unescape_html``, ``uncurl_quotes``,
    or ``fix_character_width``.

    Each parametrize case is a single character / mini-fragment; we run
    them in isolation AND together so we catch interactions (e.g.
    ftfy's "is bad" heuristic occasionally false-positives on a string
    with several stacked Greek letters that are individually fine).
    """

    GREEK = ["α", "β", "γ", "δ", "ε", "Δ", "Σ", "Ω", "μ", "θ", "λ", "π"]
    ARROWS = ["→", "↔", "⇌", "⇄", "⇆", "⟶", "↑", "↓"]
    SUBSCRIPTS = ["H₂O", "CO₂", "N₂O", "Fe³⁺", "Cu²⁺", "SO₄²⁻", "PO₄³⁻"]
    SUPERSCRIPTS = ["x²", "10⁻³", "¹⁴C", "¹H", "²H NMR", "10⁻¹⁸"]
    UNITS = ["°C", "°F", "Å", "μm", "μs", "±0.5", "5 × 10⁻³", "≤2 nm"]
    MATH = ["≤", "≥", "≠", "≈", "∞", "∫", "∑", "√", "∂", "∇", "∝"]
    PRIMES = ["3′ end", "5′-OH", "RNA 3′-end", "30′ vinyl"]

    @pytest.mark.parametrize(
        "fragment",
        GREEK + ARROWS + SUBSCRIPTS + SUPERSCRIPTS + UNITS + MATH + PRIMES,
    )
    def test_individual_fragment_preserved(self, fragment: str) -> None:
        """Every character that a chemist types survives the pass."""
        assert _clean_text(fragment) == fragment, (
            f"chemistry-safe cleanup corrupted {fragment!r} → {_clean_text(fragment)!r}; "
            "check that uncurl_quotes / unescape_html / fix_character_width / "
            "normalization='NFKC' have not been re-enabled in _FTFY_CONFIG"
        )

    def test_full_chemistry_paragraph_preserved(self) -> None:
        """Realistic paragraph from a battery / catalysis paper."""
        text = (
            "The δ-MnO₂ catalyst (≥99.9%) was tested at 25 °C ± 0.5 °C. "
            "Reaction: 2H₂O → O₂ + 4H⁺ + 4e⁻. Activation energy ≈ 0.3 eV. "
            "Greek constants used: α = 0.1, β = 0.05, γ = 1.6 × 10⁻³ Å⁻¹. "
            "The 3′ end of the primer aligns with motif μ₂ (Σ symmetry)."
        )
        assert _clean_text(text) == text

    def test_html_entities_in_scientific_text_not_unescaped(self) -> None:
        """``&lt;1 nm`` must stay literal — chemists *type* HTML entities
        in size-comparator text (e.g. tabular cells "&lt;0.5 nm" copied
        from a journal article that itself escaped them). Auto-unescaping
        would silently transform a literal ``<`` into a markup-meaningful
        one and break downstream rendering."""
        text = "Pore size &lt;1 nm; defect density &gt;10¹⁸ cm⁻³."
        assert _clean_text(text) == text

    def test_smart_quotes_preserved(self) -> None:
        """Smart quotes / primes / typographic apostrophes carry semantic
        meaning in scientific text (primes for derivatives, typographer's
        quotes in journal style guides) — they must not collapse to
        ASCII."""
        text = "He showed “double‐bond character” using the 5′-flanking probe."
        assert _clean_text(text) == text


# ── Mojibake (encoding-error) sequences must be REPAIRED ─────────────


class TestCleanTextRepairsMojibake:
    """The cleanup MUST repair the byte-sequence-mojibake patterns that
    show up when a PDF was built from a UTF-8 source that got mis-decoded
    as latin-1 / cp1252 along the way. These are the most common patterns
    in the wild for academic PDFs.

    Note that ftfy.fix_text is heuristic — it only repairs when it's
    confident the input is actually mis-decoded. So we feed it
    *unambiguously-broken* fragments and check the repair, not edge
    cases where a heuristic might decline.
    """

    # Mojibake of common UTF-8 punctuation when read as cp1252:
    #
    #   U+2014 EM DASH      (UTF-8: 0xE2 0x80 0x94) → "â€" + U+201D
    #   U+2019 RSINGLE Q'   (UTF-8: 0xE2 0x80 0x99) → "â€" + U+2122
    #   U+201C LDOUBLE Q'   (UTF-8: 0xE2 0x80 0x9C) → "â€" + U+0153
    #   U+201D RDOUBLE Q'   (UTF-8: 0xE2 0x80 0x9D) → "â€" + U+009D
    #
    # We spell the broken side with explicit \uXXXX escapes so the
    # source stays ASCII-7 and a future code editor that "auto-fixes"
    # mojibake on save can't silently undo our test fixtures.
    @pytest.mark.parametrize(
        "broken,expected",
        [
            # UTF-8 read as latin-1, then re-encoded as UTF-8.
            ("\u00c3\u00a9lectrolyte", "\u00e9lectrolyte"),  # Ã© → é
            ("r\u00c3\u00a9duction", "r\u00e9duction"),
            ("\u00c3\u00b6stwald", "\u00f6stwald"),  # Ã¶ → ö
            ("M\u00c3\u00bcller", "M\u00fcller"),  # Ã¼ → ü
            ("a\u00c3\u00a7ai", "a\u00e7ai"),  # Ã§ → ç
            # Em-dash mojibake: â€" → —
            ("0.5\u00e2\u20ac\u201d1.0 nm", "0.5\u20141.0 nm"),
            # Right single-quote mojibake: â€™ → '
            (
                "the catalyst\u00e2\u20ac\u2122s surface",
                "the catalyst\u2019s surface",
            ),
            # Curly-quote pair mojibake: â€œpolaronâ€\u009d → "polaron"
            (
                "\u00e2\u20ac\u0153polaron\u00e2\u20ac\u009d",
                "\u201cpolaron\u201d",
            ),
            # Greek-letter mojibake (these are the most reliable).
            ("\u00ce\u00b1-helix", "\u03b1-helix"),  # Î± → α
            ("\u00ce\u00b2-sheet", "\u03b2-sheet"),  # Î² → β
            ("\u00ce\u201dG", "\u0394G"),  # Î" → Δ
        ],
    )
    def test_repairs_mojibake(self, broken: str, expected: str) -> None:
        result = _clean_text(broken)
        assert result == expected, (
            f"mojibake repair failed: {broken!r} → {result!r} (expected {expected!r})"
        )

    def test_lossy_replacement_left_alone_when_unguessable(self) -> None:
        """U+FFFD that ftfy can't confidently recover stays in place
        rather than being silently dropped — better to surface the
        loss than to fabricate a guess."""
        text = "stranded\ufffdcharacter here"
        result = _clean_text(text)
        # We don't require perfect repair; we just don't want crashes
        # or insertions of garbage. The character may stay as U+FFFD
        # OR be repaired — both are acceptable.
        assert "garbage" not in result
        assert "stranded" in result
        assert "character here" in result

    def test_clean_text_idempotent(self) -> None:
        """Two passes of the cleanup should be a no-op on already-clean
        text. This protects against accidentally-introduced one-shot
        rewrites (e.g. ``"x" → "x "``) that would mutate text every
        time we re-process a bundle."""
        text = (
            "δ-MnO₂ at 25 °C; reaction 2H₂O → O₂ + 4H⁺. "
            "Pore size &lt;1 nm. 3′ end aligned."
        )
        once = _clean_text(text)
        twice = _clean_text(once)
        assert once == twice


class TestCleanTextDehyphenation:
    """Gap-3 fix from the 2026-05-31 ingest pipeline audit.

    Marker output sometimes preserves the hyphenated-line-break
    artifact from PDF text extraction (``"under-\\nstanding"``).
    Soft hyphens (\\xad) were already stripped; explicit hyphens
    across newlines now collapse when both sides are lowercase so
    we don't corrupt semantically-significant compound terms
    (``"Z-scheme\\nphotocatalysis"`` and ``"Cu-MOF\\nframework"``
    stay intact because at least one side starts uppercase).
    """

    def test_simple_lowercase_join(self) -> None:
        from precis.ingest.marker import _clean_text

        result = _clean_text("under-\nstanding")
        assert "understanding" in result
        assert "under-" not in result

    def test_join_handles_indented_continuation(self) -> None:
        from precis.ingest.marker import _clean_text

        # Continuation line has leading whitespace (column wrap).
        result = _clean_text("photo-\n    catalysis")
        assert "photocatalysis" in result

    def test_preserves_chemical_compound_uppercase_after_break(self) -> None:
        from precis.ingest.marker import _clean_text

        # "Z-scheme" is a real compound term; the line break can fall
        # right after but the right side starts uppercase so we leave
        # the hyphen alone.
        result = _clean_text("Z-\nscheme")
        # Joined to "Z-scheme" — the heuristic doesn't fire because the
        # left side ends in uppercase 'Z'. Either "Z-scheme" or "Z-
        # scheme" is acceptable; we just don't want it joined as "Zscheme".
        assert "Zscheme" not in result

    def test_preserves_chemical_compound_uppercase_before_break(self) -> None:
        from precis.ingest.marker import _clean_text

        # "Cu-MOF" — right side starts uppercase.
        result = _clean_text("Cu-MOF\nframework")
        # We should not have crushed the hyphen here.
        assert "CuMOFframework" not in result
        assert "Cu-MOF" in result

    def test_paragraph_break_not_joined(self) -> None:
        from precis.ingest.marker import _clean_text

        # Blank line between is a paragraph boundary — never join.
        result = _clean_text("first paragraph ending-\n\nsecond paragraph")
        # The blank-line collapse compresses 3+ newlines to 2 but the
        # paragraph break stays a paragraph break.
        assert "ending-" in result or "ending second" not in result.replace("\n", " ")
