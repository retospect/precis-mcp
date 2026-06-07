"""Ingest pipeline — produce a :class:`PaperToWrite` from a PDF or registry.

Replaces ``acatome_extract.pipeline``. The upstream version emitted
gzipped ``.acatome`` bundles; the v2 version returns the dataclass
that :func:`precis.ingest.db_writer.write_paper` consumes. No file
I/O on the output side — the ``precis_add()`` entry point (B4d)
owns the database transaction.

Three input modes:

* :func:`extract_paper` — local PDF; runs Marker for full-text
  blocks plus the metadata cascade.
* :func:`fetch_paper_by_doi` — DOI registered with CrossRef; pulls
  bibliographic metadata only (no PDF, no body chunks). Just the
  ref + cards.
* :func:`fetch_paper_by_arxiv` — arXiv ID via Semantic Scholar's
  ``arxiv:`` lookup. Same shape as the DOI path.

The two metadata-only paths produce a paper with ``pdf_sha256=None``
and a single ``card_combined`` chunk so the worker queue can still
build an embedding for retrieval. Body chunks land later if and
when the PDF is added via :func:`extract_paper`.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Any

from precis.identity import (
    make_cite_key,
    make_content_hash,
    make_paper_id,
    make_pdf_sha256,
    make_pub_id,
    normalize_arxiv,
    normalize_doi,
)
from precis.ingest.db_writer import ChunkToWrite, PaperToWrite
from precis.ingest.lookup import lookup_doi
from precis.ingest.marker import extract_blocks_marker
from precis.ingest.pdf_metadata import DoiProvenance, extract_metadata_from_sources
from precis.ingest.semantic_scholar import lookup_s2
from precis.utils.boilerplate import ChunkClass, classify_chunks
from precis.utils.numerics import extract_numerics

# U+FFFD — Unicode "replacement character." PDFs whose ToUnicode map is
# incomplete or contradicts the embedded cmap leak FFFD bytes through
# Marker (and through ftfy, which can repair byte-level mojibake but
# not characters that already arrived as the canonical replacement).
# Em-dashes are by far the most common loss vector in scientific PDFs;
# the alpha-space-FFFD-space-alpha pattern below auto-repairs that
# specific high-precision case. Anything else stays as U+FFFD: that
# character is *itself* the canonical Unicode sentinel for "byte
# sequence I could not decode," and downstream readers (BGE-M3,
# Postgres full-text, the user reading a chunk) handle it cleanly.
# Earlier policy fail-failed the entire paper on any unrepaired FFFD;
# in production that lost ~110 real papers per backfill to publisher
# PDFs with broken ToUnicode maps the operator could do nothing about.
_REPLACEMENT_CHAR = "�"
_EM_DASH_LOST_RE = re.compile(rf"([a-zA-Z]) {_REPLACEMENT_CHAR} ([a-zA-Z])")


def _repair_mojibake(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Auto-repair lost em-dashes in place. Any other surviving FFFD
    is left where it is — the character is self-describing.
    """
    for block in blocks:
        text = block.get("text", "")
        if _REPLACEMENT_CHAR not in text:
            continue
        block["text"] = _EM_DASH_LOST_RE.sub(r"\1 — \2", text)
    return blocks


# ---------------------------------------------------------------------------
# Marker block type → chunks.chunk_kind mapping
# ---------------------------------------------------------------------------

# Marker emits a richer type vocabulary than the v2 ``chunk_kinds``
# table. Anything not in this map is silently dropped (junk + structural
# markers); anything mapped is preserved with its section_path / page
# range. The fallback is ``paragraph`` so an unknown text-bearing type
# doesn't get lost.
_BLOCK_KIND_MAP: dict[str, str] = {
    "text": "paragraph",
    "paragraph": "paragraph",
    "list_item": "paragraph",
    # ``table`` used to fall through to ``paragraph``, which made the
    # summarize handler run RAKE on markdown table content and emit
    # noise like ``"na na na na"`` for any table with empty cells (see
    # the deng10 MTV-MOF paper, ord 28-29). Keep ``table`` as its own
    # chunk_kind so summarize / future handlers can opt in or out.
    "table": "table",
    "figure": "figure",
    "caption": "caption",
    "equation": "equation",
    "section_header": "heading",
    "code": "code_symbol",
    "references": "references",
}

# Marker types that carry no semantic payload for retrieval. Dropped
# before chunk emission. Title goes into the ``card_title`` card, not
# a body chunk; author blocks merge into ``card_authors``.
_SKIPPED_BLOCK_TYPES: frozenset[str] = frozenset(
    {"junk", "page_header", "page_footer", "title", "author"}
)

# Maps the metadata-extraction provenance to the ``providers.slug``
# value the migration seeds (see ``0001_initial.sql``). Anything
# without an external lookup hit lands on ``local`` — the seed entry
# for "local computation / no external source".
_PROVIDER_MAP: dict[DoiProvenance | None, str] = {
    DoiProvenance.SECONDARY_VALIDATOR: "crossref",
    DoiProvenance.SIDECAR_META: "local",
    DoiProvenance.EXISTING_PDF_METADATA: "local",
    DoiProvenance.INTERNAL_EXTRACTOR: "local",
    DoiProvenance.PDF2DOI_FALLBACK: "local",
    DoiProvenance.FILENAME_PATTERN: "local",
    None: "local",
}


# ---------------------------------------------------------------------------
# Identity helper
# ---------------------------------------------------------------------------


def _resolve_identity(
    *,
    doi: str | None,
    arxiv_id: str | None,
    pdf_sha256: str | None,
    authors: list[dict[str, Any]],
    year: int | None,
) -> tuple[str, str | None, str]:
    """Compute ``(paper_id, pub_id, cite_key_prefix)`` for a paper.

    The ``pub_id`` is set only when an external publisher ID exists
    (DOI or arXiv). PDF-hash-only papers get ``None`` because there's
    no public handle to publish.
    """
    paper_id = make_paper_id(arxiv=arxiv_id, doi=doi, pdf_sha256=pdf_sha256)
    pub_id = make_pub_id(paper_id) if (doi or arxiv_id) else None
    # Bare prefix (no ``taken=``); db_writer.resolve_cite_key picks the
    # final suffix once it has the live ref_identifiers index in hand.
    cite_key_prefix = make_cite_key(authors, year)
    return paper_id, pub_id, cite_key_prefix


# ---------------------------------------------------------------------------
# Block / card builders
# ---------------------------------------------------------------------------


def _retag_references(chunks: list[ChunkToWrite]) -> list[ChunkToWrite]:
    """Promote bibliography chunks to ``chunk_kind='references'``.

    Marker reliably labels the *heading* of a references section
    (``# References``) but tags the following bibliography paragraphs
    as plain text. Storage-v2's contract says references should
    arrive as ``chunk_kind='references'`` so the embedder worker
    can skip them via its ``skip_chunk_kinds`` filter — otherwise
    the bibliography pollutes search with citation-list noise.

    We delegate the detection to
    :func:`precis.utils.boilerplate.classify_chunks`, which already
    has the heading + citation-density heuristics tuned for
    scientific papers (it was previously only consulted downstream by
    the TOC segmenter; now it informs ingest too).

    Idempotent: chunks already tagged ``'references'`` pass through
    unchanged. Non-references chunks are untouched.
    """
    if not chunks:
        return chunks
    classified = classify_chunks([c.text for c in chunks])
    out: list[ChunkToWrite] = []
    for chunk, klass in zip(chunks, classified.classes, strict=True):
        if klass is ChunkClass.REFERENCES and chunk.chunk_kind != "references":
            out.append(dataclasses.replace(chunk, chunk_kind="references"))
        else:
            out.append(chunk)
    return out


def _blocks_to_chunks(blocks: list[dict[str, Any]]) -> list[ChunkToWrite]:
    """Map Marker blocks → :class:`ChunkToWrite` body chunks.

    ``ord`` starts at 0 and increments per surviving block. Skipped
    block types (junk, page-headers, …) don't reserve an ord.
    """
    chunks: list[ChunkToWrite] = []
    ord_counter = 0
    for block in blocks:
        block_type = block.get("type", "")
        if block_type in _SKIPPED_BLOCK_TYPES:
            continue
        text = (block.get("text") or "").strip()
        if not text:
            continue
        chunk_kind = _BLOCK_KIND_MAP.get(block_type, "paragraph")
        page = block.get("page")
        section_raw = block.get("section_path") or []
        section_path = [str(s) for s in section_raw if s]
        # Lexical numeric-token index — every "1.523 eV" / "12%" /
        # "0.3 V" / "1670 cm-1" detected in the chunk text. GIN-
        # indexed downstream for cheap value lookups. See
        # :mod:`precis.utils.numerics` for the recognized unit set.
        numerics = extract_numerics(text)
        chunks.append(
            ChunkToWrite(
                ord=ord_counter,
                chunk_kind=chunk_kind,
                text=text,
                section_path=section_path,
                page_first=page,
                page_last=page,
                numerics=numerics,
            )
        )
        ord_counter += 1
    return chunks


def _build_cards(
    *,
    title: str,
    authors: list[dict[str, Any]],
    abstract: str,
    keywords: list[str],
) -> list[ChunkToWrite]:
    """Build the per-ref synthetic chunks (cards). ``ord`` starts at
    ``-1`` and decreases — the schema's CHECK demands ``ord < 0`` for
    cards.

    Always emits ``card_combined`` (the "search me first" card).
    The narrower cards (title-only, authors-only, abstract-only,
    keywords-only) are skipped when their input is empty so we don't
    write rows that would never embed to anything useful.
    """
    cards: list[ChunkToWrite] = []
    ord_counter = -1

    author_names = [a.get("name", "") for a in authors if a.get("name")]

    # card_combined — concatenation of every search-relevant field.
    combined_parts: list[str] = []
    if title:
        combined_parts.append(title)
    if author_names:
        combined_parts.append("; ".join(author_names))
    if abstract:
        combined_parts.append(abstract)
    if keywords:
        combined_parts.append("; ".join(keywords))
    combined_text = "\n\n".join(combined_parts).strip()
    cards.append(
        ChunkToWrite(
            ord=ord_counter,
            chunk_kind="card_combined",
            text=combined_text or "[no metadata]",
        )
    )
    ord_counter -= 1

    if title:
        cards.append(
            ChunkToWrite(ord=ord_counter, chunk_kind="card_title", text=title),
        )
        ord_counter -= 1

    if author_names:
        cards.append(
            ChunkToWrite(
                ord=ord_counter,
                chunk_kind="card_authors",
                text="; ".join(author_names),
            ),
        )
        ord_counter -= 1

    if abstract:
        cards.append(
            ChunkToWrite(ord=ord_counter, chunk_kind="card_abstract", text=abstract),
        )
        ord_counter -= 1

    if keywords:
        cards.append(
            ChunkToWrite(
                ord=ord_counter,
                chunk_kind="card_keywords",
                text="; ".join(keywords),
            ),
        )
        ord_counter -= 1

    return cards


# ---------------------------------------------------------------------------
# extract_paper — local PDF
# ---------------------------------------------------------------------------


def extract_paper(
    pdf_path: Path,
    *,
    use_pdf2doi: bool = False,
) -> PaperToWrite:
    """Build a :class:`PaperToWrite` from a local PDF.

    Steps (in order):

    1. Read the file bytes; compute :func:`make_pdf_sha256`.
    2. Run :func:`extract_metadata_from_sources` — sidecar +
       embedded metadata + lookup cascade.
    3. Compute identity (``paper_id``, ``pub_id``, ``cite_key_prefix``).
    4. Run Marker → list of blocks; map to body chunks.
    5. Synthesize cards from the metadata.
    6. Hash the canonicalised body text → ``content_hash``.
    7. Pack everything into :class:`PaperToWrite`.

    Raises :class:`FileNotFoundError` if ``pdf_path`` doesn't exist.
    """
    pdf_path = Path(pdf_path).resolve()
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    pdf_bytes = pdf_path.read_bytes()
    pdf_sha256 = make_pdf_sha256(pdf_bytes)

    metadata = extract_metadata_from_sources(pdf_path, use_pdf2doi=use_pdf2doi)
    doi = metadata.doi or None
    arxiv_id: str | None = None  # extract_metadata_from_sources doesn't surface arxiv

    authors_dict = [{"name": n} for n in metadata.authors if n]

    paper_id, pub_id, cite_key_prefix = _resolve_identity(
        doi=doi,
        arxiv_id=arxiv_id,
        pdf_sha256=pdf_sha256,
        authors=authors_dict,
        year=metadata.year,
    )

    blocks = extract_blocks_marker(pdf_path, paper_id)
    blocks = _repair_mojibake(blocks)
    body_chunks = _blocks_to_chunks(blocks)
    body_chunks = _retag_references(body_chunks)
    cards = _build_cards(
        title=metadata.title,
        authors=authors_dict,
        abstract=metadata.abstract,
        keywords=metadata.keywords,
    )

    full_text = "\n\n".join(c.text for c in body_chunks)
    content_hash = make_content_hash(full_text) if full_text else None

    pages = [b.get("page") for b in blocks if b.get("page") is not None]
    pdf_pages_first = min(pages) if pages else None
    pdf_pages_last = max(pages) if pages else None
    page_count = len({p for p in pages if p is not None}) if pages else 0

    provider = _PROVIDER_MAP.get(metadata.doi_provenance, "embedded")

    extra: dict[str, Any] = {}
    if metadata.abstract:
        extra["abstract"] = metadata.abstract
    if metadata.journal:
        extra["journal"] = metadata.journal
    if metadata.publisher:
        extra["publisher"] = metadata.publisher
    if metadata.keywords:
        extra["keywords"] = metadata.keywords
    if metadata.verify_warnings:
        extra["verify_warnings"] = metadata.verify_warnings

    return PaperToWrite(
        title=metadata.title,
        authors=authors_dict,
        year=metadata.year,
        kind="paper",
        provider=provider,
        set_by="system",
        paper_id=paper_id,
        pub_id=pub_id,
        cite_key_prefix=cite_key_prefix,
        pdf_sha256=pdf_sha256,
        content_hash=content_hash,
        pdf_pages_first=pdf_pages_first,
        pdf_pages_last=pdf_pages_last,
        pdf_role="main",
        pdf_storage_path=str(pdf_path),
        pdf_page_count=page_count or len(blocks) or 1,
        pdf_size_bytes=len(pdf_bytes),
        doi=doi,
        arxiv_id=arxiv_id,
        meta=extra,
        chunks=cards + body_chunks,
    )


# ---------------------------------------------------------------------------
# fetch_paper_by_doi — DOI input, no PDF
# ---------------------------------------------------------------------------


def fetch_paper_by_doi(
    doi: str,
    *,
    crossref_mailto: str = "",
) -> PaperToWrite:
    """Build a metadata-only :class:`PaperToWrite` from a DOI.

    Hits CrossRef via :func:`precis.ingest.lookup.lookup_doi`. Body
    chunks list stays empty; cards are synthesized from the returned
    metadata so the worker queue still has something to embed.

    Raises :class:`ValueError` when CrossRef returns nothing — the
    caller surfaces that as a CLI error in B4d.
    """
    normalised = normalize_doi(doi)
    if not normalised:
        raise ValueError(f"Invalid DOI: {doi!r}")

    result = lookup_doi(normalised, mailto=crossref_mailto)
    if result is None:
        raise ValueError(f"DOI lookup failed (CrossRef miss): {normalised}")

    return _paper_from_lookup(
        result, doi=normalised, arxiv_id=None, provider="crossref"
    )


# ---------------------------------------------------------------------------
# fetch_paper_by_arxiv — arXiv ID input, no PDF
# ---------------------------------------------------------------------------


def fetch_paper_by_arxiv(
    arxiv_id: str,
    *,
    s2_api_key: str = "",
) -> PaperToWrite:
    """Build a metadata-only :class:`PaperToWrite` from an arXiv ID.

    Hits Semantic Scholar's ``arxiv:`` lookup. CrossRef is *not*
    consulted because arXiv preprints often lack DOIs at submission
    time; S2's index is the authoritative source.
    """
    normalised = normalize_arxiv(arxiv_id)
    if not normalised:
        raise ValueError(f"Invalid arXiv ID: {arxiv_id!r}")

    result = lookup_s2(f"arxiv:{normalised}", api_key=s2_api_key)
    if result is None:
        raise ValueError(f"arXiv lookup failed (S2 miss): {normalised}")

    # S2 frequently has the DOI for the published version; pick it up
    # so future ingests via DOI dedupe to the same ref.
    doi = result.get("doi") or None
    return _paper_from_lookup(result, doi=doi, arxiv_id=normalised, provider="s2")


# ---------------------------------------------------------------------------
# Shared body for the two metadata-only fetch paths
# ---------------------------------------------------------------------------


def _paper_from_lookup(
    result: dict[str, Any],
    *,
    doi: str | None,
    arxiv_id: str | None,
    provider: str,
) -> PaperToWrite:
    title = result.get("title", "")
    authors = result.get("authors", []) or []
    # Coerce to the dict form precis.identity expects.
    authors_dict = [a if isinstance(a, dict) else {"name": str(a)} for a in authors]
    year = result.get("year")
    abstract = result.get("abstract", "")
    keywords = result.get("keywords", []) or []

    paper_id, pub_id, cite_key_prefix = _resolve_identity(
        doi=doi,
        arxiv_id=arxiv_id,
        pdf_sha256=None,
        authors=authors_dict,
        year=year,
    )

    cards = _build_cards(
        title=title,
        authors=authors_dict,
        abstract=abstract,
        keywords=keywords,
    )

    extra: dict[str, Any] = {}
    if abstract:
        extra["abstract"] = abstract
    if result.get("journal"):
        extra["journal"] = result["journal"]
    if keywords:
        extra["keywords"] = keywords
    if result.get("s2_id"):
        extra["s2_id"] = result["s2_id"]

    return PaperToWrite(
        title=title,
        authors=authors_dict,
        year=year,
        kind="paper",
        provider=provider,
        set_by="system",
        paper_id=paper_id,
        pub_id=pub_id,
        cite_key_prefix=cite_key_prefix,
        doi=doi,
        arxiv_id=arxiv_id,
        s2_id=result.get("s2_id"),
        meta=extra,
        chunks=cards,
    )


__all__ = [
    "extract_paper",
    "fetch_paper_by_arxiv",
    "fetch_paper_by_doi",
]
