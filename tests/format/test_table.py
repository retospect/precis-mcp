"""Unit tests for :mod:`precis.format.table`.

The TTY default renderer. Pure ASCII box-drawing (`U+2500` family).
The tests pin the visible shape so a future tweak doesn't silently
change the layout that operators read in their terminals.
"""

from __future__ import annotations

from precis.format.table import render


class TestEmpty:
    def test_empty_rows_no_schema_returns_empty_string(self):
        assert render([]) == ""

    def test_empty_rows_with_schema_renders_header_box(self):
        out = render([], schema=["a", "b"])
        # Header inside a single-cell box (top, header row, bottom).
        lines = out.splitlines()
        assert len(lines) == 3
        assert lines[0].startswith("┌")
        assert lines[1].startswith("│")
        assert lines[2].startswith("└")
        assert " a " in lines[1]
        assert " b " in lines[1]


class TestSingleRow:
    def test_one_row_header_separator_data_three_borders(self):
        out = render([{"a": "1", "b": "2"}])
        lines = out.splitlines()
        # top, header, mid-rule, row, bottom = 5 lines.
        assert len(lines) == 5
        assert lines[0].startswith("┌") and lines[0].endswith("┐")
        assert lines[1].startswith("│") and " a " in lines[1] and " b " in lines[1]
        assert lines[2].startswith("├") and lines[2].endswith("┤")
        assert lines[3].startswith("│") and " 1 " in lines[3] and " 2 " in lines[3]
        assert lines[4].startswith("└") and lines[4].endswith("┘")

    def test_columns_sized_to_widest_cell(self):
        out = render([{"a": "short", "b": "considerably-longer"}])
        lines = out.splitlines()
        # All lines must be the same printable width.
        widths = {len(line) for line in lines}
        assert len(widths) == 1


class TestMultipleRows:
    def test_two_rows_one_mid_rule(self):
        # Rows aren't separated by per-row rules; only the
        # header/data boundary has a `├──┤` line. Keeps the output
        # compact while still calling out the schema.
        out = render(
            [
                {"a": "1", "b": "2"},
                {"a": "3", "b": "4"},
            ]
        )
        lines = out.splitlines()
        # top, header, separator, row1, row2, bottom = 6 lines.
        assert len(lines) == 6
        assert lines[0].startswith("┌")
        assert lines[2].startswith("├")
        assert lines[5].startswith("└")
        # Only one separator (lines[2]) — rows[3] and rows[4] are
        # both data rows.
        assert lines[3].startswith("│")
        assert lines[4].startswith("│")


class TestColumnOrdering:
    def test_default_is_first_seen_order(self):
        out = render(
            [
                {"z": "1", "a": "2"},
                {"a": "3", "z": "4", "m": "5"},
            ]
        )
        header = out.splitlines()[1]
        # Strip leading/trailing border and split on `│`.
        cells = [c.strip() for c in header.strip("│").split("│")]
        assert cells == ["z", "a", "m"]

    def test_schema_pins_order(self):
        out = render([{"a": "1", "b": "2", "c": "3"}], schema=["c", "b", "a"])
        header = out.splitlines()[1]
        cells = [c.strip() for c in header.strip("│").split("│")]
        assert cells == ["c", "b", "a"]


class TestScalarStringification:
    def test_none_renders_as_empty_cell(self):
        out = render([{"a": None, "b": "x"}])
        lines = out.splitlines()
        # Find the data row; `None` must not produce the literal
        # "None" — empty cell instead, just like in TOON.
        data_row = lines[3]
        assert "None" not in data_row

    def test_true_false_lowercase(self):
        out = render([{"a": True, "b": False}])
        # Consistency with TOON's serialisation — booleans are
        # lowercase strings everywhere so an operator switching
        # between formats sees the same shape.
        assert "true" in out
        assert "false" in out
        assert "True" not in out
        assert "False" not in out

    def test_numbers_stringify(self):
        out = render([{"a": 42, "b": 1.5}])
        assert "42" in out
        assert "1.5" in out
