"""Tests for paper handler date/tag/filter features."""

from datetime import UTC, datetime, timedelta

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
