"""Tests for PlainTextHandler — parsing, reading, and writing .txt files."""

from __future__ import annotations

import pytest

from precis.handlers.plaintext import PlainTextHandler

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def handler():
    return PlainTextHandler()


@pytest.fixture
def sample_txt(tmp_path):
    """Create a sample .txt file with paragraph blocks."""
    content = """\
This is the first paragraph.
It spans two lines.

This is the second paragraph, a single line.

Third paragraph here.
It has three lines
of plain text content.

Fourth and final paragraph.
"""
    p = tmp_path / "notes.txt"
    p.write_text(content, encoding="utf-8")
    return p


@pytest.fixture
def single_txt(tmp_path):
    """A file with a single paragraph (no blank-line breaks)."""
    content = """\
Line one.
Line two.
Line three.
"""
    p = tmp_path / "single.txt"
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Parse tests
# ---------------------------------------------------------------------------


class TestParse:
    def test_parse_returns_nodes(self, handler, sample_txt):
        nodes = handler.parse(sample_txt)
        assert len(nodes) == 4

    def test_all_paragraphs(self, handler, sample_txt):
        nodes = handler.parse(sample_txt)
        for n in nodes:
            assert n.node_type == "p"

    def test_multiline_paragraph(self, handler, sample_txt):
        nodes = handler.parse(sample_txt)
        assert "first paragraph" in nodes[0].text
        assert "two lines" in nodes[0].text

    def test_single_line_paragraph(self, handler, sample_txt):
        nodes = handler.parse(sample_txt)
        assert "second paragraph" in nodes[1].text

    def test_slugs_unique(self, handler, sample_txt):
        nodes = handler.parse(sample_txt)
        slugs = [n.slug for n in nodes]
        assert len(slugs) == len(set(slugs))

    def test_source_lines(self, handler, sample_txt):
        nodes = handler.parse(sample_txt)
        for node in nodes:
            assert node.source_line_start >= 1
            assert node.source_line_end >= node.source_line_start

    def test_source_line_accuracy(self, handler, sample_txt):
        nodes = handler.parse(sample_txt)
        # First paragraph: lines 1-2
        assert nodes[0].source_line_start == 1
        assert nodes[0].source_line_end == 2
        # Second paragraph: line 4
        assert nodes[1].source_line_start == 4
        assert nodes[1].source_line_end == 4
        # Third paragraph: lines 6-8
        assert nodes[2].source_line_start == 6
        assert nodes[2].source_line_end == 8

    def test_single_paragraph_file(self, handler, single_txt):
        nodes = handler.parse(single_txt)
        assert len(nodes) == 1
        assert "Line one" in nodes[0].text
        assert "Line three" in nodes[0].text

    def test_empty_file(self, handler, tmp_path):
        p = tmp_path / "empty.txt"
        p.write_text("", encoding="utf-8")
        nodes = handler.parse(p)
        assert nodes == []

    def test_source_files(self, handler, sample_txt):
        files = handler.source_files(sample_txt)
        assert files == [sample_txt]


# ---------------------------------------------------------------------------
# Read tests
# ---------------------------------------------------------------------------


class TestRead:
    def test_read_toc(self, handler, sample_txt):
        result = handler.read(
            path=str(sample_txt),
            selector=None,
            view=None,
            subview=None,
            query="",
            summarize=False,
            depth=0,
            page=1,
        )
        assert "notes.txt" in result
        assert "4 nodes" in result

    def test_read_selector(self, handler, sample_txt):
        nodes = handler.parse(sample_txt)
        result = handler.read(
            path=str(sample_txt),
            selector=nodes[1].slug,
            view=None,
            subview=None,
            query="",
            summarize=False,
            depth=0,
            page=1,
        )
        assert "second paragraph" in result

    def test_read_query(self, handler, sample_txt):
        result = handler.read(
            path=str(sample_txt),
            selector=None,
            view=None,
            subview=None,
            query="third",
            summarize=False,
            depth=0,
            page=1,
        )
        assert "third" in result.lower()

    def test_read_query_no_match(self, handler, sample_txt):
        result = handler.read(
            path=str(sample_txt),
            selector=None,
            view=None,
            subview=None,
            query="nonexistent_xyz",
            summarize=False,
            depth=0,
            page=1,
        )
        assert "No matches" in result

    def test_read_meta(self, handler, sample_txt):
        result = handler.read(
            path=str(sample_txt),
            selector=None,
            view="meta",
            subview=None,
            query="",
            summarize=False,
            depth=0,
            page=1,
        )
        assert "notes.txt" in result
        assert "words:" in result

    def test_read_empty(self, handler, tmp_path):
        p = tmp_path / "empty.txt"
        p.write_text("", encoding="utf-8")
        result = handler.read(
            path=str(p),
            selector=None,
            view=None,
            subview=None,
            query="",
            summarize=False,
            depth=0,
            page=1,
        )
        assert "empty" in result.lower()


# ---------------------------------------------------------------------------
# Put tests
# ---------------------------------------------------------------------------


class TestPut:
    def test_put_append(self, handler, sample_txt):
        result = handler.put(
            path=str(sample_txt),
            selector=None,
            text="A brand new paragraph.",
            mode="append",
        )
        assert "+" in result
        content = sample_txt.read_text(encoding="utf-8")
        assert "brand new paragraph" in content

    def test_put_replace(self, handler, sample_txt):
        nodes = handler.parse(sample_txt)
        slug = nodes[1].slug
        result = handler.put(
            path=str(sample_txt),
            selector=slug,
            text="Replaced content.",
            mode="replace",
        )
        assert "replace" in result
        content = sample_txt.read_text(encoding="utf-8")
        assert "Replaced content" in content
        assert "second paragraph" not in content

    def test_put_delete(self, handler, sample_txt):
        nodes = handler.parse(sample_txt)
        slug = nodes[0].slug
        old_text = nodes[0].text
        result = handler.put(
            path=str(sample_txt),
            selector=slug,
            text="",
            mode="delete",
        )
        assert "deleted" in result
        content = sample_txt.read_text(encoding="utf-8")
        assert old_text not in content

    def test_put_insert_after(self, handler, sample_txt):
        nodes = handler.parse(sample_txt)
        slug = nodes[0].slug
        result = handler.put(
            path=str(sample_txt),
            selector=slug,
            text="Inserted after first.",
            mode="after",
        )
        assert "+" in result
        content = sample_txt.read_text(encoding="utf-8")
        assert "Inserted after first" in content

    def test_put_insert_before(self, handler, sample_txt):
        nodes = handler.parse(sample_txt)
        slug = nodes[1].slug
        result = handler.put(
            path=str(sample_txt),
            selector=slug,
            text="Inserted before second.",
            mode="before",
        )
        assert "+" in result
        content = sample_txt.read_text(encoding="utf-8")
        assert "Inserted before second" in content

    def test_put_invalid_mode(self, handler, sample_txt):
        from precis.protocol import PrecisError

        with pytest.raises(PrecisError, match="invalid mode"):
            handler.put(
                path=str(sample_txt),
                selector=None,
                text="foo",
                mode="explode",
            )

    def test_put_replace_no_text(self, handler, sample_txt):
        from precis.protocol import PrecisError

        nodes = handler.parse(sample_txt)
        with pytest.raises(PrecisError, match="text required"):
            handler.put(
                path=str(sample_txt),
                selector=nodes[0].slug,
                text="",
                mode="replace",
            )

    def test_put_append_no_text(self, handler, sample_txt):
        from precis.protocol import PrecisError

        with pytest.raises(PrecisError, match="text required"):
            handler.put(
                path=str(sample_txt),
                selector=None,
                text="",
                mode="append",
            )


# ---------------------------------------------------------------------------
# Move test
# ---------------------------------------------------------------------------


class TestMove:
    def test_move_node(self, handler, sample_txt):
        nodes = handler.parse(sample_txt)
        # Move first paragraph after third
        first = nodes[0]
        third = nodes[2]
        result = handler.put(
            path=str(sample_txt),
            selector=first.slug,
            text=third.slug,
            mode="move",
        )
        assert "moved" in result


# ---------------------------------------------------------------------------
# Auto-create test
# ---------------------------------------------------------------------------


class TestAutoCreate:
    def test_auto_create_txt(self, handler, tmp_path):
        p = tmp_path / "new.txt"
        assert not p.exists()
        result = handler.read(
            path=str(p),
            selector=None,
            view=None,
            subview=None,
            query="",
            summarize=False,
            depth=0,
            page=1,
        )
        assert p.exists()
        assert "empty" in result.lower()
