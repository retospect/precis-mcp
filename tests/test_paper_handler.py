"""Tests for paper handler date/tag/filter features."""

from datetime import UTC, datetime, timedelta

import pytest

from precis.handlers._ref_base import (
    _parse_date_value,
    _parse_filters,
    _parse_year_value,
    _relative_date,
)


class TestParseDateValue:
    def test_today(self):
        result = _parse_date_value("today")
        now = datetime.now(UTC).replace(tzinfo=None)
        assert result is not None
        assert result.hour == 0 and result.minute == 0
        assert result.date() == now.date()

    def test_yesterday(self):
        result = _parse_date_value("yesterday")
        now = datetime.now(UTC).replace(tzinfo=None)
        assert result is not None
        assert result.date() == (now - timedelta(days=1)).date()

    def test_this_week(self):
        result = _parse_date_value("this-week")
        assert result is not None
        assert result.weekday() == 0  # Monday

    def test_this_month(self):
        result = _parse_date_value("this-month")
        assert result is not None
        assert result.day == 1

    def test_iso_date(self):
        result = _parse_date_value("2025-03-15")
        assert result == datetime(2025, 3, 15)

    def test_non_date_returns_none(self):
        assert _parse_date_value("MOF") is None
        assert _parse_date_value("quantum") is None
        assert _parse_date_value("/regex/i") is None

    def test_case_insensitive(self):
        assert _parse_date_value("TODAY") is not None
        assert _parse_date_value("This-Week") is not None


class TestParseYearValue:
    def test_single_year(self):
        assert _parse_year_value("2024") == (2024, 2024)

    def test_range(self):
        assert _parse_year_value("2020-2024") == (2020, 2024)

    def test_open_range(self):
        assert _parse_year_value("2020-") == (2020, None)

    def test_invalid(self):
        assert _parse_year_value("abc") == (None, None)
        assert _parse_year_value("") == (None, None)


class TestParseFilters:
    def test_plain_grep(self):
        result = _parse_filters("quantum dots")
        assert result == {"grep": "quantum dots"}

    def test_ingested_only(self):
        result = _parse_filters("ingested:today")
        assert result == {"ingested": "today", "grep": ""}

    def test_year_only(self):
        result = _parse_filters("year:2020-2024")
        assert result == {"year": "2020-2024", "grep": ""}

    def test_tag_only(self):
        result = _parse_filters("tag:review")
        assert result == {"tag": "review", "grep": ""}

    def test_combined(self):
        result = _parse_filters("ingested:today tag:review MOF")
        assert result == {"ingested": "today", "tag": "review", "grep": "MOF"}

    def test_year_and_grep(self):
        result = _parse_filters("year:2020- quantum")
        assert result == {"year": "2020-", "grep": "quantum"}

    def test_empty(self):
        result = _parse_filters("")
        assert result == {"grep": ""}

    def test_unknown_prefix_stays_in_grep(self):
        result = _parse_filters("foo:bar baz")
        assert result == {"grep": "foo:bar baz"}

    def test_no_value_after_colon(self):
        result = _parse_filters("tag: something")
        assert result == {"grep": "tag: something"}


class TestRelativeDate:
    def _utcnow(self):
        return datetime.now(UTC).replace(tzinfo=None)

    def test_today(self):
        assert _relative_date(self._utcnow()) == "today"

    def test_yesterday(self):
        assert _relative_date(self._utcnow() - timedelta(days=1)) == "yesterday"

    def test_days_ago(self):
        assert _relative_date(self._utcnow() - timedelta(days=3)) == "3d ago"

    def test_weeks_ago(self):
        result = _relative_date(self._utcnow() - timedelta(days=14))
        assert result == "2w ago"

    def test_months_ago(self):
        result = _relative_date(self._utcnow() - timedelta(days=60))
        assert result == "2mo ago"

    def test_old_date(self):
        result = _relative_date(datetime(2020, 1, 15))
        assert result == "2020-01-15"

    def test_none(self):
        assert _relative_date(None) == ""


# ---------------------------------------------------------------------------
# Citation formatting — BibTeX / RIS / ACS
# ---------------------------------------------------------------------------


class TestCitation:
    """Exercise :meth:`PaperHandler._read_citation` against the shapes of
    raw ``authors``/``title`` data that actually show up in the store.
    These cases are the regressions found on ``marquessilva1999grasp``
    and ``mikladal2013l`` — JSON-array authors, ``\\u00f8`` Unicode
    escapes, and inline HTML/JATS tags + multi-line whitespace in the
    title column.
    """

    def _handler(self):
        from precis.handlers.paper import PaperHandler

        return PaperHandler()

    def test_bib_joins_json_array_authors_with_and(self):
        ref = {
            "slug": "marquessilva1999grasp",
            "title": "GRASP: a search algorithm",
            "authors": '[{"name": "Marques-Silva, J.P."}, '
            '{"name": "Sakallah, K.A."}]',
            "year": 1999,
            "journal": "IEEE Transactions on Computers",
            "doi": "10.1109/12.769433",
        }
        out = self._handler()._read_citation(ref, "bib")
        assert "Marques-Silva, J.P." in out
        assert "Sakallah, K.A." in out
        # Joined with " and ", not a raw Python list repr.
        assert (
            "author = {Marques-Silva, J.P. and Sakallah, K.A.}" in out
        )
        # No stray JSON punctuation.
        for junk in ("[{", "}]", "\"name\":"):
            assert junk not in out

    def test_bib_decodes_unicode_escapes_in_authors(self):
        # ``\u00f8`` in the stored JSON string should land as a literal
        # ``ø`` in the emitted BibTeX, not as a 6-character escape.
        ref = {
            "slug": "mikladal2013l",
            "title": "Flexible Transparent Conductors",
            "authors": '[{"name": "Mikladal, Bj\\u00f8rn F."}, '
            '{"name": "Anisimov, Anton S."}]',
            "year": 2013,
        }
        out = self._handler()._read_citation(ref, "bib")
        assert "Mikladal, Bjørn F." in out
        assert "\\u00f8" not in out

    def test_bib_title_strips_html_tags_and_collapses_whitespace(self):
        # JATS-derived multi-line title with inline <i> tag — should
        # emit a single-line plain-text field with the tag stripped.
        raw_title = (
            "57.5L:\n                    <i>Late\u2010News Paper</i>\n"
            "                    : Flexible Transparent Conductors"
        )
        ref = {
            "slug": "mikladal2013l",
            "title": raw_title,
            "authors": '[{"name": "Mikladal, Bjørn F."}]',
            "year": 2013,
        }
        out = self._handler()._read_citation(ref, "bib")
        assert "<i>" not in out
        assert "</i>" not in out
        # Whitespace collapsed to a single space (no newlines / indent).
        title_line = next(
            line for line in out.splitlines() if line.startswith("  title = ")
        )
        assert "\n" not in title_line
        assert "  " not in title_line.split("title = {", 1)[1]
        # Actual text preserved without tag markers.
        assert "Late" in title_line and "News Paper" in title_line
        assert "Flexible Transparent Conductors" in title_line

    def test_bib_escapes_reserved_chars(self):
        ref = {
            "slug": "x2024",
            "title": "A & B in 50% of cases (S&P_500 index)",
            "authors": '[{"name": "Foo & Bar"}]',
            "year": 2024,
            "journal": "J. & K.",
        }
        out = self._handler()._read_citation(ref, "bib")
        assert "\\&" in out
        assert "\\%" in out
        assert "\\_" in out

    def test_bib_handles_missing_authors_gracefully(self):
        ref = {"slug": "foo", "title": "Just a Title", "year": 2024}
        out = self._handler()._read_citation(ref, "bib")
        # No author line when the list is empty.
        assert "author =" not in out
        assert "title = {Just a Title}" in out

    def test_ris_one_au_line_per_author(self):
        ref = {
            "slug": "x",
            "title": "A <i>Paper</i>",
            "authors": [
                {"name": "Smith, J."},
                {"name": "Jones, K."},
                {"name": "Lee, P."},
            ],
            "year": 2024,
            "journal": "Nature",
            "doi": "10.1/x",
        }
        out = self._handler()._read_citation(ref, "ris")
        au_lines = [line for line in out.splitlines() if line.startswith("AU  - ")]
        assert au_lines == [
            "AU  - Smith, J.",
            "AU  - Jones, K.",
            "AU  - Lee, P.",
        ]
        # Title stripped of tags, no backslash-escapes (RIS has none).
        assert "TI  - A Paper" in out

    def test_acs_inline_uses_first_author_surname(self):
        ref = {
            "slug": "x2024",
            "title": "...",
            "authors": '[{"name": "Smith, J."}, {"name": "Jones, K."}]',
            "year": 2024,
            "journal": "Nature",
        }
        out = self._handler()._read_citation(ref, "acs")
        assert out == "Smith et al., Nature 2024"


# ---------------------------------------------------------------------------
# List rendering — grep= filter + _list_entry (BUG-A regression)
# ---------------------------------------------------------------------------


class TestListRendererTolerateNones:
    """Regression coverage for BUG-A (discovered 2026-04-22 19:30 live
    smoke run): ``get(type='paper', grep=...)`` crashed with
    ``TypeError: sequence item 4: expected str instance, NoneType
    found`` because ``p.get(key, "")`` returns ``None`` when the key
    exists with a ``None`` value (common for partially-ingested refs
    missing DOI / year / title).
    """

    def _handler_with_papers(self, papers: list[dict]):
        from precis.handlers.paper import PaperHandler

        class FakeStore:
            def list_papers(self, limit: int = 10000):
                return papers

        h = PaperHandler()
        return h, FakeStore()

    def test_list_refs_grep_tolerates_none_doi(self):
        papers = [
            {
                "slug": "partial2024",
                "title": "A Partial Paper",
                "authors": "Author, A.",
                "year": 2024,
                "doi": None,  # ← the BUG-A trigger
            },
        ]
        h, store = self._handler_with_papers(papers)
        out = h._list_refs(store, grep="Partial")
        assert "partial2024" in out

    def test_list_refs_grep_tolerates_all_fields_none(self):
        papers = [
            {
                "slug": "minimal",
                "title": None,
                "authors": None,
                "year": None,
                "doi": None,
            },
            {
                "slug": "has-MOF-keyword",
                "title": "MOF paper",
                "authors": None,
                "year": None,
                "doi": None,
            },
        ]
        h, store = self._handler_with_papers(papers)
        out = h._list_refs(store, grep="MOF")
        assert "has-MOF-keyword" in out
        assert "minimal" not in out

    def test_list_entry_tolerates_none_fields(self):
        # Post-BUG-A, _list_entry is called on refs that may still have
        # None values; it must not crash on _truncate(None) or
        # first_author_surname(None).
        from precis.handlers.paper import PaperHandler

        ref = {
            "slug": "partial",
            "title": None,
            "authors": None,
            "year": None,
            "doi": None,
        }
        out = PaperHandler()._list_entry(ref)
        assert "partial" in out
        # No "None" literal should leak into the rendered line.
        assert "None" not in out


# ---------------------------------------------------------------------------
# Overview renderer — authors field (BUG-D regression)
# ---------------------------------------------------------------------------


class TestOverviewAuthorsNormalisation:
    """Regression coverage for BUG-D (discovered 2026-04-22 19:30 live
    smoke run): the paper landing page (``get(id='paper:<slug>')``)
    displayed the raw ``authors`` column verbatim, so papers with
    JSON-encoded author arrays rendered a literal
    ``[{"name": "Marques-Silva, J.P."}, …]`` in the header.  The cite
    formatters were already clean (bug #5 fix); this test ensures the
    overview renderer shares that normalisation.
    """

    def _handler(self):
        from precis.handlers.paper import PaperHandler

        return PaperHandler()

    class _FakeStore:
        def __init__(self):
            self.calls: list[str] = []

        def get_blocks(self, slug, block_type=None):
            self.calls.append(slug)
            return []

        def get_link_count(self, slug):
            return {}

    def test_overview_decodes_json_array_authors(self):
        ref = {
            "slug": "marquessilva1999grasp",
            "title": "GRASP: a search algorithm",
            "authors": (
                '[{"name": "Marques-Silva, J.P."}, {"name": "Sakallah, K.A."}]'
            ),
            "year": 1999,
            "journal": "IEEE Trans. Computers",
            "doi": "10.1109/12.769433",
        }
        out = self._handler()._read_overview(self._FakeStore(), ref)
        assert "Marques-Silva, J.P." in out
        assert "Sakallah, K.A." in out
        # The raw JSON markers must not leak into the header.
        for junk in ('[{"name":', '"}]', '{"name":'):
            assert junk not in out

    def test_overview_handles_plain_author_string(self):
        ref = {
            "slug": "plain",
            "title": "Plain authors test",
            "authors": "Smith, J.; Jones, K.",
            "year": 2020,
        }
        out = self._handler()._read_overview(self._FakeStore(), ref)
        assert "Smith, J." in out
        assert "Jones, K." in out

    def test_overview_handles_none_authors(self):
        ref = {
            "slug": "noauthors",
            "title": "No authors listed",
            "authors": None,
            "year": 2020,
        }
        out = self._handler()._read_overview(self._FakeStore(), ref)
        # Renderer must not crash; author line simply absent.
        assert "noauthors" in out
        # No raw "None" should leak.
        assert "None" not in out


# ---------------------------------------------------------------------------
# Search + grep interaction — BUG-F regression
# ---------------------------------------------------------------------------


class TestSearchWithGrep:
    """BUG-F regression — ``search(type='paper', query='…', grep='…')``
    used to silently drop the ``grep`` kwarg at the MCP tool boundary
    (``server.search`` signature had no ``grep`` param) and returned the
    same unfiltered top-k as the vanilla ``query`` call.  The fix adds
    ``grep`` to the tool signature and teaches ``_ref_base`` to combine
    the two: metadata pre-filter, then vector search over the filtered
    subset.
    """

    def _handler(self):
        from precis.handlers.paper import PaperHandler

        return PaperHandler()

    class _FakeStore:
        def __init__(self, papers, hits):
            self._papers = papers
            self._hits = hits

        def list_papers(self, limit=10000):
            return self._papers

        def search_text(self, query, top_k=5):
            return self._hits

    def _hit(self, slug, block_idx=3, snippet="snippet"):
        return {
            "text": snippet,
            "distance": 0.5,
            "paper": {"slug": slug},
            "metadata": {"slug": slug, "block_index": block_idx},
        }

    def test_grep_pre_filters_paper_set(self):
        # Two papers with different tags; grep should keep only "alpha"
        # and the vector-search hits outside that set must be dropped.
        papers = [
            {"slug": "alpha", "title": "Paper A", "authors": "X", "year": 2020},
            {"slug": "beta", "title": "Paper B", "authors": "Y", "year": 2021},
        ]
        hits = [
            self._hit("alpha", snippet="membrane chemistry"),
            self._hit("beta", snippet="membrane physics"),
        ]
        store = self._FakeStore(papers, hits)
        out = self._handler()._search_with_grep(
            store, query="membrane", grep="alpha", top_k=5
        )
        assert "alpha" in out
        assert "beta" not in out
        # The header must note that grep was applied so the agent
        # knows the filter landed (vs being silently dropped).
        assert "grep='alpha'" in out

    def test_empty_filter_set_surfaces_actionable_error(self):
        # grep eliminates every paper → return a friendly error that
        # explains the filter was the limiter, not the query.
        papers = [
            {"slug": "alpha", "title": "Paper A", "authors": "X", "year": 2020},
        ]
        hits = [self._hit("alpha")]
        store = self._FakeStore(papers, hits)
        out = self._handler()._search_with_grep(
            store, query="membrane", grep="no-match-tag", top_k=5
        )
        assert "No papers matching grep='no-match-tag'" in out
        # Must be actionable — the agent needs to know to broaden grep.
        assert "broader grep" in out or "drop it" in out

    def test_empty_hit_set_after_filter_surfaces_actionable_error(self):
        # grep keeps some papers but none of the vector hits land on
        # those — tell the agent which knob to widen.
        papers = [
            {"slug": "alpha", "title": "Paper A", "authors": "X", "year": 2020},
            {"slug": "beta", "title": "Paper B", "authors": "Y", "year": 2021},
        ]
        # Hits only on beta; grep keeps only alpha.
        hits = [self._hit("beta", snippet="no match")]
        store = self._FakeStore(papers, hits)
        out = self._handler()._search_with_grep(
            store, query="membrane", grep="alpha", top_k=5
        )
        assert "No results for query='membrane'" in out
        assert "grep='alpha'" in out


class TestSearchToolForwardsGrep:
    """Locks in that ``server.search(type='paper', query='…', grep='…')``
    actually reaches ``tools.read`` with the ``grep`` kwarg populated —
    BUG-F root cause was that the tool signature didn't expose it.
    """

    def test_search_tool_forwards_grep_kwarg(self, monkeypatch):
        from precis import server

        captured: dict[str, object] = {}

        def fake_read(uri, **kwargs):
            captured["uri"] = uri
            captured.update(kwargs)
            return "ok"

        monkeypatch.setattr(server.tools, "read", fake_read)
        out = server.search(
            query="membrane", type="paper", grep="tag:review", top_k=3
        )
        assert "ERROR [" not in out
        assert captured.get("grep") == "tag:review"
        assert captured.get("query") == "membrane"
        assert captured.get("top_k") == 3


# ---------------------------------------------------------------------------
# Search + scope interaction — silent-cross-paper-leak regression
# ---------------------------------------------------------------------------


class TestSearchWithScope:
    """Regression for the CRITICAL bug where ``search(scope='X', query='Y')``
    silently returned hits from any paper because the slug filter was
    dropped between server and handler.  An LLM asked "find Y in paper X"
    would receive top-ranked hits from unrelated papers and cite them as
    if they came from X — a citation-integrity bug.

    The fix threads ``path`` (the user-supplied ``scope=``) through
    ``_search_or_grep → _search``, over-fetches by 5×, and post-filters
    by slug so only the scoped paper's chunks survive.
    """

    def _handler(self):
        from precis.handlers.paper import PaperHandler

        return PaperHandler()

    class _FakeStore:
        def __init__(self, hits):
            self._hits = hits
            self.last_top_k: int | None = None

        def get(self, slug):
            return {"slug": slug, "title": "Stub", "ref_id": f"ref-{slug}"}

        def list_papers(self, limit=10000):
            return []

        def search_text(self, query, top_k=5, **kwargs):
            self.last_top_k = top_k
            return self._hits

    @staticmethod
    def _hit(slug, block_idx=3, snippet="snippet"):
        return {
            "text": snippet,
            "distance": 0.5,
            "paper": {"slug": slug},
            "metadata": {"slug": slug, "block_index": block_idx},
        }

    def test_scope_drops_hits_from_other_papers(self):
        # Vector search returns hits from THREE papers; only the
        # scoped one (sheka2011c) must survive.  Anything else is the
        # bug we are guarding against.
        store = self._FakeStore(
            [
                self._hit("bazaka2016sustainable", block_idx=328),
                self._hit("sheka2011c", block_idx=12),
                self._hit("mamaghani2024promises", block_idx=331),
                self._hit("sheka2011c", block_idx=44),
                self._hit("wu2008first", block_idx=0),
            ]
        )
        out = self._handler()._search(
            store, query="nanobuds", top_k=5, scope="sheka2011c"
        )
        assert "sheka2011c" in out
        # The other slugs MUST NOT appear in the result body.
        for foreign in (
            "bazaka2016sustainable",
            "mamaghani2024promises",
            "wu2008first",
        ):
            assert foreign not in out, (
                f"scope='sheka2011c' leaked a hit from {foreign!r} — "
                "scope filter is broken"
            )
        # Header must note the scope so the caller knows the filter ran.
        assert "scope='sheka2011c'" in out

    def test_scope_over_fetches_to_fill_top_k(self):
        # When scope is set, the handler over-fetches so a scoped
        # paper deep in the ranking still yields top_k hits.  5× is
        # the empirical multiplier (mirrors _search_with_grep).
        store = self._FakeStore([self._hit("sheka2011c", block_idx=i) for i in range(5)])
        self._handler()._search(store, query="X", top_k=5, scope="sheka2011c")
        assert store.last_top_k == 25, (
            f"expected over-fetch top_k=25 (5×5), got {store.last_top_k}"
        )

    def test_zero_match_emits_actionable_hint(self):
        # Vector search returned plenty, but none from the scoped paper.
        # The handler must NOT silently fall back to corpus-wide results
        # — that would restore the original silent-leak bug.
        store = self._FakeStore(
            [
                self._hit("other1", block_idx=1),
                self._hit("other2", block_idx=2),
            ]
        )
        out = self._handler()._search(
            store, query="nanobuds", top_k=5, scope="sheka2011c"
        )
        # Must be an empty-with-explanation result, not a list of hits.
        assert "No results" in out
        assert "scope='sheka2011c'" in out
        assert "sheka2011c" in out
        # Hint must offer a concrete recovery path.
        assert "search(query='nanobuds', type='paper')" in out
        # And must NOT silently include the other papers' chunks.
        for foreign in ("other1", "other2"):
            assert foreign not in out

    def test_unscoped_search_unchanged(self):
        # Sanity: scope='' (or unset) behaves exactly as before — every
        # hit is rendered without filtering.
        store = self._FakeStore(
            [
                self._hit("alpha", block_idx=1),
                self._hit("beta", block_idx=2),
            ]
        )
        out = self._handler()._search(store, query="X", top_k=5)
        assert "alpha" in out
        assert "beta" in out
        # No scope header noise on an unscoped search.
        assert "scope=" not in out


class TestScopeEndToEnd:
    """The fix must reach the handler — guards against any future
    reshuffling that drops `path` between server and ``_search_or_grep``.
    """

    def test_search_with_scope_invokes_handler_with_path(self, monkeypatch):
        from precis import server

        captured: dict[str, object] = {}

        def fake_read(uri, **kwargs):
            captured["uri"] = uri
            captured.update(kwargs)
            return "ok"

        monkeypatch.setattr(server.tools, "read", fake_read)
        out = server.search(
            query="nanobuds", scope="sheka2011c", top_k=3
        )
        assert "ERROR [" not in out
        # The URI carries the scope as the path component.
        assert captured["uri"] == "paper:sheka2011c"
        assert captured.get("query") == "nanobuds"
        assert captured.get("top_k") == 3


# ---------------------------------------------------------------------------
# /toc — positionless block crash regression
# ---------------------------------------------------------------------------


class TestTocPositionlessBlocks:
    """Regression for the ``TypeError: NoneType - NoneType`` crash in
    ``_read_toc_overview`` when a paper's blocks include positionless
    types (abstract, document_summary, paper_summary) whose
    ``block_index`` is NULL in the database.

    ``store.get_toc`` returns every block_type for completeness, so the
    renderer must filter to positional blocks before computing
    ``end - start + 1`` ranges.  Without the filter, the first
    positionless entry crashed the handler with an opaque ``unexpected``
    error envelope.
    """

    def _handler(self):
        from precis.handlers.paper import PaperHandler

        return PaperHandler()

    class _FakeStore:
        def __init__(self, toc, ref=None):
            self._toc = toc
            self._ref = ref or {"slug": "stub2024", "title": "Stub", "ref_id": 1}

        def get(self, slug):
            return self._ref

        def get_toc(self, slug):
            return self._toc

    @staticmethod
    def _entry(idx, block_type="text", section="1. Intro", text="Body."):
        return {
            "node_id": f"n-{idx}",
            "block_index": idx,
            "page": 0,
            "block_type": block_type,
            "section_path": f'["{section}"]',
            "preview": text,
        }

    @staticmethod
    def _positionless(block_type="abstract", text="Abstract text"):
        # ``block_index=None`` is exactly what postgres returns for
        # abstract / document_summary blocks — they have no position
        # in the page sequence.  This is the input that crashed before.
        return {
            "node_id": f"n-{block_type}",
            "block_index": None,
            "page": None,
            "block_type": block_type,
            "section_path": "",
            "preview": text,
        }

    def test_toc_drops_positionless_blocks(self):
        # Mix of positional text + an abstract + a document_summary,
        # exactly mirroring what ni2024atomic looks like in the live
        # store.  Renderer must succeed and render only the text.
        toc = [
            self._positionless("abstract", "We report…"),
            self._positionless("document_summary", "Summary."),
            *(self._entry(i) for i in range(120)),
        ]
        out = self._handler()._read_toc(self._FakeStore(toc), {"slug": "stub2024"})
        # No crash, no error envelope.
        assert "ERROR" not in out
        # The 120 positional blocks are reflected in the count.
        assert "120 blocks" in out, f"unexpected output: {out!r}"
        # No abstract/summary noise leaked into the TOC body.
        assert "abstract" not in out.lower() or "Abstract" not in out
        assert "document_summary" not in out

    def test_toc_with_only_positionless_blocks_returns_unavailable(self):
        # Stub paper with metadata but no body — abstract only.  Must
        # return a structured ERROR [unavailable] with actionable hints,
        # NOT crash and NOT render an empty TOC.
        from precis.protocol import PrecisError, ErrorCode

        toc = [
            self._positionless("abstract", "Just an abstract."),
            self._positionless("document_summary", "And a summary."),
        ]
        with pytest.raises(PrecisError) as exc_info:
            self._handler()._read_toc(
                self._FakeStore(toc), {"slug": "stub2024"}
            )
        assert exc_info.value.code == ErrorCode.UNAVAILABLE
        # Hint must point at the alternative views the agent can try.
        assert "/abstract" in exc_info.value.next
        assert "/summary" in exc_info.value.next
        # And mention the slug so the next call can be copied verbatim.
        assert "stub2024" in exc_info.value.next

    def test_toc_with_no_blocks_at_all_returns_simple_message(self):
        # Distinct case: ref exists but TOC fetch returned zero rows.
        # Pre-existing behaviour was a friendly message, not an error.
        out = self._handler()._read_toc(self._FakeStore([]), {"slug": "empty"})
        assert "No blocks" in out
        assert "empty" in out

    def test_toc_normal_paper_unchanged(self):
        # Sanity: a paper with no positionless blocks renders as before.
        toc = [self._entry(i) for i in range(150)]
        out = self._handler()._read_toc(self._FakeStore(toc), {"slug": "normal"})
        assert "ERROR" not in out
        assert "150 blocks" in out
        # The section header from each entry's section_path appears.
        assert "Intro" in out


class TestChunkReaderResolvesAllBlockTypes:
    """Regression for the search-get inconsistency.

    Previously, ``vector.search`` returned hits across every embedded
    block type (text, figure, section_header, list) but the chunk
    reader filtered to ``block_type='text'`` only.  A search hit at
    ``slug›N`` where N pointed at a non-text block was unreachable —
    ``get(id='slug›N')`` returned ``"No blocks in range"`` even though
    the search said the block was there.

    The fix drops the type filter at every chunk read site so the
    search→get round-trip always lands on the same block.  Non-text
    block_type is stamped into both the search hit output and the
    chunk header so the agent knows whether they're reading body
    text vs a figure caption.
    """

    def _handler(self):
        from precis.handlers.paper import PaperHandler

        return PaperHandler()

    class _FakeStore:
        def __init__(self, blocks, ref=None):
            self._blocks = blocks
            self._ref = ref or {"slug": "stub2024", "title": "Stub", "ref_id": 1}

        def get(self, slug):
            return self._ref

        def get_blocks(self, slug, block_type=None, supplement=None):
            if block_type:
                return [b for b in self._blocks if b.get("block_type") == block_type]
            return list(self._blocks)

        def get_links(self, slug, node_id=None, direction="both"):
            return []

    @staticmethod
    def _block(idx, block_type="text", text=None):
        return {
            "node_id": f"n-{idx}",
            "block_index": idx,
            "page": 0,
            "block_type": block_type,
            "section_path": "[]",
            "text": text or f"{block_type} content {idx}",
            "summary": None,
        }

    def test_chunk_reader_returns_section_header_block(self):
        # The exact bug from ni2024atomic: search returned ›0 (a
        # section_header) but get(›0) reported "No blocks in range".
        blocks = [
            self._block(0, "section_header", "**Title**"),
            self._block(1, "text", "First paragraph."),
            self._block(2, "text", "Second paragraph."),
        ]
        out = self._handler()._read_chunks(
            self._FakeStore(blocks), {"slug": "stub2024"}, "0"
        )
        assert "No blocks in range" not in out
        assert "**Title**" in out
        # Block_type tagged in the chunk header so the agent isn't
        # surprised they got a header back instead of body prose.
        assert "[section_header]" in out

    def test_chunk_reader_returns_figure_block(self):
        blocks = [
            self._block(0, "text", "Body."),
            self._block(1, "figure", "Figure 1: caption."),
        ]
        out = self._handler()._read_chunks(
            self._FakeStore(blocks), {"slug": "stub2024"}, "1"
        )
        assert "No blocks in range" not in out
        assert "[figure]" in out

    def test_chunk_range_includes_mixed_block_types(self):
        # The agent asks for ›0..3.  All three blocks should appear
        # regardless of type, in order.
        blocks = [
            self._block(0, "section_header", "**Title**"),
            self._block(1, "text", "Para 1."),
            self._block(2, "figure", "Caption."),
            self._block(3, "text", "Para 2."),
        ]
        out = self._handler()._read_chunks(
            self._FakeStore(blocks), {"slug": "stub2024"}, "0..3"
        )
        assert out.count(">> stub2024") == 3
        assert "Title" in out
        assert "Para 1" in out
        assert "Caption" in out
        # ›3 is exclusive end, so Para 2 is NOT included.
        assert "Para 2" not in out

    def test_chunk_reader_does_not_crash_on_positionless_blocks(self):
        # The store returns abstract / document_summary blocks with
        # block_index=None alongside positional blocks.  Without the
        # positionless filter, the int comparison in the range check
        # crashed with TypeError.
        blocks = [
            {
                "node_id": "n-abstract",
                "block_index": None,
                "block_type": "abstract",
                "text": "Abstract text.",
                "page": None,
                "section_path": "",
                "summary": None,
            },
            self._block(0, "text", "First."),
            self._block(1, "text", "Second."),
        ]
        out = self._handler()._read_chunks(
            self._FakeStore(blocks), {"slug": "stub2024"}, "0..2"
        )
        assert "ERROR" not in out
        assert "First" in out
        assert "Second" in out
        # Abstract block must not leak into chunk output.
        assert "Abstract text" not in out

    def test_summary_view_resolves_on_section_header(self):
        blocks = [self._block(0, "section_header", "**Title**")]
        blocks[0]["summary"] = "header summary"
        out = self._handler()._read_summary(
            self._FakeStore(blocks), {"slug": "stub2024"}, "0"
        )
        assert "ERROR" not in out
        assert "header summary" in out

    def test_links_view_resolves_on_figure_block(self):
        blocks = [
            self._block(0, "text", "Body."),
            self._block(1, "figure", "Caption."),
        ]
        out = self._handler()._read_links(
            self._FakeStore(blocks), {"slug": "stub2024"}, "1"
        )
        assert "ERROR" not in out
        # No links registered, so the empty-state hint is shown.
        # The point is the resolution doesn't fail with id_not_found.
        assert "stub2024" in out


class TestSearchHitTypeTag:
    """Search output must surface non-text block_type so agents
    don't get surprised when ``get(id='slug›N')`` returns a figure or
    section header instead of body prose.
    """

    def _handler(self):
        from precis.handlers.paper import PaperHandler

        return PaperHandler()

    @staticmethod
    def _hit(slug, block_idx, block_type, text):
        return {
            "text": text,
            "distance": 0.3,
            "summary": "",
            "metadata": {
                "slug": slug,
                "block_index": block_idx,
                "type": block_type,
                "block_type": block_type,
                "node_id": f"n-{block_idx}",
            },
            "paper": {"slug": slug, "title": "Stub"},
        }

    def test_text_hit_has_no_type_tag(self):
        line = self._handler()._format_search_hit_line(
            self._hit("stub", 3, "text", "Body of paragraph.")
        )
        # Text hits stay clean — no [text] noise on the common case.
        assert "[text]" not in line
        assert "stub" in line and "3" in line

    def test_section_header_hit_is_tagged(self):
        line = self._handler()._format_search_hit_line(
            self._hit("stub", 0, "section_header", "**Title**")
        )
        assert "[section_header]" in line
        assert "stub" in line and "0" in line

    def test_figure_hit_is_tagged(self):
        line = self._handler()._format_search_hit_line(
            self._hit("stub", 12, "figure", "Figure 1.")
        )
        assert "[figure]" in line

    def test_list_hit_is_tagged(self):
        line = self._handler()._format_search_hit_line(
            self._hit("stub", 5, "list", "- bullet")
        )
        assert "[list]" in line


class TestPaperStructuralNavigationSkill:
    """The new ``skill:paper-structural-navigation`` ships with the
    package and renders correctly.  This guards against the file being
    accidentally deleted from the wheel manifest.
    """

    def test_skill_resolves(self):
        from precis import server

        out = server.get(id="paper-structural-navigation", type="skill")
        # The skill body legitimately contains ``ERROR [unavailable]``
        # as documentation of the recovery path, so we can't use the
        # error envelope substring as a failure signal.  Use positive
        # structural markers instead: skill renders with its header and
        # the standard ``[cost: free]`` footer when it succeeded.
        assert "skill:paper-structural-navigation" in out, (
            f"skill header missing — likely an error: {out[:200]!r}"
        )
        assert "[cost: free]" in out, (
            f"cost footer missing — dispatch failed: {out[-200:]!r}"
        )
        # Hallmark phrases the skill body contains.
        assert "Decision table" in out or "decision table" in out.lower()
        assert "/toc" in out
        assert "/abstract" in out
