"""gr634048: precis's own draft-export PDFs must never ingest as papers.

pa553467 / pa553463 — draft-export PDFs that landed in the paper corpus
with author "precis" and empty bibtex, then drew title-fix todos. The
guard (:mod:`precis.ingest.draft_export_guard`) fingerprints the export
pipeline's own metadata — the ``\\author{precis}`` byline fallback plus
a TeX producer — and both ingest doors refuse on it:
:func:`precis.ingest.add.precis_add` raises
:class:`~precis.errors.DraftExportRefused` before any DB write, and
``paper_pull.check_pdf`` records ``draft_export``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from precis.errors import DraftExportRefused
from precis.ingest.add import PdfInput, precis_add
from precis.ingest.draft_export_guard import (
    draft_export_refusal,
    read_pdf_info,
)
from precis.workers.job_types import datasheet_pull as dl
from precis.workers.job_types import paper_pull


def _make_pdf(
    path: Path,
    *,
    author: str = "precis",
    producer: str = "LuaTeX-1.18.0",
    creator: str = "TeX",
    title: str = "Selective neuronal targeting: design draft",
) -> Path:
    """A real one-page PDF with the given Info dict (fitz reads it back)."""
    import fitz

    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "draft body text")
    doc.set_metadata(
        {"title": title, "author": author, "producer": producer, "creator": creator}
    )
    doc.save(str(path))
    doc.close()
    return path


class TestDraftExportRefusal:
    def test_export_fingerprint_refuses(self) -> None:
        reason = draft_export_refusal(
            {"author": "precis", "producer": "LuaTeX-1.18.0", "creator": "TeX"}
        )
        assert reason is not None
        assert "precis" in reason

    def test_author_case_and_whitespace_tolerated(self) -> None:
        reason = draft_export_refusal(
            {"author": "  Precis ", "producer": "PDFTEX-1.40.25"}
        )
        assert reason is not None

    def test_pdftex_and_xetex_producers_match(self) -> None:
        for producer in ("pdfTeX-1.40.25", "XeTeX 0.99999", "LuaTeX-1.15.0"):
            assert (
                draft_export_refusal({"author": "precis", "producer": producer})
                is not None
            ), producer

    def test_author_precis_without_tex_engine_passes(self) -> None:
        # The conjunct halves: a foreign PDF by an author named "precis"
        # (or with a scrubbed producer) is not evidence of own-output.
        assert (
            draft_export_refusal({"author": "precis", "producer": "Microsoft Word"})
            is None
        )
        assert draft_export_refusal({"author": "precis", "producer": ""}) is None

    def test_tex_engine_with_real_authors_passes(self) -> None:
        # An arXiv-style TeX PDF with real authors is an ordinary paper.
        assert (
            draft_export_refusal(
                {"author": "Smith, John", "producer": "pdfTeX-1.40.25"}
            )
            is None
        )

    def test_empty_info_passes(self) -> None:
        assert draft_export_refusal({}) is None

    def test_read_pdf_info_roundtrip(self, tmp_path: Path) -> None:
        pdf = _make_pdf(tmp_path / "export.pdf")
        info = read_pdf_info(pdf)
        assert info.get("author") == "precis"
        assert "LuaTeX" in (info.get("producer") or "")

    def test_read_pdf_info_unreadable_is_empty(self, tmp_path: Path) -> None:
        junk = tmp_path / "junk.pdf"
        junk.write_bytes(b"not a pdf")
        assert read_pdf_info(junk) == {}


class TestPaperPullGate:
    def test_check_pdf_refuses_draft_export(self, tmp_path: Path) -> None:
        pdf = _make_pdf(tmp_path / "export.pdf")
        with pytest.raises(dl._Failed) as excinfo:
            paper_pull.check_pdf(
                pdf, title="A stub paper", doi="10.1234/stub", entry_type=""
            )
        assert excinfo.value.reason == "draft_export"

    def test_check_pdf_allows_tex_pdf_with_real_authors(self, tmp_path: Path) -> None:
        pdf = _make_pdf(
            tmp_path / "real.pdf",
            author="Smith, John",
            title="A Real Paper",
        )
        # 1-page article would trip preview_suspected, so mark it a
        # long-form entry type — the point is that the draft-export
        # gate does NOT fire before that.
        out = paper_pull.check_pdf(pdf, title="", doi=None, entry_type="techreport")
        assert out["identity"] in ("unchecked", "title")


class TestPrecisAddRefusal:
    def test_precis_add_refuses_draft_export_before_any_write(
        self, store, tmp_path: Path
    ) -> None:
        pdf = _make_pdf(tmp_path / "export.pdf")
        with pytest.raises(DraftExportRefused) as excinfo:
            precis_add(PdfInput(pdf_path=pdf), store=store)
        assert "draft export" in str(excinfo.value)
        # Nothing minted: no paper ref exists for the file.
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) FROM refs WHERE kind = 'paper'"
            ).fetchone()
        assert row is not None and row[0] == 0

    def test_precis_add_ingests_ordinary_tex_pdf(
        self, store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The guard must not block an ordinary TeX-produced paper; the
        # extraction itself is stubbed (Marker is not the point here).
        import precis.ingest.add as add_mod

        pdf = _make_pdf(
            tmp_path / "real.pdf", author="Smith, John", title="A Real Paper"
        )
        monkeypatch.setattr(
            add_mod,
            "_ingest_pdf",
            lambda input, **kw: add_mod.IngestResult(
                ref_id=1,
                inserted=True,
                paper_id="p",
                pub_id=None,
                cite_key="smith26",
                pdf_sha256="ab" * 32,
                content_hash=None,
                chunks_written=1,
                identifiers={},
            ),
        )
        result = precis_add(PdfInput(pdf_path=pdf), store=store)
        assert result is not None and result.inserted
