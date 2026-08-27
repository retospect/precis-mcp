"""PaperHandler tests: get() with views, chunk selectors, search."""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.embedder import MockEmbedder
from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers.draft import DraftHandler
from precis.handlers.paper import PaperHandler, _maybe_resolve_doi, _parse_paper_id
from precis.handlers.todo import TodoHandler
from precis.runtime import PrecisRuntime
from precis.store import BlockInsert, Store
from precis.store.types import Tag
from precis.utils import handle_registry
from tests.conftest import chunk_handle, record_handle

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

    def test_list_view_error_echoes_requested_kind(self) -> None:
        """gr48511: `get(kind='cfp', id='/recent')` used to reply "paper has
        no list view" — leaking the internal handler kind. The message and
        the next-hint must echo the caller-requested kind instead."""
        with pytest.raises(BadInput) as exc:
            _parse_paper_id("/recent", "cfp")
        assert "cfp has no list view" in str(exc.value)
        assert exc.value.next is not None and "get(kind='cfp')" in exc.value.next
        # The default (paper) path is unchanged.
        with pytest.raises(BadInput) as exc2:
            _parse_paper_id("/recent")
        assert "paper has no list view" in str(exc2.value)


# ---------------------------------------------------------------------------
# DOI-form id resolution
# ---------------------------------------------------------------------------


class TestResolveDoi:
    """``_maybe_resolve_doi`` routes DOI-form ids through meta->>'doi'."""

    def test_non_doi_passthrough(self, store: Store) -> None:
        # Slug-form inputs are untouched — no DB lookup, no error.
        assert _maybe_resolve_doi(store, "wang2020state") == "wang2020state"
        assert _maybe_resolve_doi(store, "wang2020state~38") == "wang2020state~38"
        assert _maybe_resolve_doi(store, "wang2020state/abstract") == (
            "wang2020state/abstract"
        )

    def test_doi_translates_to_slug(self, store: Store) -> None:
        _seed_paper(store, slug="wang2020state", doi="10.1111/jnc.13915")
        assert _maybe_resolve_doi(store, "10.1111/jnc.13915") == "wang2020state"

    def test_doi_preserves_chunk_selector(self, store: Store) -> None:
        _seed_paper(store, slug="wang2020state", doi="10.1038/s41598-023-44772-6")
        assert (
            _maybe_resolve_doi(store, "10.1038/s41598-023-44772-6~38..42")
            == "wang2020state~38..42"
        )

    def test_doi_with_dots_in_suffix(self, store: Store) -> None:
        # Suffixes like ``jnc.13915`` carry dots; the slug parser would
        # reject these at the ``.`` — DOI resolution short-circuits that.
        _seed_paper(store, slug="smith2019foo", doi="10.1016/j.ejphar.2025.177633")
        assert (
            _maybe_resolve_doi(store, "10.1016/j.ejphar.2025.177633") == "smith2019foo"
        )

    def test_unknown_doi_raises_notfound(self, store: Store) -> None:
        _seed_paper(store, doi="10.1/x")
        with pytest.raises(NotFound, match="DOI .* not ingested"):
            _maybe_resolve_doi(store, "10.9999/nope")

    def test_unknown_doi_hint_has_no_empty_scope_placeholder(
        self, store: Store
    ) -> None:
        """The chase-target hint must not carry an unfilled ``scope={}``
        placeholder — it should show a concrete example dict (#39253)."""
        _seed_paper(store, doi="10.1/x")
        with pytest.raises(NotFound) as exc_info:
            _maybe_resolve_doi(store, "10.9999/nope")
        hint = exc_info.value.next
        assert hint is not None
        hint_text = hint if isinstance(hint, str) else " ".join(hint)
        assert "scope={}" not in hint_text
        assert "scope={'electrode': 'Cu'" in hint_text

    def test_get_by_doi_end_to_end(self, store: Store, handler: PaperHandler) -> None:
        _seed_paper(store, slug="wang2020state", doi="10.1111/jnc.13915")
        resp = handler.get(id="10.1111/jnc.13915")
        assert record_handle(store, "wang2020state") in resp.body
        assert "State of the art" in resp.body

    def test_get_by_doi_with_view_kwarg(
        self, store: Store, handler: PaperHandler
    ) -> None:
        # DOI + ``view=`` kwarg is the supported combo (path-view
        # forms like ``/abstract`` don't work on DOIs).
        _seed_paper(
            store,
            slug="wang2020state",
            doi="10.1111/jnc.13915",
            abstract="A long-form abstract here.",
        )
        resp = handler.get(id="10.1111/jnc.13915", view="abstract")
        assert "A long-form abstract here." in resp.body


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _seed_paper(
    store: Store,
    *,
    slug: str = "wang2020state",
    title: str = "State of the art in nitrate reduction",
    authors: list[dict[str, Any]] | None = None,
    year: int = 2020,
    journal: str = "Nature",
    doi: str = "10.1/x",
    abstract: str = "An abstract.",
    blocks: list[str] | None = None,
    embedder: MockEmbedder | None = None,
) -> int:
    e = embedder or MockEmbedder(dim=1024)
    # v2: authors + year are first-class columns; journal/doi/abstract
    # stay in meta. The bibtex / RIS / EndNote renderers read
    # ``Ref.authors`` / ``Ref.year`` directly.
    author_dicts = authors or [{"name": "Wang, Q."}]
    ref = store.insert_ref(
        kind="paper",
        slug=slug,
        title=title,
        provider="manual",
        authors=author_dicts,
        year=year,
        meta={
            "authors": author_dicts,  # legacy duplicate; some readers still consult meta
            "year": year,  # legacy duplicate
            "journal": journal,
            "doi": doi,
            "abstract": abstract,
        },
    )
    # Mirror the ingest path: every paper ref also lands an alias
    # row so DOI-form ``get(id=...)`` lookups resolve via
    # `ref_identifiers` rather than scanning `refs.meta`.
    if doi:
        store.insert_ref_identifiers(ref.id, [("doi", doi, "manual")])
    if blocks is None:
        blocks = ["Introduction.", "Methods.", "Results.", "Discussion."]
    store.blocks.insert_blocks(
        ref.id,
        [
            BlockInsert(pos=i, text=t, embedding=e.embed_one(t))
            for i, t in enumerate(blocks)
        ],
    )
    return ref.id


@pytest.fixture
def handler(hub: Hub) -> PaperHandler:
    return PaperHandler(hub=hub)


def test_cited_chunk_returns_universal_handle(store: Store) -> None:
    """``_cited_chunk`` (the reader's ``?chunk=`` / Jump / TOC resolver) returns
    the chunk's ``pc<chunk_id>`` universal handle alongside ord/text/page, so
    the Jump card can show the durable pointer, not just "chunk N".

    Real-PG regression: the raw SQL selects ``chunk_id`` (the PK column is
    ``chunk_id``, not ``id``) — the FakeStore route tests never execute this
    SQL, so a wrong column name would pass CI there and 500 in production.
    """
    from precis_web.routes.papers import _cited_chunk

    ref_id = _seed_paper(
        store,
        slug="carbonx",
        blocks=["Introduction.", "Table 1 summarizes properties.", "Methods."],
    )
    cited = _cited_chunk(store, ref_id, "1")
    assert cited is not None
    assert cited["ord"] == 1
    assert cited["text"].startswith("Table 1")
    assert cited["handle"] == chunk_handle(store, "carbonx", ord=1)
    assert cited["handle"].startswith("pc")
    # The compound handle the TOC displays resolves for THIS paper …
    assert _cited_chunk(store, ref_id, f"pa{ref_id}~1") is not None
    # … but a compound handle naming a different paper never resolves here.
    assert _cited_chunk(store, ref_id, "pa999999~1") is None


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


class TestOverview:
    def test_basic_overview(self, store: Store, handler: PaperHandler) -> None:
        _seed_paper(store)
        resp = handler.get(id="wang2020state")
        body = resp.body
        # the paper is addressed by its record handle (pa<id>).
        assert record_handle(store, "wang2020state") in body
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
        # Total corpus depth in the header (#38683): two papers, four
        # body chunks each from ``_seed_paper`` → 8 chunks.
        assert "(8 chunks)" in resp.body
        assert record_handle(store, "aaa") in resp.body
        assert record_handle(store, "bbb") in resp.body

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
        # The abstract view now carries a slug header, the title in
        # italics, and a Next: trailer so the caller knows which paper
        # they're reading and where to drill next. (MCP critic NIT.)
        assert "A long-form abstract here." in resp.body
        assert resp.body.startswith("# wang2020state - abstract")
        assert "Next:" in resp.body

    def test_abstract_missing(self, store: Store, handler: PaperHandler) -> None:
        _seed_paper(store, abstract="")
        resp = handler.get(id="wang2020state", view="abstract")
        assert "no abstract" in resp.body

    def test_toc(self, store: Store, handler: PaperHandler) -> None:
        """F20 (superseded): view='toc' renders a flat
        per-chunk keyword table — one TOON row per chunk — off the
        ``chunks.keywords`` discovery layer, not a heading hierarchy."""
        _seed_paper(store, blocks=["Block A", "Block B", "Block C"])
        resp = handler.get(id="wang2020state", view="toc")
        body = resp.body
        # Header counts chunks (F20 renamed the old "N blocks" count).
        assert "TOC" in body
        assert "3 chunks" in body
        # TOON table with one row per chunk. Keyword cells are empty
        # until the chunk_keywords worker runs (not run in this seed),
        # so we assert on the stable per-chunk handle suffixes.
        assert "keywords}" in body
        for pos in range(3):
            assert f"~{pos}" in body

    def test_toc_is_flat_not_hierarchical(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """F20 superseded the old persistent discovery layer's H1/H2 hierarchy: heading-like
        blocks no longer produce indented ■-marked sections. Every
        chunk is a flat TOON row — this guards that the hierarchy
        renderer stays retired."""
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
        assert "TOC" in body
        assert "8 chunks" in body
        # Flat: one row per chunk, addressed by handle suffix.
        for pos in range(8):
            assert f"~{pos}" in body
        # No heading-hierarchy markers from the retired discovery-layer renderer.
        assert "■ INTRODUCTION" not in body
        assert "■ METHODS" not in body

    def test_toc_drilldown_via_id(self, store: Store, handler: PaperHandler) -> None:
        """`get(id='slug~A..B/toc')` returns a TOC scoped to the range —
        range-scoping survived the F20 rewrite; only chunks inside the
        range render."""
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
        # Range label appears in header and the scope is honoured:
        # only chunks 2..4 render, 0..1 and 5..6 are excluded.
        assert "~2..4" in body
        assert "3 chunks" in body
        for pos in (2, 3, 4):
            assert f"~{pos}" in body
        assert "~0" not in body
        assert "~1" not in body
        assert "~5" not in body
        assert "~6" not in body

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
        # Phase F validates view= against per-kind enum and raises
        # BadInput rather than Unsupported; either is a clean
        # rejection, but the v2 enum-validation path picked BadInput
        # so the agent gets the accepted-views list in the same
        # error envelope.
        with pytest.raises((Unsupported, BadInput)):
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
        assert chunk_handle(store, "wang2020state", ord=1) in resp.body
        assert "methods" in resp.body
        assert "intro" not in resp.body

    def test_chunk_range(self, store: Store, handler: PaperHandler) -> None:
        _seed_paper(store, blocks=["a", "b", "c", "d"])
        resp = handler.get(id="wang2020state~1..2")
        # Block 1 + 2 contents are rendered with their (handle) headings.
        assert f"# {chunk_handle(store, 'wang2020state', ord=1)}" in resp.body
        assert f"# {chunk_handle(store, 'wang2020state', ord=2)}" in resp.body
        assert "b" in resp.body and "c" in resp.body
        # Block 3 is NOT rendered as a body chunk — only referenced in
        # the Next: hint as the suggested next read.
        assert f"# {chunk_handle(store, 'wang2020state', ord=3)}" not in resp.body
        # Trimmed trailer (2026-05-04 token-budget fix): forward
        # range + full TOC + BibTeX. ``previous chunk`` and
        # ``TOC of this range`` were dropped — the former is rarely
        # the right move when reading forward; the latter is usually
        # one section header for a small range and wastes tokens in
        # every chunk response.
        assert "Next:" in resp.body
        assert "next chunk" in resp.body  # matches "next chunk" or "next N chunks"
        assert "full TOC" in resp.body
        assert "TOC of this range" not in resp.body
        assert "previous chunk" not in resp.body

    def test_single_chunk_promotes_search_toc_and_range(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """Single-block reads (the typical landing from a search hit)
        must NOT advertise ``next chunk: ~N+1`` as the primary
        follow-up.

        Pre-fix, the trailer's first hint was ``next chunk: ~N+1``,
        which trained a linear ~N → ~N+1 → ~N+2 paging scan. Observed
        in the dopamine-modulation runner on 2026-05-04: the worker
        read gerfen2011~13 through ~21 across ~10 LLM turns
        (~3 min/turn = 30 min wall-clock) when a single
        ``search(scope=...)`` or ``view='toc'`` would have finished
        in one turn.

        Post-fix, single-block trailers lead with:
          1. in-paper ``search(scope=...)`` — fused lexical+embedding
          2. ``view='toc'`` — structural map
          3. forward range (``~N+1..N+5``), NOT single ``~N+1``
        """
        _seed_paper(store, blocks=[f"para {i}" for i in range(20)])
        resp = handler.get(id="wang2020state~3")
        assert "Next:" in resp.body
        # Promoted: in-paper semantic search.
        _pa = record_handle(store, "wang2020state")
        assert f"search(kind='paper', q='your query', scope='{_pa}')" in resp.body
        # Promoted: TOC.
        assert "view='toc'" in resp.body
        # Forward read is a 5-block span via relative nav, not a
        # bare next-block: ``pc<id>+1..5`` (5 chunks after the one just read).
        assert "+1..5" in resp.body
        # And the bare-block legacy hint is NOT the primary follow-up.
        assert "next chunk: get(kind='paper', id='wang2020state~4')" not in resp.body

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
        resp = handler.search(q="nitrate reduction", page_size=5)
        assert chunk_handle(store, "paper-a") in resp.body
        assert "block hit" in resp.body

    def test_search_scoped(self, store: Store, handler: PaperHandler) -> None:
        _seed_paper(store, slug="aa", title="A", blocks=["nitrate cycle in soil"])
        _seed_paper(store, slug="bb", title="B", blocks=["nitrate cycle in water"])
        resp = handler.search(q="nitrate", scope="aa")
        assert chunk_handle(store, "aa") in resp.body
        assert chunk_handle(store, "bb") not in resp.body

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
        lex_only = PaperHandler(hub=Hub(store=store))
        _seed_paper(store, blocks=["alpha"])
        resp = lex_only.search(q="zzqqxx")
        assert "no paper blocks match" in resp.body

    def test_search_doi_miss_routes_to_request_doi(self, store: Store) -> None:
        """DOI-shaped query that misses should point to request_doi.md
        (perplexity / fetch pipeline) rather than suggest a wider
        lexical search that will also miss. Friction fix for the
        shotgun pattern where agents fire 3-5 keyword variants
        trying to find a paper that isn't in the corpus.
        """
        lex_only = PaperHandler(hub=Hub(store=store))
        _seed_paper(store, blocks=["alpha"])
        resp = lex_only.search(q="10.1038/nature10352")
        body = resp.body
        assert "no paper blocks match" in body
        assert "request_doi.md" in body
        assert "10.1038/nature10352" in body
        # The generic "widen the lexical net" hint should not appear
        # for DOI misses - we know widening won't help.
        assert "widen the lexical net" not in body

    def test_singleton_hit_no_redundant_trailer(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """MCP critic MINOR-$: ``page_size=1`` against a corpus with many
        matches used to render a two-line nav (``scope=<that slug>``
        + ``+ <salient term>``) that was 46 % of the response and
        100 % redundant — scoping to the only hit's own paper is a
        no-op, and the salient-term hint is moot when the caller
        already has a tight match.

        New shape: a single ``page_size=10`` widen hint replaces both
        lines. ``scope=`` with the hit's own slug must NOT appear,
        and the long-form salient-term suggestion must be gone too.
        """
        # Seed multiple papers all matching the same word so a
        # ``page_size=1`` query has many more matches than it returned.
        for slug in ("paper-a", "paper-b", "paper-c", "paper-d"):
            _seed_paper(
                store,
                slug=slug,
                title=f"Paper {slug}",
                blocks=[f"photocatalytic nitrogen reduction in {slug}"],
            )
        resp = handler.search(q="photocatalytic", page_size=1)
        # Header still announces "1 of N" so the caller knows there
        # are more matches.
        assert " of " in resp.body
        # New: a page_size=10 widen hint is present.
        assert "page_size=10" in resp.body
        assert "see more of the" in resp.body
        # Old: scope=<self> is no longer suggested.
        # ``scope='paper-a'`` would only appear in the legacy two-
        # line trailer; pin its absence.
        assert "narrow to blocks inside" not in resp.body
        assert "tighten the query with a hit-specific token" not in resp.body

    def test_multi_hit_keeps_full_nav(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """The singleton suppression is *only* for ``len(hits) == 1`` —
        multi-hit pages still get the scope + salient-term suggestions
        because each piece of the nav is genuinely useful when the
        caller has multiple matches to triangulate from."""
        for slug in ("paper-a", "paper-b", "paper-c"):
            _seed_paper(
                store,
                slug=slug,
                title=f"Paper {slug}",
                blocks=[f"photocatalytic reduction in {slug}"],
            )
        resp = handler.search(q="photocatalytic", page_size=2)
        # Two-line nav must still be present.
        assert "narrow to blocks inside" in resp.body
        assert "tighten the query with a hit-specific token" in resp.body

    # ── exclude= ──────────────────────────────────────────────────

    def test_search_exclude_drops_listed_papers(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """``exclude=['slug']`` drops every block of that paper from
        the result set, surfacing the next papers in the rank order.
        This is the canonical "show me hits 6-N" pagination idiom."""
        for slug in ("paper-a", "paper-b", "paper-c"):
            _seed_paper(
                store,
                slug=slug,
                title=f"Paper {slug}",
                blocks=[f"photocatalytic reduction in {slug}"],
                doi=f"10.1/{slug}",
            )
        # Without exclude: all three papers.
        full = handler.search(q="photocatalytic", page_size=10)
        assert "paper-a" in full.body
        assert "paper-b" in full.body
        assert "paper-c" in full.body
        # With exclude: paper-a drops out.
        excluded = handler.search(q="photocatalytic", page_size=10, exclude=["paper-a"])
        assert chunk_handle(store, "paper-a") not in excluded.body
        assert "paper-b" in excluded.body
        assert "paper-c" in excluded.body

    def test_search_exclude_accepts_slug_with_selector(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """A copy-pasted hit handle (``slug~38``) in ``exclude=`` is
        normalised to the bare slug. Coarse-only by design — the
        whole paper drops, not just the one block."""
        _seed_paper(
            store,
            slug="paper-a",
            title="A",
            blocks=["photocatalytic copper", "photocatalytic zinc"],
            doi="10.1/paper-a",
        )
        _seed_paper(
            store,
            slug="paper-b",
            title="B",
            blocks=["photocatalytic iron"],
            doi="10.1/paper-b",
        )
        resp = handler.search(
            q="photocatalytic",
            page_size=10,
            exclude=["paper-a~0"],  # copy-pasted handle
        )
        assert chunk_handle(store, "paper-a") not in resp.body
        assert chunk_handle(store, "paper-b") in resp.body

    def test_search_exclude_accepts_doi(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """``exclude=['10.1234/x']`` resolves the DOI to a slug before
        the SQL filter, so DOI-form exclude entries work the same as
        slug-form ones."""
        _seed_paper(
            store,
            slug="paper-a",
            title="A",
            blocks=["alpha topic"],
            doi="10.1111/jnc.13915",
        )
        _seed_paper(
            store,
            slug="paper-b",
            title="B",
            blocks=["alpha topic"],
            doi="10.1/b",
        )
        resp = handler.search(
            q="alpha",
            page_size=10,
            exclude=["10.1111/jnc.13915"],
        )
        assert chunk_handle(store, "paper-a") not in resp.body
        assert chunk_handle(store, "paper-b") in resp.body

    def test_search_exclude_silently_drops_stale_slugs(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """An unknown slug in ``exclude=`` is silently dropped — the
        agent's exclude list may carry slugs that no longer resolve
        (renamed, soft-deleted, or just typos), and we'd rather
        quietly skip than fail the whole search.

        Mixed list (one stale + one valid) still excludes the valid
        one, proving the stale entry isn't poisoning the call.
        """
        _seed_paper(
            store,
            slug="paper-a",
            title="A",
            blocks=["alpha topic"],
            doi="10.1/a",
        )
        _seed_paper(
            store,
            slug="paper-b",
            title="B",
            blocks=["alpha topic"],
            doi="10.1/b",
        )
        resp = handler.search(
            q="alpha",
            page_size=10,
            exclude=["does-not-exist", "paper-a"],
        )
        # Valid slug still drops; stale slug is no-op (no error).
        assert chunk_handle(store, "paper-a") not in resp.body
        assert chunk_handle(store, "paper-b") in resp.body

    def test_search_exclude_accepts_draft_ref_container(
        self, store: Store, handler: PaperHandler, hub: Hub
    ) -> None:
        """``exclude=['dr…']`` drops every paper cited anywhere in that
        draft — the container form, not a bare slug
        (docs/backlog/discovery-exclude-by-container.md)."""
        _seed_paper(
            store,
            slug="paper-a",
            title="A",
            blocks=["photocatalytic reduction in paper-a"],
            doi="10.1/dr-a",
        )
        _seed_paper(
            store,
            slug="paper-b",
            title="B",
            blocks=["photocatalytic reduction in paper-b"],
            doi="10.1/dr-b",
        )
        proj = TodoHandler(hub=hub).put(text="proj", meta={"rotation_root": True})
        proj_id = int(proj.body.split("id=")[1].split()[0].rstrip(",.()"))
        draft = DraftHandler(hub=hub)
        draft.put(id="exclude-draft", title="Cites paper-a", project=proj_id)
        paper_a_ref = store.get_ref(kind="paper", id="paper-a")
        assert paper_a_ref is not None
        handle_a = handle_registry.format_handle("paper", paper_a_ref.id)
        draft.put(
            id="exclude-draft",
            chunk_kind="paragraph",
            text=f"builds on [{handle_a}]",
            at={"last": True},
        )
        draft_ref = store.get_ref(kind="draft", id="exclude-draft")
        assert draft_ref is not None

        resp = handler.search(
            q="photocatalytic", page_size=10, exclude=[f"dr{draft_ref.id}"]
        )
        assert chunk_handle(store, "paper-a") not in resp.body
        assert chunk_handle(store, "paper-b") in resp.body

    def test_search_exclude_unknown_draft_container_raises_bad_input(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """Unlike a stale bare paper slug (silently dropped), a
        caller-named ``dr…`` container that doesn't resolve is a
        visible mistake — it names the offending entry."""
        with pytest.raises(BadInput, match="dr999999"):
            handler.search(q="photocatalytic", page_size=10, exclude=["dr999999"])

    def test_search_exclude_total_header_reflects_remainder(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """The ``N of K`` header reports the *remaining* universe
        after exclusion, not the global count. A 7B caller seeing
        ``# 2 of 3`` after dropping one paper knows there's one
        more page worth; ``# 2 of 4`` would over-count and bait the
        agent into a paginate that can't move forward.

        Seeds 4 matching papers so ``page_size=2`` triggers the ``N of K``
        form in both branches (the format collapses to ``# N`` when
        ``total == n_returned``).
        """
        for slug in ("paper-a", "paper-b", "paper-c", "paper-d"):
            _seed_paper(
                store,
                slug=slug,
                title=f"Paper {slug}",
                blocks=[f"alpha topic in {slug}"],
                doi=f"10.1/{slug}",
            )
        # Without exclude: 2 of 4.
        full = handler.search(q="alpha", page_size=2)
        assert " of 4" in full.body
        # With one excluded: 2 of 3 — header subtracts the dropped ref.
        excluded = handler.search(q="alpha", page_size=2, exclude=["paper-a"])
        assert " of 3" in excluded.body
        # And specifically NOT "of 4" — would lie about how much
        # is still paginate-able.
        assert " of 4" not in excluded.body

    def test_search_next_trailer_offers_page_continuation(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """When ``total > len(hits)``, the multi-hit Next: block
        offers a ``page=2`` continuation so the agent can paginate
        without managing exclude lists by hand. (Pagination was
        moved off ``exclude=[...]`` onto the canonical ``page=`` knob
        — see the inline comment in
        :meth:`PaperHandler.search`.)"""
        for slug in ("paper-a", "paper-b", "paper-c", "paper-d", "paper-e"):
            _seed_paper(
                store,
                slug=slug,
                title=f"Paper {slug}",
                blocks=[f"photocatalytic reduction in {slug}"],
                doi=f"10.1/{slug}",
            )
        resp = handler.search(q="photocatalytic", page_size=2)
        body = resp.body
        # Trailer offers a page= continuation.
        assert "page=2" in body
        # And surfaces the total so the agent sees how many remain.
        assert "5 hits" in body or "of 5" in body

    def test_search_with_prior_exclude_keeps_filter(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """``exclude=`` still works as a hand-skip filter — it's no
        longer the recommended pagination knob, but a caller passing
        ``exclude=['paper-a']`` still gets a result set without
        paper-a in it."""
        for slug in ("paper-a", "paper-b", "paper-c", "paper-d"):
            _seed_paper(
                store,
                slug=slug,
                title=f"Paper {slug}",
                blocks=[f"photocatalytic reduction in {slug}"],
                doi=f"10.1/{slug}",
            )
        resp = handler.search(
            q="photocatalytic",
            page_size=2,
            exclude=["paper-a"],
        )
        body = resp.body
        # paper-a was the requested exclude; it must not show up
        # in the result handles.
        assert chunk_handle(store, "paper-a") not in body
        # And at least one of the non-excluded papers' chunk handles is present.
        assert any(chunk_handle(store, f"paper-{ch}") in body for ch in "bcd")

    def test_search_exclude_trailer_singleton_keeps_widen(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """Singleton-hit branch (``len(hits) == 1``) keeps the
        existing ``page_size=10`` widen hint and does NOT render an
        exclude-continuation — widening is the right next step
        when only one match was returned."""
        for slug in ("paper-a", "paper-b", "paper-c"):
            _seed_paper(
                store,
                slug=slug,
                title=f"Paper {slug}",
                blocks=[f"alpha topic in {slug}"],
                doi=f"10.1/{slug}",
            )
        resp = handler.search(q="alpha", page_size=1)
        body = resp.body
        assert "page_size=10" in body
        # Singleton branch must not render an exclude continuation.
        assert "exclude=[" not in body


# ── NotFound nearest-match suggestions ─────────────────────────────


class TestNearestMatchSuggestions:
    """Round-2 critic finding: a typo in a paper slug used to surface
    a bare ``[error:NotFound] paper slug 'wang2020stat' not found`` —
    no breadcrumb back to the real slug. The handler now runs a
    ``difflib`` close-match scan over the paper corpus and surfaces
    the top suggestions in the error envelope's ``options:`` line so
    the agent can recover in one step.

    These tests pin three properties:

      1. **Plausible typos surface suggestions.** One-char drops /
         transpositions / single-letter mistakes get a useful ``options:``
         list.
      2. **Far-off queries get NO suggestions.** When the user types a
         topic phrase ("nitrate reduction") into the slug slot, we don't
         clutter the error with random noise. Empty options → no
         ``options:`` line.
      3. **The suggestion path covers all three NotFound emission sites
         (get, search-scope, tag/link).** All three resolve through
         ``_suggest_paper_slugs`` so the experience is uniform.
    """

    def test_typo_in_get_surfaces_close_match(
        self, store: Store, handler: PaperHandler
    ) -> None:
        _seed_paper(store, slug="wang2020state", title="A")
        with pytest.raises(NotFound) as exc_info:
            handler.get(id="wang2020stat")  # one char short
        # Suggestions appear on the .options field of the error.
        assert exc_info.value.options is not None
        assert "wang2020state" in exc_info.value.options

    def test_typo_in_search_scope_surfaces_close_match(
        self, store: Store, handler: PaperHandler
    ) -> None:
        _seed_paper(
            store, slug="wang2020state", title="A", blocks=["nitrate reduction"]
        )
        with pytest.raises(NotFound) as exc_info:
            handler.search(q="nitrate", scope="wang2020stat")
        assert exc_info.value.options is not None
        assert "wang2020state" in exc_info.value.options

    def test_typo_in_tag_target_surfaces_close_match(
        self, store: Store, handler: PaperHandler
    ) -> None:
        _seed_paper(store, slug="wang2020state", title="A")
        with pytest.raises(NotFound) as exc_info:
            handler.tag(id="wang2020stat", add=["topic:foo"])
        assert exc_info.value.options is not None
        assert "wang2020state" in exc_info.value.options

    def test_far_off_query_emits_no_suggestions(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """When the agent types something that looks like a search query
        rather than a slug typo, we emit NO ``options=`` so the error
        envelope stays clean."""
        _seed_paper(store, slug="wang2020state", title="A")
        with pytest.raises(NotFound) as exc_info:
            handler.get(id="completely-unrelated-topic-string")
        # No spurious suggestions when nothing clears the cutoff.
        assert exc_info.value.options is None

    def test_empty_corpus_emits_no_suggestions(self, handler: PaperHandler) -> None:
        """Defensive: don't crash or fabricate suggestions on an empty
        corpus. The error must still raise cleanly."""
        with pytest.raises(NotFound) as exc_info:
            handler.get(id="anything")
        assert exc_info.value.options is None

    def test_suggestions_capped_to_top_n(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """Even with many close-match candidates we surface no more
        than ``_SUGGEST_TOP_N`` (default 3) so the error envelope
        doesn't bloat into a corpus dump."""
        # Seed several slugs that all start with the same prefix.
        for n in range(8):
            _seed_paper(store, slug=f"smith2020paper{n}", title=f"Paper {n}")
        with pytest.raises(NotFound) as exc_info:
            handler.get(id="smith2020paper")  # 8 close matches
        assert exc_info.value.options is not None
        assert len(exc_info.value.options) <= 3

    def test_suggestion_renders_in_error_envelope(
        self, store: Store, hub: Hub, handler: PaperHandler
    ) -> None:
        """End-to-end: the rendered envelope (the string the LLM
        actually sees) must contain the ``options:`` line populated
        with the suggested slug. Regression for the case where the
        suggestion silently lands on .options but then never renders.

        We use the canonical :meth:`PrecisRuntime.render_error` here
        rather than rebuilding the format inline — that's the same
        method the MCP transport layer calls, so testing through it
        catches any future format drift in one place.
        """
        from precis.config import PrecisConfig
        from precis.runtime import PrecisRuntime

        _seed_paper(store, slug="wang2020state", title="A")
        # Build a minimal runtime — we only need the render_error()
        # method, which doesn't touch the hub or config beyond
        # construction. ``PrecisConfig()`` reads env vars / defaults.
        runtime = PrecisRuntime(config=PrecisConfig(), hub=hub)
        try:
            handler.get(id="wang2020stat")
        except NotFound as err:
            envelope = runtime.render_error(err)
        else:
            pytest.fail("expected NotFound")
        assert "options:" in envelope
        assert "wang2020state" in envelope


class TestNumericIdTagLink:
    """The web addresses papers by numeric ``ref_id`` (the triage queue's
    "Clear flag" → ``tag(remove=['needs-triage'])`` and the detail page's
    link ops). ``_resolve_paper_slug`` previously stringified the id and
    looked it up as a *cite_key* — a guaranteed miss that raised
    ``NotFound``; the web ``untriage`` route swallows the dispatch error
    and still redirects, so the flag silently never cleared. Pin that a
    numeric id resolves to the live ref for both tag and link.
    """

    def test_tag_remove_by_numeric_id_clears_tag(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref_id = _seed_paper(store, slug="wang2020state")
        store.add_tag(ref_id, Tag.open("needs-triage"), set_by="system")
        assert store.has_tag(ref_id, "OPEN", "needs-triage")

        resp = handler.tag(id=ref_id, remove=["needs-triage"])

        assert "-1 tag" in resp.body
        assert not store.has_tag(ref_id, "OPEN", "needs-triage")

    def test_tag_add_by_numeric_id(self, store: Store, handler: PaperHandler) -> None:
        ref_id = _seed_paper(store, slug="wang2020state")
        handler.tag(id=ref_id, add=["topic:foo"])
        assert store.has_tag(ref_id, "OPEN", "topic:foo")

    def test_numeric_id_as_digit_string(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """A digit *string* (FastAPI may stringify path params) resolves
        the same way an int does — slugs are never all-digits."""
        ref_id = _seed_paper(store, slug="wang2020state")
        store.add_tag(ref_id, Tag.open("needs-triage"), set_by="system")
        handler.tag(id=str(ref_id), remove=["needs-triage"])
        assert not store.has_tag(ref_id, "OPEN", "needs-triage")

    def test_unknown_numeric_id_raises_notfound(self, handler: PaperHandler) -> None:
        with pytest.raises(NotFound):
            handler.tag(id=999_999, remove=["needs-triage"])


# ---------------------------------------------------------------------------
# edit() — bibliographic metadata repair (Slice: "fix the editor")
# ---------------------------------------------------------------------------


class TestPaperEdit:
    def test_edit_persists_columns_and_canonicalises_authors(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """edit() writes the year/authors columns (the only write path
        for them) and canonicalises authors via
        :func:`precis.utils.authors.normalize_authors`: an unambiguous
        "Family, Given" string splits, a bare name stays ``{name}``."""
        ref_id = _seed_paper(store, slug="smith2019foo", year=2019)
        handler.edit(
            id=ref_id,
            year=2021,
            authors=["Smith, Jane", "Aristotle"],
            abstract="A repaired abstract.",
        )
        ref = store.fetch_refs_by_ids([ref_id])[ref_id]
        assert ref.year == 2021
        assert ref.authors == [
            {"given": "Jane", "family": "Smith"},
            {"name": "Aristotle"},
        ]
        assert ref.meta["abstract"] == "A repaired abstract."

    def test_edit_normalises_legacy_family_given_input(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """A ``{family, given}`` author payload (what the web editor
        canonicalises a split name to) passes through unchanged."""
        ref_id = _seed_paper(store, slug="doe2020bar")
        handler.edit(id=ref_id, authors=[{"family": "Doe", "given": "Alice"}])
        ref = store.fetch_refs_by_ids([ref_id])[ref_id]
        assert ref.authors == [{"given": "Alice", "family": "Doe"}]

    def test_edit_blank_fields_keep_existing(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """Only passed fields change; omitted ones are left untouched."""
        ref_id = _seed_paper(store, slug="wang2020state", title="Original", year=2020)
        handler.edit(id=ref_id, title="New title only")
        ref = store.fetch_refs_by_ids([ref_id])[ref_id]
        assert ref.title == "New title only"
        assert ref.year == 2020  # untouched

    def test_edit_replaces_doi_alias(self, store: Store, handler: PaperHandler) -> None:
        """A corrected DOI replaces this ref's alias (vs first-write-wins
        insert), so ``get(id=<new doi>)`` resolves afterwards."""
        ref_id = _seed_paper(store, slug="wang2020state", doi="10.1/old")
        handler.edit(id=ref_id, doi="10.2/new")
        ids = store.identifiers_for_refs([ref_id])[ref_id]
        assert ids["doi"] == "10.2/new"

    def test_edit_requires_a_field(self, store: Store, handler: PaperHandler) -> None:
        ref_id = _seed_paper(store, slug="wang2020state")
        with pytest.raises(BadInput):
            handler.edit(id=ref_id)

    def test_edit_unknown_id_raises_notfound(
        self, store: Store, handler: PaperHandler
    ) -> None:
        with pytest.raises(NotFound):
            handler.edit(id=9_999_999, year=2020)

    def test_edit_through_runtime_dispatch_like_web(
        self, runtime_with_store: PrecisRuntime
    ) -> None:
        """End-to-end: the web posts a flat ``{kind, id, year, authors,
        …}`` payload to ``edit`` via the in-process runtime. Verify it
        reaches the handler and persists (the fake-runtime web test only
        checks forwarding, not that the verb is actually accepted)."""
        store = runtime_with_store.store
        assert store is not None
        ref_id = _seed_paper(store, slug="wang2020state", year=2020)
        body, is_error = runtime_with_store.dispatch_with_status(
            "edit",
            {
                "kind": "paper",
                "id": ref_id,
                "year": 2022,
                "authors": ["Smith, Jane", "Jones, Bob"],
            },
        )
        assert not is_error, body
        ref = store.fetch_refs_by_ids([ref_id])[ref_id]
        assert ref.year == 2022
        assert ref.authors == [
            {"given": "Jane", "family": "Smith"},
            {"given": "Bob", "family": "Jones"},
        ]

    def test_edit_persists_journal_and_entry_type(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """``journal`` / ``entry_type`` (paper-meta-surfacing) merge into
        ``meta`` like ``abstract`` — no ``authors_source``-style
        provenance stamp (that key is authors-only; see
        ``paper_meta_enrich.py``)."""
        ref_id = _seed_paper(store, slug="wang2020state", journal="Old Journal")
        handler.edit(id=ref_id, journal="New Journal", entry_type="proceedings-article")
        ref = store.fetch_refs_by_ids([ref_id])[ref_id]
        assert ref.meta["journal"] == "New Journal"
        assert ref.meta["entry_type"] == "proceedings-article"
        assert "authors_source" not in ref.meta

    def test_edit_dry_run_previews_journal_and_entry_type(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref_id = _seed_paper(store, slug="wang2020state", journal="Old Journal")
        resp = handler.edit(
            id=ref_id,
            journal="New Journal",
            entry_type="book-chapter",
            dry_run=True,
        )
        assert "journal: 'Old Journal' → 'New Journal'" in resp.body
        assert "entry_type: '(none)' → 'book-chapter'" in resp.body
        # Dry run writes nothing.
        ref = store.fetch_refs_by_ids([ref_id])[ref_id]
        assert ref.meta["journal"] == "Old Journal"
        assert "entry_type" not in ref.meta


# ---------------------------------------------------------------------------
# edit() — DOI-edit metadata-clobber guard (gr180189)
# ---------------------------------------------------------------------------


def _doi_edit_events(store: Store, ref_id: int) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT payload FROM ref_events "
            "WHERE ref_id = %s AND event = 'doi_edit_metadata_risk' "
            "ORDER BY event_id",
            (ref_id,),
        ).fetchall()
    return [r[0] for r in rows]


class TestPaperEditDoiWarning:
    """A DOI edit never overwrites title/authors itself, but the
    *next* metadata re-resolve pass trusts whatever DOI is on file — a
    wrong DOI edit is exactly how gr180189 corrupted pa1056. The guard
    warns (in the response body and a ``doi_edit_metadata_risk``
    ref_event); it never blocks the edit."""

    def test_doi_change_warns_generically_without_crossref(
        self, store: Store, handler: PaperHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Crossref unreachable/miss → the cheap, no-fetch note still
        fires (a paper with an existing title is at risk regardless of
        whether we can verify the new DOI right now)."""
        import precis.ingest.crossref as crossref_mod

        monkeypatch.setattr(crossref_mod, "lookup_crossref", lambda *a, **k: None)
        ref_id = _seed_paper(
            store, slug="wang2020state", doi="10.1/old", title="Original Title"
        )
        resp = handler.edit(id=ref_id, doi="10.2/new")
        assert "note: doi changed to '10.2/new'" in resp.body
        assert "WARNING" not in resp.body
        events = _doi_edit_events(store, ref_id)
        assert len(events) == 1
        assert events[0]["doi"] == "10.2/new"
        assert events[0]["title"] == "Original Title"
        assert "crossref_title" not in events[0]

    def test_doi_change_no_warning_without_existing_title(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """A bare stub (no title yet) has nothing for a re-resolve to
        clobber — no warning, no event."""
        ref = store.insert_ref(kind="paper", slug="notitle2020", title="")
        store.set_ref_identifier(ref.id, "doi", "10.1/old", source="edit")
        resp = handler.edit(id=ref.id, doi="10.2/new")
        assert "note:" not in resp.body
        assert "WARNING" not in resp.body
        assert _doi_edit_events(store, ref.id) == []

    def test_doi_change_no_warning_when_doi_unchanged(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """Editing something else entirely (no ``doi=``) never trips
        the guard."""
        ref_id = _seed_paper(store, slug="wang2020state", doi="10.1/old", year=2020)
        resp = handler.edit(id=ref_id, year=2021)
        assert "note:" not in resp.body
        assert _doi_edit_events(store, ref_id) == []

    def test_doi_change_escalates_on_crossref_title_mismatch(
        self, store: Store, handler: PaperHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The new DOI resolves to an unrelated title on Crossref — the
        gr180189 signature — so the warning escalates and the event
        records the mismatch score."""
        import precis.ingest.crossref as crossref_mod

        monkeypatch.setattr(
            crossref_mod,
            "lookup_crossref",
            lambda *a, **k: {
                "title": "A Completely Unrelated Study on Zebrafish Genetics"
            },
        )
        ref_id = _seed_paper(
            store,
            slug="wang2020state",
            doi="10.1/old",
            title="State of the art in nitrate reduction",
        )
        resp = handler.edit(id=ref_id, doi="10.2/new")
        assert "WARNING: doi '10.2/new'" in resp.body
        assert "different paper" in resp.body
        events = _doi_edit_events(store, ref_id)
        assert len(events) == 1
        assert events[0]["crossref_title"] == (
            "A Completely Unrelated Study on Zebrafish Genetics"
        )
        assert events[0]["title_score"] < 0.6

    def test_doi_change_stays_generic_on_crossref_title_match(
        self, store: Store, handler: PaperHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The new DOI's Crossref title agrees with the stored title —
        no escalation, just the generic note."""
        import precis.ingest.crossref as crossref_mod

        monkeypatch.setattr(
            crossref_mod,
            "lookup_crossref",
            lambda *a, **k: {"title": "State of the art in nitrate reduction"},
        )
        ref_id = _seed_paper(
            store,
            slug="wang2020state",
            doi="10.1/old",
            title="State of the art in nitrate reduction",
        )
        resp = handler.edit(id=ref_id, doi="10.2/new")
        assert "note: doi changed" in resp.body
        assert "WARNING" not in resp.body
        events = _doi_edit_events(store, ref_id)
        assert events[0]["title_score"] >= 0.6

    def test_doi_change_never_blocks_on_crossref_failure(
        self, store: Store, handler: PaperHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Crossref lookup that raises is swallowed — the edit still
        succeeds with the generic note, never a 5xx / raised error."""
        import precis.ingest.crossref as crossref_mod

        def _boom(*_a: Any, **_k: Any) -> None:
            raise RuntimeError("network is down")

        monkeypatch.setattr(crossref_mod, "lookup_crossref", _boom)
        ref_id = _seed_paper(
            store, slug="wang2020state", doi="10.1/old", title="Original Title"
        )
        resp = handler.edit(id=ref_id, doi="10.2/new")
        assert "note: doi changed" in resp.body
        ids = store.identifiers_for_refs([ref_id])[ref_id]
        assert ids["doi"] == "10.2/new"  # the edit itself still landed
