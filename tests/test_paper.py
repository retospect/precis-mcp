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
        assert "TOC" in body
        assert "3 blocks" in body
        assert "Block A" in body and "Block B" in body and "Block C" in body
        # positional indicator
        assert "~   0" in body
        assert "~   1" in body
        assert "~   2" in body

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
        assert "wang2020state~1" in resp.body
        assert "wang2020state~2" in resp.body
        assert "b" in resp.body and "c" in resp.body
        assert "wang2020state~3" not in resp.body

    def test_chunk_out_of_range_404s(self, store: Store, handler: PaperHandler) -> None:
        _seed_paper(store, blocks=["a"])
        with pytest.raises(NotFound):
            handler.get(id="wang2020state~99")

    def test_chunk_view_combo_rejected(
        self, store: Store, handler: PaperHandler
    ) -> None:
        _seed_paper(store)
        with pytest.raises(BadInput):
            handler.get(id="wang2020state~0", view="bibtex")


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
