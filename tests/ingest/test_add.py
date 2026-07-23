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
    MarkupInput,
    PdfInput,
    _reconcile_orphan_stub,
    _valid_fold_stub,
    precis_add,
)
from precis.ingest.db_writer import ChunkToWrite, PaperToWrite
from precis.ingest.fetch_sidecar import read_sidecar, write_sidecar
from precis.ingest.markup import MarkupParseError

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


class TestDoiCaseCanonicalization:
    """Cross-route dedup must survive publisher DOI-case inconsistency.

    DOIs are case-insensitive (DOI Handbook §2.4). ``make_paper_id``
    already lowercases, but the ``ref_identifiers`` doi *alias* row used
    by ``probe_existing`` must also be canonical — otherwise a paper
    ingested via arXiv/S2 (which hand back the raw publisher casing) and
    the same paper later ingested via its DOI write two differently-cased
    rows and the idempotency probe misses, spawning a duplicate ref.
    """

    def test_alias_row_is_lowercased_on_write(self, store):
        # S2 hands back the published DOI in uppercase on an arXiv ingest.
        base = _fixture_paper(
            paper_id="arxiv:2401.12345", cite_key_prefix="wei24", doi="10.1038/UPPER"
        )
        arxiv_paper = PaperToWrite(
            **{
                **base.__dict__,
                "arxiv_id": "2401.12345",
                "provider": "s2",
                "pub_id": "doi:10.1038/upper",
            }
        )
        with patch(
            "precis.ingest.pipeline.fetch_paper_by_arxiv",
            return_value=arxiv_paper,
        ):
            r1 = precis_add(ArxivInput(arxiv_id="2401.12345"), store=store)

        assert r1.inserted is True
        # The stored alias must be the canonical lowercase form.
        assert r1.identifiers["doi"] == "10.1038/upper"
        with store.pool.connection() as conn:
            stored = conn.execute(
                "SELECT id_value FROM ref_identifiers "
                "WHERE id_kind = 'doi' AND ref_id = %s",
                (r1.ref_id,),
            ).fetchone()
        assert stored is not None and stored[0] == "10.1038/upper"

    def test_doi_ingest_dedups_onto_arxiv_ingested_paper(self, store):
        # 1) arXiv route writes the paper with an uppercase DOI from S2.
        base = _fixture_paper(
            paper_id="arxiv:2402.55555", cite_key_prefix="ng24", doi="10.1038/MixedCase"
        )
        arxiv_paper = PaperToWrite(
            **{
                **base.__dict__,
                "arxiv_id": "2402.55555",
                "provider": "s2",
                "pub_id": "doi:10.1038/mixedcase",
            }
        )
        with patch(
            "precis.ingest.pipeline.fetch_paper_by_arxiv",
            return_value=arxiv_paper,
        ):
            r1 = precis_add(ArxivInput(arxiv_id="2402.55555"), store=store)

        # 2) Same paper arrives later via its DOI (lowercase). It must
        # collapse onto the existing ref, not spawn a duplicate.
        doi_paper = _fixture_paper(
            paper_id="doi:10.1038/mixedcase",
            cite_key_prefix="ng24",
            doi="10.1038/mixedcase",
        )
        with patch(
            "precis.ingest.pipeline.fetch_paper_by_doi",
            return_value=doi_paper,
        ):
            r2 = precis_add(DoiInput(doi="10.1038/mixedcase"), store=store)

        assert r1.inserted is True
        assert r2.inserted is False
        assert r2.ref_id == r1.ref_id

        # Exactly one ref carries this DOI.
        with store.pool.connection() as conn:
            n = conn.execute(
                "SELECT count(*) FROM ref_identifiers "
                "WHERE id_kind = 'doi' AND id_value = '10.1038/mixedcase'"
            ).fetchone()
        assert n is not None and n[0] == 1


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


class TestSidecarFold:
    """A fetched PDF whose *extracted* identity doesn't dedup against any
    ref folds into the OA-fetch sidecar's designated stub in place —
    keeping the stub's good title/DOI — instead of minting a duplicate."""

    def test_folds_into_fold_ref_id_stub_in_place(self, store, tmp_path: Path):
        pdf = tmp_path / "download.pdf"
        pdf.write_bytes(b"%PDF-1.4 sidecar")
        sha = hashlib.sha256(b"%PDF-1.4 sidecar").hexdigest()

        # The stub the fetcher created: good title + DOI, no PDF yet.
        stub = store.insert_ref(
            kind="paper",
            slug="continuous83",
            title="Continuous deformations in random networks",
        )
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
                "VALUES (%s, 'doi', '10.1016/0022-3093(83)90424-6', 'manual')",
                (stub.id,),
            )
            conn.commit()

        # Marker extracts metadata-poor content (no DOI) — nothing to dedup
        # against the stub, so without the sidecar this would mint a duplicate.
        poor = _fixture_paper(paper_id="anonpid7", doi=None, pdf_sha256=sha)
        with patch("precis.ingest.pipeline.extract_paper", return_value=poor):
            result = precis_add(
                PdfInput(pdf_path=pdf, fold_ref_id=stub.id), store=store
            )

        assert result.inserted is False  # folded, not a fresh insert
        assert result.ref_id == stub.id
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT pdf_sha256, title FROM refs WHERE ref_id=%s", (stub.id,)
            ).fetchone()
            nchunks = conn.execute(
                "SELECT count(*) FROM chunks WHERE ref_id=%s", (stub.id,)
            ).fetchone()[0]
            owners = conn.execute(
                "SELECT ref_id FROM ref_identifiers "
                "WHERE id_kind='pdf_sha256' AND id_value=%s",
                (sha,),
            ).fetchall()
        assert row[0] == sha  # promoted in place
        # Good stub metadata preserved — NOT overwritten by the poor extract.
        assert row[1] == "Continuous deformations in random networks"
        assert nchunks >= 1
        # Exactly one ref owns the new PDF — no duplicate minted.
        assert [o[0] for o in owners] == [stub.id]

    def test_invalid_fold_target_falls_through_to_insert(self, store, tmp_path: Path):
        # fold_ref_id points at a soft-deleted ref (e.g. a stub already
        # merged away) → not a valid fold target → normal insert, no crash.
        pdf = tmp_path / "d2.pdf"
        pdf.write_bytes(b"%PDF-1.4 d2")
        sha = hashlib.sha256(b"%PDF-1.4 d2").hexdigest()
        dead = store.insert_ref(kind="paper", slug="dead24", title="merged away")
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET deleted_at = now() WHERE ref_id = %s", (dead.id,)
            )
            conn.commit()
            assert _valid_fold_stub(dead.id, kind="paper", conn=conn) is None

        paper = _fixture_paper(paper_id="ftpid", doi="10.1/ft", pdf_sha256=sha)
        with patch("precis.ingest.pipeline.extract_paper", return_value=paper):
            result = precis_add(
                PdfInput(pdf_path=pdf, fold_ref_id=dead.id), store=store
            )
        assert result.inserted is True
        assert result.ref_id != dead.id

    def test_new_ref_branch_folds_orphan_stub_by_filename(self, store, tmp_path: Path):
        """Root-cause fix: the *first* ingest of a paper (a dedup miss →
        new ref) must still fold the orphan stub it was fetched for, even
        with no sidecar. Previously this reconcile ran only on the
        dedup-*hit* branches, so a metadata-mismatched paper stayed split
        until a second ingest."""
        pdf = tmp_path / "fair26.pdf"
        pdf.write_bytes(b"%PDF-1.4 fair")
        sha = hashlib.sha256(b"%PDF-1.4 fair").hexdigest()

        # Orphan stub: good full DOI, named after its cite_key (= file stem).
        stub = store.insert_ref(
            kind="paper", slug="fair26", title="FAIR Data and the Future"
        )
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
                "VALUES (%s, 'doi', '10.17352/gjmccr.000235', 'manual')",
                (stub.id,),
            )
            conn.commit()

        # Extracted DOI is truncated → misses the stub → new-ref branch.
        paper = _fixture_paper(
            paper_id="chetrypid",
            cite_key_prefix="chetry26",
            doi="10.17352/gjmccr",
            pdf_sha256=sha,
        )
        with patch("precis.ingest.pipeline.extract_paper", return_value=paper):
            result = precis_add(PdfInput(pdf_path=pdf), store=store)  # no sidecar

        assert result.inserted is True
        assert result.ref_id != stub.id
        with store.pool.connection() as conn:
            stub_row = conn.execute(
                "SELECT deleted_at, meta->>'superseded_by' FROM refs WHERE ref_id=%s",
                (stub.id,),
            ).fetchone()
            doi_owner = conn.execute(
                "SELECT ref_id FROM ref_identifiers "
                "WHERE id_kind='doi' AND id_value='10.17352/gjmccr.000235'"
            ).fetchone()
        assert stub_row[0] is not None  # stub soft-deleted
        assert stub_row[1] == str(result.ref_id)  # provenance → survivor
        assert doi_owner is not None and doi_owner[0] == result.ref_id  # DOI migrated


class TestMarkupParseFailureRecovery:
    """gr161905: on a MarkupParseError, the companion PDF (tagged
    ``printable_only`` at fetch time so it never independently races
    Marker against this markup) is the deterministic OCR fallback,
    recovered right here in this same process — not via a cross-host
    race on which trigger a watcher reaches first."""

    def test_clears_printable_only_when_companion_still_in_inbox(
        self, store, tmp_path: Path
    ) -> None:
        stub = store.insert_ref(kind="paper", slug="park83", title="Parked stub")
        markup = tmp_path / "park83.xml"
        markup.write_bytes(b"<xml>not real jats</xml>")
        pdf = tmp_path / "park83.pdf"
        pdf.write_bytes(b"%PDF-1.4 park")

        write_sidecar(
            markup,
            ref_id=stub.id,
            identifiers={"doi": "10.1000/markup-leg-doi"},
            source="fetcher:europepmc_jats",
            source_format="jats",
            companion_pdf="park83.pdf",
        )
        write_sidecar(
            pdf,
            ref_id=stub.id,
            identifiers={"doi": "10.1000/pdf-leg-doi", "arxiv": "2401.99999"},
            source="fetcher:arxiv",
            printable_only=True,
        )

        with patch(
            "precis.ingest.pipeline.extract_paper_from_markup",
            side_effect=MarkupParseError("no <body>", fmt="jats"),
        ):
            result = precis_add(
                MarkupInput(markup_path=markup, fmt="jats", fold_ref_id=stub.id),
                store=store,
            )

        assert result is None
        pdf_sc = read_sidecar(pdf)
        assert pdf_sc is not None
        assert pdf_sc.printable_only is False
        # gr161905 recovery must clear printable_only WITHOUT clobbering the
        # companion's own provenance with the markup trigger's — a re-fetched
        # Elsevier companion whose source got overwritten to
        # fetcher:europepmc_jats would silently bypass the gr162364
        # Elsevier-truncation guard (which gates on sidecar.source).
        assert pdf_sc.source == "fetcher:arxiv"
        assert pdf_sc.identifiers == {
            "doi": "10.1000/pdf-leg-doi",
            "arxiv": "2401.99999",
        }

    def test_ocrs_stored_copy_when_companion_already_attached(
        self, store, tmp_path: Path
    ) -> None:
        # The companion PDF already landed as an attach-only printable
        # (has_body False, pdf_sha256 set) — simulate that state directly,
        # then confirm a markup parse failure triggers the synchronous OCR
        # fallback on the stored copy.
        pdf_bytes = b"%PDF-1.4 already-attached-printable"
        sha = hashlib.sha256(pdf_bytes).hexdigest()
        stored_dir = tmp_path / "corpus"
        stored_dir.mkdir()
        stored = stored_dir / "stored.pdf"
        stored.write_bytes(pdf_bytes)

        stub = store.insert_ref(kind="paper", slug="attached83", title="Attached")
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
                "size_bytes, storage_path) VALUES (%s, %s, %s, %s, %s)",
                (sha, sha, 1, len(pdf_bytes), str(stored)),
            )
            conn.execute(
                "UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s",
                (sha, stub.id),
            )
            conn.execute(
                "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
                "VALUES ('pdf_sha256', %s, %s, 'fetcher:arxiv')",
                (sha, stub.id),
            )
            conn.commit()

        markup = tmp_path / "attached83.xml"
        markup.write_bytes(b"<xml>not real jats</xml>")
        write_sidecar(
            markup,
            ref_id=stub.id,
            identifiers={},
            source="fetcher:europepmc_jats",
            source_format="jats",
            companion_pdf="already-moved.pdf",  # no longer sitting in the inbox
        )

        full_paper = _fixture_paper(paper_id="attachpid", doi=None, pdf_sha256=sha)
        with (
            patch(
                "precis.ingest.pipeline.extract_paper_from_markup",
                side_effect=MarkupParseError("no <body>", fmt="jats"),
            ),
            patch(
                "precis.ingest.pipeline.extract_paper",
                return_value=full_paper,
            ),
        ):
            result = precis_add(
                MarkupInput(markup_path=markup, fmt="jats", fold_ref_id=stub.id),
                store=store,
            )

        assert result is None  # the markup ingest itself still returns None
        with store.pool.connection() as conn:
            nchunks = conn.execute(
                "SELECT count(*) FROM chunks WHERE ref_id=%s", (stub.id,)
            ).fetchone()[0]
        assert nchunks == 2  # the OCR fallback populated the body (+ card)

    def test_no_recovery_without_fold_ref_id(self, tmp_path: Path, store) -> None:
        # A manually-dropped markup file (no sidecar, no fold target) —
        # nothing to recover, must not raise.
        markup = tmp_path / "manual.xml"
        markup.write_bytes(b"<xml>not real jats</xml>")
        with patch(
            "precis.ingest.pipeline.extract_paper_from_markup",
            side_effect=MarkupParseError("no <body>", fmt="jats"),
        ):
            result = precis_add(
                MarkupInput(markup_path=markup, fmt="jats"), store=store
            )
        assert result is None
