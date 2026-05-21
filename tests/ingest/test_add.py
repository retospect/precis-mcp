"""Tests for ``precis.ingest.add.precis_add``.

End-to-end ingest with a real Postgres (the ``store`` fixture).
The pipeline producers (``extract_paper`` /
``fetch_paper_by_doi`` / ``fetch_paper_by_arxiv``) are stubbed
because Marker, CrossRef, and S2 are heavy / network-bound. The
focus here is the orchestration: pipeline → probe → write_paper
or short-circuit, and the IngestResult shape.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from precis.ingest.add import (
    ArxivInput,
    DoiInput,
    IngestResult,
    PdfInput,
    precis_add,
)
from precis.ingest.db_writer import ChunkToWrite, PaperToWrite

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _fixture_paper(
    *,
    paper_id: str = "z7q2k4m5",
    cite_key_prefix: str = "smith24",
    doi: str | None = "10.1038/test",
    pdf_sha256: str | None = None,
) -> PaperToWrite:
    return PaperToWrite(
        title="Quantum Error Correction in Practice",
        authors=[{"name": "Smith, John"}],
        year=2024,
        kind="paper",
        provider="crossref",
        set_by="system",
        paper_id=paper_id,
        pub_id=f"doi:{doi}" if doi else None,
        cite_key_prefix=cite_key_prefix,
        doi=doi,
        pdf_sha256=pdf_sha256,
        content_hash=pdf_sha256,
        pdf_storage_path="/tmp/fake.pdf" if pdf_sha256 else None,
        pdf_page_count=1 if pdf_sha256 else None,
        pdf_size_bytes=100 if pdf_sha256 else None,
        chunks=[
            ChunkToWrite(
                ord=-1,
                chunk_kind="card_combined",
                text="Quantum Error Correction in Practice\nSmith, John",
            ),
            ChunkToWrite(
                ord=0,
                chunk_kind="paragraph",
                text="Surface codes are…",
                page_first=1,
                page_last=1,
            ),
        ],
    )


# ---------------------------------------------------------------------------
# precis_add — DOI input, fresh DB
# ---------------------------------------------------------------------------


class TestPrecisAddFresh:
    def test_doi_input_writes_full_set(self, store):
        paper = _fixture_paper()
        with patch(
            "precis.ingest.add.fetch_paper_by_doi",
            return_value=paper,
        ):
            result = precis_add(DoiInput(doi="10.1038/test"), store=store)

        assert isinstance(result, IngestResult)
        assert result.inserted is True
        assert result.cite_key == "smith24"
        assert result.identifiers["doi"] == "10.1038/test"
        assert result.chunks_written == 2

        # Sanity: row count via direct SQL.
        with store.pool.connection() as conn:
            ref_count = conn.execute(
                "SELECT count(*) FROM refs WHERE ref_id = %s",
                (result.ref_id,),
            ).fetchone()
            chunk_count = conn.execute(
                "SELECT count(*) FROM chunks WHERE ref_id = %s",
                (result.ref_id,),
            ).fetchone()
        assert ref_count is not None and ref_count[0] == 1
        assert chunk_count is not None and chunk_count[0] == 2

    def test_arxiv_input_dispatches_to_s2(self, store):
        paper = _fixture_paper(
            paper_id="aabbccdd",
            cite_key_prefix="wei24",
            doi=None,
        )
        # We mutate to give it an arxiv_id so probe_existing has
        # something distinguishing if a future ingest comes through DOI.
        paper_with_arxiv = PaperToWrite(
            **{**paper.__dict__, "arxiv_id": "2401.99999", "provider": "s2"},
        )
        with patch(
            "precis.ingest.add.fetch_paper_by_arxiv",
            return_value=paper_with_arxiv,
        ) as m:
            result = precis_add(ArxivInput(arxiv_id="2401.99999"), store=store)

        assert m.call_count == 1
        assert result.inserted is True
        assert result.identifiers["arxiv"] == "2401.99999"

    def test_pdf_input_dispatches_to_extract_paper(self, store, tmp_path: Path):
        pdf = tmp_path / "fake.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        paper = _fixture_paper(pdf_sha256="a" * 64)
        with patch("precis.ingest.add.extract_paper", return_value=paper) as m:
            result = precis_add(PdfInput(pdf_path=pdf), store=store)

        assert m.call_count == 1
        # extract_paper got the resolved path.
        called_pdf = m.call_args[0][0]
        assert called_pdf == pdf
        assert result.inserted is True
        assert result.identifiers["pdf_sha256"] == "a" * 64

    def test_unsupported_input_type_raises(self, store):
        # Pass a raw string — not one of the tagged-union variants.
        with pytest.raises(TypeError):
            precis_add("not-a-tagged-union", store=store)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# precis_add — idempotency
# ---------------------------------------------------------------------------


class TestPrecisAddIdempotent:
    def test_second_call_short_circuits(self, store):
        paper = _fixture_paper()
        with patch(
            "precis.ingest.add.fetch_paper_by_doi",
            return_value=paper,
        ):
            r1 = precis_add(DoiInput(doi="10.1038/test"), store=store)
            r2 = precis_add(DoiInput(doi="10.1038/test"), store=store)

        assert r1.inserted is True
        assert r1.chunks_written == 2

        assert r2.inserted is False
        assert r2.ref_id == r1.ref_id  # same ref
        assert r2.chunks_written == 0  # no rewrite
        assert r2.cite_key == r1.cite_key

    def test_dedup_via_pdf_sha256(self, store, tmp_path: Path):
        """Re-ingesting with a different DOI but the same PDF hash
        must still hit the existing ref via pdf_sha256."""
        sha = "f" * 64
        pdf = tmp_path / "x.pdf"
        pdf.write_bytes(b"%PDF-1.4")

        first = _fixture_paper(
            paper_id="firstpid",
            cite_key_prefix="kim24",
            doi="10.1/first",
            pdf_sha256=sha,
        )
        second_paper_id = "second11"
        second = PaperToWrite(
            **{
                **first.__dict__,
                "paper_id": second_paper_id,
                "doi": None,
                "pub_id": None,
                "cite_key_prefix": "lee24",
            },
        )

        with patch("precis.ingest.add.extract_paper", side_effect=[first, second]):
            r1 = precis_add(PdfInput(pdf_path=pdf), store=store)
            r2 = precis_add(PdfInput(pdf_path=pdf), store=store)

        assert r1.inserted is True
        assert r2.inserted is False
        assert r2.ref_id == r1.ref_id  # pdf_sha256 hit


# ---------------------------------------------------------------------------
# Pipeline failure surfaces cleanly
# ---------------------------------------------------------------------------


class TestPrecisAddErrors:
    def test_missing_pdf_raises(self, store, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            precis_add(PdfInput(pdf_path=tmp_path / "missing.pdf"), store=store)

    def test_doi_lookup_miss_raises(self, store):
        with patch(
            "precis.ingest.add.fetch_paper_by_doi",
            side_effect=ValueError("CrossRef miss"),
        ):
            with pytest.raises(ValueError, match="CrossRef miss"):
                precis_add(DoiInput(doi="10.1/none"), store=store)

        # Failed pipeline must not leave any ref behind.
        with store.pool.connection() as conn:
            count = conn.execute("SELECT count(*) FROM refs").fetchone()
        assert count is not None and count[0] == 0
