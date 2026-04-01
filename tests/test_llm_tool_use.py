"""Tests that tool descriptions teach LLMs the correct syntax.

Every example URI in a tool docstring must parse correctly.
Every plausible LLM input to _to_uri() must dispatch to the right scheme.
Descriptions must stay concise (token budget matters).
"""

import re

import pytest

from precis.server import _to_uri, get, put, search, move
from precis.uri import parse


# ── Extract every example URI from tool docstrings ─────────────────

def _extract_ids(docstring: str) -> list[str]:
    """Pull id='...' and scope='...' values from a docstring."""
    return re.findall(r"(?:id|scope|after)='([^']+)'", docstring)


def _extract_all_tool_examples() -> list[str]:
    """Collect every example URI from all tool docstrings."""
    ids: list[str] = []
    for fn in (search, get, put, move):
        doc = fn.__doc__ or ""
        ids.extend(_extract_ids(doc))
    return ids


TOOL_EXAMPLES = _extract_all_tool_examples()


class TestDocstringExamplesParse:
    """Every id='...' example in tool docstrings must survive _to_uri → parse."""

    @pytest.mark.parametrize("example_id", TOOL_EXAMPLES)
    def test_example_parses(self, example_id):
        # Skip grep-only examples and comma-separated (those split first)
        if "," in example_id:
            for part in example_id.split(","):
                uri = _to_uri(part.strip())
                p = parse(uri)
                assert p.scheme, f"No scheme for {part!r} → {uri!r}"
        else:
            uri = _to_uri(example_id)
            p = parse(uri)
            assert p.scheme, f"No scheme for {example_id!r} → {uri!r}"


# ── _to_uri dispatch: does the LLM's raw input land correctly? ─────

class TestToUriDispatch:
    """_to_uri must route common LLM inputs to the right scheme."""

    # Papers
    def test_bare_slug(self):
        assert _to_uri("wang2020state") == "paper:wang2020state"

    def test_slug_with_selector(self):
        assert _to_uri("wang2020state~38") == "paper:wang2020state~38"

    def test_slug_with_view(self):
        assert _to_uri("wang2020state/toc") == "paper:wang2020state/toc"

    def test_slug_selector_view(self):
        assert _to_uri("wang2020state~38/summary") == "paper:wang2020state~38/summary"

    def test_empty_gives_bare_paper(self):
        assert _to_uri("") == "paper:"

    # DOIs
    def test_bare_doi(self):
        assert _to_uri("10.1021/jacs.2c01234").startswith("doi:")

    def test_doi_scheme(self):
        assert _to_uri("doi:10.1021/jacs.2c01234") == "doi:10.1021/jacs.2c01234"

    # Files
    def test_docx(self):
        assert _to_uri("report.docx") == "file:report.docx"

    def test_docx_with_selector(self):
        assert _to_uri("report.docx~PLXDX") == "file:report.docx~PLXDX"

    def test_tex(self):
        assert _to_uri("main.tex") == "file:main.tex"

    def test_md(self):
        assert _to_uri("notes.md") == "file:notes.md"

    # Scheme prefixes the LLM might copy from output
    def test_strips_slug_prefix(self):
        assert _to_uri("slug:wang2020state") == "paper:wang2020state"

    # Arxiv
    def test_arxiv(self):
        assert _to_uri("arxiv:2301.12345") == "arxiv:2301.12345"

    # Todo
    def test_todo(self):
        assert _to_uri("todo:fix-the-bug") == "todo:fix-the-bug"


# ── Tilde is the selector separator ────────────────────────────────

class TestTildeSeparator:
    """~ must work as selector separator throughout the parse chain."""

    def test_paper_chunk(self):
        p = parse(_to_uri("wang2020state~38"))
        assert p.path == "wang2020state"
        assert p.selector == "38"
        assert p.range_start == 38

    def test_paper_range(self):
        p = parse(_to_uri("wang2020state~38..42"))
        assert p.path == "wang2020state"
        assert p.range_start == 38
        assert p.range_end == 42

    def test_paper_open_range(self):
        p = parse(_to_uri("wang2020state~38.."))
        assert p.range_start == 38
        assert p.is_open_range

    def test_file_slug_selector(self):
        p = parse(_to_uri("doc.docx~PLXDX"))
        assert p.scheme == "file"
        assert p.path == "doc.docx"
        assert p.selector == "PLXDX"

    def test_selector_plus_view(self):
        p = parse(_to_uri("wang2020state~38/toc"))
        assert p.selector == "38"
        assert p.view == "toc"

    def test_selector_plus_summary(self):
        p = parse(_to_uri("wang2020state~38/summary"))
        assert p.selector == "38"
        assert p.view == "summary"


# ── Hash is NOT a separator (clean break) ──────────────────────────

class TestHashRejected:
    """# must NOT be treated as a selector separator."""

    def test_hash_in_paper_slug_no_selector(self):
        """paper:slug#38 — the # is NOT split, whole thing becomes path."""
        p = parse("paper:slug#38")
        # # is not a separator, so no selector is extracted
        assert p.selector is None
        # The entire 'slug#38' stays in path
        assert "#" in p.path

    def test_hash_in_file_no_selector(self):
        p = parse("file:doc.docx#PLXDX")
        assert p.selector is None


# ── View paths stay with / ─────────────────────────────────────────

class TestViewPaths:
    """/ separates views — these are NOT selectors."""

    def test_toc(self):
        p = parse(_to_uri("wang2020state/toc"))
        assert p.view == "toc"
        assert p.selector is None

    def test_cite_bib(self):
        p = parse(_to_uri("wang2020state/cite/bib"))
        assert p.view == "cite"
        assert p.subview == "bib"

    def test_fig_list(self):
        p = parse(_to_uri("wang2020state/fig"))
        assert p.view == "fig"

    def test_fig_subview(self):
        p = parse(_to_uri("wang2020state/fig/3"))
        assert p.view == "fig"
        assert p.subview == "3"

    def test_fig_deep_subview(self):
        p = parse(_to_uri("wang2020state/fig/3/image/export"))
        assert p.view == "fig"
        assert p.subview == "3/image/export"

    def test_abstract(self):
        p = parse(_to_uri("wang2020state/abstract"))
        assert p.view == "abstract"

    def test_summary(self):
        p = parse(_to_uri("wang2020state/summary"))
        assert p.view == "summary"


# ── Description conciseness ────────────────────────────────────────

# Approximate token count: ~4 chars per token for English
def _approx_tokens(text: str) -> int:
    return len(text) // 4


class TestDescriptionBudget:
    """Tool descriptions must stay concise — LLM context is expensive."""

    MAX_TOKENS = 600  # per tool description

    @pytest.mark.parametrize("fn", [search, get, put, move])
    def test_description_under_budget(self, fn):
        doc = fn.__doc__ or ""
        tokens = _approx_tokens(doc)
        assert tokens < self.MAX_TOKENS, (
            f"{fn.__name__}.__doc__ is ~{tokens} tokens (max {self.MAX_TOKENS})"
        )

    def test_total_under_2000_tokens(self):
        """All tool descriptions combined must fit in a reasonable budget."""
        total = sum(_approx_tokens(fn.__doc__ or "") for fn in (search, get, put, move))
        assert total < 2000, f"Total tool description ~{total} tokens (max 2000)"
