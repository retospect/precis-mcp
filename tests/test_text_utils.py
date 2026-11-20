"""Tests for ``precis.utils.text``.

Three handlers (paper, markdown, patent) used to ship near-identical
``_excerpt`` helpers; this module documents the consolidated contract.
"""

from __future__ import annotations

import re

from precis.utils.text import excerpt, sci_anchor_pattern


class TestSciAnchorPattern:
    def test_no_scientific_notation_returns_none(self) -> None:
        assert sci_anchor_pattern("95") is None
        assert sci_anchor_pattern("9.6 ± 1.7") is None

    def test_unsigned_atom_expands_to_flattened_prints(self) -> None:
        # boundaries are the caller's lookarounds; delimit spans with spaces
        pattern = sci_anchor_pattern("5e12")
        assert pattern is not None
        for flattened in ("5 × 1012", "peak 5 × 10 12 w", "5e12 w"):
            assert re.search(pattern, flattened) is not None
        for other in ("5 × 1011", "5 × 10^11"):
            assert re.search(pattern, other) is None

    def test_mantissa_one_also_gets_bare_prints(self) -> None:
        pattern = sci_anchor_pattern("1e11-1e12")
        assert pattern is not None
        assert re.search(pattern, "fluxes of 1011-1012 w") is not None
        assert re.search(pattern, "fluxes of 10 11-10 12 w") is not None

    def test_fractional_mantissa_gets_no_bare_prints(self) -> None:
        pattern = sci_anchor_pattern("1.3e11")
        assert pattern is not None
        assert re.search(pattern, "at 1.3 × 1011 cm-2") is not None
        # a bare flattened print carries no mantissa; it cannot be 1.3 × 10^11
        assert re.search(pattern, "at 1011 cm-2") is None

    def test_signed_atom_is_left_verbatim(self) -> None:
        # signed prints (× 10-3) already fold to the e-form on the span side
        assert sci_anchor_pattern("2e-3") is None

    def test_flattened_literal_folds_to_the_e_form(self) -> None:
        # the caller squashes the span first, so the glyph print is already
        # the e-form at search time
        pattern = sci_anchor_pattern("5 × 1012")
        assert pattern is not None
        assert re.search(pattern, "peak of 5e12 w") is not None
        assert re.search(pattern, "peak of 5 × 10 12 w") is not None


class TestExcerpt:
    def test_short_passes_through(self) -> None:
        assert excerpt("hello world", limit=80) == "hello world"

    def test_empty_input(self) -> None:
        assert excerpt("", limit=10) == ""

    def test_collapses_whitespace(self) -> None:
        assert excerpt("hello    world\n\nfoo", limit=80) == "hello world foo"

    def test_truncates_with_ellipsis(self) -> None:
        out = excerpt("a" * 50 + " " + "b" * 50, limit=30)
        assert out.endswith("…")
        # Ellipsis is appended only when the text was actually shortened.
        assert len(out) <= 31  # 30 chars + ellipsis

    def test_snaps_to_word_boundary(self) -> None:
        text = "the quick brown fox jumps over the lazy dog"
        out = excerpt(text, limit=18)
        # 18-char prefix is "the quick brown fo"; nearest space at index 15
        # → output is "the quick brown" + "…"
        assert out == "the quick brown…"

    def test_no_word_boundary_falls_back_to_hard_cut(self) -> None:
        # No spaces — long URL / hash / token. Old paper-side helper
        # would have returned just "…"; the new helper preserves the
        # head of the string.
        url = "https://example.com/" + "x" * 200
        out = excerpt(url, limit=40)
        assert out.startswith("https://example.com/")
        assert out.endswith("…")
        assert len(out) == 41  # 40 chars + ellipsis

    def test_idempotent_on_collapsed_input(self) -> None:
        # Running excerpt twice should be a no-op once the first call
        # already collapsed whitespace and trimmed to limit.
        once = excerpt("hello   world", limit=80)
        twice = excerpt(once, limit=80)
        assert once == twice == "hello world"

    def test_custom_ellipsis(self) -> None:
        assert excerpt("a b c d e f g", limit=5, ellipsis="...") == "a b...".replace(
            "...", "..."
        )
