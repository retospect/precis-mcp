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
    _compute_file_hash,
    _is_valid_doi_format,
    _normalize_doi,
    _read_sidecar_meta,
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
        sidecar.write_text('{"doi": "10.1000/abc", "title": "Test Paper"}')

        result = _read_sidecar_meta(pdf)
        assert result == {"doi": "10.1000/abc", "title": "Test Paper"}

    def test_read_missing_sidecar(self, tmp_path: Path):
        pdf = tmp_path / "test.pdf"
        result = _read_sidecar_meta(pdf)
        assert result == {}

    def test_read_invalid_sidecar(self, tmp_path: Path):
        pdf = tmp_path / "test.pdf"
        sidecar = tmp_path / "test.meta.json"
        sidecar.write_text("not valid json {")

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
