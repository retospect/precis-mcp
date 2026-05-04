"""Tests for `precis.utils.next_block` — column-aligned "Next:" trailers.

Pure logic. No DB.
"""

from __future__ import annotations

from precis.utils.next_block import format_next_block, render_next_section


class TestFormatNextBlock:
    def test_basic_alignment(self) -> None:
        out = format_next_block(
            [
                ("short", "first"),
                ("a longer call here", "second"),
            ]
        )
        assert len(out) == 2
        # Both lines should put their separator at the same column.
        sep_positions = [line.index(" - ") for line in out]
        assert sep_positions[0] == sep_positions[1]

    def test_indent_default(self) -> None:
        out = format_next_block([("c", "d")])
        assert out[0].startswith("  ")  # default 2-space indent

    def test_indent_override(self) -> None:
        out = format_next_block([("c", "d")], indent="    ")
        assert out[0].startswith("    c")

    def test_separator_is_ascii_hyphen(self) -> None:
        """Renamed from ``test_em_dash_present`` 2026-05-04: separator
        is ASCII ``-`` (not em-dash) for tokeniser safety on small
        models. See :func:`format_next_block` for the rationale.
        """
        out = format_next_block([("get(x)", "describe")])
        assert " - " in out[0]
        assert "—" not in out[0]
        assert " describe" in out[0]

    def test_empty_input(self) -> None:
        assert format_next_block([]) == []

    def test_single_entry(self) -> None:
        out = format_next_block([("get(x)", "do x")])
        assert out == ["  get(x)  - do x"]

    def test_descriptions_not_truncated(self) -> None:
        long_desc = "a very long description that should be preserved verbatim"
        out = format_next_block([("c", long_desc)])
        assert long_desc in out[0]

    def test_call_with_quotes_preserved(self) -> None:
        out = format_next_block(
            [("get(kind='paper', id='wang2020state~46..105/toc')", "drill")]
        )
        assert "'wang2020state~46..105/toc'" in out[0]


class TestRenderNextSection:
    def test_full_section(self) -> None:
        out = render_next_section(
            [
                ("get(kind='paper', id='X~46..105/toc')", "drill into theory"),
                ("get(kind='paper', id='X', view='bibtex')", "BibTeX citation"),
            ]
        )
        assert "\nNext:\n" in out
        assert "drill into theory" in out
        assert "BibTeX citation" in out

    def test_empty_returns_empty_string(self) -> None:
        # No header, no blank line — caller can unconditionally append.
        assert render_next_section([]) == ""

    def test_starts_with_blank_line(self) -> None:
        """Caller's body ends without a trailing newline; the rendered
        section must include its own leading blank line so the result
        joins cleanly when concatenated."""
        out = render_next_section([("c", "d")])
        assert out.startswith("\n")
