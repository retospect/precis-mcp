"""Tests for metadata verification."""

from __future__ import annotations

from precis.ingest.verify_metadata import (
    _doi_prefix_in_text,
    _normalize,
    _word_overlap_score,
    verify_metadata,
)


class TestVerifyMetadata:
    def test_matching_title_and_author(self):
        header = {
            "title": "Quantum Error Correction",
            "authors": [{"name": "Smith, John"}],
        }
        text = "Quantum Error Correction\nJohn Smith\nDepartment of Physics"
        verified, warnings = verify_metadata(header, text)
        assert verified is True
        assert warnings == []

    def test_mismatched_title(self):
        header = {
            "title": "Completely Different Paper Title",
            "authors": [{"name": "Smith, John"}],
        }
        text = "Quantum Error Correction\nJohn Smith"
        verified, warnings = verify_metadata(header, text)
        assert verified is False
        assert any("Title mismatch" in w for w in warnings)

    def test_mismatched_author(self):
        header = {
            "title": "Quantum Error Correction",
            "authors": [{"name": "Zzzzynski, Xander"}],
        }
        text = "Quantum Error Correction\nJohn Smith"
        verified, warnings = verify_metadata(header, text)
        assert verified is False
        assert any("Author surname" in w for w in warnings)

    def test_empty_header(self):
        header = {"title": "", "authors": []}
        text = "Some text"
        verified, warnings = verify_metadata(header, text)
        assert verified is True
        assert warnings == []

    def test_custom_threshold(self):
        header = {
            "title": "Quantum Error Correction",
            "authors": [],
        }
        text = "quantum error corrections"
        verified, warnings = verify_metadata(header, text, threshold=95)
        assert verified is True  # partial_ratio is lenient

    def test_unicode_dash_in_title(self):
        """Title with Unicode hyphen (U+2010) should match ASCII hyphen in PDF."""
        header = {
            "title": "Heterogeneous single\u2010atom catalysis",
            "authors": [{"name": "Wang, Aiqin"}],
        }
        text = "Heterogeneous single-atom catalysis\nAiqin Wang"
        verified, warnings = verify_metadata(header, text)
        assert verified is True
        assert warnings == []

    def test_en_dash_in_title(self):
        """En-dash (U+2013) in crossref title vs hyphen in PDF."""
        header = {
            "title": "Metal\u2013organic frameworks for CO2 capture",
            "authors": [],
        }
        text = "Metal-organic frameworks for CO2 capture"
        verified, warnings = verify_metadata(header, text)
        assert verified is True

    def test_subtitle_fallback(self):
        """If full title with subtitle fails, main title alone should pass."""
        header = {
            "title": "Single-atom catalysis: a concise review of recent advances",
            "authors": [],
        }
        # PDF only shows the main title
        text = "Single-atom catalysis\nAuthors and affiliations..."
        verified, warnings = verify_metadata(header, text)
        assert verified is True

    def test_subtitle_with_dash_separator(self):
        """Subtitle separated by ' - ' should also try main title."""
        header = {
            "title": "Carbon capture and storage - current status and future directions",
            "authors": [],
        }
        text = "Carbon capture and storage\nA. Smith et al."
        verified, warnings = verify_metadata(header, text)
        assert verified is True

    def test_one_author_passes_multi_author_ok(self):
        """If at least one author matches, the paper passes."""
        header = {
            "title": "Quantum Error Correction",
            "authors": [
                {"name": "Zzzzynski, Xander"},
                {"name": "Smith, John"},
            ],
        }
        text = "Quantum Error Correction\nJohn Smith"
        verified, warnings = verify_metadata(header, text)
        assert verified is True

    def test_all_authors_fail_still_rejected(self):
        """If NO authors match, the paper is rejected."""
        header = {
            "title": "Quantum Error Correction",
            "authors": [
                {"name": "Zzzzynski, Xander"},
                {"name": "Qqqbert, Yaroslav"},
            ],
        }
        text = "Quantum Error Correction\nJohn Smith"
        verified, warnings = verify_metadata(header, text)
        assert verified is False
        assert any("Author surname" in w for w in warnings)

    def test_short_surname_lower_threshold(self):
        """Short surnames (≤4 chars) use a 60 threshold, not 80."""
        header = {
            "title": "Quantum Error Correction",
            "authors": [{"name": "Dai, Yun"}],
        }
        # 'dai' is 3 chars — partial_ratio against long text is low
        # but it should be found in the text with threshold=60
        text = "Quantum Error Correction\nYun Dai\nDepartment of Chemistry"
        verified, warnings = verify_metadata(header, text)
        assert verified is True

    def test_html_sub_tags_in_title(self):
        """S2 titles with <sub>/<sup> tags should match plain PDF text."""
        header = {
            "title": "How CO<sub>2</sub> Self-Consumption Distorts the Apparent Tafel Slope",
            "authors": [],
        }
        text = "How CO2 Self-Consumption Distorts the Apparent Tafel Slope"
        verified, warnings = verify_metadata(header, text)
        assert verified is True
        assert warnings == []

    def test_html_sub_with_whitespace(self):
        """S2 titles with whitespace around HTML tags (real-world format)."""
        header = {
            "title": "Electrocatalytic CO\n                    <sub>2</sub>\n                    Reduction on Pd Nanoplates",
            "authors": [],
        }
        text = "Electrocatalytic CO2 Reduction on Pd Nanoplates"
        verified, warnings = verify_metadata(header, text)
        assert verified is True
        assert warnings == []

    def test_genuine_mismatch_still_fails(self):
        """Real mismatches should still be caught after normalization."""
        header = {
            "title": "Completely unrelated paper about quantum computing",
            "authors": [],
        }
        text = "Heterogeneous single-atom catalysis\nWang et al."
        verified, warnings = verify_metadata(header, text)
        assert verified is False


class TestGreekLetterFold:
    """CrossRef titles use Greek letters; bodies often use Latin transcription.

    The classic failure: title says ``High-κ dielectrics``, body says
    ``High-k dielectrics`` — partial_ratio collapses because κ vs k
    derails the string alignment.
    """

    def test_kappa_fold(self):
        assert _normalize("High-κ dielectrics") == "high-k dielectrics"

    def test_multiple_greek_letters(self):
        assert _normalize("α-helix and β-sheet") == "a-helix and b-sheet"

    def test_uppercase_greek(self):
        assert _normalize("ΔG of reaction") == "dg of reaction"

    def test_high_kappa_title_matches_high_k_body(self):
        """Regression: nmat769 / Javey 2002 — was scoring 56% pre-Phase-2."""
        header = {
            "title": "High-κ dielectrics for advanced carbon-nanotube transistors and logic gates",
            "authors": [{"name": "Javey, Ali"}],
        }
        # Body as PyMuPDF extracts it — note wrapped "H\nigh-κ" and the
        # Greek κ without a space before "dielectrics" (typical of column-
        # break artefacts in Nature PDFs).
        text = (
            "© 2002 Nature Publishing Group\nARTICLES\n"
            "Ali Javey, Hyoungsub Kim, Markus Brink, et al.\n"
            "H\nigh-κdielectrics have been actively pursued to replace SiO2 "
            "as gate insulators for silicon devices. Carbon nanotube "
            "transistors with advanced logic gates..."
        )
        verified, warnings = verify_metadata(header, text)
        assert verified is True, f"expected verified, got warnings: {warnings}"


class TestWordOverlapScore:
    def test_full_overlap(self):
        assert (
            _word_overlap_score(
                "high-k dielectrics carbon nanotube",
                "high-k dielectrics have been used in carbon nanotube devices",
            )
            == 100.0
        )

    def test_partial_overlap(self):
        # 2 of 3 content words appear → 66.7%
        score = _word_overlap_score(
            "addition enhance flux", "enhance flux measurements"
        )
        assert 60 <= score <= 70

    def test_stopwords_excluded(self):
        # Only "quantum" and "correction" are content words; both appear
        assert (
            _word_overlap_score(
                "the quantum in correction", "quantum error correction methods"
            )
            == 100.0
        )

    def test_short_words_excluded(self):
        # "of", "to" would be stopwords anyway, but also under 4 chars
        assert _word_overlap_score("the a of to as", "something else entirely") == 0.0

    def test_substring_pluralisation(self):
        # "correction" is a substring of "corrections" → match
        assert (
            _word_overlap_score(
                "quantum correction methods", "quantum corrections methods work"
            )
            == 100.0
        )

    def test_empty_title(self):
        assert _word_overlap_score("", "any text") == 0.0


class TestDOIPrefixInText:
    def test_lowercase_doi_prefix(self):
        assert (
            _doi_prefix_in_text(
                "10.1038/nature02792",
                "Received 22 April; accepted 28 June 2004; doi:10.1038/nature02792.",
            )
            is True
        )

    def test_uppercase_DOI_prefix(self):
        assert (
            _doi_prefix_in_text(
                "10.1103/PhysRevLett.89.106801",
                "We present experimental data.\nDOI: 10.1103/PhysRevLett.89.106801 PACS: 73.63.Fg",
            )
            is True
        )

    def test_doi_org_url(self):
        assert (
            _doi_prefix_in_text(
                "10.1021/nl404795z",
                "See https://doi.org/10.1021/nl404795z for the published version.",
            )
            is True
        )

    def test_dx_doi_org_url(self):
        assert (
            _doi_prefix_in_text(
                "10.1021/nl404795z",
                "dx.doi.org/10.1021/nl404795z",
            )
            is True
        )

    def test_bare_doi_does_not_count(self):
        # A bare DOI (no prefix) in a reference list should NOT trigger:
        # it's likely a cited paper's DOI, not this paper's.
        assert (
            _doi_prefix_in_text(
                "10.1038/nature02792",
                "See ref [23] Smith et al., Nature 2004, 10.1038/nature02792 for details.",
            )
            is False
        )

    def test_mismatched_doi(self):
        assert (
            _doi_prefix_in_text(
                "10.1038/nature02792",
                "doi:10.1038/SOMETHING-ELSE",
            )
            is False
        )

    def test_empty_inputs(self):
        assert _doi_prefix_in_text("", "doi:10.1038/nature02792") is False
        assert _doi_prefix_in_text("10.1038/nature02792", "") is False


class TestDOIConfirmsTitle:
    """When the publisher's typeset DOI appears in the body, we trust
    CrossRef's metadata even if the title itself isn't in the extractable
    text (e.g. partial-page reprints, weird column orderings)."""

    def test_partial_page_extract_with_matching_doi_passes(self):
        """Regression: nature02817 — PDF is only the last page of the
        Haugan 2004 paper. Title words aren't in body, but doi:... is."""
        header = {
            "title": "Addition of nanoparticle dispersions to enhance flux pinning of the YBa2Cu3O7-x superconductor",
            "authors": [{"name": "Haugan, T."}],
            "doi": "10.1038/nature02792",
        }
        # Body text without title/abstract but WITH the publisher DOI stamp
        text = (
            "composite films had a flatter dependence on applied field. "
            "The self-field Jc of the composite films were increased... "
            "Received 22 April; accepted 28 June 2004; doi:10.1038/nature02792. "
            "T. J. Haugan acknowledges AFRL support."
        )
        verified, warnings = verify_metadata(header, text)
        assert verified is True, f"expected verified, got warnings: {warnings}"

    def test_no_doi_confirmation_still_requires_title_match(self):
        """Without DOI confirmation, an unmatchable title should still fail."""
        header = {
            "title": "Something Completely Unrelated",
            "authors": [{"name": "Haugan, T."}],
            "doi": "10.1038/nature02792",
        }
        # DOI not in body → can't rely on DOI confirmation
        text = "Haugan et al. studied various flux pinning mechanisms."
        verified, warnings = verify_metadata(header, text)
        assert verified is False
        assert any("Title mismatch" in w for w in warnings)


class TestWordOverlapVerifyGate:
    def test_body_with_title_words_scattered_passes(self):
        """Regression: title words present but not contiguous → partial_ratio
        fails but word_overlap succeeds."""
        header = {
            "title": "High-resolution imaging of graphene heterostructures",
            "authors": [{"name": "Doe, Jane"}],
        }
        # Title words appear but not in order and separated by other text
        text = (
            "We present a study of graphene devices with high-resolution "
            "scanning tunnelling microscopy. Our heterostructures show "
            "novel imaging contrast. Jane Doe led the experimental work."
        )
        verified, warnings = verify_metadata(header, text)
        assert verified is True, f"expected verified, got warnings: {warnings}"
