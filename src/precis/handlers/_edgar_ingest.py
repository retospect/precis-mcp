"""Ingest pipeline for the ``edgar`` kind.

Drives the fetch-as-ingest flow (spec § "Mental model"):

    parse_accession(slug)
        ↓
    EdgarClient.submissions(cik)  → find the filing row
        ↓
    EdgarClient.filing_document(...) → primary document HTML
        ↓
    write raw to $PRECIS_EDGAR_RAW_ROOT/<cik>/<dashless>/{submission.json,primary.htm,ingest.log}
        ↓
    assemble_filing(...) → ParsedFiling (section-labelled blocks)
        ↓
    Store.insert_ref('edgar', slug=<accession dashed>, ...)
        ↓
    Store.insert_blocks([one block per paragraph, section labels on meta])
        ↓
    Store.add_tag(...) for each auto-tag (form:, cik:, fiscal-year:)
        ↓
    return EdgarIngestResult

Embeddings are populated **lazily** by the ``embed:bge-m3`` derived-queue
worker (ADR 0007) — never synchronously in the verb (spec § Divergences
item 6). The pipeline is idempotent on the accession slug: re-ingesting
an existing filing returns the existing ref and skips SEC calls.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from precis.embedder import Embedder
from precis.errors import NotFound
from precis.handlers._edgar_accession import Accession, parse_accession
from precis.handlers._edgar_client import EdgarClientProto, EdgarNotFound
from precis.handlers._edgar_parse import (
    ParsedFiling,
    assemble_filing,
    find_filing,
    parse_submissions,
)
from precis.ingest.blocks import classify_density
from precis.store import Store, Tag
from precis.store.types import BlockInsert

log = logging.getLogger(__name__)

#: chunk_kind stamped on every parsed filing block (spec § Section
#: tagging). A content kind, so the chunk_keywords / embed workers claim
#: it automatically — confirmed absent from their skip-lists.
EDGAR_CHUNK_KIND = "edgar_section"


@dataclass(frozen=True, slots=True)
class EdgarIngestResult:
    """Outcome of one ingest pass."""

    ref_id: int
    slug: str
    accession: Accession
    block_count: int
    inserted: bool  # False if the filing was already present
    bytes_fetched: int  # raw SEC body size, for fair-use accounting


# ---------------------------------------------------------------------------
# Disk-cache helpers
# ---------------------------------------------------------------------------


def _disk_dir(root: Path, accession: Accession) -> Path:
    """Path to ``$ROOT/<cik>/<dashless>/`` for this accession."""
    cik, dashless = accession.disk_subpath
    return root / cik / dashless


def _write_bytes(target: Path, data: bytes) -> None:
    """Atomic write: tmp file + rename, parents created on demand."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(target)


def _append_log(target: Path, entry: dict) -> None:
    """Append one JSONL line to the ingest log (best-effort)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def ingest_filing(
    accession: str | Accession,
    *,
    store: Store,
    client: EdgarClientProto,
    embedder: Embedder | None,
    raw_root: Path,
) -> EdgarIngestResult:
    """Fetch a filing from SEC EDGAR, store it, return the result.

    Idempotent on the accession slug: if the ref already exists, the
    method short-circuits without any SEC call.

    Args:
        accession: Canonical dashed slug or a pre-parsed ``Accession``.
        store:     Connected ``Store``. Caller owns its lifetime.
        client:    Live or fake EDGAR client (``EdgarClientProto``).
        embedder:  Accepted for signature compatibility but unused —
                   embeddings are populated lazily by the derived-queue
                   worker (ADR 0007). Callers may pass ``None``.
        raw_root:  Directory where raw artefacts land on disk
                   (``$PRECIS_EDGAR_RAW_ROOT``).

    Raises:
        NotFound: SEC reports no such filing / accession. No state
            mutated.
    """
    parsed = (
        accession if isinstance(accession, Accession) else parse_accession(accession)
    )
    slug = parsed.slug

    existing = store.get_ref(kind="edgar", id=slug)
    if existing is not None:
        return EdgarIngestResult(
            ref_id=existing.id,
            slug=slug,
            accession=parsed,
            block_count=store.count_blocks(existing.id),
            inserted=False,
            bytes_fetched=0,
        )

    # 1. Submissions index for the filer → locate the filing row.
    try:
        submissions_raw = client.submissions(parsed.cik)
    except EdgarNotFound as e:
        raise NotFound(
            f"no SEC filer with CIK {parsed.cik!r}",
            next="search(kind='edgar', q='...') to find a filing",
        ) from e

    subs = parse_submissions(submissions_raw)
    filing = find_filing(subs, slug)
    if filing is None:
        raise NotFound(
            f"accession {slug!r} not found in CIK {parsed.cik} recent filings",
            next="check the accession number, or search(kind='edgar', q='...')",
        )

    # 2. Primary filing document.
    try:
        primary_html = client.filing_document(
            cik=parsed.cik,
            accession_dashless=parsed.dashless,
            primary_doc=filing.primary_doc,
        )
    except EdgarNotFound as e:
        raise NotFound(
            f"primary document {filing.primary_doc!r} missing for {slug}",
            next="the filing may be an older text-only submission",
        ) from e

    bytes_fetched = len(submissions_raw) + len(primary_html)

    # 3. Mirror raw artefacts to disk before parsing.
    disk_dir = _disk_dir(raw_root, parsed)
    _write_bytes(disk_dir / "submission.json", submissions_raw)
    _write_bytes(disk_dir / "primary.htm", primary_html)
    _append_log(
        disk_dir / "ingest.log",
        {
            "ts": datetime.now(UTC).isoformat(),
            "accession": slug,
            "form": filing.form,
            "bytes": bytes_fetched,
        },
    )

    # 4. Parse into section-labelled blocks.
    parsed_filing = assemble_filing(
        filing=filing,
        company=subs.company,
        cik=subs.cik or parsed.cik,
        tickers=subs.tickers,
        primary_html=primary_html,
    )

    meta = _build_meta(parsed_filing, parsed, fair_use_bytes=bytes_fetched)

    block_inserts = _build_block_inserts(parsed_filing)

    with store.tx() as conn:
        ref = store.insert_ref(
            kind="edgar",
            slug=slug,
            title=parsed_filing.title,
            provider="sec_edgar",
            meta=meta,
            conn=conn,
        )
        if block_inserts:
            store.insert_blocks(ref.id, block_inserts, conn=conn)

    _apply_auto_tags(store, ref.id, parsed_filing)

    return EdgarIngestResult(
        ref_id=ref.id,
        slug=slug,
        accession=parsed,
        block_count=len(block_inserts),
        inserted=True,
        bytes_fetched=bytes_fetched,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_block_inserts(parsed: ParsedFiling) -> list[BlockInsert]:
    """One BlockInsert per parsed paragraph, section labels on meta.

    ``chunk_kind`` + ``section_path`` are popped by ``insert_blocks``
    into their dedicated columns; ``item_code`` / ``canonical_id`` stay
    in ``chunks.meta`` for the diff layer + section-scoped search.
    """
    inserts: list[BlockInsert] = []
    for i, fb in enumerate(parsed.blocks):
        inserts.append(
            BlockInsert(
                pos=i,
                text=fb.text,
                density=classify_density(fb.text),
                token_count=len(fb.text.split()),
                meta={
                    "chunk_kind": EDGAR_CHUNK_KIND,
                    "section_path": list(fb.section.section_path),
                    "item_code": fb.section.item_code,
                    "canonical_id": fb.section.canonical_id,
                },
            )
        )
    return inserts


def _build_meta(
    parsed: ParsedFiling,
    accession: Accession,
    *,
    fair_use_bytes: int = 0,
) -> dict:
    """Compose the ``refs.meta`` payload (spec § refs / blocks reuse)."""
    return {
        "accession": accession.dashed,
        "cik": parsed.cik or accession.cik,
        "company": parsed.company,
        "ticker": parsed.tickers[0] if parsed.tickers else None,
        "tickers": parsed.tickers,
        "form": parsed.form,
        "filed_date": parsed.filed_date,
        "period_of_report": parsed.period_of_report,
        "primary_doc": parsed.primary_doc,
        "items": parsed.items,
        "fair_use_bytes": fair_use_bytes,
    }


def _apply_auto_tags(store: Store, ref_id: int, parsed: ParsedFiling) -> None:
    """Drop auto-tags onto the freshly-inserted ref.

    All lowercase open prefixes (``ref_open_tags`` CHECK requires
    lowercase). Company name / ticker stay in ``refs.meta`` (structured
    JSONB), not as tag rows — same anti-clutter lesson as patent
    (spec § Closed-axis whitelist).
    """
    auto_tags: list[str] = []
    if parsed.form:
        auto_tags.append(f"form:{parsed.form.lower()}")
    if parsed.cik:
        auto_tags.append(f"cik:{parsed.cik}")
    fiscal = (parsed.period_of_report or parsed.filed_date or "")[:4]
    if fiscal.isdigit():
        auto_tags.append(f"fiscal-year:{fiscal}")

    for tag_str in auto_tags:
        try:
            store.add_tag(ref_id, Tag.parse(tag_str), set_by="system")
        except Exception:
            log.warning("edgar ingest: skipped malformed tag %r", tag_str)


__all__ = [
    "EDGAR_CHUNK_KIND",
    "EdgarIngestResult",
    "ingest_filing",
]
