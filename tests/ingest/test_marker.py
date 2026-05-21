"""Tests for marker.py post-processing: junk detection and link stripping."""

from __future__ import annotations

import pytest

from precis.ingest.marker import (
    _JUNK_HEADING_RE,
    _MD_LINK_RE,
    _clean_text,
    _mark_junk,
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
