"""Tests for the Wikipedia on-demand handler.

Covers the pure parse helpers, canonical-key/slug shape, the provenance
fence-tag, and the ``_fence_wiki`` decision table. Network is never
touched — the upstream ``_fetch`` path is exercised separately via the
stub-client integration tests.
"""

from __future__ import annotations

import pytest

from precis.handlers.wikipedia import (
    WikipediaHandler,
    _article_url,
    _parse_extract,
    _pick_title,
)
from precis.store._blocks_ops import BlocksMixin
from precis.store._tag_filter import WIKI_TAG, is_wiki_tag, wiki_fence

# ── pure parse helpers ────────────────────────────────────────────────


def test_pick_title_returns_top_hit() -> None:
    body = {
        "query": {
            "search": [
                {"title": "Transformer (deep learning)", "wordcount": 14351},
                {"title": "Attention Is All You Need"},
            ]
        }
    }
    assert _pick_title(body) == "Transformer (deep learning)"


def test_pick_title_empty_results() -> None:
    assert _pick_title({"query": {"search": []}}) is None
    assert _pick_title({}) is None
    assert _pick_title({"query": {}}) is None


def test_parse_extract_normal_page() -> None:
    body = {
        "query": {
            "pages": [
                {
                    "pageid": 4346142,
                    "title": "Attention (machine learning)",
                    "extract": "In machine learning, attention is a method…",
                }
            ]
        }
    }
    title, pageid, extract = _parse_extract(body, fallback_title="x")
    assert title == "Attention (machine learning)"
    assert pageid == 4346142
    assert extract.startswith("In machine learning")


def test_parse_extract_missing_page_falls_back() -> None:
    body = {"query": {"pages": [{"title": "Deleted", "missing": True}]}}
    title, pageid, extract = _parse_extract(body, fallback_title="fallback")
    assert title == "fallback"
    assert pageid is None
    assert extract == ""


def test_article_url_encodes_spaces_and_specials() -> None:
    assert (
        _article_url("en", "Attention (machine learning)")
        == "https://en.wikipedia.org/wiki/Attention_%28machine_learning%29"
    )


# ── canonical key + slug ──────────────────────────────────────────────


def test_canonical_key_lowercases_and_collapses_ws() -> None:
    h = WikipediaHandler.__new__(WikipediaHandler)
    assert h._canonical_key("  CRISPR   Gene  Editing ") == "crispr gene editing"


def test_canonical_key_rejects_empty() -> None:
    from precis.errors import BadInput

    h = WikipediaHandler.__new__(WikipediaHandler)
    with pytest.raises(BadInput):
        h._canonical_key("   ")


# ── provenance fence ──────────────────────────────────────────────────


def test_wiki_tag_constant_and_detector() -> None:
    assert WIKI_TAG == "ORIGIN:wikipedia"
    assert is_wiki_tag("ORIGIN:wikipedia")
    assert is_wiki_tag("  ORIGIN:wikipedia  ")
    assert not is_wiki_tag("ORIGIN:gutenberg")
    assert not is_wiki_tag("bookmark")


def test_wiki_fence_is_parameterless_sql() -> None:
    sql = wiki_fence("r")
    assert "NOT EXISTS" in sql
    assert "ORIGIN" in sql and "wikipedia" in sql
    assert "%s" not in sql  # parameterless — safe under CTE double-splice


@pytest.mark.parametrize(
    ("tags", "kind", "expect_fence"),
    [
        (None, None, True),  # default search → fenced
        (None, "paper", True),  # cross-kind / other kind → fenced
        (None, "*", True),  # explicit fan → fenced
        (None, "wikipedia", False),  # explicit wiki scope → lifted
        (["ORIGIN:wikipedia"], None, False),  # explicit opt-in → lifted
        (["bookmark"], None, True),  # unrelated tag → still fenced
    ],
)
def test_fence_wiki_decision_table(tags, kind, expect_fence) -> None:
    assert BlocksMixin._fence_wiki(tags, kind) is expect_fence


# ── spec sanity ───────────────────────────────────────────────────────


def test_spec_surface() -> None:
    spec = WikipediaHandler.spec
    assert spec.kind == "wikipedia"
    assert spec.supports_get and spec.supports_search and spec.supports_search_hits
    assert spec.supports_tag and spec.supports_link
    assert not spec.is_numeric
