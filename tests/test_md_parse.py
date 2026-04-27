"""Tests for the markdown parser utility (`precis.utils.md_parse`)."""

from __future__ import annotations

import pytest

from precis.utils.md_parse import (
    file_slug_from_path,
    is_valid_file_slug,
    parse_markdown,
    path_from_file_slug,
)

# ── parse_markdown ───────────────────────────────────────────────────


def test_empty_input() -> None:
    assert parse_markdown("") == []
    assert parse_markdown("\n\n   \n") == []


def test_single_heading() -> None:
    blocks = parse_markdown("# Hello")
    assert len(blocks) == 1
    b = blocks[0]
    assert b.kind == "heading"
    assert b.heading_level == 1
    assert b.text == "# Hello"
    assert b.line_start == 1 and b.line_end == 1


def test_heading_levels_1_to_6() -> None:
    md = "\n".join(f"{'#' * lvl} L{lvl}" for lvl in range(1, 7))
    blocks = parse_markdown(md)
    assert [b.heading_level for b in blocks] == [1, 2, 3, 4, 5, 6]
    assert all(b.kind == "heading" for b in blocks)


def test_heading_then_paragraph() -> None:
    md = "# Title\n\nFirst paragraph.\n"
    blocks = parse_markdown(md)
    assert len(blocks) == 2
    assert blocks[0].kind == "heading"
    assert blocks[1].kind == "paragraph"
    assert blocks[1].text == "First paragraph."


def test_multi_paragraph_separated_by_blank_lines() -> None:
    md = "Para one\nstill one.\n\nPara two.\n\nPara three.\n"
    blocks = parse_markdown(md)
    assert [b.kind for b in blocks] == ["paragraph"] * 3
    assert blocks[0].text == "Para one\nstill one."
    assert blocks[1].text == "Para two."
    assert blocks[2].text == "Para three."


def test_fenced_code_block() -> None:
    md = "Before.\n\n```python\ndef foo():\n    return 1\n```\n\nAfter."
    blocks = parse_markdown(md)
    assert len(blocks) == 3
    assert blocks[0].kind == "paragraph"
    assert blocks[1].kind == "code"
    assert blocks[1].meta.get("lang") == "python"
    assert "def foo()" in blocks[1].text
    assert blocks[2].kind == "paragraph"


def test_fenced_code_with_tildes() -> None:
    md = "~~~\nplain\ncode\n~~~"
    blocks = parse_markdown(md)
    assert len(blocks) == 1
    assert blocks[0].kind == "code"


def test_table_with_separator() -> None:
    md = "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n"
    blocks = parse_markdown(md)
    assert len(blocks) == 1
    assert blocks[0].kind == "table"
    assert "| a | b |" in blocks[0].text
    assert "| 3 | 4 |" in blocks[0].text


def test_pipe_paragraph_without_separator_is_paragraph() -> None:
    md = "| not a table\n| just text"
    blocks = parse_markdown(md)
    assert len(blocks) == 1
    assert blocks[0].kind == "paragraph"


def test_unordered_list() -> None:
    md = "- item one\n- item two\n- item three\n"
    blocks = parse_markdown(md)
    assert len(blocks) == 1
    assert blocks[0].kind == "list"


def test_ordered_list() -> None:
    md = "1. first\n2. second\n3. third\n"
    blocks = parse_markdown(md)
    assert len(blocks) == 1
    assert blocks[0].kind == "list"


def test_thematic_break_is_dropped() -> None:
    md = "Para one.\n\n---\n\nPara two.\n"
    blocks = parse_markdown(md)
    assert len(blocks) == 2
    assert all(b.kind == "paragraph" for b in blocks)


def test_line_numbers_track_through_document() -> None:
    md = "# H1\n\nPara A.\n\n## H2\n\nPara B.\n"
    blocks = parse_markdown(md)
    assert len(blocks) == 4
    assert blocks[0].line_start == 1
    assert blocks[1].line_start == 3 and blocks[1].line_end == 3
    assert blocks[2].line_start == 5
    assert blocks[3].line_start == 7


# ── slug stability ───────────────────────────────────────────────────


def test_block_slugs_are_deterministic() -> None:
    md = "# Hello\n\nFirst paragraph.\n\n## World\n\nSecond.\n"
    a = parse_markdown(md)
    b = parse_markdown(md)
    assert [bl.slug for bl in a] == [bl.slug for bl in b]


def test_heading_slugs_track_title() -> None:
    md = "# Hello World\n\n## Sub-Section\n"
    blocks = parse_markdown(md)
    assert blocks[0].slug == "hello-world"
    assert blocks[1].slug == "sub-section"


def test_paragraph_slug_has_content_hash() -> None:
    """Paragraph slugs include a 6-char hex suffix."""
    md = "The quick brown fox jumps over the lazy dog.\n"
    blocks = parse_markdown(md)
    slug = blocks[0].slug
    # Expected shape: <words>-<6 hex chars>
    assert "-" in slug
    parts = slug.split("-")
    last = parts[-1]
    assert len(last) == 6
    assert all(c in "0123456789abcdef" for c in last)


def test_two_identical_headings_get_disambiguated() -> None:
    md = "# Conclusion\n\n# Conclusion\n"
    blocks = parse_markdown(md)
    assert blocks[0].slug == "conclusion"
    assert blocks[1].slug == "conclusion-2"


def test_two_different_paragraphs_with_same_first_words() -> None:
    """Same opening but different content → different slugs (hash differs)."""
    md = "Hello world this is one variant.\n\nHello world this is another variant.\n"
    blocks = parse_markdown(md)
    assert len(blocks) == 2
    assert blocks[0].slug != blocks[1].slug
    # Both share the same prefix.
    assert blocks[0].slug.startswith("hello-world-this-is")
    assert blocks[1].slug.startswith("hello-world-this-is")


# ── file_slug_from_path ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "rel,expected",
    [
        ("foo.md", "foo"),
        ("notes/meeting.md", "notes--meeting"),
        ("a/b/c.md", "a--b--c"),
        ("My File.md", "my-file"),
        ("UPPER.MD", "upper"),
        ("dir name/file name.md", "dir-name--file-name"),
    ],
)
def test_file_slug_from_path(rel: str, expected: str) -> None:
    assert file_slug_from_path(rel) == expected


def test_file_slug_round_trips() -> None:
    rel = "notes/meeting.md"
    slug = file_slug_from_path(rel)
    assert path_from_file_slug(slug) == rel


def test_file_slug_rejects_empty() -> None:
    with pytest.raises(ValueError):
        file_slug_from_path("")


# ── is_valid_file_slug ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "slug,ok",
    [
        ("foo", True),
        ("foo--bar", True),
        ("a1-b2", True),
        ("foo_bar", True),
        ("a--b--c", True),
        ("UPPER", False),
        ("../etc", False),
        ("foo/bar", False),
        ("", False),
        ("foo--", False),  # empty trailing segment
        ("--foo", False),
    ],
)
def test_is_valid_file_slug(slug: str, ok: bool) -> None:
    assert is_valid_file_slug(slug) is ok
