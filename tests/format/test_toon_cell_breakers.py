"""A table cell never carries a row- or column-breaking character (gr740128).

A title or summary holding a tab or a line break must not fake a row,
shift a column, or read as the end of the table. The cell encoder
(``precis.format.toon.escape_cell``) writes each such character as a
visible escape; ``load`` reverses it.

Rows are counted with ``str.splitlines``, which honours every line
break listed here, so the row count is what a line-based reader sees.
"""

from __future__ import annotations

import pytest

from precis.format import render_agent_table
from precis.format.toon import dump, load

#: name → character: tab plus every line break ``str.splitlines`` honours.
BREAKERS = {
    "tab": "\t",
    "newline": "\n",
    "carriage return": "\r",
    "vertical tab": "\v",
    "form feed": "\f",
    "file separator": "\x1c",
    "group separator": "\x1d",
    "record separator": "\x1e",
    "next line": "\x85",
    "line separator": "\u2028",
    "paragraph separator": "\u2029",
}
_SCHEMA = ["id", "title", "n"]

breaker = pytest.mark.parametrize("ch", BREAKERS.values(), ids=BREAKERS.keys())


def _rows(ch: str) -> list[dict[str, object]]:
    return [
        {"id": "pa1", "title": f"first{ch}second", "n": 3},
        {"id": "pa2", "title": "plain", "n": 4},
    ]


@breaker
def test_toon_row_and_column_count_unchanged(ch: str) -> None:
    out = render_agent_table(_rows(ch), schema=_SCHEMA, format="toon")
    lines = out.splitlines()
    assert len(lines) == 3
    assert [len(line.split("\t")) for line in lines] == [3, 3, 3]
    if ch not in "\t\n":  # the column and row separators themselves
        assert ch not in out


@breaker
def test_toon_header_cell_cannot_break_the_header(ch: str) -> None:
    out = dump([{f"first{ch}second": "v", "b": "w"}])
    lines = out.splitlines()
    assert len(lines) == 2
    assert [len(line.split("\t")) for line in lines] == [2, 2]


@breaker
def test_box_table_row_count_unchanged(ch: str) -> None:
    plain = render_agent_table(_rows(" "), schema=_SCHEMA, format="table")
    out = render_agent_table(_rows(ch), schema=_SCHEMA, format="table")
    assert len(out.splitlines()) == len(plain.splitlines())
    assert ch == "\n" or ch not in out


@breaker
def test_load_reverses_the_escape(ch: str) -> None:
    rows = _rows(ch)
    decoded = load(dump(rows, schema=_SCHEMA))
    assert [r["title"] for r in decoded] == [r["title"] for r in rows]


def test_escapes_are_visible() -> None:
    out = dump([{"a": "x\ty\nz\r\v\f\x85\u2028\u2029"}])
    assert out == "{a}\nx\\ty\\nz\\r\\v\\f\\u0085\\u2028\\u2029"


def test_non_tab_separator_still_escapes_and_quotes() -> None:
    out = dump([{"a": "x,y\nz", "b": "w"}], sep=",")
    assert out == '{a,b}\n"x,y\\nz",w'
    assert load(out, sep=",") == [{"a": "x,y\nz", "b": "w"}]


@pytest.mark.parametrize(
    "cell", [r"\alpha", r"\nu", r"C:\temp", "put(text='a\\nb')", "trailing\\"]
)
def test_cell_without_a_breaker_is_emitted_unchanged(cell: str) -> None:
    # No backslash doubling: a call recipe or a LaTeX command reaches
    # the reader exactly as stored.
    assert dump([{"a": cell}]) == "{a}\n" + cell
