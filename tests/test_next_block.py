"""Tests for `precis.utils.next_block` — TOON Next-block trailer.

Pure logic. No DB.

The helper used to render a column-aligned plaintext block
(``  get(x)  - do x``); since the cross-tool TOON refactor
(2026-05-xx) it emits a tab-separated D2 agent table with a
``{if you want to\texecute this call}`` header row. These
tests pin the current contract.
"""

from __future__ import annotations

from precis.utils.next_block import format_next_block, render_next_section

_HEADER = "{if you want to\texecute this call}"


class TestFormatNextBlock:
    def test_header_first(self) -> None:
        out = format_next_block([("get(x)", "do x")])
        assert out[0] == _HEADER

    def test_row_order_description_then_call(self) -> None:
        out = format_next_block([("get(x)", "do x")])
        assert out[1] == "do x\tget(x)"

    def test_multi_row(self) -> None:
        out = format_next_block([("short", "first"), ("a longer call here", "second")])
        assert out == [_HEADER, "first\tshort", "second\ta longer call here"]

    def test_empty_input(self) -> None:
        assert format_next_block([]) == []

    def test_indent_argument_ignored(self) -> None:
        """``indent`` is kept for legacy callsites but does not affect output."""
        a = format_next_block([("c", "d")])
        b = format_next_block([("c", "d")], indent="        ")
        assert a == b

    def test_descriptions_not_truncated(self) -> None:
        long_desc = "a very long description that should be preserved verbatim"
        out = format_next_block([("c", long_desc)])
        assert long_desc in out[1]

    def test_call_with_quotes_preserved(self) -> None:
        out = format_next_block(
            [("get(kind='paper', id='wang2020state~46..105/toc')", "drill")]
        )
        assert "'wang2020state~46..105/toc'" in out[1]


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
        # Header row is present.
        assert _HEADER in out

    def test_empty_returns_empty_string(self) -> None:
        # No header, no blank line — caller can unconditionally append.
        assert render_next_section([]) == ""

    def test_starts_with_blank_line(self) -> None:
        """Caller's body ends without a trailing newline; the rendered
        section must include its own leading blank line so the result
        joins cleanly when concatenated."""
        out = render_next_section([("c", "d")])
        assert out.startswith("\n")
