"""PaperHandler tests: get() with views, chunk selectors, search."""

from __future__ import annotations

import pytest

from precis.embedder import MockEmbedder
from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers.paper import PaperHandler, _parse_paper_id
from precis.store import BlockInsert, Store

# ---------------------------------------------------------------------------
# Slug parsing — pure logic, no DB
# ---------------------------------------------------------------------------


class TestParsePaperId:
    def test_plain_slug(self) -> None:
        assert _parse_paper_id("wang2020state") == ("wang2020state", None, None)

    def test_chunk_single(self) -> None:
        slug, rng, view = _parse_paper_id("wang2020state~38")
        assert slug == "wang2020state"
        assert rng == (38, 38)
        assert view is None

    def test_chunk_range(self) -> None:
        slug, rng, view = _parse_paper_id("wang2020state~38..42")
        assert slug == "wang2020state"
        assert rng == (38, 42)
        assert view is None

    def test_view_path_cite_bib(self) -> None:
        slug, rng, view = _parse_paper_id("wang2020state/cite/bib")
        assert slug == "wang2020state"
        assert rng is None
        assert view == "bibtex"

    def test_view_path_abstract(self) -> None:
        _, _, view = _parse_paper_id("wang2020state/abstract")
        assert view == "abstract"

    def test_view_path_toc(self) -> None:
        _, _, view = _parse_paper_id("wang2020state/toc")
        assert view == "toc"

    def test_range_with_view_combo(self) -> None:
        """Phase 3.5: ``slug~A..B/toc`` is the drill-down form."""
        slug, rng, view = _parse_paper_id("wang2020state~46..105/toc")
        assert slug == "wang2020state"
        assert rng == (46, 105)
        assert view == "toc"

    def test_single_chunk_with_view_combo(self) -> None:
        """``slug~38/toc`` is also legal (TOC scoped to a single block)."""
        slug, rng, view = _parse_paper_id("wang2020state~38/toc")
        assert rng == (38, 38)
        assert view == "toc"

    def test_invalid_chunk_selector(self) -> None:
        with pytest.raises(BadInput):
            _parse_paper_id("wang2020state~xyz")

    def test_inverted_range_rejected(self) -> None:
        with pytest.raises(BadInput, match="empty chunk range"):
            _parse_paper_id("wang2020state~5..3")

    def test_unknown_view_path(self) -> None:
        with pytest.raises(BadInput):
            _parse_paper_id("wang2020state/notarealview")

    def test_invalid_slug(self) -> None:
        with pytest.raises(BadInput):
            _parse_paper_id("/cite/bib")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _seed_paper(
    store: Store,
    *,
    slug: str = "wang2020state",
    title: str = "State of the art in nitrate reduction",
    authors: list[str] | None = None,
    year: int = 2020,
    journal: str = "Nature",
    doi: str = "10.1/x",
    abstract: str = "An abstract.",
    blocks: list[str] | None = None,
    embedder: MockEmbedder | None = None,
) -> int:
    cid = store.ensure_corpus("default")
    e = embedder or MockEmbedder(dim=1024)
    ref = store.insert_ref(
        corpus_id=cid,
        kind="paper",
        slug=slug,
        title=title,
        provider="manual",
        meta={
            "authors": authors or [{"name": "Wang, Q."}],
            "year": year,
            "journal": journal,
            "doi": doi,
            "abstract": abstract,
        },
    )
    if blocks is None:
        blocks = ["Introduction.", "Methods.", "Results.", "Discussion."]
    store.insert_blocks(
        ref.id,
        [
            BlockInsert(pos=i, text=t, embedding=e.embed_one(t))
            for i, t in enumerate(blocks)
        ],
    )
    return ref.id


@pytest.fixture
def handler(store: Store) -> PaperHandler:
    return PaperHandler(store=store, embedder=MockEmbedder(dim=1024))


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


class TestOverview:
    def test_basic_overview(self, store: Store, handler: PaperHandler) -> None:
        _seed_paper(store)
        resp = handler.get(id="wang2020state")
        body = resp.body
        assert "wang2020state" in body
        assert "State of the art in nitrate reduction" in body
        assert "Wang, Q." in body
        assert "Nature" in body
        assert "2020" in body
        assert "10.1/x" in body
        assert "4 blocks" in body
        assert "An abstract." in body

    def test_unknown_slug_raises_notfound(self, handler: PaperHandler) -> None:
        with pytest.raises(NotFound):
            handler.get(id="ghost")

    def test_no_id_lists_papers(self, store: Store, handler: PaperHandler) -> None:
        _seed_paper(store, slug="aaa", title="Paper A")
        _seed_paper(store, slug="bbb", title="Paper B")
        resp = handler.get()
        assert "2 papers" in resp.body
        assert "aaa" in resp.body
        assert "bbb" in resp.body

    def test_no_id_no_papers(self, handler: PaperHandler) -> None:
        resp = handler.get()
        assert "no papers ingested" in resp.body


# ---------------------------------------------------------------------------
# Views: abstract / toc / bibtex / ris / endnote
# ---------------------------------------------------------------------------


class TestViews:
    def test_abstract(self, store: Store, handler: PaperHandler) -> None:
        _seed_paper(store, abstract="A long-form abstract here.")
        resp = handler.get(id="wang2020state", view="abstract")
        assert resp.body == "A long-form abstract here."

    def test_abstract_missing(self, store: Store, handler: PaperHandler) -> None:
        _seed_paper(store, abstract="")
        resp = handler.get(id="wang2020state", view="abstract")
        assert "no abstract" in resp.body

    def test_toc(self, store: Store, handler: PaperHandler) -> None:
        _seed_paper(store, blocks=["Block A", "Block B", "Block C"])
        resp = handler.get(id="wang2020state", view="toc")
        body = resp.body
        # Header counts blocks + sections.
        assert "TOC" in body
        assert "3 blocks" in body
        # No headings detected → single implicit section spanning all
        # blocks. Range column shows ``~0..2`` and a preview of the
        # first block surfaces in the row label.
        assert "~0..2" in body
        assert "Block A" in body  # preview from pos=0
        assert "<untitled>" in body

    def test_toc_with_headings_is_hierarchical(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """Phase 3.5: blocks matching heading patterns produce sections."""
        _seed_paper(
            store,
            blocks=[
                "abstract text",
                "■ **INTRODUCTION**",
                "intro body",
                "■ **METHODS**",
                "**Materials**",
                "materials body",
                "■ **RESULTS**",
                "results body",
            ],
        )
        resp = handler.get(id="wang2020state", view="toc")
        body = resp.body
        # All three top-level sections appear with the ■ marker.
        assert "■ INTRODUCTION" in body
        assert "■ METHODS" in body
        assert "■ RESULTS" in body
        # Subsection appears with deeper indent (no ■ marker).
        materials_line = next(
            line
            for line in body.splitlines()
            if "Materials" in line and "■" not in line
        )
        methods_line = next(line for line in body.splitlines() if "■ METHODS" in line)
        materials_indent = len(materials_line) - len(materials_line.lstrip())
        methods_indent = len(methods_line) - len(methods_line.lstrip())
        assert materials_indent > methods_indent
        # Drill-down hint trailer present.
        assert "Next:" in body
        assert "drill into" in body

    def test_toc_drilldown_via_id(self, store: Store, handler: PaperHandler) -> None:
        """`get(id='slug~A..B/toc')` returns a TOC scoped to the range."""
        _seed_paper(
            store,
            blocks=[
                "■ **INTRO**",
                "intro body",
                "■ **METHODS**",
                "method body 1",
                "method body 2",
                "■ **RESULTS**",
                "result body",
            ],
        )
        resp = handler.get(id="wang2020state~2..4/toc")
        body = resp.body
        # Range label appears in header.
        assert "~2..4" in body
        # Only METHODS overlaps the range.
        assert "■ METHODS" in body
        assert "■ INTRO" not in body
        assert "■ RESULTS" not in body

    def test_bibtex(self, store: Store, handler: PaperHandler) -> None:
        _seed_paper(store)
        resp = handler.get(id="wang2020state", view="bibtex")
        b = resp.body
        assert "@article{wang2020state," in b
        assert "title = {State of the art in nitrate reduction}" in b
        assert "author = {Wang, Q.}" in b
        assert "year = {2020}" in b
        assert "journal = {Nature}" in b
        assert "doi = {10.1/x}" in b

    def test_ris(self, store: Store, handler: PaperHandler) -> None:
        _seed_paper(store)
        resp = handler.get(id="wang2020state", view="ris")
        b = resp.body
        assert b.startswith("TY  - JOUR")
        assert "TI  - State of the art in nitrate reduction" in b
        assert "AU  - Wang, Q." in b
        assert "PY  - 2020" in b
        assert "DO  - 10.1/x" in b
        assert b.rstrip().endswith("ER  -")

    def test_endnote(self, store: Store, handler: PaperHandler) -> None:
        _seed_paper(store)
        resp = handler.get(id="wang2020state", view="endnote")
        b = resp.body
        assert "%0 Journal Article" in b
        assert "%T State of the art in nitrate reduction" in b
        assert "%A Wang, Q." in b
        assert "%R 10.1/x" in b

    def test_unknown_view_raises(self, store: Store, handler: PaperHandler) -> None:
        _seed_paper(store)
        with pytest.raises(Unsupported):
            handler.get(id="wang2020state", view="nope")

    def test_view_path_in_id(self, store: Store, handler: PaperHandler) -> None:
        _seed_paper(store)
        resp = handler.get(id="wang2020state/cite/bib")
        assert "@article" in resp.body


# ---------------------------------------------------------------------------
# Chunk selectors
# ---------------------------------------------------------------------------


class TestChunks:
    def test_single_chunk(self, store: Store, handler: PaperHandler) -> None:
        _seed_paper(store, blocks=["intro", "methods", "results"])
        resp = handler.get(id="wang2020state~1")
        assert "wang2020state~1" in resp.body
        assert "methods" in resp.body
        assert "intro" not in resp.body

    def test_chunk_range(self, store: Store, handler: PaperHandler) -> None:
        _seed_paper(store, blocks=["a", "b", "c", "d"])
        resp = handler.get(id="wang2020state~1..2")
        # Block 1 + 2 contents are rendered with their headings.
        assert "# wang2020state~1" in resp.body
        assert "# wang2020state~2" in resp.body
        assert "b" in resp.body and "c" in resp.body
        # Block 3 is NOT rendered as a body chunk — only referenced in
        # the Next: hint as the suggested next-range to read.
        assert "# wang2020state~3" not in resp.body
        # Phase 3.5: Next: trailer offers adjacent ranges + TOC. The
        # adjacent suggestion may render as a degenerate single-block
        # ``~N`` (label "next chunk") or a multi-block ``~N..M``
        # ("next chunk range"); either is fine here. (MCP critic
        # MINOR m6 — single-block trailers no longer emit ``~N..N``.)
        assert "Next:" in resp.body
        assert "next chunk" in resp.body  # matches "next chunk" or "next chunk range"
        assert "TOC of this range" in resp.body

    def test_chunk_out_of_range_404s(self, store: Store, handler: PaperHandler) -> None:
        _seed_paper(store, blocks=["a"])
        with pytest.raises(NotFound):
            handler.get(id="wang2020state~99")

    def test_chunk_view_combo_rejected_for_non_toc(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """Phase 3.5: ``~N..M`` may combine with ``view='toc'`` (drill-down)
        but not with other views \u2014 there's no sensible meaning for a
        range-scoped citation, abstract, or single-chunk view."""
        _seed_paper(store)
        with pytest.raises(BadInput, match="cannot combine"):
            handler.get(id="wang2020state~0", view="bibtex")
        with pytest.raises(BadInput, match="cannot combine"):
            handler.get(id="wang2020state~0", view="abstract")


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class TestSearch:
    def test_search_basic(self, store: Store, handler: PaperHandler) -> None:
        _seed_paper(
            store,
            slug="paper-a",
            title="A",
            blocks=["nitrate reduction copper"],
        )
        _seed_paper(
            store,
            slug="paper-b",
            title="B",
            blocks=["unrelated text"],
        )
        resp = handler.search(q="nitrate reduction", top_k=5)
        assert "paper-a" in resp.body
        assert "block hit" in resp.body

    def test_search_scoped(self, store: Store, handler: PaperHandler) -> None:
        _seed_paper(store, slug="aa", title="A", blocks=["nitrate cycle in soil"])
        _seed_paper(store, slug="bb", title="B", blocks=["nitrate cycle in water"])
        resp = handler.search(q="nitrate", scope="aa")
        assert "aa~" in resp.body
        assert "bb~" not in resp.body

    def test_search_no_q_raises(self, handler: PaperHandler) -> None:
        with pytest.raises(BadInput):
            handler.search(q="")

    def test_search_unknown_scope_raises(self, handler: PaperHandler) -> None:
        with pytest.raises(NotFound):
            handler.search(q="x", scope="nonexistent")

    def test_search_no_hits_lex_only(self, store: Store) -> None:
        # Use a handler without an embedder so we exercise the lex-only
        # path; semantic search would always score nonzero RRF on the
        # closest neighbour even without a lex match.
        lex_only = PaperHandler(store=store, embedder=None)
        _seed_paper(store, blocks=["alpha"])
        resp = lex_only.search(q="zzqqxx")
        assert "no paper blocks match" in resp.body
