"""Tests for CrossRef metadata lookup."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from precis.ingest.crossref import _normalize, lookup_crossref


class TestCrossrefNormalize:
    def test_normalize_full(self, sample_crossref_response):
        msg = sample_crossref_response["message"]
        result = _normalize(msg, "10.1038/s41567-024-1234-5")
        assert result["title"] == "Quantum Error Correction in Practice"
        assert len(result["authors"]) == 2
        assert result["authors"][0]["name"] == "Smith, John"
        assert result["year"] == 2024
        assert result["journal"] == "Nature Physics"
        assert result["source"] == "crossref"

    def test_normalize_missing_authors(self):
        msg = {"title": ["A Paper"], "type": "article"}
        result = _normalize(msg, "10.1038/test")
        assert result["authors"] == []
        assert result["title"] == "A Paper"

    def test_normalize_empty_title(self):
        msg = {"author": [{"family": "Smith"}], "type": "article"}
        result = _normalize(msg, "10.1038/test")
        assert result["title"] == ""

    def test_normalize_corporate_author_uses_name_field(self):
        """Crossref returns ``{"name": "OECD"}`` for organisational authors —
        previously dropped, slug fell back to ``anon``. Now preserved."""
        msg = {
            "author": [{"name": "OECD", "sequence": "first"}],
            "title": ["Teacher support for student learning"],
            "type": "report",
        }
        result = _normalize(msg, "10.1787/97b3a899-en")
        assert result["authors"] == [{"name": "OECD"}]

    def test_normalize_skips_affiliation_strings_in_author_list(self):
        """Some publishers (e.g. 10.63125/grqtf978) inject affiliation
        strings as fake author entries. The slug surname must come from a
        real author, not "Master of Science in Management, ...".
        """
        msg = {
            "author": [
                {
                    "name": "Master of Science in Management, St. Francis College, NY, USA",
                    "sequence": "first",
                },
                {"given": "Zahir", "family": "Babar", "sequence": "first"},
                {
                    "name": "MSc in Business Analyst, St. Francis College, NY, USA",
                    "sequence": "additional",
                },
                {"given": "Rajesh", "family": "Paul", "sequence": "additional"},
            ],
            "title": ["A Systematic Review"],
            "type": "journal-article",
        }
        result = _normalize(msg, "10.63125/grqtf978")
        # Affiliation entries dropped; first real author is Babar.
        assert result["authors"] == [
            {"name": "Babar, Zahir"},
            {"name": "Paul, Rajesh"},
        ]

    def test_normalize_falls_back_to_editors(self):
        """Edited collections (proceedings, books) often have no authors —
        only editors. Use them so the slug isn't anon."""
        msg = {
            "author": None,
            "editor": [{"given": "Maria", "family": "De Marsico", "sequence": "first"}],
            "title": ["Pattern Recognition and Image Analysis"],
            "type": "book",
        }
        result = _normalize(msg, "10.1007/978-3-031-04881-4")
        assert result["authors"] == [{"name": "De Marsico, Maria"}]

    def test_normalize_no_author_no_editor(self):
        """When neither authors nor editors exist (rare), return [] —
        slug will fall back to ``anon`` cleanly."""
        msg = {"title": ["Some paper"], "type": "journal-article"}
        result = _normalize(msg, "10.2533/chimia.2014.204")
        assert result["authors"] == []


class TestCrossrefLookup:
    @patch("precis.ingest.crossref.Crossref")
    def test_lookup_success(self, mock_cr_cls, sample_crossref_response):
        mock_cr = MagicMock()
        mock_cr.works.return_value = sample_crossref_response
        mock_cr_cls.return_value = mock_cr

        result = lookup_crossref("10.1038/s41567-024-1234-5")
        assert result is not None
        assert result["title"] == "Quantum Error Correction in Practice"

    @patch("precis.ingest.crossref.Crossref")
    def test_lookup_not_found(self, mock_cr_cls):
        mock_cr = MagicMock()
        mock_cr.works.return_value = None
        mock_cr_cls.return_value = mock_cr

        result = lookup_crossref("10.1038/nonexistent")
        assert result is None

    @patch("precis.ingest.crossref.Crossref")
    def test_lookup_exception(self, mock_cr_cls):
        mock_cr = MagicMock()
        mock_cr.works.side_effect = Exception("network error")
        mock_cr_cls.return_value = mock_cr

        result = lookup_crossref("10.1038/error")
        assert result is None

    @patch("precis.ingest.crossref.Crossref")
    def test_mailto_passed(self, mock_cr_cls):
        mock_cr = MagicMock()
        mock_cr.works.return_value = None
        mock_cr_cls.return_value = mock_cr

        lookup_crossref("10.1038/test", mailto="test@example.com")
        mock_cr_cls.assert_called_once_with(mailto="test@example.com")
