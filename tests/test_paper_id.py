"""Phase 5 — paper-id classifier + _to_uri integration tests."""

from __future__ import annotations

from precis import server
from precis.paper_id import (
    PaperIdentifier,
    classify_paper_id,
    normalize_arxiv,
    normalize_doi,
    normalize_isbn,
    normalize_issn,
    normalize_pmcid,
)

# ---------------------------------------------------------------------------
# DOI normalisation — ported from acatome-quest-mcp, same test shape
# ---------------------------------------------------------------------------


class TestNormalizeDoi:
    def test_bare_doi(self):
        assert normalize_doi("10.1021/jacs.2c01234") == "10.1021/jacs.2c01234"

    def test_doi_with_prefix(self):
        assert normalize_doi("doi:10.1021/jacs.2c01234") == "10.1021/jacs.2c01234"

    def test_doi_with_url(self):
        assert (
            normalize_doi("https://doi.org/10.1021/jacs.2c01234")
            == "10.1021/jacs.2c01234"
        )

    def test_doi_with_dx_url(self):
        assert (
            normalize_doi("http://dx.doi.org/10.1021/jacs.2c01234")
            == "10.1021/jacs.2c01234"
        )

    def test_trailing_punctuation_stripped(self):
        assert normalize_doi("10.1021/jacs.2c01234.") == "10.1021/jacs.2c01234"
        assert normalize_doi("10.1021/jacs.2c01234,") == "10.1021/jacs.2c01234"

    def test_rejects_non_doi(self):
        assert normalize_doi("foo") is None
        assert normalize_doi("10.abc/bar") is None

    def test_rejects_empty(self):
        assert normalize_doi("") is None
        assert normalize_doi(None) is None


# ---------------------------------------------------------------------------
# arXiv normalisation
# ---------------------------------------------------------------------------


class TestNormalizeArxiv:
    def test_new_form(self):
        assert normalize_arxiv("2401.12345") == "2401.12345"

    def test_new_form_with_version(self):
        assert normalize_arxiv("2401.12345v3") == "2401.12345v3"

    def test_old_form(self):
        assert normalize_arxiv("cs.CL/0701042") == "cs.cl/0701042"

    def test_old_form_no_subclass(self):
        assert normalize_arxiv("hep-th/9901001") == "hep-th/9901001"

    def test_with_arxiv_prefix(self):
        assert normalize_arxiv("arXiv:2401.12345") == "2401.12345"

    def test_with_abs_url(self):
        assert normalize_arxiv("https://arxiv.org/abs/2401.12345") == "2401.12345"

    def test_with_pdf_url(self):
        assert normalize_arxiv("https://arxiv.org/pdf/2401.12345.pdf") == "2401.12345"

    def test_rejects_non_arxiv(self):
        assert normalize_arxiv("random-string") is None
        assert normalize_arxiv("") is None


# ---------------------------------------------------------------------------
# PMCID normalisation
# ---------------------------------------------------------------------------


class TestNormalizePmcid:
    def test_bare(self):
        assert normalize_pmcid("PMC1234567") == "PMC1234567"

    def test_lowercase_normalised_to_upper(self):
        assert normalize_pmcid("pmc1234567") == "PMC1234567"

    def test_extracted_from_url(self):
        url = "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC1234567/"
        assert normalize_pmcid(url) == "PMC1234567"

    def test_rejects_plain_digits(self):
        # Bare 1234567 is a PMID, not a PMCID.
        assert normalize_pmcid("1234567") is None

    def test_rejects_short_id(self):
        assert normalize_pmcid("PMC12") is None  # too short


# ---------------------------------------------------------------------------
# ISBN normalisation — checksum validated
# ---------------------------------------------------------------------------


class TestNormalizeIsbn:
    # Valid ISBN-13: 978-3-16-148410-0 (Wikipedia's canonical example)
    VALID_13 = "9783161484100"
    VALID_13_HYPHENATED = "978-3-16-148410-0"
    # Valid ISBN-10: 0-306-40615-2
    VALID_10 = "0306406152"
    VALID_10_HYPHENATED = "0-306-40615-2"
    # Valid ISBN-10 with X checksum: 0-8044-2957-X (The Elements of Style)
    VALID_10_X = "080442957X"

    def test_isbn13(self):
        assert normalize_isbn(self.VALID_13) == "9783161484100"

    def test_isbn13_hyphenated(self):
        assert normalize_isbn(self.VALID_13_HYPHENATED) == "9783161484100"

    def test_isbn10(self):
        assert normalize_isbn(self.VALID_10) == "0306406152"

    def test_isbn10_hyphenated(self):
        assert normalize_isbn(self.VALID_10_HYPHENATED) == "0306406152"

    def test_isbn10_with_x_checksum(self):
        assert normalize_isbn(self.VALID_10_X) == "080442957X"

    def test_isbn10_lowercase_x_normalised(self):
        assert normalize_isbn("080442957x") == "080442957X"

    def test_rejects_bad_checksum_13(self):
        assert normalize_isbn("9783161484101") is None  # last digit wrong

    def test_rejects_bad_checksum_10(self):
        assert normalize_isbn("0306406153") is None  # last digit wrong

    def test_rejects_wrong_length(self):
        assert normalize_isbn("123") is None
        assert normalize_isbn("12345678901234") is None  # 14 digits


# ---------------------------------------------------------------------------
# ISSN normalisation
# ---------------------------------------------------------------------------


class TestNormalizeIssn:
    # Valid ISSN: 2049-3630 (Wikipedia's own ISSN)
    VALID = "2049-3630"
    # ISSN with X checksum: 0378-5955 — let's use a real one: 0027-8424 (PNAS)
    VALID_PNAS = "0027-8424"

    def test_canonical_form(self):
        assert normalize_issn(self.VALID) == "2049-3630"

    def test_pnas_issn(self):
        assert normalize_issn(self.VALID_PNAS) == "0027-8424"

    def test_unhyphenated_accepted_and_hyphenated(self):
        assert normalize_issn("20493630") == "2049-3630"

    def test_rejects_bad_checksum(self):
        assert normalize_issn("2049-3631") is None

    def test_rejects_wrong_length(self):
        assert normalize_issn("12345") is None


# ---------------------------------------------------------------------------
# classify_paper_id — the main dispatcher
# ---------------------------------------------------------------------------


class TestClassify:
    # Explicit scheme prefixes

    def test_explicit_paper_prefix(self):
        got = classify_paper_id("paper:wang2020state")
        assert got == PaperIdentifier("paper", "wang2020state")

    def test_explicit_doi_prefix_normalises(self):
        got = classify_paper_id("doi:10.1021/jacs.2c01234")
        assert got == PaperIdentifier("doi", "10.1021/jacs.2c01234")

    def test_explicit_arxiv_prefix_normalises(self):
        got = classify_paper_id("arxiv:2401.12345")
        assert got == PaperIdentifier("arxiv", "2401.12345")

    def test_explicit_pmid_prefix(self):
        got = classify_paper_id("pmid:12345678")
        assert got == PaperIdentifier("pmid", "12345678")

    def test_explicit_pmcid_prefix_normalises(self):
        got = classify_paper_id("pmcid:pmc1234567")
        assert got == PaperIdentifier("pmcid", "PMC1234567")

    def test_explicit_isbn_prefix_normalises_hyphens(self):
        got = classify_paper_id("isbn:978-3-16-148410-0")
        assert got == PaperIdentifier("isbn", "9783161484100")

    def test_explicit_issn_prefix(self):
        got = classify_paper_id("issn:20493630")
        assert got == PaperIdentifier("issn", "2049-3630")

    # URL forms

    def test_doi_url(self):
        got = classify_paper_id("https://doi.org/10.1021/jacs.2c01234")
        assert got == PaperIdentifier("doi", "10.1021/jacs.2c01234")

    def test_arxiv_url(self):
        got = classify_paper_id("https://arxiv.org/abs/2401.12345")
        assert got == PaperIdentifier("arxiv", "2401.12345")

    def test_arxiv_pdf_url(self):
        got = classify_paper_id("https://arxiv.org/pdf/2401.12345v2.pdf")
        assert got == PaperIdentifier("arxiv", "2401.12345v2")

    def test_pmcid_url(self):
        got = classify_paper_id("https://www.ncbi.nlm.nih.gov/pmc/articles/PMC1234567/")
        assert got == PaperIdentifier("pmcid", "PMC1234567")

    # Bare structural patterns

    def test_bare_doi(self):
        assert classify_paper_id("10.1021/jacs.2c01234") == PaperIdentifier(
            "doi", "10.1021/jacs.2c01234"
        )

    def test_bare_arxiv_new(self):
        assert classify_paper_id("2401.12345") == PaperIdentifier("arxiv", "2401.12345")

    def test_bare_arxiv_old(self):
        assert classify_paper_id("cs.CL/0701042") == PaperIdentifier(
            "arxiv", "cs.cl/0701042"
        )

    def test_bare_pmcid(self):
        assert classify_paper_id("PMC1234567") == PaperIdentifier("pmcid", "PMC1234567")

    def test_bare_isbn13(self):
        assert classify_paper_id("9783161484100") == PaperIdentifier(
            "isbn", "9783161484100"
        )

    def test_bare_isbn13_hyphenated(self):
        assert classify_paper_id("978-3-16-148410-0") == PaperIdentifier(
            "isbn", "9783161484100"
        )

    def test_bare_isbn10(self):
        assert classify_paper_id("0306406152") == PaperIdentifier("isbn", "0306406152")

    def test_bare_issn(self):
        assert classify_paper_id("2049-3630") == PaperIdentifier("issn", "2049-3630")

    # Ambiguity — bare digits prefer slug (§13.5)

    def test_bare_pmid_digits_goes_to_slug_with_hint(self):
        # 8-digit numeric: could be PMID, ISSN-without-dashes, or ISBN-10-minus-1.
        # §13.5 rule: slug lookup first, then hint toward pmid:.
        got = classify_paper_id("12345678")
        assert got.scheme == "paper"
        assert got.value == "12345678"
        assert "pmid:12345678" in got.note

    def test_short_digits_goes_to_slug(self):
        got = classify_paper_id("123")
        assert got.scheme == "paper"
        assert got.value == "123"
        # The note mentions pmid fallback.
        assert "pmid" in got.note

    # Fallback — slug lookup

    def test_bare_slug(self):
        assert classify_paper_id("wang2020state") == PaperIdentifier(
            "paper", "wang2020state"
        )

    def test_empty_input(self):
        assert classify_paper_id("") == PaperIdentifier("paper", "")

    def test_whitespace_only(self):
        assert classify_paper_id("   ") == PaperIdentifier("paper", "")

    # URI property

    def test_uri_property_combines_scheme_value(self):
        got = classify_paper_id("10.1021/jacs.2c01234")
        assert got.uri == "doi:10.1021/jacs.2c01234"


# ---------------------------------------------------------------------------
# _to_uri integration — classifier wired into server
# ---------------------------------------------------------------------------


class TestToUriClassifierIntegration:
    def test_bare_doi_routes_to_doi_scheme(self):
        assert server._to_uri("10.1021/jacs.2c01234") == "doi:10.1021/jacs.2c01234"

    def test_bare_arxiv_routes_to_arxiv_scheme(self):
        assert server._to_uri("2401.12345") == "arxiv:2401.12345"

    def test_bare_arxiv_old_form_routes_to_arxiv(self):
        assert server._to_uri("cs.CL/0701042") == "arxiv:cs.cl/0701042"

    def test_bare_pmcid_routes_to_pmcid_scheme(self):
        assert server._to_uri("PMC1234567") == "pmcid:PMC1234567"

    def test_bare_isbn_routes_to_isbn_scheme(self):
        assert server._to_uri("9783161484100") == "isbn:9783161484100"

    def test_bare_issn_routes_to_issn_scheme(self):
        assert server._to_uri("2049-3630") == "issn:2049-3630"

    def test_bare_slug_routes_to_paper(self):
        assert server._to_uri("wang2020state") == "paper:wang2020state"

    def test_file_extension_still_wins(self):
        # docx routing must beat classifier.
        assert server._to_uri("report.docx") == "file:report.docx"

    def test_explicit_paper_prefix_preserved(self):
        assert server._to_uri("paper:wang2020state") == "paper:wang2020state"

    def test_explicit_doi_prefix_preserved(self):
        assert server._to_uri("doi:10.1021/x") == "doi:10.1021/x"

    def test_selector_suffix_preserved_through_classification(self):
        # chunk selector ›38 rides along.
        out = server._to_uri("wang2020state›38")
        assert out == "paper:wang2020state›38"

    def test_doi_with_selector_suffix(self):
        out = server._to_uri("10.1021/x›5")
        assert out == "doi:10.1021/x›5"

    def test_slug_with_view_path(self):
        # Paper is non-opaque: `/toc` is parsed downstream as the view.
        assert server._to_uri("wang2020state/toc") == "paper:wang2020state/toc"

    def test_kind_hint_still_wins(self):
        # Phase 1 precedence: explicit type= beats classifier.
        assert server._to_uri("12345678", kind="pmid") == "pmid:12345678"


# ---------------------------------------------------------------------------
# Real-world gotchas — keep regressions from creeping in
# ---------------------------------------------------------------------------


class TestRegressionCases:
    def test_url_looking_slug_not_classified_as_doi(self):
        # No '10.' prefix, no arXiv shape → slug.
        assert classify_paper_id("my-paper/section").scheme == "paper"

    def test_doi_with_parens_colons_in_suffix(self):
        # Modern Crossref DOIs can contain parens and colons in the suffix.
        got = classify_paper_id("10.1021/jacs.2c01234:sup(1)")
        assert got.scheme == "doi"

    def test_case_insensitive_explicit_prefix(self):
        # DOI: and doi: are both valid.
        assert classify_paper_id("DOI:10.1021/x").scheme == "doi"
        assert classify_paper_id("ArXiv:2401.12345").scheme == "arxiv"
