"""Tests for ``precis.ingest.pdf_metadata``.

Vendored from ``acatome-extract.tests.test_pdf_metadata`` during
B4b. The acatome-side suite covered three roles for the module:

1. **Metadata extraction** (DOI normalisation, validation, sidecar
   reading, candidate parsing) — preserved here. The functions
   are unchanged.
2. **Bundle-first lookup** (``_find_acatome_bundle``,
   ``get_valid_hashes_for_bundle``, ``_update_bundle_hash_history``)
   — dropped. Bundle reads/writes go away in v2; the equivalent
   "have we seen this paper?" probe is
   :func:`precis.ingest.db_writer.probe_existing` against the
   ``ref_identifiers`` table, exercised by
   ``tests/ingest/test_db_writer.py``.
3. **PDF enrichment workflow** (``build_exiftool_command``,
   ``should_update_file``, ``_backup_pdf``, ``write_pdf_metadata``,
   ``enrich_*``) — dropped. v2 stores extracted metadata as DB
   rows rather than patching the PDF in place, so the exiftool /
   backup helpers go with the bundle format.

What survives in this test file is the extraction subset.
"""

from __future__ import annotations

from pathlib import Path

from precis.ingest.pdf_metadata import (
    DoiCandidate,
    DoiProvenance,
    PdfMetadata,
    _compute_file_hash,
    _is_valid_doi_format,
    _metadata_matches_pdf_text,
    _normalize_doi,
    _read_sidecar_meta,
    _select_best_doi,
    _strip_nul_bytes,
)


class TestDoiNormalization:
    """Tests for DOI normalization."""

    def test_normalize_doi_strips_prefix(self):
        assert _normalize_doi("doi:10.1000/abc") == "10.1000/abc"
        assert _normalize_doi("DOI:10.1000/ABC") == "10.1000/abc"

    def test_normalize_doi_lowercases(self):
        assert _normalize_doi("10.1000/ABC") == "10.1000/abc"

    def test_normalize_doi_strips_whitespace(self):
        assert _normalize_doi("  10.1000/abc  ") == "10.1000/abc"


class TestDoiValidation:
    """Tests for DOI format validation."""

    def test_valid_doi_formats(self):
        assert _is_valid_doi_format("10.1000/abc")
        assert _is_valid_doi_format("10.1234/jacs.2023.001")
        assert _is_valid_doi_format("10.1038/nature12345")
        assert _is_valid_doi_format("10.1234/very-long-suffix-with.dots-and-dashes")

    def test_invalid_doi_formats(self):
        assert not _is_valid_doi_format("not-a-doi")
        assert not _is_valid_doi_format("10/abc")
        assert not _is_valid_doi_format("10.123/abc")  # registrant too short
        assert not _is_valid_doi_format("")
        assert not _is_valid_doi_format("10.1000/")  # missing suffix


class TestDoiCandidate:
    """Tests for DoiCandidate dataclass."""

    def test_normalization_on_creation(self):
        c = DoiCandidate(
            doi="  DOI:10.1000/ABC  ", provenance=DoiProvenance.SIDECAR_META
        )
        assert c.doi == "10.1000/abc"

    def test_validated_flag_default(self):
        c = DoiCandidate(doi="10.1000/abc", provenance=DoiProvenance.SIDECAR_META)
        assert c.validated is False
        assert c.metadata == {}


class TestSidecarReading:
    """Tests for .meta.json sidecar reading."""

    def test_read_existing_sidecar(self, tmp_path: Path):
        pdf = tmp_path / "test.pdf"
        sidecar = tmp_path / "test.meta.json"
        sidecar.write_text(
            '{"doi": "10.1000/abc", "title": "Test Paper"}', encoding="utf-8"
        )

        result = _read_sidecar_meta(pdf)
        assert result == {"doi": "10.1000/abc", "title": "Test Paper"}

    def test_read_missing_sidecar(self, tmp_path: Path):
        pdf = tmp_path / "test.pdf"
        result = _read_sidecar_meta(pdf)
        assert result == {}

    def test_read_invalid_sidecar(self, tmp_path: Path):
        pdf = tmp_path / "test.pdf"
        sidecar = tmp_path / "test.meta.json"
        sidecar.write_text("not valid json {", encoding="utf-8")

        result = _read_sidecar_meta(pdf)
        assert result == {}


class TestComputeFileHash:
    """Tests for the file SHA-256 helper."""

    def test_compute_file_hash(self, tmp_path: Path):
        pdf = tmp_path / "test.pdf"
        pdf.write_bytes(b"hello world")
        # SHA-256 of "hello world"
        expected = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
        assert _compute_file_hash(pdf) == expected

    def test_compute_file_hash_large_file(self, tmp_path: Path):
        # Exercise the chunked-read branch (>65536 bytes).
        pdf = tmp_path / "large.pdf"
        pdf.write_bytes(b"a" * 200_000)
        h = _compute_file_hash(pdf)
        assert len(h) == 64
        # Determinism — same bytes, same hash.
        assert _compute_file_hash(pdf) == h


class TestStripNulBytes:
    """Pin the cleanup that prevents psycopg.DataError on INSERT.

    Postgres TEXT rejects NUL; the metadata cascade has been seen to
    pull NULs out of corrupt embedded info / XMP streams in the wild.
    """

    def test_strips_nul_from_all_text_fields(self, tmp_path: Path) -> None:
        meta = PdfMetadata(
            pdf_path=tmp_path / "x.pdf",
            title="A\x00 paper",
            authors=["Smith\x00, J.", "clean"],
            doi="10.1000/\x00abc",
            journal="Journal\x00",
            publisher="\x00Pub",
            abstract="abs\x00tract",
            keywords=["k\x001", "k2"],
        )
        _strip_nul_bytes(meta)
        assert meta.title == "A paper"
        assert meta.authors == ["Smith, J.", "clean"]
        assert meta.doi == "10.1000/abc"
        assert meta.journal == "Journal"
        assert meta.publisher == "Pub"
        assert meta.abstract == "abstract"
        assert meta.keywords == ["k1", "k2"]

    def test_is_idempotent_when_no_nul_present(self, tmp_path: Path) -> None:
        meta = PdfMetadata(
            pdf_path=tmp_path / "x.pdf",
            title="A paper",
            authors=["Smith, J."],
            doi="10.1000/abc",
        )
        _strip_nul_bytes(meta)
        assert meta.title == "A paper"
        assert meta.authors == ["Smith, J."]
        assert meta.doi == "10.1000/abc"


class TestSelectBestDoiPdfMatch:
    """gr513878: a DOI that resolves is not proof of identity — the
    resolved record must also match the PDF's own text.

    The incident shape: a MethodsX PDF's "Related research article" box
    typesets the 2019 J. Microbiol. Methods DOI; that DOI resolves in
    Crossref fine, but its record describes a different paper.
    """

    MEX_TEXT = (
        "Methods X 13 (2026) 104138\n"
        "journal homepage: www.elsevier.com/locate/mex\n"
        "A lyophilization-compatible colorimetric LAMP assay for cold "
        "chain-free, extraction-free and rapid post-enrichment detection "
        "of E. coli\n"
        "Wansadaj Jaroenram et al.\n"
        "2215-0161/© 2026 The Authors. Published by Elsevier B.V.\n"
    )

    @staticmethod
    def _lookup(responses: dict[str, dict]):
        def fake_lookup(doi: str, mailto: str = ""):
            return responses.get(doi)

        return fake_lookup

    def test_related_article_doi_rejected_own_doi_wins(self):
        from unittest.mock import patch

        related = DoiCandidate(
            doi="10.1016/j.mimet.2018.11.020",
            provenance=DoiProvenance.INTERNAL_EXTRACTOR,
        )
        own = DoiCandidate(
            doi="10.1016/j.mex.2026.104138",
            provenance=DoiProvenance.EXISTING_PDF_METADATA,
        )
        responses = {
            "10.1016/j.mimet.2018.11.020": {
                "title": "Xylenol orange-based loop-mediated DNA isothermal "
                "amplification for sensitive naked-eye detection of "
                "Escherichia coli",
                "year": 2019,
            },
            "10.1016/j.mex.2026.104138": {
                "title": "A lyophilization-compatible colorimetric LAMP "
                "assay for cold chain-free, extraction-free and rapid "
                "post-enrichment detection of E. coli",
                "year": 2026,
            },
        }
        with patch(
            "precis.ingest.pdf_metadata.lookup_doi",
            side_effect=self._lookup(responses),
        ):
            best = _select_best_doi([related, own], first_pages_text=self.MEX_TEXT)
        assert best is not None and best.doi == "10.1016/j.mex.2026.104138"
        assert best.validated is True

    def test_mismatched_candidate_never_wins_fallback(self):
        """A validated-but-foreign DOI must not come back as best effort."""
        from unittest.mock import patch

        foreign = DoiCandidate(
            doi="10.1016/j.mimet.2018.11.020",
            provenance=DoiProvenance.INTERNAL_EXTRACTOR,
        )
        responses = {
            "10.1016/j.mimet.2018.11.020": {
                "title": "Xylenol orange-based loop-mediated DNA isothermal "
                "amplification for sensitive naked-eye detection of "
                "Escherichia coli",
                "year": 2019,
            },
        }
        with patch(
            "precis.ingest.pdf_metadata.lookup_doi",
            side_effect=self._lookup(responses),
        ):
            best = _select_best_doi([foreign], first_pages_text=self.MEX_TEXT)
        assert best is None

    def test_year_off_by_more_than_one_rejected(self):
        from unittest.mock import patch

        cand = DoiCandidate(
            doi="10.1016/j.mex.2020.999999",
            provenance=DoiProvenance.INTERNAL_EXTRACTOR,
        )
        responses = {
            "10.1016/j.mex.2020.999999": {
                # Title matches, but the registry year is 6 off the © 2026.
                "title": "A lyophilization-compatible colorimetric LAMP "
                "assay for cold chain-free detection",
                "year": 2020,
            },
        }
        with patch(
            "precis.ingest.pdf_metadata.lookup_doi",
            side_effect=self._lookup(responses),
        ):
            best = _select_best_doi([cand], first_pages_text=self.MEX_TEXT)
        assert best is None

    def test_year_within_one_accepted(self):
        from unittest.mock import patch

        cand = DoiCandidate(
            doi="10.1016/j.mex.2025.104138",
            provenance=DoiProvenance.INTERNAL_EXTRACTOR,
        )
        responses = {
            "10.1016/j.mex.2025.104138": {
                "title": "A lyophilization-compatible colorimetric LAMP "
                "assay for cold chain-free detection",
                "year": 2025,  # online-first year vs © 2026 issue year
            },
        }
        with patch(
            "precis.ingest.pdf_metadata.lookup_doi",
            side_effect=self._lookup(responses),
        ):
            best = _select_best_doi([cand], first_pages_text=self.MEX_TEXT)
        assert best is cand and best.validated is True

    def test_no_text_means_no_gate(self):
        # Extraction produced no text (scanned reprint): the check abstains.
        assert (
            _metadata_matches_pdf_text({"title": "Any title", "year": 2001}, "") is True
        )

    def test_missing_year_evidence_not_a_mismatch(self):
        # No year back from the registry → title evidence alone decides.
        assert (
            _metadata_matches_pdf_text(
                {
                    "title": "A lyophilization-compatible colorimetric LAMP "
                    "assay for cold chain-free detection"
                },
                self.MEX_TEXT,
            )
            is True
        )


class TestOfflineExtraction:
    """``online=False`` (cfp): no Crossref / Semantic Scholar traffic."""

    def test_no_network_and_pdf_info_title_used(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from precis.ingest.pdf_metadata import extract_metadata_from_sources

        pdf = tmp_path / "call.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        fake = {
            "pdf_hash": "h",
            "info": {"title": "Horizon Call 7 Quantum Sensing"},
            "first_pages_text": "",
        }
        cand = DoiCandidate(
            doi="10.1000/x", provenance=DoiProvenance.INTERNAL_EXTRACTOR
        )

        def boom(*_a, **_k):
            raise AssertionError("network lookup attempted")

        with (
            patch("precis.ingest.pdf_metadata.extract_pdf_meta", return_value=fake),
            patch(
                "precis.ingest.pdf_metadata._extract_doi_candidates",
                return_value=[cand],
            ),
            patch("precis.ingest.pdf_metadata.lookup_doi", side_effect=boom),
            patch("precis.ingest.pdf_metadata.lookup", side_effect=boom),
        ):
            md = extract_metadata_from_sources(pdf, online=False)

        assert md.title == "Horizon Call 7 Quantum Sensing"
        assert md.doi == "10.1000/x"
