"""Tests for the URI parser."""

import pytest

from precis.uri import ParsedURI, parse


# ─── Basic scheme + path ────────────────────────────────────────────


class TestBasicParsing:
    def test_paper_bare(self):
        p = parse("paper:")
        assert p.scheme == "paper"
        assert p.path == ""
        assert p.is_bare
        assert p.selector is None
        assert p.view is None

    def test_paper_slug(self):
        p = parse("paper:miller2023foo")
        assert p.scheme == "paper"
        assert p.path == "miller2023foo"
        assert not p.is_bare
        assert p.selector is None

    def test_file_docx(self):
        p = parse("file:planning.docx")
        assert p.scheme == "file"
        assert p.path == "planning.docx"

    def test_file_tex(self):
        p = parse("file:main.tex")
        assert p.scheme == "file"
        assert p.path == "main.tex"

    def test_no_scheme_raises(self):
        with pytest.raises(ValueError, match="no scheme"):
            parse("miller2023foo")

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="no scheme"):
            parse("")

    def test_scheme_case_insensitive(self):
        p = parse("Paper:miller2023foo")
        assert p.scheme == "paper"

    def test_raw_preserved(self):
        p = parse("paper:miller2023foo#38/toc")
        assert p.raw == "paper:miller2023foo#38/toc"


# ─── Views ──────────────────────────────────────────────────────────


class TestViews:
    def test_single_view(self):
        p = parse("paper:miller2023foo/toc")
        assert p.path == "miller2023foo"
        assert p.view == "toc"
        assert p.subview is None

    def test_view_with_subview(self):
        p = parse("paper:miller2023foo/cite/bib")
        assert p.view == "cite"
        assert p.subview == "bib"

    def test_meta_view(self):
        p = parse("paper:miller2023foo/meta")
        assert p.view == "meta"

    def test_abstract_view(self):
        p = parse("paper:miller2023foo/abstract")
        assert p.view == "abstract"

    def test_cites_view(self):
        p = parse("paper:miller2023foo/cites")
        assert p.view == "cites"

    def test_cited_by_view(self):
        p = parse("paper:miller2023foo/cited-by")
        assert p.view == "cited-by"

    def test_cite_acs(self):
        p = parse("paper:miller2023foo/cite/acs")
        assert p.view == "cite"
        assert p.subview == "acs"

    def test_file_toc(self):
        p = parse("file:planning.docx/toc")
        assert p.view == "toc"

    def test_file_meta(self):
        p = parse("file:planning.docx/meta")
        assert p.view == "meta"


# ─── Selectors ──────────────────────────────────────────────────────


class TestSelectors:
    def test_slug(self):
        p = parse("file:planning.docx#KR8M2")
        assert p.selector == "KR8M2"
        assert p.selector_type == "slug"
        assert p.anchor == "KR8M2"

    def test_slug_with_collision_suffix(self):
        p = parse("file:planning.docx#KR8M2.2")
        assert p.selector_type == "slug"
        assert p.anchor == "KR8M2.2"

    def test_index(self):
        p = parse("paper:miller2023foo#38")
        assert p.selector == "38"
        assert p.selector_type == "index"
        assert p.anchor == "38"
        assert p.range_start == 38
        assert p.range_end == 38

    def test_path_selector(self):
        p = parse("file:planning.docx#S1.2")
        assert p.selector_type == "path"
        assert p.anchor == "S1.2"

    def test_path_with_child(self):
        p = parse("file:planning.docx#S1.2¶3")
        assert p.selector_type == "path"
        assert p.anchor == "S1.2¶3"

    def test_label(self):
        p = parse("file:main.tex#sec:methods")
        assert p.selector_type == "label"
        assert p.anchor == "sec:methods"


# ─── Ranges ─────────────────────────────────────────────────────────


class TestRanges:
    def test_absolute_range(self):
        p = parse("paper:miller2023foo#38..42")
        assert p.range_start == 38
        assert p.range_end == 42
        assert not p.is_open_range

    def test_open_range(self):
        p = parse("paper:miller2023foo#38..")
        assert p.range_start == 38
        assert p.range_end is None
        assert p.is_open_range

    def test_single_index_is_range(self):
        """Single index #38 sets range_start == range_end == 38."""
        p = parse("paper:miller2023foo#38")
        assert p.range_start == 38
        assert p.range_end == 38


# ─── Context windows ────────────────────────────────────────────────


class TestContextWindows:
    def test_slug_context_symmetric(self):
        p = parse("paper:miller2023foo#KR8M2-3..+3")
        assert p.selector_type == "slug"
        assert p.anchor == "KR8M2"
        assert p.context_before == 3
        assert p.context_after == 3

    def test_slug_context_after_only(self):
        p = parse("paper:miller2023foo#KR8M2..+5")
        assert p.anchor == "KR8M2"
        assert p.context_before is None
        assert p.context_after == 5

    def test_slug_context_before_only(self):
        p = parse("paper:miller2023foo#KR8M2-2..")
        assert p.anchor == "KR8M2"
        assert p.context_before == 2
        assert p.context_after is None

    def test_index_context(self):
        """Index-based context window: #38-2..+2."""
        p = parse("paper:miller2023foo#38-2..+2")
        assert p.anchor == "38"
        assert p.selector_type == "index"
        assert p.context_before == 2
        assert p.context_after == 2


# ─── Selector + view combined ───────────────────────────────────────


class TestSelectorAndView:
    def test_selector_then_view(self):
        p = parse("paper:miller2023foo#38/toc")
        assert p.selector == "38"
        assert p.view == "toc"

    def test_slug_then_view(self):
        p = parse("file:planning.docx#KR8M2/meta")
        assert p.selector == "KR8M2"
        assert p.view == "meta"

    def test_range_then_view(self):
        p = parse("paper:miller2023foo#38..42/toc")
        assert p.selector == "38..42"
        assert p.range_start == 38
        assert p.range_end == 42
        assert p.view == "toc"
