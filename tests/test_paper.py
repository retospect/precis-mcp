"""PaperHandler tests: get() with views, chunk selectors, search."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
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
def handler(hub: Hub) -> PaperHandler:
    return PaperHandler(hub=hub)


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
        # The abstract view now carries a slug header, the title in
        # italics, and a Next: trailer so the caller knows which paper
        # they're reading and where to drill next. (MCP critic NIT.)
        assert "A long-form abstract here." in resp.body
        assert resp.body.startswith("# wang2020state — abstract")
        assert "Next:" in resp.body

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
        lex_only = PaperHandler(hub=Hub(store=store))
        _seed_paper(store, blocks=["alpha"])
        resp = lex_only.search(q="zzqqxx")
        assert "no paper blocks match" in resp.body

    def test_singleton_hit_no_redundant_trailer(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """MCP critic MINOR-$: ``top_k=1`` against a corpus with many
        matches used to render a two-line nav (``scope=<that slug>``
        + ``+ <salient term>``) that was 46 % of the response and
        100 % redundant — scoping to the only hit's own paper is a
        no-op, and the salient-term hint is moot when the caller
        already has a tight match.

        New shape: a single ``top_k=10`` widen hint replaces both
        lines. ``scope=`` with the hit's own slug must NOT appear,
        and the long-form salient-term suggestion must be gone too.
        """
        # Seed multiple papers all matching the same word so a
        # ``top_k=1`` query has many more matches than it returned.
        for slug in ("paper-a", "paper-b", "paper-c", "paper-d"):
            _seed_paper(
                store,
                slug=slug,
                title=f"Paper {slug}",
                blocks=[f"photocatalytic nitrogen reduction in {slug}"],
            )
        resp = handler.search(q="photocatalytic", top_k=1)
        # Header still announces "1 of N" so the caller knows there
        # are more matches.
        assert " of " in resp.body
        # New: a top_k=10 widen hint is present.
        assert "top_k=10" in resp.body
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
        resp = handler.search(q="photocatalytic", top_k=2)
        # Two-line nav must still be present.
        assert "narrow to blocks inside" in resp.body
        assert "tighten the query with a hit-specific token" in resp.body


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
