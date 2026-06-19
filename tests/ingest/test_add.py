"""Tests for ``precis.ingest.add.precis_add``.

End-to-end ingest with a real Postgres (the ``store`` fixture).
The pipeline producers (``extract_paper`` /
``fetch_paper_by_doi`` / ``fetch_paper_by_arxiv``) are stubbed
because Marker, CrossRef, and S2 are heavy / network-bound. The
focus here is the orchestration: pipeline → probe → write_paper
or short-circuit, and the IngestResult shape.

The fast path (``pdf_sha256`` probe before Marker) is exercised
by two tests in :class:`TestPrecisAddIdempotent`:
``test_dedup_via_pdf_sha256`` (re-ingest of the same file) and
``test_fast_path_skips_marker_when_pdf_sha256_known`` (pre-seeded
row, no prior precis_add call). Both assert
``extract_paper.call_count`` so a regression that moves Marker
back before the probe fails loudly.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from precis.ingest.add import (
    ArxivInput,
    DoiInput,
    IngestResult,
    PdfInput,
    _reconcile_orphan_stub,
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
            "precis.ingest.pipeline.fetch_paper_by_doi",
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
            "precis.ingest.pipeline.fetch_paper_by_arxiv",
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
        with patch("precis.ingest.pipeline.extract_paper", return_value=paper) as m:
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
            "precis.ingest.pipeline.fetch_paper_by_doi",
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
        """Re-ingesting the same PDF must hit the existing ref via
        ``pdf_sha256`` *before* Marker runs.

        The fixture's ``pdf_sha256`` is the actual hash of the bytes
        on disk so the fast-path probe in ``precis_add`` finds the
        row written by the first call; the second call therefore
        short-circuits without invoking ``extract_paper``.
        """
        pdf = tmp_path / "x.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        sha = hashlib.sha256(b"%PDF-1.4").hexdigest()

        first = _fixture_paper(
            paper_id="firstpid",
            cite_key_prefix="kim24",
            doi="10.1/first",
            pdf_sha256=sha,
        )

        with patch("precis.ingest.pipeline.extract_paper", return_value=first) as m:
            r1 = precis_add(PdfInput(pdf_path=pdf), store=store)
            r2 = precis_add(PdfInput(pdf_path=pdf), store=store)

        assert r1.inserted is True
        assert r2.inserted is False
        assert r2.ref_id == r1.ref_id  # pdf_sha256 hit
        # Marker ran exactly once — the second call short-circuits at
        # the pre-Marker probe. Guards against accidentally moving
        # extraction back before the dedup check.
        assert m.call_count == 1

    def test_fast_path_skips_marker_when_pdf_sha256_known(self, store, tmp_path: Path):
        """If the PDF's ``pdf_sha256`` is already in ``ref_identifiers``,
        ``precis_add(PdfInput)`` must return ``inserted=False`` without
        invoking ``extract_paper`` at all.

        Stronger than ``test_dedup_via_pdf_sha256``: no prior
        ``precis_add`` call — the row is seeded directly via SQL so a
        regression that moves the probe behind Marker still produces
        ``inserted=False`` (via the slow path) but fails ``call_count
        == 0``.
        """
        pdf = tmp_path / "seeded.pdf"
        pdf.write_bytes(b"%PDF-1.4 seeded")
        sha = hashlib.sha256(b"%PDF-1.4 seeded").hexdigest()

        # Seed a minimal ref with just the pdf_sha256 identifier.
        with store.pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO refs (kind, set_by, title) "
                "VALUES ('paper', 'system', 'seeded') "
                "RETURNING ref_id"
            ).fetchone()
            assert row is not None
            seeded_ref_id = row[0]
            conn.execute(
                "INSERT INTO ref_identifiers (id_kind, id_value, ref_id) "
                "VALUES (%s, %s, %s)",
                ("pdf_sha256", sha, seeded_ref_id),
            )
            conn.commit()

        with patch("precis.ingest.pipeline.extract_paper") as m:
            result = precis_add(PdfInput(pdf_path=pdf), store=store)

        assert m.call_count == 0  # Marker never invoked
        assert result.inserted is False
        assert result.ref_id == seeded_ref_id
        assert result.pdf_sha256 == sha
        assert result.chunks_written == 0


# ---------------------------------------------------------------------------
# Pipeline failure surfaces cleanly
# ---------------------------------------------------------------------------


class TestPrecisAddErrors:
    def test_missing_pdf_is_silent_noop(self, store, tmp_path: Path):
        """A missing PDF returns ``None`` rather than raising — the
        watcher path enqueues files and the file may disappear before
        the worker picks them up; treating it as a no-op avoids
        loud failures in that race. CLI / API callers that want a
        loud miss should ``Path.exists()``-gate before calling.
        """
        result = precis_add(PdfInput(pdf_path=tmp_path / "missing.pdf"), store=store)
        assert result is None

    def test_doi_lookup_miss_raises(self, store):
        with patch(
            "precis.ingest.pipeline.fetch_paper_by_doi",
            side_effect=ValueError("CrossRef miss"),
        ):
            with pytest.raises(ValueError, match="CrossRef miss"):
                precis_add(DoiInput(doi="10.1/none"), store=store)

        # Failed pipeline must not leave any ref behind.
        with store.pool.connection() as conn:
            count = conn.execute("SELECT count(*) FROM refs").fetchone()
        assert count is not None and count[0] == 0


class TestReconcileOrphanStub:
    """`_reconcile_orphan_stub` folds a content-duplicate fetch stub
    (named after its cite_key by the OA fetcher) into the survivor ref
    a dedup hit landed on, so the stub stops re-qualifying for fetch."""

    def test_folds_orphan_stub_into_survivor(self, store):
        # Survivor: already-ingested paper (its own cite_key, no DOI/arXiv).
        survivor = store.insert_ref(
            kind="paper", slug="lee24b", title="Atomic evolution of hydrogen"
        )
        # Orphan stub: same paper, minted by chase with an arXiv id under a
        # DIFFERENT cite_key — the name the fetcher gives the downloaded PDF.
        stub = store.insert_ref(
            kind="paper", slug="atomic24", title="Atomic evolution (stub)"
        )
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
                "VALUES (%s, 'arxiv', '2404.02416', 'manual')",
                (stub.id,),
            )
            conn.commit()

        with store.pool.connection() as conn:
            merged = _reconcile_orphan_stub(
                store,
                survivor_ref_id=survivor.id,
                file_stem="atomic24",
                conn=conn,
            )
            conn.commit()
        assert merged == stub.id

        with store.pool.connection() as conn:
            arxiv_owner = conn.execute(
                "SELECT ref_id FROM ref_identifiers "
                "WHERE id_kind='arxiv' AND id_value='2404.02416'"
            ).fetchone()
            stub_row = conn.execute(
                "SELECT deleted_at, meta->>'superseded_by' FROM refs WHERE ref_id=%s",
                (stub.id,),
            ).fetchone()
            link = conn.execute(
                "SELECT 1 FROM links WHERE src_ref_id=%s AND dst_ref_id=%s "
                "AND relation='supersedes'",
                (survivor.id, stub.id),
            ).fetchone()
        # arXiv id moved onto the survivor → future probe_existing dedups it.
        assert arxiv_owner is not None and arxiv_owner[0] == survivor.id
        # stub soft-deleted with provenance back to the survivor.
        assert stub_row is not None and stub_row[0] is not None
        assert stub_row[1] == str(survivor.id)
        # supersedes edge recorded for audit.
        assert link is not None

    def test_noop_when_no_matching_stub(self, store):
        survivor = store.insert_ref(kind="paper", slug="solo24", title="No twin")
        with store.pool.connection() as conn:
            merged = _reconcile_orphan_stub(
                store,
                survivor_ref_id=survivor.id,
                file_stem="nonexistent99",
                conn=conn,
            )
            conn.commit()
        assert merged is None

    def test_does_not_fold_survivor_into_itself(self, store):
        # Filename cite_key belongs to the survivor (normal in-place stub
        # upgrade) — nothing to reconcile, no self-merge.
        survivor = store.insert_ref(kind="paper", slug="self24", title="Self")
        with store.pool.connection() as conn:
            merged = _reconcile_orphan_stub(
                store,
                survivor_ref_id=survivor.id,
                file_stem="self24",
                conn=conn,
            )
            conn.commit()
        assert merged is None
