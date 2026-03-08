"""Tests for nodes.py — slug generation, path parsing, path counter."""

from precis.nodes import Path as NodePath, PathCounter, make_slug, resolve_slug


class TestMakeSlug:
    def test_deterministic(self):
        assert make_slug("hello world") == make_slug("hello world")

    def test_different_text_different_slug(self):
        assert make_slug("hello") != make_slug("world")

    def test_five_chars(self):
        assert len(make_slug("test")) == 5

    def test_strips_whitespace(self):
        assert make_slug("  hello  ") == make_slug("hello")

    def test_base34_chars_only(self):
        slug = make_slug("anything")
        valid = set("0123456789ABCDEFGHJKLMNPQRSTUVWXYZ")
        assert all(c in valid for c in slug)


class TestResolveSlug:
    def test_first_occurrence(self):
        counts: dict[str, int] = {}
        assert resolve_slug("ABC12", counts) == "ABC12"

    def test_second_occurrence(self):
        counts: dict[str, int] = {}
        resolve_slug("ABC12", counts)
        assert resolve_slug("ABC12", counts) == "ABC12.2"

    def test_third_occurrence(self):
        counts: dict[str, int] = {}
        resolve_slug("ABC12", counts)
        resolve_slug("ABC12", counts)
        assert resolve_slug("ABC12", counts) == "ABC12.3"


class TestPath:
    def test_parse_heading(self):
        p = NodePath.parse("H1.0.0.0")
        assert p.h1 == 1
        assert p.h2 == 0
        assert p.node_type == ""
        assert p.is_heading()

    def test_parse_paragraph(self):
        p = NodePath.parse("H3.2.1.0p4")
        assert p.h1 == 3
        assert p.h2 == 2
        assert p.h3 == 1
        assert p.h4 == 0
        assert p.node_type == "p"
        assert p.index == 4
        assert not p.is_heading()

    def test_parse_table(self):
        p = NodePath.parse("H2.1.0.0t1")
        assert p.node_type == "t"
        assert p.index == 1

    def test_parse_equation(self):
        p = NodePath.parse("H2.1.0.0e1")
        assert p.node_type == "e"

    def test_parse_figure(self):
        p = NodePath.parse("H1.0.0.0f2")
        assert p.node_type == "f"
        assert p.index == 2

    def test_roundtrip(self):
        cases = [
            "H1.0.0.0",
            "H3.2.1.0p4",
            "H0.0.0.0p1",
            "H2.1.0.0t1",
            "H2.1.0.0e1",
        ]
        for s in cases:
            assert str(NodePath.parse(s)) == s

    def test_invalid(self):
        import pytest

        with pytest.raises(ValueError):
            NodePath.parse("invalid")

    def test_heading_level(self):
        assert NodePath.parse("H1.0.0.0").heading_level() == 1
        assert NodePath.parse("H3.2.0.0").heading_level() == 2
        assert NodePath.parse("H3.2.1.0").heading_level() == 3
        assert NodePath.parse("H3.2.1.4").heading_level() == 4
        assert NodePath.parse("H0.0.0.0p1").heading_level() == 0

    def test_starts_with(self):
        p = NodePath.parse("H2.1.0.0p3")
        assert p.starts_with("H2.1")
        assert not p.starts_with("H3")


class TestPathCounter:
    def test_sequential_headings(self):
        c = PathCounter()
        p1 = c.next_heading(1)
        assert str(p1) == "H1.0.0.0"
        p2 = c.next_heading(1)
        assert str(p2) == "H2.0.0.0"

    def test_nested_headings(self):
        c = PathCounter()
        c.next_heading(1)  # H1
        p = c.next_heading(2)  # H1.1
        assert str(p) == "H1.1.0.0"
        p = c.next_heading(2)  # H1.2
        assert str(p) == "H1.2.0.0"

    def test_children(self):
        c = PathCounter()
        c.next_heading(1)
        p1 = c.next_child("p")
        assert str(p1) == "H1.0.0.0p1"
        p2 = c.next_child("p")
        assert str(p2) == "H1.0.0.0p2"
        t1 = c.next_child("t")
        assert str(t1) == "H1.0.0.0t1"

    def test_heading_resets_children(self):
        c = PathCounter()
        c.next_heading(1)
        c.next_child("p")  # H1.0.0.0p1
        c.next_heading(2)  # H1.1.0.0
        p = c.next_child("p")
        assert str(p) == "H1.1.0.0p1"

    def test_preamble(self):
        c = PathCounter()
        p = c.next_child("p")
        assert str(p) == "H0.0.0.0p1"
