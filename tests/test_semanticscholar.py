"""Tests for the Semantic Scholar handler (#186).

Covers the response-formatting helper and the canonical-key + slug
shape. Network is mocked via httpx so tests stay offline.
"""

from __future__ import annotations

import pytest

from precis.handlers.semanticscholar import _format_paper


def test_format_paper_full_record() -> None:
    """A typical S2 response row renders with every field we project."""
    paper = {
        "title": "Carbon nanotube field-effect transistors",
        "year": 2003,
        "authors": [{"name": "A. Javey"}, {"name": "J. Guo"}],
        "venue": "Nature",
        "externalIds": {"DOI": "10.1038/nature01797", "ArXiv": "0307108"},
        "citationCount": 1742,
        "openAccessPdf": {"url": "https://example.com/paper.pdf"},
        "abstract": "We report on ballistic CNT-FETs operating near…",
    }
    out = _format_paper(paper)
    assert "## Carbon nanotube field-effect transistors (2003)" in out
    assert "A. Javey, J. Guo" in out
    assert "Nature" in out
    assert "1742" in out
    assert "10.1038/nature01797" in out
    assert "https://doi.org/10.1038/nature01797" in out
    assert "0307108" in out
    assert "https://arxiv.org/abs/0307108" in out
    assert "https://example.com/paper.pdf" in out
    assert "ballistic CNT-FETs" in out


def test_format_paper_minimal_record_no_extras() -> None:
    """A bare hit (no abstract / no externalIds / no venue) renders
    cleanly without raising — we just lose the absent fields."""
    paper = {"title": "Anon work", "year": 2001, "authors": []}
    out = _format_paper(paper)
    assert "## Anon work (2001)" in out
    # No section markers for missing fields.
    assert "DOI" not in out
    assert "arXiv" not in out
    assert "Venue" not in out


def test_format_paper_truncates_long_author_lists() -> None:
    """Six authors then ``et al. (N authors)`` to keep the block tight."""
    paper = {
        "title": "Many-author paper",
        "year": 2024,
        "authors": [{"name": f"Author{i}"} for i in range(15)],
    }
    out = _format_paper(paper)
    assert "Author5" in out
    assert "et al. (15 authors)" in out


def test_format_paper_untitled_fallback() -> None:
    """``(untitled)`` placeholder so the heading still renders."""
    paper = {"year": 2020}
    out = _format_paper(paper)
    assert "## (untitled) (2020)" in out


# ---- Canonical key + slug ------------------------------------------


@pytest.fixture
def handler() -> object:
    """A stub handler instance — bypassing ``__init__`` since we only
    exercise the pure-function methods on ``CacheBackedHandler``."""
    from precis.handlers.semanticscholar import SemanticScholarHandler

    return SemanticScholarHandler.__new__(SemanticScholarHandler)


def test_canonical_key_lowercases_and_collapses_whitespace(handler) -> None:
    assert (
        handler._canonical_key("  Carbon  Nanotube  TRANSISTORS ")
        == "carbon nanotube transistors"
    )


def test_canonical_key_rejects_empty_query(handler) -> None:
    from precis.errors import BadInput

    with pytest.raises(BadInput):
        handler._canonical_key("")
    with pytest.raises(BadInput):
        handler._canonical_key("   ")


def test_slug_from_query(handler) -> None:
    """The slug is a kebab from the canonical key, with a fallback for
    queries that slugify to empty."""
    slug = handler._slug_for("carbon nanotube transistors")
    assert "carbon" in slug
    assert "transistors" in slug


def test_recover_key_from_cache_meta(handler) -> None:
    """A cached ref can re-fetch from its meta-stored query string."""
    from types import SimpleNamespace

    ref = SimpleNamespace()
    cache = SimpleNamespace(meta={"query": "graphene heterojunctions"})
    assert handler._recover_key(ref, cache) == "graphene heterojunctions"
