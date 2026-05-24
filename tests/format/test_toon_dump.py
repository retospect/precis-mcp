"""Unit tests for :func:`precis.format.toon.dump`.

The TOON shape we emit is the flat homogeneous-rows form documented
in `docs/design/b10-toon-output.md` §"Format spec":

    col1<TAB>col2<TAB>col3
    val1<TAB>val2<TAB>val3
    val1<TAB>val2<TAB>val3

These tests pin the exact serialisation contract — header rendering,
column ordering, escape semantics, scalar coercion, and separator
override. They do not exercise the registry or CLI integration;
those live in sibling test files.
"""

from __future__ import annotations

import math

import pytest

from precis.format.toon import dump


class TestEmpty:
    def test_empty_list_renders_empty_string(self):
        assert dump([]) == ""

    def test_empty_list_with_explicit_schema_renders_header_only(self):
        # An empty list with a pinned schema is still meaningful: the
        # caller is declaring the column shape even when no rows exist.
        # Useful for "search returned 0 hits" responses that should
        # still teach the agent the column names of a future hit.
        assert dump([], schema=["a", "b"]) == "a\tb"


class TestHeaderAndRows:
    def test_single_row_emits_header_and_one_data_row(self):
        out = dump([{"a": "1", "b": "2"}])
        assert out == "a\tb\n1\t2"

    def test_multiple_rows_emit_header_and_one_line_each(self):
        out = dump(
            [
                {"a": "1", "b": "2"},
                {"a": "3", "b": "4"},
                {"a": "5", "b": "6"},
            ]
        )
        assert out == "a\tb\n1\t2\n3\t4\n5\t6"

    def test_dict_argument_treated_as_one_row_table(self):
        # ADR 0002 lists JSON as the canonical single-record format,
        # but `dump` accepting a bare dict as syntactic sugar avoids
        # forcing every caller to wrap in `[...]`. The shape is the
        # same as a one-row list.
        out = dump({"x": "1", "y": "2"})
        assert out == "x\ty\n1\t2"

    def test_no_trailing_newline(self):
        # Tight output keeps the token count predictable. Callers
        # that need a trailing newline (e.g. `print`) add it
        # themselves; the serialiser never does.
        out = dump([{"a": "1"}])
        assert not out.endswith("\n")


class TestColumnOrdering:
    def test_default_order_is_first_seen_keys(self):
        # Insertion order of the first row drives the header. New
        # keys appearing in later rows append in first-seen order.
        out = dump(
            [
                {"z": "1", "a": "2"},
                {"a": "3", "z": "4", "m": "5"},
            ]
        )
        header = out.splitlines()[0]
        assert header.split("\t") == ["z", "a", "m"]

    def test_explicit_schema_pins_order(self):
        out = dump(
            [{"b": "2", "a": "1", "c": "3"}],
            schema=["a", "b", "c"],
        )
        assert out == "a\tb\tc\n1\t2\t3"

    def test_schema_omits_unlisted_columns(self):
        # A pinned schema is *the* column list; any keys absent from
        # it are dropped. Lets callers project a wider row down to
        # the columns they want to expose.
        out = dump(
            [{"a": "1", "b": "2", "secret": "shh"}],
            schema=["a", "b"],
        )
        assert "secret" not in out
        assert "shh" not in out

    def test_schema_pads_missing_keys_as_empty(self):
        # The reverse case: schema lists a column the row lacks.
        # Render as empty cell so the column shape stays uniform.
        out = dump([{"a": "1"}], schema=["a", "b"])
        assert out == "a\tb\n1\t"


class TestScalarEncoding:
    def test_none_renders_as_empty_cell(self):
        out = dump([{"a": None, "b": "x"}])
        assert out == "a\tb\n\tx"

    def test_true_renders_lowercase(self):
        assert dump([{"a": True}]) == "a\ntrue"

    def test_false_renders_lowercase(self):
        assert dump([{"a": False}]) == "a\nfalse"

    def test_integer_renders_with_str(self):
        assert dump([{"a": 42}]) == "a\n42"

    def test_negative_integer(self):
        assert dump([{"a": -7}]) == "a\n-7"

    def test_float_round_trips_via_repr(self):
        # `repr` keeps "1.0" rather than collapsing to "1" and gives
        # full precision for irrational values.
        assert dump([{"a": 1.0}]) == "a\n1.0"
        out = dump([{"a": math.pi}])
        assert out.endswith(repr(math.pi))

    def test_non_string_non_scalar_stringifies(self):
        # Any value that isn't `None`, `bool`, `int`, `float`, or
        # `str` falls back to `str(value)` — this catches Path
        # objects, datetimes, custom dataclasses, and the like.
        from pathlib import PurePosixPath

        path = PurePosixPath("/tmp/x")
        out = dump([{"a": path}])
        assert out == f"a\n{path!s}"


class TestEscapeRules:
    """The dump-side quoting rules.

    The audience for our TOON output is an LLM, not a parser. We
    only wrap cells in ``"..."`` when the wrapper is *strictly
    necessary* to preserve the columnar shape — i.e. when the cell
    contains the separator, a newline, or a CR. Bare double quotes
    pass through verbatim because they don't break the column
    structure: an LLM reads ``He said X`` (with literal quotes
    around X) more easily than the RFC 4180 escape with doubled
    inner quotes wrapped in an outer pair.
    """

    def test_cell_with_separator_is_quoted(self):
        out = dump([{"a": "ab\tcd"}])
        assert out == 'a\n"ab\tcd"'

    def test_cell_with_newline_is_quoted(self):
        out = dump([{"a": "line1\nline2"}])
        assert out == 'a\n"line1\nline2"'

    def test_cell_with_double_quote_passes_through_literally(self):
        # An LLM reads bare quotes inside cells without confusion;
        # the column structure is tab-delimited, not quote-delimited.
        # Token-cheaper than wrapping + ``""``-escaping.
        out = dump([{"a": 'he said "hi"'}])
        assert out == 'a\nhe said "hi"'

    def test_cell_starting_with_double_quote_passes_through(self):
        # Cells that *start* with a quote could confuse a strict
        # parser (the leading quote looks like a wrapper open). We
        # accept the round-trip ambiguity — see the module docstring
        # — because the LLM consumer doesn't run ``load``.
        out = dump([{"a": '"already-quoted"'}])
        assert out == 'a\n"already-quoted"'

    def test_cell_with_embedded_quotes_inside_wrapped_cell(self):
        # When a cell *does* need wrapping (because of \t / \n / \r),
        # embedded quotes inside the wrapper still follow RFC 4180:
        # double them so the closing-wrapper boundary is unambiguous.
        out = dump([{"a": 'he said "hi"\nthen left'}])
        assert out == 'a\n"he said ""hi""\nthen left"'

    def test_cell_with_carriage_return_is_quoted(self):
        out = dump([{"a": "x\ry"}])
        assert out == 'a\n"x\ry"'

    def test_normal_cell_not_quoted(self):
        # Sanity: a clean string passes through unmodified. Quoting
        # everything would defeat the token-saving rationale.
        out = dump([{"a": "hello world"}])
        assert out == "a\nhello world"

    def test_header_cell_with_separator_is_quoted(self):
        # Header keys can in principle contain a tab too. Unlikely
        # in our column names, but the format stays internally
        # consistent.
        out = dump([{"weird\tkey": "v"}])
        assert out == '"weird\tkey"\nv'


class TestSeparatorOverride:
    def test_comma_separator(self):
        # Not the default but supported so callers can opt into CSV-
        # adjacent output (e.g. for ad-hoc piping to spreadsheets).
        out = dump([{"a": "1", "b": "2"}], sep=",")
        assert out == "a,b\n1,2"

    def test_separator_drives_escape_rule(self):
        # With sep="," a cell containing a comma must be quoted but
        # a cell containing a tab is *not* quoted any more.
        out = dump([{"a": "x,y", "b": "x\ty"}], sep=",")
        assert out == 'a,b\n"x,y",x\ty'


class TestErrors:
    def test_non_list_non_dict_input_raises(self):
        with pytest.raises(TypeError):
            dump("not a row")  # type: ignore[arg-type]

    def test_list_of_non_dicts_raises(self):
        with pytest.raises(TypeError):
            dump([1, 2, 3])  # type: ignore[list-item]
