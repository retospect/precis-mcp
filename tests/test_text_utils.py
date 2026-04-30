"""Tests for ``precis.utils.text``.

Three handlers (paper, markdown, patent) used to ship near-identical
``_excerpt`` helpers; this module documents the consolidated contract.
"""

from __future__ import annotations

from precis.utils.text import excerpt


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
