"""Unit tests for the regex find/substitute primitives (pure text ops, no
store) backing the draft grep + s/// — `precis.utils.draft_regex`."""

from __future__ import annotations

import pytest

from precis.errors import BadInput
from precis.utils import draft_regex as dr


def test_find_locates_line_col_and_span() -> None:
    text = "alpha\nbeta **bold** gamma\nlast"
    rx = dr.compile_pattern(r"\*\*\w+\*\*")
    ms = dr.find_in_text(rx, text)
    assert len(ms) == 1
    (m,) = ms
    assert m.line_no == 2
    assert m.matched == "**bold**"
    assert m.line == "beta **bold** gamma"
    # column is 0-based within the line ("beta " is 5 chars)
    assert m.col == 5
    assert m.line[m.col : m.col + len(m.matched)] == "**bold**"


def test_find_multiline_anchors_per_line() -> None:
    rx = dr.compile_pattern(r"^x")  # MULTILINE is always on
    ms = dr.find_in_text(rx, "x1\ny2\nx3")
    assert [m.line_no for m in ms] == [1, 3]


def test_find_skips_empty_matches() -> None:
    rx = dr.compile_pattern(r"a*")  # can match empty everywhere
    assert dr.find_in_text(rx, "bbb") == []  # no real (non-empty) hit


def test_find_case_fold_flag() -> None:
    rx = dr.compile_pattern("todo", flags="i")
    assert len(dr.find_in_text(rx, "ToDo and TODO")) == 2


def test_sub_global_with_count() -> None:
    rx = dr.compile_pattern("—")
    new, n = dr.sub_in_text(rx, ", ", "a—b—c")
    assert new == "a, b, c"
    assert n == 2


def test_sub_backreference() -> None:
    rx = dr.compile_pattern(r"\*\*(\w+)\*\*")
    new, n = dr.sub_in_text(rx, r"\1", "see **bold** here")
    assert new == "see bold here"
    assert n == 1


def test_compile_rejects_bad_regex() -> None:
    with pytest.raises(BadInput):
        dr.compile_pattern("(unclosed")


def test_compile_rejects_unknown_flag() -> None:
    with pytest.raises(BadInput):
        dr.compile_pattern("x", flags="z")


def test_compile_rejects_overlong() -> None:
    with pytest.raises(BadInput):
        dr.compile_pattern("a" * (dr.MAX_PATTERN_LEN + 1))


def test_sub_bad_backreference_is_badinput() -> None:
    rx = dr.compile_pattern(r"(\w)")  # only group 1 exists
    with pytest.raises(BadInput):
        dr.sub_in_text(rx, r"\9", "abc")


@pytest.mark.parametrize(
    "expr,want",
    [
        ("s/a/b/", ("a", "b", "")),
        ("s/a/b/g", ("a", "b", "")),  # g dropped (always global)
        ("s/a/b/i", ("a", "b", "i")),
        ("s|a/b|c|", ("a/b", "c", "")),  # alt delimiter when pattern has /
        (r"s/a\/b/c/", ("a/b", "c", "")),  # escaped delimiter
        ("s/x//", ("x", "", "")),  # deletion
    ],
)
def test_parse_sed(expr: str, want: tuple[str, str, str]) -> None:
    assert dr.parse_sed(expr) == want


@pytest.mark.parametrize("bad", ["", "x/a/b/", "s/a/b", "s a b ", "snananb"])
def test_parse_sed_rejects_malformed(bad: str) -> None:
    with pytest.raises(BadInput):
        dr.parse_sed(bad)
