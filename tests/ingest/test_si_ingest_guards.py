"""SI ingest guards: reuse only links a real SI; ordinary resolves are untouched."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from unittest.mock import patch

from precis.ingest.add import PdfInput, precis_add
from precis.ingest.db_writer import ChunkToWrite, PaperToWrite
from precis.store.si_links import SI_RELATION


def _seed_parent(store) -> int:
    ref = store.insert_ref(
        kind="paper", slug="smith2023cat", title="Parent", authors=[], year=2023
    )
    return ref.id


def _paper(sha: str) -> PaperToWrite:
    return PaperToWrite(
        title="An ordinary paper",
        authors=[{"name": "Jones, A"}],
        year=2020,
        kind="paper",
        provider="crossref",
        paper_id="doi:10.1/ordinary",
        cite_key_prefix="jones20",
        pdf_sha256=sha,
        content_hash="c" * 64,
        pdf_role="main",
        pdf_storage_path="/tmp/x.pdf",
        pdf_page_count=1,
        pdf_size_bytes=10,
        doi="10.1/ordinary",
        chunks=[ChunkToWrite(ord=0, chunk_kind="paragraph", text="Body.")],
    )


def test_reuse_does_not_link_an_ordinary_paper(store, tmp_path: Path, caplog) -> None:
    parent_id = _seed_parent(store)
    payload = b"%PDF-1.4 same bytes"
    pdf = tmp_path / "main.pdf"
    pdf.write_bytes(payload)
    sha = hashlib.sha256(payload).hexdigest()
    with patch("precis.ingest.pipeline.extract_paper", return_value=_paper(sha)):
        ordinary = precis_add(PdfInput(pdf_path=pdf), store=store)
    assert ordinary is not None
    with caplog.at_level(logging.WARNING):
        res = precis_add(PdfInput(pdf_path=pdf, supplement_of=parent_id), store=store)
    assert res is not None and res.ref_id == ordinary.ref_id
    with store.pool.connection() as conn:
        n = conn.execute(
            "SELECT count(*) FROM links WHERE relation = %s", (SI_RELATION,)
        ).fetchone()
        role = conn.execute(
            "SELECT pdf_role FROM refs WHERE ref_id = %s", (ordinary.ref_id,)
        ).fetchone()
    assert n is not None and n[0] == 0
    assert role is not None and role[0] == "main"
    assert "not linking" in caplog.text


def test_resolve_handle_ordinary_paper_has_no_cite_override(store) -> None:
    pid = _seed_parent(store)
    ref_h = store.resolve_handle(f"pa{pid}")
    assert ref_h is not None
    assert ref_h.cite_public_id is None
    assert ref_h.public_id == "smith2023cat" and ref_h.ref_id == pid
    # chunk handle of an ordinary paper too
    from precis.store.types import ChunkInsert

    store.chunks.insert_chunks(pid, [ChunkInsert(ord=0, text="Body.", meta={})])
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord = 0", (pid,)
        ).fetchone()
    assert row is not None
    chunk_h = store.resolve_handle(f"pc{row[0]}")
    assert chunk_h is not None and chunk_h.cite_public_id is None
    assert chunk_h.ref_id == pid
