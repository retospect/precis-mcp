"""Unit tests for :func:`precis.format.toon.load`.

`load` is the inverse of `dump`: parse a TOON document back to a
list of dicts. Cells always come back as strings — TOON is a
transport, not a schema; type recovery is the caller's job.
"""

from __future__ import annotations

from precis.format.toon import load


class TestEmpty:
    def test_empty_string_returns_empty_list(self):
        assert load("") == []

    def test_only_whitespace_returns_empty_list(self):
        assert load("   \n  \n") == []

    def test_header_only_returns_empty_list(self):
        # A header with no data rows is a "no rows but schema known"
        # message; we return [] because there's no concrete row to
        # surface, but we don't crash.
        assert load("a\tb") == []


class TestBasicParse:
    def test_one_row(self):
        assert load("a\tb\n1\t2") == [{"a": "1", "b": "2"}]

    def test_multiple_rows(self):
        out = load("a\tb\n1\t2\n3\t4\n5\t6")
        assert out == [
            {"a": "1", "b": "2"},
            {"a": "3", "b": "4"},
            {"a": "5", "b": "6"},
        ]

    def test_returns_list_of_dicts(self):
        out = load("a\tb\n1\t2")
        assert isinstance(out, list)
        assert isinstance(out[0], dict)


class TestStringSemantics:
    def test_cells_are_strings(self):
        # No numeric coercion: "42" stays "42". Callers that want
        # an int call `int(...)` themselves.
        out = load("n\n42")
        assert out == [{"n": "42"}]
        assert isinstance(out[0]["n"], str)

    def test_empty_cell_renders_as_empty_string(self):
        # `dump` writes `None` as `""`; `load` reverses to `""`,
        # not `None`. Round-tripping a `None` loses the type;
        # acceptable for our use case (search-result rows treat
        # empty and missing as the same downstream).
        out = load("a\tb\n\tx")
        assert out == [{"a": "", "b": "x"}]


class TestQuoting:
    def test_quoted_cell_with_separator(self):
        out = load('a\tb\n"x\ty"\tz')
        assert out == [{"a": "x\ty", "b": "z"}]

    def test_quoted_cell_with_newline(self):
        out = load('a\n"line1\nline2"')
        assert out == [{"a": "line1\nline2"}]

    def test_doubled_quote_unescapes(self):
        out = load('a\n"he said ""hi"""')
        assert out == [{"a": 'he said "hi"'}]

    def test_quoted_empty_string(self):
        out = load('a\tb\n""\tx')
        assert out == [{"a": "", "b": "x"}]


class TestLineEndings:
    def test_trailing_newline_tolerated(self):
        # Pipes commonly append a final newline; `load` must not
        # treat it as a phantom empty row.
        out = load("a\tb\n1\t2\n")
        assert out == [{"a": "1", "b": "2"}]

    def test_multiple_trailing_newlines_tolerated(self):
        out = load("a\tb\n1\t2\n\n\n")
        assert out == [{"a": "1", "b": "2"}]

    def test_crlf_line_endings_tolerated(self):
        out = load("a\tb\r\n1\t2\r\n")
        assert out == [{"a": "1", "b": "2"}]


class TestHeaderKeys:
    def test_header_keys_with_quoting(self):
        # The header itself can be quoted to embed separators.
        out = load('"weird\tkey"\tb\nv\tw')
        assert out == [{"weird\tkey": "v", "b": "w"}]


class TestSeparatorOverride:
    def test_comma_separator(self):
        out = load("a,b\n1,2", sep=",")
        assert out == [{"a": "1", "b": "2"}]

    def test_comma_separator_respects_quoting(self):
        out = load('a,b\n"x,y",z', sep=",")
        assert out == [{"a": "x,y", "b": "z"}]
