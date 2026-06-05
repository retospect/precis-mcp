"""Phase 2.5 tests: text normalisation + metadata verification.

Coverage:
- ``_text_norm`` primitives (NFKD strip, German phonetic alt,
  Jaccard, surname matching)
- ``verify_against_crossref`` per-field outcomes
- ``check_doi(bib_entry=...)`` end-to-end (mocked Crossref)
- Renderer ``view='verify'`` Metadata mismatch section
- Handler structured-input detection / validation
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from precis.handlers._provenance_report import render_batch
from precis.ingest._text_norm import (
    best_jaccard,
    jaccard,
    normalised_token_sets,
    surname_matches,
)
from precis.ingest.provenance import (
    BibEntry,
    ProvenanceResult,
    check_doi,
    check_dois,
    verify_against_crossref,
)

# ---------------------------------------------------------------------------
# _text_norm primitives
# ---------------------------------------------------------------------------


class TestNormalisedTokenSets:
    def test_nfkd_strips_diacritics(self) -> None:
        nfkd, _phon = normalised_token_sets("Müller")
        # NFKD: Müller → Muller → muller
        assert "muller" in nfkd

    def test_nfkd_handles_naive(self) -> None:
        nfkd, _phon = normalised_token_sets("naïve")
        assert "naive" in nfkd

    def test_nfkd_handles_subscripts(self) -> None:
        nfkd, _phon = normalised_token_sets("H₂O")
        assert "h2o" in nfkd

    def test_nfkd_handles_superscripts(self) -> None:
        nfkd, _phon = normalised_token_sets("E=mc²")
        # ² decomposes to 2 under NFKD; mc² → mc2 after collapse
        assert "mc2" in nfkd

    def test_nfkd_handles_ligatures(self) -> None:
        nfkd, _phon = normalised_token_sets("ﬁnding")
        # ﬁ → fi under NFKD
        assert "finding" in nfkd

    def test_german_phonetic_muller(self) -> None:
        _nfkd, phon = normalised_token_sets("Müller")
        assert "mueller" in phon

    def test_german_phonetic_schroder(self) -> None:
        _nfkd, phon = normalised_token_sets("Schröder")
        assert "schroeder" in phon

    def test_german_phonetic_weiss(self) -> None:
        _nfkd, phon = normalised_token_sets("Weiß")
        assert "weiss" in phon

    def test_stopwords_dropped(self) -> None:
        nfkd, _phon = normalised_token_sets("The role of beta cells")
        # 'the' and 'of' should be gone; 'role', 'beta', 'cells' stay
        assert "the" not in nfkd
        assert "of" not in nfkd
        assert "role" in nfkd
        assert "beta" in nfkd
        assert "cells" in nfkd

    def test_punctuation_replaced_with_space(self) -> None:
        nfkd, _phon = normalised_token_sets("A,B,C:D-E")
        assert nfkd == frozenset({"b", "c", "d", "e"})  # 'a' dropped as stopword


class TestJaccard:
    def test_full_overlap(self) -> None:
        assert jaccard(frozenset({"a", "b"}), frozenset({"a", "b"})) == 1.0

    def test_no_overlap(self) -> None:
        assert jaccard(frozenset({"a"}), frozenset({"b"})) == 0.0

    def test_partial(self) -> None:
        # |A∩B|/|A∪B| = 1/3
        assert jaccard(frozenset({"a", "b"}), frozenset({"a", "c"})) == pytest.approx(
            1 / 3
        )

    def test_both_empty(self) -> None:
        # Vacuously equal
        assert jaccard(frozenset(), frozenset()) == 1.0

    def test_one_empty(self) -> None:
        assert jaccard(frozenset({"a"}), frozenset()) == 0.0


class TestBestJaccard:
    def test_title_word_order_invariant(self) -> None:
        score, _, _ = best_jaccard(
            "The role of X in Y",
            "Y: the role of X",
        )
        assert score == 1.0  # same tokens after stopword drop + normalise

    def test_substantive_difference_penalised(self) -> None:
        score, _, _ = best_jaccard(
            "Role of beta cells in diabetes",
            "Role of beta cells in cancer",
        )
        # 'diabetes' vs 'cancer' is one swap out of 4 content tokens
        # Jaccard = 3/5 = 0.6
        assert 0.5 < score < 0.7

    def test_diacritic_invariant(self) -> None:
        score, _, _ = best_jaccard("Müller study", "Muller study")
        assert score == 1.0


class TestSurnameMatches:
    def test_exact(self) -> None:
        assert surname_matches("Smith", "Smith") is True

    def test_case_insensitive(self) -> None:
        assert surname_matches("smith", "SMITH") is True

    def test_diacritic_nfkd(self) -> None:
        # Müller (with umlaut) ↔ Muller (without): NFKD path matches
        assert surname_matches("Muller", "Müller") is True

    def test_german_phonetic(self) -> None:
        # Müller ↔ Mueller: phonetic alt matches
        assert surname_matches("Mueller", "Müller") is True

    def test_schroder_phonetic(self) -> None:
        assert surname_matches("Schroeder", "Schröder") is True

    def test_different_surnames(self) -> None:
        assert surname_matches("Smith", "Jones") is False

    def test_empty_supplied(self) -> None:
        assert surname_matches("", "Smith") is False

    def test_ascii_ascii_german_fold(self) -> None:
        """Pure ASCII↔ASCII surname pairs match via reverse-phonetic fold.

        Trades false positives (Sue↔Su) for catching real-world cases
        where both bib and Crossref happen to be ASCII transliterated.
        """
        assert surname_matches("Mueller", "Muller") is True
        assert surname_matches("Muller", "Mueller") is True
        assert surname_matches("Schroeder", "Schroder") is True
        assert surname_matches("Strauss", "Straus") is True

    def test_known_false_positives_accepted(self) -> None:
        """Documented trade-off — these are wrong but acceptable cost."""
        # The reverse-phonetic fold treats 'ue' as 'u' unconditionally.
        # This is the price we pay for catching Mueller↔Muller.
        assert surname_matches("Sue", "Su") is True
        assert surname_matches("Press", "Pres") is True

    def test_substantively_different_surnames_still_rejected(self) -> None:
        """The fold does not introduce false positives across unrelated names."""
        assert surname_matches("Smith", "Jones") is False
        assert surname_matches("Wang", "Wong") is False
        assert surname_matches("Garcia", "Martinez") is False


# ---------------------------------------------------------------------------
# verify_against_crossref
# ---------------------------------------------------------------------------


class TestVerifyAgainstCrossref:
    def _msg(
        self,
        title: str = "A definitive study",
        author_family: str = "Smith",
        year: int = 2020,
    ) -> dict:
        return {
            "title": [title],
            "author": [{"family": author_family, "given": "John"}],
            "published-print": {"date-parts": [[year]]},
            "DOI": "10.1234/foo",
        }

    def test_perfect_match(self) -> None:
        msg = self._msg()
        bib = BibEntry(
            doi="10.1234/foo",
            title="A definitive study",
            authors=["Smith"],
            year=2020,
        )
        v = verify_against_crossref(msg, bib)
        assert v.title_score == 1.0
        assert v.first_author_match is True
        assert v.year_match == "match"

    def test_title_mismatch(self) -> None:
        msg = self._msg(title="A definitive study of quantum widgets")
        bib = BibEntry(
            doi="10.1234/foo",
            title="An entirely different topic",
            authors=["Smith"],
            year=2020,
        )
        v = verify_against_crossref(msg, bib)
        assert v.title_score is not None
        assert v.title_score < 0.5
        assert v.title_added_tokens  # tokens in Crossref not in supplied
        assert v.title_removed_tokens

    def test_first_author_mismatch(self) -> None:
        msg = self._msg(author_family="Smith")
        bib = BibEntry(
            doi="10.1234/foo",
            authors=["Jones"],
        )
        v = verify_against_crossref(msg, bib)
        assert v.first_author_match is False
        assert v.first_author_supplied == "Jones"
        assert v.first_author_crossref == "Smith"

    def test_first_author_german_phonetic_match(self) -> None:
        msg = self._msg(author_family="Müller")
        bib = BibEntry(doi="10.1234/foo", authors=["Mueller"])
        v = verify_against_crossref(msg, bib)
        assert v.first_author_match is True

    def test_year_off_by_one(self) -> None:
        msg = self._msg(year=2020)
        bib = BibEntry(doi="10.1234/foo", year=2021)
        v = verify_against_crossref(msg, bib)
        assert v.year_match == "off_by_one"

    def test_year_mismatch(self) -> None:
        msg = self._msg(year=2020)
        bib = BibEntry(doi="10.1234/foo", year=2015)
        v = verify_against_crossref(msg, bib)
        assert v.year_match == "mismatch"

    def test_unchecked_when_field_missing(self) -> None:
        msg = self._msg()
        bib = BibEntry(doi="10.1234/foo")  # nothing to compare
        v = verify_against_crossref(msg, bib)
        assert v.title_score is None
        assert v.first_author_match is None
        assert v.year_match == "unchecked"


# ---------------------------------------------------------------------------
# check_doi / check_dois with bib_entry
# ---------------------------------------------------------------------------


class TestCheckDoiWithBibEntry:
    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_verification_populated_when_bib_supplied(
        self,
        mock_fetch,
    ) -> None:
        mock_fetch.return_value = {
            "title": ["Real title"],
            "author": [{"family": "Smith", "given": "J"}],
            "published-print": {"date-parts": [[2020]]},
            "DOI": "10.1234/foo",
        }
        bib = BibEntry(
            doi="10.1234/foo",
            title="Wrong title entirely",
            authors=["Jones"],
            year=2020,
        )
        r = check_doi("10.1234/foo", bib_entry=bib)
        assert r.verification is not None
        assert r.has_metadata_mismatch is True
        assert r.verification.first_author_match is False

    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_no_verification_when_bib_omitted(self, mock_fetch) -> None:
        mock_fetch.return_value = {
            "title": ["Anything"],
            "DOI": "10.1234/foo",
        }
        r = check_doi("10.1234/foo")
        assert r.verification is None
        assert r.has_metadata_mismatch is False


class TestCheckDoisWithBibEntries:
    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_positional_pairing(self, mock_fetch) -> None:
        # Different Crossref response per DOI
        responses = {
            "10.1234/a": {
                "title": ["Title A"],
                "author": [{"family": "Smith"}],
                "DOI": "10.1234/a",
            },
            "10.5678/b": {
                "title": ["Title B"],
                "author": [{"family": "Jones"}],
                "DOI": "10.5678/b",
            },
        }
        mock_fetch.side_effect = lambda doi, mailto: responses[doi]
        entries = [
            BibEntry(doi="10.1234/a", authors=["Smith"]),  # match
            BibEntry(doi="10.5678/b", authors=["WrongName"]),  # mismatch
        ]
        results = check_dois(["10.1234/a", "10.5678/b"], bib_entries=entries)
        assert len(results) == 2
        assert results[0].verification is not None
        assert results[0].verification.first_author_match is True
        assert results[1].verification.first_author_match is False

    def test_mismatched_length_raises(self) -> None:
        with pytest.raises(ValueError, match="positional pairing"):
            check_dois(
                ["10.1234/a", "10.5678/b"],
                bib_entries=[BibEntry(doi="10.1234/a")],  # too short
            )


# ---------------------------------------------------------------------------
# Renderer view='verify'
# ---------------------------------------------------------------------------


class TestRenderVerifyView:
    def _make_mismatch(self) -> ProvenanceResult:
        from precis.ingest.provenance import MetadataVerification

        return ProvenanceResult(
            doi="10.1234/bad",
            status="ok",
            paper_title="Real title",
            paper_authors=[{"name": "Smith, J"}],
            paper_year=2020,
            verification=MetadataVerification(
                title_score=0.3,
                title_supplied="Wrong title",
                title_crossref="Real title",
                title_added_tokens=["real"],
                title_removed_tokens=["wrong"],
                first_author_match=False,
                first_author_supplied="Jones",
                first_author_crossref="Smith",
                year_match="match",
                year_supplied=2020,
                year_crossref=2020,
            ),
        )

    def _make_clean_verified(self) -> ProvenanceResult:
        from precis.ingest.provenance import MetadataVerification

        return ProvenanceResult(
            doi="10.1234/ok",
            status="ok",
            paper_title="Clean paper",
            verification=MetadataVerification(
                title_score=1.0,
                first_author_match=True,
                year_match="match",
            ),
        )

    def test_verify_view_surfaces_mismatch(self) -> None:
        out = render_batch([self._make_mismatch()], view="verify")
        assert "Metadata mismatch" in out
        assert "Jones" in out  # supplied surname
        assert "Smith" in out  # Crossref surname
        assert "Wrong title" in out
        assert "Real title" in out

    def test_verify_view_hides_clean(self) -> None:
        out = render_batch([self._make_clean_verified()], view="verify")
        assert "Metadata mismatch" not in out  # nothing to flag
        # Still shows the verification-ran note
        assert "Metadata verification ran on" in out

    def test_default_view_omits_mismatch_section(self) -> None:
        out = render_batch([self._make_mismatch()], view="default")
        # Mismatch section is verify-view-specific
        assert "Metadata mismatch" not in out


# ---------------------------------------------------------------------------
# Handler structured-input detection
# ---------------------------------------------------------------------------


class TestHandlerStructuredInput:
    def test_parse_structured_input_list(self) -> None:
        from precis.handlers.provenance import _parse_structured_input

        raw = '[{"doi": "10.1234/foo", "title": "Some title", "year": 2020}]'
        out = _parse_structured_input(raw)
        assert out is not None
        assert len(out) == 1
        assert out[0].doi == "10.1234/foo"
        assert out[0].title == "Some title"
        assert out[0].year == 2020

    def test_parse_structured_input_single_object(self) -> None:
        from precis.handlers.provenance import _parse_structured_input

        raw = '{"doi": "10.1234/foo"}'
        out = _parse_structured_input(raw)
        assert out is not None
        assert len(out) == 1

    def test_parse_structured_input_returns_none_for_plain_doi(self) -> None:
        from precis.handlers.provenance import _parse_structured_input

        assert _parse_structured_input("10.1234/foo") is None

    def test_parse_structured_input_authors_object_form(self) -> None:
        from precis.handlers.provenance import _parse_structured_input

        raw = (
            '[{"doi": "10.1234/foo", "authors": '
            '[{"family": "Smith", "given": "J"}, {"family": "Jones"}]}]'
        )
        out = _parse_structured_input(raw)
        assert out is not None
        assert out[0].authors == ["Smith", "Jones"]

    def test_parse_structured_input_authors_string_form(self) -> None:
        from precis.handlers.provenance import _parse_structured_input

        raw = '[{"doi": "10.1234/foo", "authors": ["Smith", "Jones"]}]'
        out = _parse_structured_input(raw)
        assert out is not None
        assert out[0].authors == ["Smith", "Jones"]

    def test_parse_structured_input_missing_doi_raises(self) -> None:
        from precis.errors import BadInput
        from precis.handlers.provenance import _parse_structured_input

        with pytest.raises(BadInput, match="missing 'doi'"):
            _parse_structured_input('[{"title": "no doi"}]')

    def test_parse_structured_input_bad_json_raises(self) -> None:
        from precis.errors import BadInput
        from precis.handlers.provenance import _parse_structured_input

        with pytest.raises(BadInput, match="won't parse"):
            _parse_structured_input("[not valid json")
