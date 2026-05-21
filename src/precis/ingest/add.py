"""``precis_add()`` — single ingest entry point for the v2 schema.

Public API for the ingest pipeline. Wires the three pipeline
producers (``extract_paper`` / ``fetch_paper_by_doi`` /
``fetch_paper_by_arxiv``) to the v2 INSERT cascade
(:func:`precis.ingest.db_writer.write_paper`) with idempotency
checks via :func:`precis.ingest.db_writer.probe_existing`.

Atomic: every successful ingest commits exactly one transaction.
If the writer raises, the transaction rolls back and no rows
land. Caller (CLI / watch / future MCP tool) just sees the
exception.

Idempotent: every identifier the pipeline assembled — paper_id,
DOI, arXiv, S2, pdf_sha256, content_hash — is probed against
``ref_identifiers`` before any write. A hit short-circuits to
``IngestResult(inserted=False, ref_id=...)`` without touching the
DB further.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from precis.ingest.db_writer import (
    PaperToWrite,
    probe_existing,
    write_paper,
)
from precis.ingest.pipeline import (
    extract_paper,
    fetch_paper_by_arxiv,
    fetch_paper_by_doi,
)
from precis.store import Store

# ---------------------------------------------------------------------------
# Tagged-union input + result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PdfInput:
    pdf_path: Path


@dataclass(frozen=True)
class DoiInput:
    doi: str


@dataclass(frozen=True)
class ArxivInput:
    arxiv_id: str


PrecisAddInput = PdfInput | DoiInput | ArxivInput


@dataclass(frozen=True)
class IngestResult:
    """Outcome of a :func:`precis_add` call.

    ``inserted=False`` means an idempotency hit — the paper (or one
    of its identifiers) was already in the DB and we returned the
    existing ``ref_id`` unchanged. ``inserted=True`` means the writer
    produced new rows in this call.
    """

    ref_id: int
    inserted: bool
    paper_id: str
    pub_id: str | None
    cite_key: str
    pdf_sha256: str | None
    content_hash: str | None
    chunks_written: int
    identifiers: dict[str, str]


# ---------------------------------------------------------------------------
# precis_add — the public entry point
# ---------------------------------------------------------------------------


def precis_add(
    input: PrecisAddInput,
    *,
    store: Store,
    use_pdf2doi: bool = False,
    crossref_mailto: str = "",
    s2_api_key: str = "",
) -> IngestResult:
    """Ingest one paper into the v2 schema.

    Dispatches on the input type:

    * :class:`PdfInput` — runs Marker + the metadata cascade via
      :func:`precis.ingest.pipeline.extract_paper`.
    * :class:`DoiInput` — CrossRef-only fetch via
      :func:`precis.ingest.pipeline.fetch_paper_by_doi`.
    * :class:`ArxivInput` — Semantic Scholar via
      :func:`precis.ingest.pipeline.fetch_paper_by_arxiv`.

    The pipeline is run *outside* the DB transaction (it can be
    expensive and shouldn't hold locks). Once the
    :class:`PaperToWrite` is in hand, we open a connection,
    probe ``ref_identifiers`` for any pre-existing match, and
    either short-circuit or run the INSERT cascade in one tx.
    """
    paper = _build_paper(
        input,
        use_pdf2doi=use_pdf2doi,
        crossref_mailto=crossref_mailto,
        s2_api_key=s2_api_key,
    )

    with store.pool.connection() as conn:
        existing = probe_existing(
            paper_id=paper.paper_id,
            doi=paper.doi,
            arxiv_id=paper.arxiv_id,
            s2_id=paper.s2_id,
            pubmed_id=paper.pubmed_id,
            openalex_id=paper.openalex_id,
            pdf_sha256=paper.pdf_sha256,
            content_hash=paper.content_hash,
            conn=conn,
        )
        if existing is not None:
            return _hit_result(paper, ref_id=existing, conn=conn)

        result = write_paper(paper, conn=conn)
        conn.commit()

    return IngestResult(
        ref_id=result.ref_id,
        inserted=True,
        paper_id=paper.paper_id,
        pub_id=paper.pub_id,
        cite_key=result.cite_key,
        pdf_sha256=paper.pdf_sha256,
        content_hash=paper.content_hash,
        chunks_written=result.chunks_written,
        identifiers=result.identifiers_written,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _build_paper(
    input: PrecisAddInput,
    *,
    use_pdf2doi: bool,
    crossref_mailto: str,
    s2_api_key: str,
) -> PaperToWrite:
    """Dispatch on the input variant and run the matching pipeline producer."""
    if isinstance(input, PdfInput):
        return extract_paper(input.pdf_path, use_pdf2doi=use_pdf2doi)
    if isinstance(input, DoiInput):
        return fetch_paper_by_doi(input.doi, crossref_mailto=crossref_mailto)
    if isinstance(input, ArxivInput):
        return fetch_paper_by_arxiv(input.arxiv_id, s2_api_key=s2_api_key)
    raise TypeError(f"Unsupported input type: {type(input).__name__}")


def _hit_result(paper: PaperToWrite, *, ref_id: int, conn: Any) -> IngestResult:
    """Build an ``inserted=False`` result by re-fetching the existing
    ref's identifiers from the DB. The values returned reflect what's
    already stored, not what the pipeline freshly computed (the latter
    might disagree on cite_key suffix, etc.)."""
    rows = conn.execute(
        "SELECT id_kind, id_value FROM ref_identifiers WHERE ref_id = %s",
        (ref_id,),
    ).fetchall()
    identifiers: dict[str, str] = {kind: value for kind, value in rows}
    return IngestResult(
        ref_id=ref_id,
        inserted=False,
        paper_id=identifiers.get("paper_id", paper.paper_id),
        pub_id=identifiers.get("pub_id", paper.pub_id),
        cite_key=identifiers.get("cite_key", paper.cite_key_prefix),
        pdf_sha256=identifiers.get("pdf_sha256", paper.pdf_sha256),
        content_hash=identifiers.get("content_hash", paper.content_hash),
        chunks_written=0,
        identifiers=identifiers,
    )


__all__ = [
    "ArxivInput",
    "DoiInput",
    "IngestResult",
    "PdfInput",
    "PrecisAddInput",
    "precis_add",
]
