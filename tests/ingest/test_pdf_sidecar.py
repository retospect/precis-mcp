"""Tests for PDF metadata extraction."""

from __future__ import annotations

from precis.ingest.pdf_sidecar import (
    _clean_doi,
    _extract_doi,
    _pii_to_doi,
    _trim_at_references,
    candidate_title_from_text,
    extract_doi_from_filename,
    is_garbage_title,
    is_pii,
)


class TestDOIExtraction:
    def test_doi_from_xmp(self):
        xmp = "<dc:identifier>doi:10.1038/s41567-024-1234-5</dc:identifier>"
        assert _extract_doi(xmp, "", {}) == "10.1038/s41567-024-1234-5"

    def test_doi_from_first_page(self):
        text = "Published: https://doi.org/10.1145/1234567.1234568\nAbstract..."
        assert _extract_doi("", text, {}) == "10.1145/1234567.1234568"

    def test_doi_from_info_dict(self):
        info = {"doi": "10.1103/PhysRevLett.123.456"}
        assert _extract_doi("", "", info) == "10.1103/PhysRevLett.123.456"

    def test_doi_cascade_priority(self):
        """A prefixed DOI in body text (publisher-typeset marker) outranks
        XMP (which can be stale) and info-dict (which can be missing)."""
        xmp = "<dc:identifier>doi:10.1038/xmp-doi</dc:identifier>"
        text = "doi:10.1038/text-doi"
        info = {"doi": "10.1038/info-doi"}
        assert _extract_doi(xmp, text, info) == "10.1038/text-doi"

    def test_xmp_wins_over_bare_body_doi(self):
        """When body DOI is bare (no prefix) but XMP has one, XMP wins —
        bare body DOIs are often reference-list citations."""
        xmp = "<dc:identifier>doi:10.1038/xmp-doi</dc:identifier>"
        text = "See 10.1038/text-doi for a related paper."
        assert _extract_doi(xmp, text, {}) == "10.1038/xmp-doi"

    def test_bare_body_doi_before_references_used(self):
        """Bare DOI in body before any References heading is this paper's."""
        text = "Acme et al. 2024. 10.1038/paper-doi Received today."
        assert _extract_doi("", text, {}) == "10.1038/paper-doi"

    def test_bare_body_doi_after_references_skipped(self):
        """Bare DOI after References is a citation to another paper; skip it."""
        text = (
            "Abstract content here with no prefixed DOI.\n"
            "References\n"
            "1. Smith, J. Nature 500, 10.1038/cited-other-paper (2020)."
        )
        # No prefixed DOI, no XMP, bare DOI is in references section → None
        assert _extract_doi("", text, {}) is None

    def test_no_doi_found(self):
        assert _extract_doi("", "no doi here", {}) is None

    def test_doi_from_pii_in_title(self):
        info = {"title": "PII: S0009-2614(95)00905-J"}
        assert _extract_doi("", "", info) == "10.1016/S0009-2614(95)00905-J"

    def test_doi_from_pii_without_prefix(self):
        info = {"title": "0009-2614(80)80221-1"}
        assert _extract_doi("", "", info) == "10.1016/0009-2614(80)80221-1"

    def test_doi_from_pii_in_subject(self):
        info = {"subject": "S0926-3373(98)00040-X"}
        assert _extract_doi("", "", info) == "10.1016/S0926-3373(98)00040-X"

    def test_real_doi_beats_pii(self):
        """DOI in XMP should win over PII in title."""
        xmp = "<dc:identifier>doi:10.1038/real-doi</dc:identifier>"
        info = {"title": "PII: S0009-2614(95)00905-J"}
        assert _extract_doi(xmp, "", info) == "10.1038/real-doi"

    def test_clean_doi_trailing_punct(self):
        assert _clean_doi("10.1038/s41567-024-1234-5.") == "10.1038/s41567-024-1234-5"
        assert _clean_doi("10.1038/s41567-024-1234-5,") == "10.1038/s41567-024-1234-5"
        assert _clean_doi("10.1038/s41567-024-1234-5") == "10.1038/s41567-024-1234-5"


class TestPII:
    def test_pii_to_doi_with_prefix(self):
        assert (
            _pii_to_doi("PII: S0009-2614(95)00905-J") == "10.1016/S0009-2614(95)00905-J"
        )

    def test_pii_to_doi_without_s(self):
        assert _pii_to_doi("0009-2614(80)80221-1") == "10.1016/0009-2614(80)80221-1"

    def test_pii_to_doi_busca(self):
        assert (
            _pii_to_doi("PII: S0926-3373(98)00040-X") == "10.1016/S0926-3373(98)00040-X"
        )

    def test_pii_to_doi_no_match(self):
        assert _pii_to_doi("The Grotthuss mechanism") is None

    def test_pii_to_doi_empty(self):
        assert _pii_to_doi("") is None

    def test_is_pii_true(self):
        assert is_pii("PII: S0009-2614(95)00905-J") is True
        assert is_pii("0009-2614(80)80221-1") is True

    def test_is_pii_false(self):
        assert is_pii("The Grotthuss mechanism") is False
        assert is_pii("") is False
        assert is_pii("Direct Electrochemical Ammonia Synthesis") is False


class TestGarbageTitle:
    """Detection of embedded-metadata titles that should not be trusted."""

    def test_page_range_suffix(self):
        # ACS / InDesign page-range notation leaked into dc:title
        assert is_garbage_title("nl404795z 1..9") is True
        assert is_garbage_title("LQ8388 2..5") is True
        assert is_garbage_title("acs_nn_nn-2013-02954e 1..6") is True
        assert is_garbage_title("78868 651..703") is True

    def test_indesign_source_filename(self):
        assert is_garbage_title("nmat1849 Geim Progress Article.indd") is True
        assert is_garbage_title("paper_draft.doc") is True
        assert is_garbage_title("manuscript.docx") is True
        assert is_garbage_title("source.tex") is True

    def test_revtex_boilerplate(self):
        # APS revtex \title{USING STANDARD PRB STYLE...} template leakage
        assert is_garbage_title("USING STANDARD PRB S") is True
        assert is_garbage_title("Using Standard PRB Style") is True

    def test_latex_source_leakage(self):
        assert is_garbage_title(r"\documentclass{revtex4-2}") is True
        assert is_garbage_title(r"\begin{document} Some text") is True

    def test_authoring_tool_default_titles(self):
        # Generator-baked default /Title values — describe the toolchain,
        # never the paper. "No Job Name" is dvips/TeX's default (ref 36186).
        assert is_garbage_title("No Job Name") is True
        assert is_garbage_title("no job name") is True
        assert is_garbage_title("Microsoft Word - paper_final.doc") is True
        assert is_garbage_title("untitled") is True
        assert is_garbage_title("Untitled document") is True
        assert is_garbage_title("PowerPoint Presentation") is True
        assert is_garbage_title("Presentation1") is True
        assert is_garbage_title("Slide 1") is True

    def test_empty(self):
        assert is_garbage_title("") is True
        assert is_garbage_title("   ") is True

    def test_real_titles_pass(self):
        # Genuine paper titles — must NOT be flagged as garbage
        real = [
            "High-κ dielectrics for advanced carbon-nanotube transistors and logic gates",
            "The rise of graphene",
            "Addition of nanoparticle dispersions to enhance flux pinning of the YBa2Cu3O7-x superconductor",
            "Graphene/MoS2 Hybrid Technology for Large-Scale Two-Dimensional Electronics",
            "Carbon Nanotubes as Schottky Barrier Transistors",
            "Flexible and Transparent MoS2 Field-Effect Transistors on Hexagonal Boron Nitride–Graphene Heterostructures",
            "Direct Electrochemical Ammonia Synthesis",
        ]
        for title in real:
            assert is_garbage_title(title) is False, f"false positive: {title!r}"


class TestCandidateTitleFromText:
    """Mining a candidate title from first-page body text."""

    def test_simple_title_first_line(self):
        text = (
            "Ballistic carbon nanotube field-effect transistors\n"
            "Ali Javey, Jing Guo\n\nAbstract: ...\n"
        )
        assert (
            candidate_title_from_text(text)
            == "Ballistic carbon nanotube field-effect transistors"
        )

    def test_skips_furniture_lines(self):
        text = (
            "Downloaded from https://example.org\n"
            "doi:10.1234/x\n"
            "12\n"
            "The rise of two-dimensional materials\n"
            "J. Smith\n"
        )
        assert (
            candidate_title_from_text(text) == "The rise of two-dimensional materials"
        )

    def test_wrapped_title_returns_leading_line(self):
        # A 2-line title is left truncated on purpose — the leading
        # fragment is enough for S2's fuzzy search + verify gate.
        text = (
            "Flexible and transparent field-effect transistors\n"
            "on hexagonal boron nitride heterostructures\n\n"
            "Authors here\n"
        )
        assert (
            candidate_title_from_text(text)
            == "Flexible and transparent field-effect transistors"
        )

    def test_empty_or_unusable(self):
        assert candidate_title_from_text("") == ""
        assert candidate_title_from_text("   \n\n  ") == ""
        assert candidate_title_from_text("12\nNo Job Name\n") == ""

    def test_garbage_candidate_rejected(self):
        # First usable line is itself a known-garbage default title.
        assert candidate_title_from_text("No Job Name\nsome author\n") == ""


class TestTrimAtReferences:
    def test_no_references_heading(self):
        text = "Abstract and content.\nSome body text."
        assert _trim_at_references(text) == text

    def test_references_uppercase(self):
        text = "Body text here.\nREFERENCES\n1. Smith 2020\n2. Doe 2021"
        trimmed = _trim_at_references(text)
        assert "1. Smith" not in trimmed
        assert "Body text here." in trimmed

    def test_references_mixed_case(self):
        text = "Abstract.\nReferences\n1. Smith."
        assert "1. Smith" not in _trim_at_references(text)

    def test_bibliography_heading(self):
        text = "Content.\nBibliography\n1. Ref."
        assert "1. Ref" not in _trim_at_references(text)

    def test_acknowledgements_not_confused_with_references(self):
        # Acknowledgements does NOT terminate the scan zone — paper's own
        # DOI is sometimes typeset in the Acknowledgements/footer block.
        text = "Body.\nAcknowledgements\nWe thank X."
        assert _trim_at_references(text) == text


class TestExtractDOIFromFilename:
    """Generalised filename → DOI heuristic for archival reprints."""

    def test_nature_old_style(self):
        assert (
            extract_doi_from_filename("/papers/nature01797.pdf")
            == "10.1038/nature01797"
        )
        assert extract_doi_from_filename("nature02792.pdf") == "10.1038/nature02792"

    def test_nmat(self):
        assert extract_doi_from_filename("nmat1849.pdf") == "10.1038/nmat1849"
        # trailing "-2" version suffix
        assert extract_doi_from_filename("nmat769-2.pdf") == "10.1038/nmat769"

    def test_nano_dotted_new_style(self):
        # nnano.2013.167 → 10.1038/nnano.2013.167
        assert (
            extract_doi_from_filename("nnano.2013.167.pdf") == "10.1038/nnano.2013.167"
        )

    def test_s_prefix_new_doi_style(self):
        # s41586-020-2649-2 → 10.1038/s41586-020-2649-2
        assert (
            extract_doi_from_filename("/in/s41586-020-2649-2.pdf")
            == "10.1038/s41586-020-2649-2"
        )

    def test_physrevlett(self):
        assert (
            extract_doi_from_filename("PhysRevLett.89.106801.pdf")
            == "10.1103/PhysRevLett.89.106801"
        )

    def test_physrevlett_with_page_suffix(self):
        assert (
            extract_doi_from_filename("PhysRevLett.89.106801-6.pdf")
            == "10.1103/PhysRevLett.89.106801"
        )

    def test_physrevb(self):
        assert (
            extract_doi_from_filename("/archive/PhysRevB.63.193409.pdf")
            == "10.1103/PhysRevB.63.193409"
        )

    def test_physrevx(self):
        assert (
            extract_doi_from_filename("PhysRevX.12.012345.pdf")
            == "10.1103/PhysRevX.12.012345"
        )

    def test_physrev_materials(self):
        assert (
            extract_doi_from_filename("PhysRevMaterials.5.033405.pdf")
            == "10.1103/PhysRevMaterials.5.033405"
        )

    def test_rsc_article_id(self):
        # Royal Society of Chemistry article IDs are the deterministic
        # "decade-letter + year + journal-code + sequence + check" form.
        assert extract_doi_from_filename("c8me00086g.pdf") == "10.1039/c8me00086g"
        assert extract_doi_from_filename("c8ee00122g.pdf") == "10.1039/c8ee00122g"
        assert extract_doi_from_filename("d1ee01170g.pdf") == "10.1039/d1ee01170g"
        # trailing -N version suffix
        assert extract_doi_from_filename("c0lc00403k-3.pdf") == "10.1039/c0lc00403k"

    def test_doi_as_filename_underscore(self):
        # Common when a user saves a PDF with the DOI in the name,
        # replacing the ``/`` with ``_``.
        assert (
            extract_doi_from_filename("10.30501_jree.2015.70071-4.pdf")
            == "10.30501/jree.2015.70071-4"
        )
        # Nature DOI legitimately ends in ``-N`` — must not be stripped.
        assert (
            extract_doi_from_filename("10.1038_s41560-021-00973-9.pdf")
            == "10.1038/s41560-021-00973-9"
        )

    def test_doi_as_filename_no_underscore_no_match(self):
        # Filename starts with 10. but has no underscore separator → not a
        # DOI-as-filename (could be e.g. "10.x.y.report.pdf").
        assert extract_doi_from_filename("10.30501.jree.report.pdf") is None

    def test_rsc_uppercase(self):
        # The article-id check letter and journal code are always lowercase
        # in the canonical DOI, even if the filename was uppercase.
        assert extract_doi_from_filename("C8ME00086G.pdf") == "10.1039/c8me00086g"

    def test_slug_filename_no_match(self):
        # Human-readable slugs have no DOI pattern
        assert extract_doi_from_filename("graphene-mos2-hybrid-technology.pdf") is None
        assert extract_doi_from_filename("some-random-paper.pdf") is None

    def test_arxiv_style_no_match(self):
        # arXiv filenames are handled by the arxiv extractor, not this one
        assert extract_doi_from_filename("2508.20254v1.pdf") is None

    def test_unknown_publisher_no_false_match(self):
        # Filename looks DOI-like but pattern isn't known → None, not a guess
        assert extract_doi_from_filename("unknown-prefix-12345.pdf") is None

    def test_case_insensitive_nature(self):
        assert extract_doi_from_filename("NATURE01797.pdf") == "10.1038/nature01797"

    def test_empty_filename(self):
        assert extract_doi_from_filename(".pdf") is None
