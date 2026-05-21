"""Tests for lookup module — arxiv filename extraction and author parsing."""

from __future__ import annotations

from unittest.mock import patch

from precis.ingest.lookup import (
    _extract_arxiv_from_filename,
    _parse_author_string,
    lookup,
)


class TestExtractArxivFromFilename:
    def test_standard_arxiv(self):
        assert _extract_arxiv_from_filename("/papers/2508.20254v1.pdf") == "2508.20254"

    def test_arxiv_with_version(self):
        assert _extract_arxiv_from_filename("/papers/2310.18288v3.pdf") == "2310.18288"

    def test_arxiv_no_version(self):
        assert _extract_arxiv_from_filename("/papers/2601.16955.pdf") == "2601.16955"

    def test_arxiv_with_timestamp_suffix(self):
        assert (
            _extract_arxiv_from_filename("/papers/2504.02767v1_20260402224204.pdf")
            == "2504.02767"
        )

    def test_non_arxiv_filename(self):
        assert _extract_arxiv_from_filename("/papers/smith2024catalyst.pdf") is None

    def test_doi_style_filename(self):
        assert _extract_arxiv_from_filename("/papers/s41557-025-01815-x.pdf") is None

    def test_ssrn_filename(self):
        assert _extract_arxiv_from_filename("/papers/ssrn-5409063.pdf") is None

    def test_five_digit_arxiv(self):
        assert _extract_arxiv_from_filename("/papers/2603.29152v1.pdf") == "2603.29152"

    def test_arxiv_with_page_suffix(self):
        """Filenames like 2603.29152v1-4.pdf (pages) — still extract the ID."""
        # The regex matches at the start, so this should work
        assert (
            _extract_arxiv_from_filename("/papers/2603.29152v1-4.pdf") == "2603.29152"
        )


class TestParseAuthorString:
    def test_empty(self):
        assert _parse_author_string("") == []

    def test_whitespace(self):
        assert _parse_author_string("   ") == []

    def test_single_author(self):
        assert _parse_author_string("Smith, John") == [{"name": "Smith, John"}]

    def test_semicolon_separated(self):
        result = _parse_author_string("Daniel S. Levine; Nicholas Liesen; Lauren Chua")
        assert len(result) == 3
        assert result[0] == {"name": "Daniel S. Levine"}
        assert result[2] == {"name": "Lauren Chua"}

    def test_and_separated(self):
        result = _parse_author_string("Smith, John and Doe, Jane")
        assert len(result) == 2
        assert result[0] == {"name": "Smith, John"}
        assert result[1] == {"name": "Doe, Jane"}

    def test_none_input(self):
        assert _parse_author_string(None) == []


class TestGarbageTitleGatesS2Fallback:
    """When embedded title is garbage (InDesign filename, tracking ID, etc.)
    the cascade must skip S2 title fallback, which otherwise returns a
    random wrong paper with high confidence."""

    _BASE_META = {
        "pdf_hash": "deadbeef",
        "page_count": 9,
        "first_pages_text": "",
        "info": {},
        "doi": None,
    }

    @staticmethod
    def _fail_s2_loudly(
        *args, **kwargs
    ):  # pragma: no cover - only called on regression
        raise AssertionError(
            "lookup_title must not be called for garbage embedded titles"
        )

    def _run_with_garbage_title(self, title: str) -> dict:
        meta = dict(self._BASE_META)
        meta["info"] = {"title": title}
        with (
            patch("precis.ingest.lookup.extract_pdf_meta", return_value=meta),
            patch(
                "precis.ingest.lookup.lookup_title",
                side_effect=self._fail_s2_loudly,
            ),
        ):
            return lookup("/fake/path.pdf")

    def test_indesign_filename_skips_s2(self):
        result = self._run_with_garbage_title("nmat1849 Geim Progress Article.indd")
        assert result["source"] == "embedded"
        assert result["title"] == ""  # garbage title cleared in fallback
        assert result["doi"] is None

    def test_page_range_id_skips_s2(self):
        result = self._run_with_garbage_title("nl404795z 1..9")
        assert result["source"] == "embedded"
        assert result["title"] == ""

    def test_revtex_boilerplate_skips_s2(self):
        result = self._run_with_garbage_title("USING STANDARD PRB S")
        assert result["source"] == "embedded"
        assert result["title"] == ""

    def test_numeric_manuscript_id_skips_s2(self):
        result = self._run_with_garbage_title("78868 651..703")
        assert result["source"] == "embedded"
        assert result["title"] == ""

    def test_real_title_still_queries_s2(self):
        """A plausible title must still hit S2 — regression guard."""
        meta = dict(self._BASE_META)
        meta["info"] = {"title": "The rise of graphene"}
        s2_hit = {
            "title": "The rise of graphene",
            "authors": [{"name": "Geim, A. K."}],
            "year": 2007,
            "doi": "10.1038/nmat1849",
            "journal": "Nature Materials",
            "abstract": "",
            "entry_type": "article",
            "source": "s2",
        }
        with (
            patch("precis.ingest.lookup.extract_pdf_meta", return_value=meta),
            patch("precis.ingest.lookup.lookup_title", return_value=s2_hit) as m,
        ):
            result = lookup("/fake/path.pdf")
        m.assert_called_once()
        assert result["doi"] == "10.1038/nmat1849"
        assert result["source"] == "s2"
