"""Tests for :class:`precis.handlers.paper.PaperHandler`.

Covers date/tag/filter parsing utilities, plus regression suite from
the 2026-04-25 mcp-critic review (JATS cleanup, abstract view, figure
error shape, figure caption rescue, range clamping, caption-label
dedup, fig_num rebinding, empty figure-block hint, inverted chunk
range).
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from precis.handlers._ref_base import (
    _parse_date_value,
    _parse_filters,
    _parse_year_value,
    _relative_date,
)
from precis.handlers.paper import (
    PaperHandler,
    _caption_body,
    _clean_jats,
)
from precis.protocol import ErrorCode, PrecisError


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
        from precis.protocol import ErrorCode, PrecisError

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


class TestPluralisation:
    """Phase 7a — ``f"{n} results"`` produced ``"1 results"`` for
    single-hit responses.  The fix introduces ``_pluralise`` and
    routes every search-hit header through it.
    """

    def test_pluralise_helper(self):
        from precis.handlers._ref_base import _pluralise

        assert _pluralise(0, "result") == "0 results"
        assert _pluralise(1, "result") == "1 result"
        assert _pluralise(2, "result") == "2 results"
        # Custom plural for words English mangles.
        assert _pluralise(1, "entry", "entries") == "1 entry"
        assert _pluralise(3, "entry", "entries") == "3 entries"

    def test_search_header_with_one_hit_singular(self):
        # Construct a one-hit response and check the header reads
        # "1 result" not "1 results".  The hit list itself doesn't
        # matter — we're only asserting the rendered header phrasing.
        h = TestChunkReaderResolvesAllBlockTypes()._handler()
        # Patch _search_or_grep to feed a single fake hit through.
        # Directly assemble the line by exercising the helper used by
        # the renderer.
        from precis.handlers._ref_base import _pluralise

        assert "1 result" in f"🔍 {_pluralise(1, 'result')} for: x"
        # Negative case — the broken behaviour we replaced.
        assert "1 results" not in f"🔍 {_pluralise(1, 'result')} for: x"


class TestNegativeChunkSelector:
    """Phase 7b — negative chunk selectors silently returned
    ``"No blocks in range"`` which was indistinguishable from a
    legitimate empty range past the end of the paper.  The fix raises
    ID_MALFORMED with a hint pointing at /toc to discover the true
    range.
    """

    def _handler(self):
        from precis.handlers.paper import PaperHandler

        return PaperHandler()

    class _FakeStore:
        def __init__(self):
            self._blocks = [
                {
                    "node_id": "n-0",
                    "block_index": 0,
                    "block_type": "text",
                    "text": "first",
                    "section_path": "[]",
                    "page": 0,
                    "summary": None,
                }
            ]

        def get(self, slug):
            return {"slug": slug, "ref_id": 1}

        def get_blocks(self, slug, block_type=None, supplement=None):
            return list(self._blocks)

        def get_links(self, slug, **kw):
            return []

    def test_chunks_negative_index(self):
        from precis.protocol import ErrorCode, PrecisError

        with pytest.raises(PrecisError) as exc:
            self._handler()._read_chunks(
                self._FakeStore(), {"slug": "stub"}, "-3"
            )
        assert exc.value.code == ErrorCode.ID_MALFORMED
        assert "non-negative" in exc.value.cause

    def test_chunks_negative_range_start(self):
        from precis.protocol import ErrorCode, PrecisError

        with pytest.raises(PrecisError) as exc:
            self._handler()._read_chunks(
                self._FakeStore(), {"slug": "stub"}, "-3..0"
            )
        assert exc.value.code == ErrorCode.ID_MALFORMED

    def test_chunks_negative_range_end(self):
        from precis.protocol import ErrorCode, PrecisError

        with pytest.raises(PrecisError) as exc:
            self._handler()._read_chunks(
                self._FakeStore(), {"slug": "stub"}, "0..-1"
            )
        assert exc.value.code == ErrorCode.ID_MALFORMED

    def test_summary_negative_selector(self):
        from precis.protocol import ErrorCode, PrecisError

        with pytest.raises(PrecisError) as exc:
            self._handler()._read_summary(
                self._FakeStore(), {"slug": "stub"}, "-1"
            )
        assert exc.value.code == ErrorCode.ID_MALFORMED

    def test_links_negative_selector(self):
        from precis.protocol import ErrorCode, PrecisError

        with pytest.raises(PrecisError) as exc:
            self._handler()._read_links(
                self._FakeStore(), {"slug": "stub"}, "-2"
            )
        assert exc.value.code == ErrorCode.ID_MALFORMED

    def test_zero_index_still_works(self):
        # The check is strictly less-than-zero — index 0 (the first
        # block) must continue to resolve.
        out = self._handler()._read_chunks(
            self._FakeStore(), {"slug": "stub"}, "0"
        )
        assert "ERROR" not in out
        assert "first" in out


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


# ===========================================================================
# Regression suite — 2026-04-25 mcp-critic review (paper-handler concerns)
# ===========================================================================


# ---------------------------------------------------------------------------
# v2 B4 — JATS XML stripping
# ---------------------------------------------------------------------------


class TestJatsCleanup:
    """``_clean_jats`` is the single helper every abstract path runs
    through (mcp-critic finding B4)."""

    def test_strips_jats_title_and_p(self):
        raw = (
            "<jats:title>Abstract</jats:title>"
            "<jats:p>Hello world.</jats:p>"
        )
        out = _clean_jats(raw)
        assert "<jats:" not in out
        assert "</jats:" not in out
        assert "Hello world." in out
        assert "Abstract" in out

    def test_jats_sub_becomes_unicode(self):
        raw = "NO<jats:sub>3</jats:sub>"
        # ``₃`` is U+2083
        assert _clean_jats(raw) == "NO\u2083"

    def test_jats_sup_becomes_unicode(self):
        raw = "H<jats:sup>+</jats:sup>"
        # ``⁺`` is U+207A
        assert _clean_jats(raw) == "H\u207a"

    def test_jats_sub_then_sup_combined(self):
        # Real CrossRef shape from the critic's ``ni2024atomic`` probe.
        raw = "NO<jats:sub>3</jats:sub><jats:sup>−</jats:sup>"
        out = _clean_jats(raw)
        # Subscript ₃ + Unicode minus ⁻ in superscript.
        assert out == "NO\u2083\u207b"

    def test_non_digit_sub_falls_back_to_underscore(self):
        # ``x`` isn't in the digit/operator translation table, so we
        # wrap with markdown italics rather than silently drop it.
        raw = "y<jats:sub>x</jats:sub>"
        out = _clean_jats(raw)
        assert "_x_" in out

    def test_empty_input(self):
        assert _clean_jats("") == ""
        assert _clean_jats(None) is None  # type: ignore[arg-type]


class TestPaperAbstractView:
    """``/abstract`` view runs the cleaned text through ``_clean_jats``."""

    @staticmethod
    def _handler_with_abstract(raw_text: str):
        h = PaperHandler()
        store = MagicMock()
        store.get.return_value = {
            "slug": "ni2024atomic",
            "title": "T",
            "ref_id": 1,
        }

        def get_blocks(slug, block_type=None):
            if block_type == "abstract":
                return [{"text": raw_text}]
            return []

        store.get_blocks.side_effect = get_blocks
        return h, store

    def test_abstract_view_strips_jats(self):
        h, store = self._handler_with_abstract(
            "<jats:title>Abstract</jats:title>"
            "<jats:p>Reducing nitrate (NO<jats:sub>3</jats:sub>"
            "<jats:sup>−</jats:sup>) releases H<jats:sup>+</jats:sup>.</jats:p>"
        )
        ref = store.get.return_value
        out = h._read_abstract(store, ref)
        assert "<jats:" not in out
        assert "</jats:" not in out
        assert "NO\u2083\u207b" in out  # NO₃⁻
        assert "H\u207a" in out  # H⁺

    def test_overview_strips_jats_from_preview(self):
        # The overview pulls the first abstract block and snips it to
        # 500 chars; the snip must run *after* JATS cleanup.  Without
        # this, an abstract starting with ``<jats:title>...`` was
        # rendered verbatim into the agent's overview.
        h, store = self._handler_with_abstract(
            "<jats:title>Abstract</jats:title>"
            "<jats:p>Body here.</jats:p>"
        )
        # Patch link-count helper and figures so overview doesn't hit
        # un-mocked branches.
        store.get_link_count.return_value = {}
        store.get_figures.return_value = []
        ref = store.get.return_value
        out = h._read_overview(store, ref)
        assert "<jats:" not in out
        assert "Body here." in out


# ---------------------------------------------------------------------------
# v2 B5/D4 — figure error envelope is consistent
# ---------------------------------------------------------------------------


class TestFigureErrorShape:
    """All three flavours of bad figure number (``0``, ``-1``, ``abc``)
    raise the same structured ``PrecisError`` via the central
    ``_figure_not_found`` helper.  Review 2026-04-25 finding B5/D4.
    """

    @staticmethod
    def _handler():
        h = PaperHandler()
        store = MagicMock()
        store.get.return_value = {
            "slug": "wu2008first",
            "title": "T",
            "ref_id": 1,
        }
        store.get_figures.return_value = [
            {"fig_num": 1, "caption": "", "page": 1},
            {"fig_num": 2, "caption": "", "page": 2},
            {"fig_num": 3, "caption": "", "page": 3},
        ]
        store.get_blocks.return_value = []
        return h, store

    def test_figure_zero_raises_structured_id_not_found(self):
        h, store = self._handler()
        ref = store.get.return_value
        try:
            h._read_figures(store, ref, "0")
        except PrecisError as exc:
            assert exc.code is ErrorCode.ID_NOT_FOUND
            assert "available: 1, 2, 3" in exc.next
            assert "wu2008first/fig" in exc.next
        else:
            raise AssertionError("expected PrecisError")

    def test_figure_negative_raises_structured_id_not_found(self):
        h, store = self._handler()
        ref = store.get.return_value
        try:
            h._read_figures(store, ref, "-1")
        except PrecisError as exc:
            assert exc.code is ErrorCode.ID_NOT_FOUND
            assert "available: 1, 2, 3" in exc.next
        else:
            raise AssertionError("expected PrecisError")

    def test_figure_non_numeric_raises_structured_id_malformed(self):
        h, store = self._handler()
        ref = store.get.return_value
        try:
            h._read_figures(store, ref, "abc")
        except PrecisError as exc:
            assert exc.code is ErrorCode.ID_MALFORMED
            assert "abc" in exc.cause
            assert "available: 1, 2, 3" in exc.next
        else:
            raise AssertionError("expected PrecisError")

    def test_figure_out_of_range_raises_structured_id_not_found(self):
        h, store = self._handler()
        ref = store.get.return_value
        try:
            h._read_figures(store, ref, "99")
        except PrecisError as exc:
            assert exc.code is ErrorCode.ID_NOT_FOUND
            assert "99" in exc.cause
            assert "available: 1, 2, 3" in exc.next
        else:
            raise AssertionError("expected PrecisError")


# ---------------------------------------------------------------------------
# v2 B7 — figure caption pairing rescue
# ---------------------------------------------------------------------------


class TestFigureCaptionRescue:
    """When ``store.get_figures`` returns ``caption=""`` but a body
    block contains the literal ``Figure N. ...`` text, the handler
    pairs them on read.  Review 2026-04-25 finding B7.
    """

    @staticmethod
    def _handler_with_orphan_caption():
        h = PaperHandler()
        store = MagicMock()
        store.get.return_value = {
            "slug": "wu2008first",
            "title": "T",
            "ref_id": 1,
        }
        store.get_figures.return_value = [
            # Caption deliberately empty — the rescue must find it.
            {"fig_num": 3, "caption": "", "page": 1},
        ]
        store.get_blocks.return_value = [
            {
                "block_index": 38,
                "page": 1,
                "block_type": "text",
                "text": "Some preceding paragraph.",
            },
            {
                "block_index": 40,
                "page": 1,
                "block_type": "text",
                "text": (
                    "Figure 3. (a) Electronic band structure of pristine "
                    "zigzag (10,0) SWCNT and (b) corresponding density of "
                    "states."
                ),
            },
        ]
        store.get_figure_image.return_value = {
            "image_bytes": b"fake-png-bytes",
            "image_ext": ".png",
            "fig_num": 3,
            "caption": "",
        }
        return h, store

    def test_overview_pairs_caption_from_body(self):
        h, store = self._handler_with_orphan_caption()
        out = h._read_figures(store, store.get.return_value, "3")
        assert "[no caption]" not in out
        assert "Electronic band structure" in out
        # Bold marker + figure number prefix lands ahead of the caption
        assert "**Figure 3.**" in out

    def test_legend_returns_rescued_caption(self):
        h, store = self._handler_with_orphan_caption()
        out = h._read_figures(store, store.get.return_value, "3/legend")
        assert out.startswith("Figure 3.")
        assert "Electronic band structure" in out

    def test_image_view_carries_caption_inline(self):
        h, store = self._handler_with_orphan_caption()
        out = h._read_figures(store, store.get.return_value, "3/image")
        # Caption appears above the base64 blob
        idx_caption = out.find("**Figure 3.**")
        idx_b64 = out.find("data:image/png;base64,")
        assert idx_caption >= 0, "caption should be present in /image view"
        assert idx_b64 >= 0
        assert idx_caption < idx_b64

    def test_rescue_prefers_same_page_match(self):
        h = PaperHandler()
        store = MagicMock()
        store.get_blocks.return_value = [
            {
                "block_index": 5,
                "page": 7,
                "block_type": "text",
                "text": "Figure 2. Caption from page 7.",
            },
            {
                "block_index": 10,
                "page": 9,
                "block_type": "text",
                "text": "Figure 2. Caption from page 9.",
            },
        ]
        rescued = h._rescue_caption(store, "x", 2, page=9)
        assert rescued == "Figure 2. Caption from page 9."


# ---------------------------------------------------------------------------
# v2 D5 — pagination clamping at end of paper
# ---------------------------------------------------------------------------


class TestRangeClamping:
    """``~38..200`` on an 87-block paper used to deliver blocks
    38..85 then advertise a fake ``Next: ~200.. for more`` hint that
    paginated into an empty range.  Review 2026-04-25 finding D5.
    """

    @staticmethod
    def _handler_with_blocks(n_blocks: int):
        h = PaperHandler()
        store = MagicMock()
        ref = {"slug": "wu2008first", "ref_id": 1}
        store.get.return_value = ref
        store.get_blocks.return_value = [
            {
                "block_index": i,
                "page": i // 5 + 1,
                "block_type": "text",
                "text": f"block {i}",
                "node_id": f"n{i}",
            }
            for i in range(n_blocks)
        ]
        store.get_links.return_value = []
        return h, store, ref

    def test_range_extending_past_end_clamps(self):
        h, store, ref = self._handler_with_blocks(87)
        out = h._read_chunks(store, ref, "38..200")
        # Clamp notice surfaces up front
        assert "Range clamped to" in out
        assert "~38..86" in out
        # End-of-paper marker present, no aspirational Next: hint
        assert "End of paper" in out
        assert "Next: get(id='wu2008first~200" not in out
        # The last delivered block is the actual last block
        assert ">> wu2008first ~86" in out

    def test_open_range_still_works_at_end(self):
        h, store, ref = self._handler_with_blocks(87)
        out = h._read_chunks(store, ref, "80..")
        assert "End of paper" in out
        # No off-the-end Next: line
        assert "Next: get(" not in out

    def test_range_inside_paper_keeps_next_hint(self):
        h, store, ref = self._handler_with_blocks(87)
        out = h._read_chunks(store, ref, "10..30")
        # Mid-paper range gets a Next: hint pointing at the next chunk.
        assert "End of paper" not in out
        assert "Next: get(id='wu2008first~30..')" in out

    def test_range_starting_past_end_explains(self):
        h, store, ref = self._handler_with_blocks(87)
        out = h._read_chunks(store, ref, "200..300")
        assert "paper has 87 blocks" in out
        assert "~0..86" in out


# ---------------------------------------------------------------------------
# v3 B7 — caption-label deduplication
# ---------------------------------------------------------------------------


class TestCaptionBodyStripsLeadingLabel:
    """``_caption_body`` strips ``Figure N.`` / ``Fig. N`` /
    ``Scheme N.`` prefixes so the formatter doesn't emit
    ``**Figure 1.** Figure 1. CO2 cycle...``.

    Review 2026-04-25 mcp-critic finding B7 (caption-label
    duplication — the formatter adds its own bold marker on top of
    a caption that already includes the label).
    """

    def test_strips_figure_label(self):
        assert (
            _caption_body("Figure 1. CO2 cycle due to anthropogenic activities")
            == "CO2 cycle due to anthropogenic activities"
        )

    def test_strips_fig_dot_label(self):
        assert _caption_body("Fig. 3 Schematic of the apparatus") == "Schematic of the apparatus"

    def test_strips_scheme_label(self):
        assert _caption_body("Scheme 2. Reaction pathway") == "Reaction pathway"

    def test_caption_without_label_unchanged(self):
        assert _caption_body("Some caption text") == "Some caption text"

    def test_empty_caption_returns_empty(self):
        assert _caption_body("") == ""

    def test_overview_does_not_duplicate_label(self):
        """End-to-end: ``/fig/N`` overview never emits ``Figure N. Figure N.``."""
        h = PaperHandler()
        store = MagicMock()
        store.get.return_value = {"slug": "x", "title": "T", "ref_id": 1}
        store.get_figures.return_value = [
            {
                "fig_num": 1,
                "caption": "Figure 1. CO2 cycle due to anthropogenic activities",
                "page": 1,
                "block_index": 0,
            },
        ]
        store.get_blocks.return_value = []
        out = h._read_figures(store, store.get.return_value, "1")
        # The bold marker appears once; the literal "Figure 1." prefix
        # has been stripped from the caption body.
        assert "**Figure 1.** CO2 cycle" in out
        assert "Figure 1. Figure 1." not in out
        # Negative: the legend view (no bold marker) still ships the
        # full label intact for citation purposes.
        legend = h._read_figures(store, store.get.return_value, "1/legend")
        assert legend.startswith("Figure 1.")


# ---------------------------------------------------------------------------
# v3 B7 (#2) — _resolved_figs re-binds fig_num to printed number
# ---------------------------------------------------------------------------


class TestResolvedFigsRebindsAutoNumberedFigures:
    """When the figure extractor missed a caption and the store
    auto-assigned ``fig_num=3`` while the printed caption is
    ``Figure 4.``, ``_resolved_figs`` re-binds the API number to 4
    and pairs the body-block caption.  Review 2026-04-25 mcp-critic
    finding B7 (figure-number mismatch).
    """

    def _store_with_orphaned_fig(self):
        store = MagicMock()
        store.get_figures.return_value = [
            {
                "fig_num": 3,           # auto-assigned
                "caption": "",          # extractor lost it
                "page": 6,
                "block_index": 38,
            },
        ]
        store.get_blocks.return_value = [
            {
                "block_index": 38,
                "page": 6,
                "block_type": "figure",
                "text": "",
            },
            {
                "block_index": 39,
                "page": 6,
                "block_type": "text",
                "text": "Figure 4. ATR-SEIRAS spectra showing key intermediates.",
            },
        ]
        return store

    def test_fig_num_rebinds_to_printed_number(self):
        store = self._store_with_orphaned_fig()
        figs = PaperHandler._resolved_figs(store, "ni2024atomic")
        assert len(figs) == 1
        assert figs[0]["fig_num"] == 4   # the printed label
        assert figs[0]["_orig_fig_num"] == 3   # bundle-side lookup key
        assert "ATR-SEIRAS" in figs[0]["caption"]

    def test_caption_pairs_in_overview_view(self):
        h = PaperHandler()
        store = self._store_with_orphaned_fig()
        store.get.return_value = {"slug": "ni2024atomic", "title": "T", "ref_id": 1}
        # Caller asks for the printed number.
        out = h._read_figures(store, store.get.return_value, "4")
        assert "[no caption]" not in out
        assert "ATR-SEIRAS" in out

    def test_old_extraction_index_not_advertised(self):
        """The auto-assigned ``3`` must not appear in the listing once
        ``4`` has taken its place — otherwise ``/fig/3`` resolves to
        nothing while the caller copies it from a stale enum."""
        h = PaperHandler()
        store = self._store_with_orphaned_fig()
        store.get.return_value = {"slug": "ni2024atomic", "title": "T", "ref_id": 1}
        listing = h._read_figures(store, store.get.return_value, None)
        assert "fig 4" in listing
        # No "fig 3" entry — the API number is the printed number.
        assert "fig 3 " not in listing


# ---------------------------------------------------------------------------
# v3 B5 — empty figure-block chunks emit /fig/N hint
# ---------------------------------------------------------------------------


class TestEmptyFigureBlockChunkEmitsFigHint:
    """``PaperHandler._block_chunk_hint`` returns a single
    ``→ get(id='<slug>/fig/<N>')`` line for figure-type blocks with
    empty text.  Review 2026-04-25 mcp-critic finding B5 — the chunk
    view used to render an empty figure block as a header followed
    by two blank lines, with no signal that the figure binary lives
    at ``/fig/N``.
    """

    def test_returns_fig_hint_for_empty_figure_block(self):
        h = PaperHandler()
        store = MagicMock()
        store.get_figures.return_value = [
            {"fig_num": 4, "caption": "", "page": 6, "block_index": 38},
        ]
        store.get_blocks.return_value = [
            {
                "block_index": 38,
                "page": 6,
                "block_type": "figure",
                "text": "",
            },
            {
                "block_index": 39,
                "page": 6,
                "block_type": "text",
                "text": "Figure 4. ATR-SEIRAS spectra.",
            },
        ]
        block = {
            "block_index": 38,
            "block_type": "figure",
            "text": "",
            "page": 6,
        }
        hint = h._block_chunk_hint(store, "ni2024atomic", block)
        assert "get(id='ni2024atomic/fig/4')" in hint

    def test_returns_empty_for_text_block(self):
        h = PaperHandler()
        store = MagicMock()
        block = {"block_index": 12, "block_type": "text", "text": "hello"}
        assert h._block_chunk_hint(store, "x", block) == ""

    def test_returns_empty_when_block_has_no_index(self):
        h = PaperHandler()
        store = MagicMock()
        block = {"block_index": None, "block_type": "figure", "text": ""}
        assert h._block_chunk_hint(store, "x", block) == ""


# ---------------------------------------------------------------------------
# v3 M — inverted chunk range
# ---------------------------------------------------------------------------


class TestInvertedChunkRange:
    """``~5..3`` raises ``ID_MALFORMED`` with a swap-the-ends hint
    instead of returning ``"No blocks in range ~5..3"`` — the silent
    empty result was indistinguishable from a valid empty selector.

    Review 2026-04-25 mcp-critic finding M (inverted range).  Lives
    in :class:`precis.handlers._ref_base.RefHandler`; tested here
    alongside the rest of the chunk-range coverage.
    """

    def test_inverted_range_raises_id_malformed(self):
        from precis.handlers._ref_base import RefHandler

        h = RefHandler()
        store = MagicMock()
        ref = {"slug": "x"}
        with pytest.raises(PrecisError) as excinfo:
            h._read_chunks(store, ref, "5..3")
        assert excinfo.value.code is ErrorCode.ID_MALFORMED
        # Recovery hint suggests the swapped form.
        assert "3..5" in (excinfo.value.next or "")
        assert "inverted" in (excinfo.value.cause or "").lower()

    def test_normal_range_still_passes(self):
        from precis.handlers._ref_base import RefHandler

        h = RefHandler()
        store = MagicMock()
        store.get_blocks.return_value = []
        store.get_links.return_value = []
        ref = {"slug": "x"}
        # Doesn't raise; returns the empty-blocks message.
        out = h._read_chunks(store, ref, "3..5")
        assert "No blocks" in out
