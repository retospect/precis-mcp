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

Idempotent on two layers:

1. **Fast path** — for :class:`PdfInput`, the cheap ``pdf_sha256``
   is computed from bytes and probed against ``ref_identifiers``
   *before* Marker runs. A hit short-circuits without invoking
   the pipeline at all (saves ~30–60 s/PDF on duplicates). See
   ``docs/design/extract-once.md``.
2. **Slow path** — every identifier the pipeline assembled
   (paper_id, DOI, arXiv, S2, content_hash, …) is probed again
   after extraction. Catches "same paper, different bytes" cases
   that the fast path misses.

In either case a hit yields ``IngestResult(inserted=False,
ref_id=...)`` with identifiers re-fetched from ``ref_identifiers``
(so the result reflects what's actually stored, not what the
pipeline freshly computed).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from precis.identity import make_pdf_sha256
from precis.ingest.db_writer import (
    PaperToWrite,
    probe_existing,
    write_paper,
)
from precis.ingest.pdf_writer import PatchInfo, patch_pdf_metadata
from precis.store import Store

# NOTE: ``precis.ingest.pipeline`` imports are deferred into
# :func:`_build_paper` because that module pulls in the paper-extra
# deps (habanero, semanticscholar, rapidfuzz, pymupdf, marker-pdf).
# Keeping the import lazy here means ``precis serve`` /
# ``precis migrate`` / ``precis worker`` keep working on a bare
# install without the ``[paper]`` extra; only ``precis add`` /
# ``precis watch`` ever actually call into the pipeline and they
# fail with a clean ``ModuleNotFoundError`` at runtime if the
# extra is missing.

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
      :func:`precis.ingest.pipeline.extract_paper`. The cheap
      ``pdf_sha256`` is probed against ``ref_identifiers`` *before*
      Marker so re-ingesting a known file short-circuits without
      paying for extraction (see ``docs/design/extract-once.md``).
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
    # Fast path: PDF inputs get a pre-Marker probe on pdf_sha256.
    # The hash is bytes-cheap (~1 ms/PDF); the probe is one round
    # trip. A hit here skips both extraction and the slow-path
    # probe entirely — that's the whole point of the optimisation.
    if isinstance(input, PdfInput):
        existing_ref_id = _probe_pdf_sha256(input.pdf_path, store=store)
        if existing_ref_id is not None:
            with store.pool.connection() as conn:
                return _hit_result_from_db(existing_ref_id, conn=conn)

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
            return _hit_result_from_db(existing, conn=conn, fallback=paper)

        # Write-back: for PdfInput, patch the on-disk file with the
        # resolved canonical metadata so a re-ingest from a clean DB
        # still finds the right DOI via embedded metadata. Pre- and
        # post-patch hashes both land in ref_identifiers (see ADR
        # 0014). Honours ``PRECIS_PATCH_PDFS=0`` off-switch.
        if isinstance(input, PdfInput):
            paper = _maybe_patch_pdf(input.pdf_path, paper)

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
    """Dispatch on the input variant and run the matching pipeline producer.

    The pipeline producers live in :mod:`precis.ingest.pipeline` and
    pull in the paper-extra dep tree (marker-pdf, pymupdf, habanero,
    semanticscholar, rapidfuzz). Import is deferred to here so the
    rest of the CLI keeps loading on a bare install — see the
    module-level note.
    """
    from precis.ingest.pipeline import (
        extract_paper,
        fetch_paper_by_arxiv,
        fetch_paper_by_doi,
    )

    if isinstance(input, PdfInput):
        return extract_paper(input.pdf_path, use_pdf2doi=use_pdf2doi)
    if isinstance(input, DoiInput):
        return fetch_paper_by_doi(input.doi, crossref_mailto=crossref_mailto)
    if isinstance(input, ArxivInput):
        return fetch_paper_by_arxiv(input.arxiv_id, s2_api_key=s2_api_key)
    raise TypeError(f"Unsupported input type: {type(input).__name__}")


def _maybe_patch_pdf(pdf_path: Path, paper: PaperToWrite) -> PaperToWrite:
    """Run the PDF metadata write-back and return an updated
    ``PaperToWrite`` reflecting whichever hash is now canonical.

    If the patch ran and produced new bytes, the file on disk now
    carries the resolved canonical identifiers (title / authors /
    DOI), the post-patch hash becomes the canonical
    ``pdf_sha256``, and the pre-patch hash is prepended to
    ``pdf_sha256_aliases`` so re-ingest of either byte sequence
    still hits the fast-path probe.

    If the patch was skipped (signed PDF, encrypted, no-op, env
    off-switch, error), ``paper`` is returned unchanged — the
    pre-existing ``paper.pdf_sha256`` stays canonical and there's
    nothing to alias.
    """
    info = PatchInfo(
        title=paper.title or None,
        authors=_format_authors_for_pdf(paper.authors),
        doi=paper.doi,
        arxiv_id=paper.arxiv_id,
    )
    outcome = patch_pdf_metadata(pdf_path, info, pre_hash=paper.pdf_sha256)
    if outcome.post_hash is None:
        return paper
    return replace(
        paper,
        pdf_sha256=outcome.post_hash,
        pdf_size_bytes=outcome.post_size,
        pdf_sha256_aliases=[outcome.pre_hash, *paper.pdf_sha256_aliases],
    )


def _format_authors_for_pdf(authors: list[dict[str, Any]] | None) -> list[str]:
    """Pull a flat list of surname strings out of the ref's authors
    JSON for the PDF ``Author`` field. Different pipelines (CrossRef,
    S2, Marker) use different key names, so we probe a small set.
    """
    if not authors:
        return []
    out: list[str] = []
    for entry in authors:
        if not isinstance(entry, dict):
            continue
        name = (
            entry.get("family")
            or entry.get("last")
            or entry.get("name")
            or entry.get("full")
            or ""
        ).strip()
        if name:
            out.append(name)
    return out


def _probe_pdf_sha256(pdf_path: Path, *, store: Store) -> int | None:
    """Compute ``pdf_sha256`` for ``pdf_path`` and probe ``ref_identifiers``.

    Returns the existing ``ref_id`` if the hash is already known,
    else ``None``. Read fully + hashed in-process; on bytes that
    can't be read (missing / permission denied) the function
    returns ``None`` and lets the slow path surface the error from
    :func:`precis.ingest.pipeline.extract_paper` so the diagnostic
    is consistent regardless of whether the file was known.

    The connection lifetime is bounded by this function; we don't
    hold it across the Marker run because the pool is sized for
    short-lived tx.
    """
    try:
        pdf_bytes = Path(pdf_path).read_bytes()
    except OSError:
        return None
    sha256 = make_pdf_sha256(pdf_bytes)
    with store.pool.connection() as conn:
        return probe_existing(pdf_sha256=sha256, conn=conn)


def _hit_result_from_db(
    ref_id: int,
    *,
    conn: Any,
    fallback: PaperToWrite | None = None,
) -> IngestResult:
    """Build an ``inserted=False`` result by re-fetching the existing
    ref's identifiers from the DB.

    The values returned reflect what's already stored, not what the
    pipeline freshly computed (the latter might disagree on
    cite_key suffix, etc.). ``fallback`` is consulted only for the
    rare case where ``ref_identifiers`` is missing a row we expect
    (e.g. ``paper_id`` was never written) — defensive belt-and-
    braces for the slow path. The fast path passes ``fallback=None``
    because no pipeline has run yet.
    """
    rows = conn.execute(
        "SELECT id_kind, id_value FROM ref_identifiers WHERE ref_id = %s",
        (ref_id,),
    ).fetchall()
    identifiers: dict[str, str] = {kind: value for kind, value in rows}

    if fallback is not None:
        paper_id = identifiers.get("paper_id", fallback.paper_id)
        pub_id = identifiers.get("pub_id", fallback.pub_id)
        cite_key = identifiers.get("cite_key", fallback.cite_key_prefix)
        pdf_sha256 = identifiers.get("pdf_sha256", fallback.pdf_sha256)
        content_hash = identifiers.get("content_hash", fallback.content_hash)
    else:
        paper_id = identifiers.get("paper_id", "")
        pub_id = identifiers.get("pub_id")
        cite_key = identifiers.get("cite_key", "")
        pdf_sha256 = identifiers.get("pdf_sha256")
        content_hash = identifiers.get("content_hash")

    return IngestResult(
        ref_id=ref_id,
        inserted=False,
        paper_id=paper_id,
        pub_id=pub_id,
        cite_key=cite_key,
        pdf_sha256=pdf_sha256,
        content_hash=content_hash,
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
