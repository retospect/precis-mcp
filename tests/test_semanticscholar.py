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


def test_provider_is_registered_slug() -> None:
    """The handler must stamp a provider that exists in the providers
    table. Semantic Scholar is registered under the slug ``s2`` — the
    literal ``semanticscholar`` is NOT a row, so stamping it FK-violated
    on every cache write (gripe #39242)."""
    from precis.handlers.semanticscholar import SemanticScholarHandler

    assert SemanticScholarHandler.provider == "s2"


# ---- Citation-graph navigation (refs: / cites:) --------------------


def test_canonical_key_passes_nav_prefix_through(handler) -> None:
    """``refs:`` / ``cites:`` survive canonicalisation as distinct cache
    keys; the identifier is lower-cased (safe for DOI / arXiv / S2)."""
    assert handler._canonical_key("refs:10.1038/Nature12373") == (
        "refs:10.1038/nature12373"
    )
    assert handler._canonical_key("  CITES: 10.x/Y ") == "cites:10.x/y"


def test_canonical_key_nav_prefix_requires_identifier(handler) -> None:
    from precis.errors import BadInput

    with pytest.raises(BadInput):
        handler._canonical_key("refs:")
    with pytest.raises(BadInput):
        handler._canonical_key("cites:   ")


def test_parse_nav_key(handler) -> None:
    assert handler._parse_nav_key("refs:10.x/y") == ("refs", "10.x/y")
    assert handler._parse_nav_key("cites:abc123") == ("cites", "abc123")
    # A plain search key is not a nav key.
    assert handler._parse_nav_key("carbon nanotubes") is None


def test_s2_path_id_maps_bare_and_prefixed_ids(handler) -> None:
    # Bare DOI / arXiv get auto-prefixed for the S2 path.
    assert handler._s2_path_id("10.1038/nature12373") == "DOI:10.1038/nature12373"
    assert handler._s2_path_id("2401.00001") == "ARXIV:2401.00001"
    assert handler._s2_path_id("2401.00001v2") == "ARXIV:2401.00001v2"
    # Explicit prefixes normalise; s2: drops to the bare hash.
    assert handler._s2_path_id("doi:10.x/y") == "DOI:10.x/y"
    assert handler._s2_path_id("arxiv:2401.00001") == "ARXIV:2401.00001"
    assert handler._s2_path_id("s2:abcdef0123") == "abcdef0123"
    assert handler._s2_path_id("CorpusId:215416146") == "CorpusId:215416146"
    # An unrecognised shape is assumed to be a raw S2 hash, passed through.
    assert handler._s2_path_id("deadbeefcafe") == "deadbeefcafe"


def _refs_payload() -> dict:
    """A minimal ``/paper/{id}/references`` response — neighbour nested
    under ``citedPaper`` (the shape the endpoint actually returns)."""
    return {
        "data": [
            {
                "citedPaper": {
                    "title": "Ballistic carbon nanotube transistors",
                    "year": 1998,
                    "authors": [{"name": "S. Tans"}],
                    "externalIds": {"DOI": "10.1038/29954"},
                    "citationCount": 4200,
                }
            },
            {"citedPaper": None},  # S2 returns nulls for unresolved refs
        ]
    }


def test_fetch_graph_references(handler, monkeypatch) -> None:
    """``refs:`` hits the references endpoint, lifts ``citedPaper``,
    drops null rows, and renders one block per neighbour."""
    captured: dict = {}

    def fake_get(url, params):
        captured["url"] = url
        captured["params"] = params
        return _refs_payload()

    monkeypatch.setattr(handler, "_s2_get_json", fake_get)
    result = handler._fetch("refs:10.1038/nature12373")

    assert captured["url"].endswith("/paper/DOI:10.1038/nature12373/references")
    assert len(result.body_blocks) == 1  # the null row is dropped
    assert "Ballistic carbon nanotube transistors" in result.body_blocks[0].text
    assert "10.1038/29954" in result.body_blocks[0].text  # DOI to feed a stub
    assert result.meta["nav"] == "refs"
    assert result.meta["result_count"] == 1


def test_fetch_graph_citations_endpoint(handler, monkeypatch) -> None:
    """``cites:`` hits the citations endpoint and reads ``citingPaper``."""
    captured: dict = {}

    def fake_get(url, params):
        captured["url"] = url
        return {"data": [{"citingPaper": {"title": "Later work", "year": 2020}}]}

    monkeypatch.setattr(handler, "_s2_get_json", fake_get)
    result = handler._fetch("cites:2401.00001")

    assert captured["url"].endswith("/paper/ARXIV:2401.00001/citations")
    assert "Later work" in result.body_blocks[0].text
    assert result.meta["nav"] == "cites"


def test_fetch_graph_empty_is_not_an_error(handler, monkeypatch) -> None:
    """A paper with no recorded references yields a friendly empty body,
    not a raise — the agent learns the graph is bare here."""
    monkeypatch.setattr(handler, "_s2_get_json", lambda url, params: {"data": []})
    result = handler._fetch("refs:10.x/y")
    assert result.meta["result_count"] == 0
    assert "No references found" in result.body_blocks[0].text
