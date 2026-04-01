"""Tests for MarkdownHandler — parsing, reading, and writing .md files."""

from __future__ import annotations

from pathlib import Path

import pytest

from precis.handlers.markdown import MarkdownHandler


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def handler():
    return MarkdownHandler()


@pytest.fixture
def sample_md(tmp_path):
    """Create a sample .md file for testing."""
    content = """\
# Document Title

## Introduction

This is the introduction paragraph.
It spans multiple lines.

## Methods

### Data Collection

We collected data from multiple sources.

### Analysis

The analysis was performed using standard methods.

## Results

| Metric | Value | Unit |
| --- | --- | --- |
| Speed | 42 | m/s |
| Accuracy | 99.1 | % |

The results are shown in the table above.

## Conclusion

In conclusion, everything works.
"""
    p = tmp_path / "test.md"
    p.write_text(content, encoding="utf-8")
    return p


@pytest.fixture
def code_md(tmp_path):
    """Create a .md file with fenced code blocks."""
    content = """\
# Code Examples

Here is some Python:

```python
def hello():
    print("world")
```

And some JSON:

```json
{"key": "value"}
```
"""
    p = tmp_path / "code.md"
    p.write_text(content, encoding="utf-8")
    return p


@pytest.fixture
def list_md(tmp_path):
    """Create a .md file with lists."""
    content = """\
# Shopping

- Apples
- Bananas
- Cherries

## Steps

1. First step
2. Second step
3. Third step
"""
    p = tmp_path / "lists.md"
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Parse tests
# ---------------------------------------------------------------------------

class TestParse:
    def test_parse_returns_nodes(self, handler, sample_md):
        nodes = handler.parse(sample_md)
        assert len(nodes) > 0

    def test_parse_headings(self, handler, sample_md):
        nodes = handler.parse(sample_md)
        headings = [n for n in nodes if n.node_type == "h"]
        titles = [n.text for n in headings]
        assert "Document Title" in titles
        assert "Introduction" in titles
        assert "Methods" in titles
        assert "Data Collection" in titles
        assert "Analysis" in titles
        assert "Results" in titles
        assert "Conclusion" in titles

    def test_heading_levels(self, handler, sample_md):
        nodes = handler.parse(sample_md)
        headings = {n.text: n for n in nodes if n.node_type == "h"}
        assert headings["Document Title"].style == "h1"
        assert headings["Introduction"].style == "h2"
        assert headings["Data Collection"].style == "h3"

    def test_parse_paragraphs(self, handler, sample_md):
        nodes = handler.parse(sample_md)
        paras = [n for n in nodes if n.node_type == "p"]
        assert len(paras) > 0
        # Multi-line paragraph should be joined
        intro = [n for n in paras if "introduction paragraph" in n.text]
        assert len(intro) == 1
        assert "multiple lines" in intro[0].text

    def test_parse_table(self, handler, sample_md):
        nodes = handler.parse(sample_md)
        tables = [n for n in nodes if n.node_type == "t"]
        assert len(tables) == 1
        assert "Speed" in tables[0].text
        assert "42" in tables[0].text
        # Synopsis should have header info
        assert "Metric" in tables[0].precis

    def test_parse_code_blocks(self, handler, code_md):
        nodes = handler.parse(code_md)
        code_nodes = [n for n in nodes if n.style == "code"]
        assert len(code_nodes) == 2
        assert "python" in code_nodes[0].precis
        assert "json" in code_nodes[1].precis
        assert "hello" in code_nodes[0].text

    def test_parse_lists(self, handler, list_md):
        nodes = handler.parse(list_md)
        list_nodes = [n for n in nodes if n.style == "list"]
        assert len(list_nodes) >= 1
        # Check bullet list captured
        bullet = [n for n in list_nodes if "Apples" in n.text]
        assert len(bullet) == 1

    def test_parse_slugs_unique(self, handler, sample_md):
        nodes = handler.parse(sample_md)
        slugs = [n.slug for n in nodes]
        assert len(slugs) == len(set(slugs))

    def test_parse_source_lines(self, handler, sample_md):
        nodes = handler.parse(sample_md)
        for node in nodes:
            assert node.source_line_start >= 1
            assert node.source_line_end >= node.source_line_start

    def test_parse_empty_file(self, handler, tmp_path):
        p = tmp_path / "empty.md"
        p.write_text("", encoding="utf-8")
        nodes = handler.parse(p)
        assert nodes == []

    def test_source_files(self, handler, sample_md):
        files = handler.source_files(sample_md)
        assert files == [sample_md]


# ---------------------------------------------------------------------------
# Read tests
# ---------------------------------------------------------------------------

class TestRead:
    def test_read_toc(self, handler, sample_md):
        result = handler.read(
            path=str(sample_md), selector=None, view=None, subview=None,
            query="", summarize=False, depth=0, page=1,
        )
        assert "test.md" in result
        assert "Document Title" in result
        assert "Introduction" in result

    def test_read_toc_depth(self, handler, sample_md):
        result = handler.read(
            path=str(sample_md), selector=None, view=None, subview=None,
            query="", summarize=False, depth=2, page=1,
        )
        assert "Introduction" in result
        # depth=2 → h1 + h2, but not h3
        assert "Data Collection" not in result

    def test_read_selector_heading(self, handler, sample_md):
        nodes = handler.parse(sample_md)
        intro = [n for n in nodes if n.text == "Introduction"][0]
        result = handler.read(
            path=str(sample_md), selector=intro.slug, view=None, subview=None,
            query="", summarize=False, depth=0, page=1,
        )
        assert "Introduction" in result

    def test_read_selector_paragraph(self, handler, sample_md):
        nodes = handler.parse(sample_md)
        paras = [n for n in nodes if n.node_type == "p"]
        result = handler.read(
            path=str(sample_md), selector=paras[0].slug, view=None, subview=None,
            query="", summarize=False, depth=0, page=1,
        )
        assert paras[0].text[:20] in result

    def test_read_query(self, handler, sample_md):
        result = handler.read(
            path=str(sample_md), selector=None, view=None, subview=None,
            query="introduction", summarize=False, depth=0, page=1,
        )
        assert "introduction" in result.lower()

    def test_read_query_no_match(self, handler, sample_md):
        result = handler.read(
            path=str(sample_md), selector=None, view=None, subview=None,
            query="nonexistent_term_xyz", summarize=False, depth=0, page=1,
        )
        assert "No matches" in result

    def test_read_meta(self, handler, sample_md):
        result = handler.read(
            path=str(sample_md), selector=None, view="meta", subview=None,
            query="", summarize=False, depth=0, page=1,
        )
        assert "test.md" in result
        assert "headings:" in result
        assert "words:" in result

    def test_read_empty_doc(self, handler, tmp_path):
        p = tmp_path / "empty.md"
        p.write_text("", encoding="utf-8")
        result = handler.read(
            path=str(p), selector=None, view=None, subview=None,
            query="", summarize=False, depth=0, page=1,
        )
        assert "empty" in result.lower()


# ---------------------------------------------------------------------------
# Put tests
# ---------------------------------------------------------------------------

class TestPut:
    def test_put_append(self, handler, sample_md):
        result = handler.put(
            path=str(sample_md), selector=None,
            text="A new paragraph at the end.", mode="append",
        )
        assert "+" in result
        content = sample_md.read_text(encoding="utf-8")
        assert "new paragraph" in content

    def test_put_append_heading(self, handler, sample_md):
        result = handler.put(
            path=str(sample_md), selector=None,
            text="## New Section", mode="append",
        )
        assert "+" in result
        content = sample_md.read_text(encoding="utf-8")
        assert "New Section" in content

    def test_put_replace(self, handler, sample_md):
        nodes = handler.parse(sample_md)
        paras = [n for n in nodes if n.node_type == "p" and "introduction" in n.text.lower()]
        assert paras
        slug = paras[0].slug
        result = handler.put(
            path=str(sample_md), selector=slug,
            text="Replaced paragraph text.", mode="replace",
        )
        assert "replace" in result
        content = sample_md.read_text(encoding="utf-8")
        assert "Replaced paragraph text" in content

    def test_put_delete(self, handler, sample_md):
        nodes = handler.parse(sample_md)
        paras = [n for n in nodes if n.node_type == "p"]
        slug = paras[0].slug
        old_text = paras[0].text
        result = handler.put(
            path=str(sample_md), selector=slug,
            text="", mode="delete",
        )
        assert "deleted" in result
        content = sample_md.read_text(encoding="utf-8")
        assert old_text not in content

    def test_put_insert_after(self, handler, sample_md):
        nodes = handler.parse(sample_md)
        intro_h = [n for n in nodes if n.text == "Introduction"][0]
        result = handler.put(
            path=str(sample_md), selector=intro_h.slug,
            text="Inserted after heading.", mode="after",
        )
        assert "+" in result
        content = sample_md.read_text(encoding="utf-8")
        assert "Inserted after heading" in content

    def test_put_insert_before(self, handler, sample_md):
        nodes = handler.parse(sample_md)
        methods_h = [n for n in nodes if n.text == "Methods"][0]
        result = handler.put(
            path=str(sample_md), selector=methods_h.slug,
            text="Inserted before methods.", mode="before",
        )
        assert "+" in result
        content = sample_md.read_text(encoding="utf-8")
        assert "Inserted before methods" in content

    def test_put_invalid_mode(self, handler, sample_md):
        from precis.protocol import PrecisError
        with pytest.raises(PrecisError, match="invalid mode"):
            handler.put(
                path=str(sample_md), selector=None,
                text="foo", mode="explode",
            )

    def test_put_replace_no_text(self, handler, sample_md):
        from precis.protocol import PrecisError
        nodes = handler.parse(sample_md)
        paras = [n for n in nodes if n.node_type == "p"]
        with pytest.raises(PrecisError, match="text required"):
            handler.put(
                path=str(sample_md), selector=paras[0].slug,
                text="", mode="replace",
            )

    def test_put_append_no_text(self, handler, sample_md):
        from precis.protocol import PrecisError
        with pytest.raises(PrecisError, match="text required"):
            handler.put(
                path=str(sample_md), selector=None,
                text="", mode="append",
            )


# ---------------------------------------------------------------------------
# Move test
# ---------------------------------------------------------------------------

class TestMove:
    def test_move_node(self, handler, sample_md):
        nodes = handler.parse(sample_md)
        # Move Conclusion heading before Results
        conclusion = [n for n in nodes if n.text == "Conclusion"][0]
        results = [n for n in nodes if n.text == "Results"][0]
        result = handler.put(
            path=str(sample_md), selector=conclusion.slug,
            text=results.slug, mode="move",
        )
        assert "moved" in result


# ---------------------------------------------------------------------------
# Auto-create test
# ---------------------------------------------------------------------------

class TestAutoCreate:
    def test_auto_create_md(self, handler, tmp_path):
        p = tmp_path / "new.md"
        assert not p.exists()
        result = handler.read(
            path=str(p), selector=None, view=None, subview=None,
            query="", summarize=False, depth=0, page=1,
        )
        assert p.exists()
        assert "empty" in result.lower()
