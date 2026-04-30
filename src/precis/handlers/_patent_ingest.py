"""Ingest pipeline for the ``patent`` kind.

Drives the fetch-as-ingest flow:

    parse_docdb_id(slug)
        ↓
    OpsClient.{biblio,description,claims}(docdb)
        ↓
    write XML to $PRECIS_PATENT_RAW_ROOT/<cc>/<num>/<kind>/*.xml
        ↓
    parse_patent(...)
        ↓
    Store.insert_ref('patent', slug=..., title=...)
        ↓
    Store.insert_blocks([description blocks, claim blocks])
        ↓
    fill_embeddings(...)  ← reuses the bundle-side helper
        ↓
    Store.add_tag(...) for each auto-tag (cpc:, ipc:, applicant:, …)
        ↓
    return IngestResult

The pipeline is idempotent: re-ingesting an existing slug returns
the existing ref and skips OPS calls. Force-refresh is a future
flag; the spec keeps it out of phase 1.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from precis.embedder import Embedder
from precis.errors import NotFound
from precis.handlers._patent_cql import slugify_applicant
from precis.handlers._patent_ops import (
    OpsClientProto,
    OpsNotFound,
)
from precis.handlers._patent_slug import DocDbId, parse_docdb_id
from precis.handlers._patent_xml import ParsedPatent, parse_patent
from precis.ingest import ParsedBlock, classify_density, fill_embeddings
from precis.store import Store, Tag
from precis.store.types import BlockInsert

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PatentIngestResult:
    """Outcome of one ingest pass."""

    ref_id: int
    slug: str
    docdb: DocDbId
    block_count: int
    inserted: bool  # False if the patent was already present
    bytes_fetched: int  # raw OPS body size, for fair-use accounting


# ---------------------------------------------------------------------------
# Disk-cache helpers
# ---------------------------------------------------------------------------


def _disk_dir(root: Path, docdb: DocDbId) -> Path:
    """Path to ``$ROOT/<cc>/<num>/<kind>/`` for this DOCDB id."""
    cc, num, kind_full = docdb.disk_subpath
    return root / cc / num / kind_full


def _write_xml(target: Path, xml: bytes) -> None:
    """Atomic write: tmp file + rename, parents created on demand."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(xml)
    tmp.replace(target)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def ingest_patent(
    docdb: str | DocDbId,
    *,
    store: Store,
    ops: OpsClientProto,
    embedder: Embedder | None,
    raw_root: Path,
    corpus_slug: str = "default",
) -> PatentIngestResult:
    """Fetch a patent from OPS, store it, embed it, return the result.

    Idempotent on the patent's slug: if the ref already exists, the
    method short-circuits without any OPS call.

    Args:
        docdb:        Either the canonical lowercased slug or a
                      pre-parsed ``DocDbId``.
        store:        Connected ``Store``. The caller owns its lifetime.
        ops:          Live or fake OPS client. Must implement
                      ``OpsClientProto``.
        embedder:     If provided, blocks are embedded; otherwise they
                      go in unembedded (semantic search will skip them).
        raw_root:     Directory where raw XML lands on disk
                      (``$PRECIS_PATENT_RAW_ROOT``).
        corpus_slug:  Corpus to insert the ref into. ``"default"``
                      matches the rest of the kinds.

    Raises:
        NotFound: OPS reports no such publication. No state mutated.
    """
    parsed_id = docdb if isinstance(docdb, DocDbId) else parse_docdb_id(docdb)
    slug = parsed_id.slug

    # Idempotency check — return early if we've already ingested this one.
    existing = store.get_ref(kind="patent", id=slug)
    if existing is not None:
        return PatentIngestResult(
            ref_id=existing.id,
            slug=slug,
            docdb=parsed_id,
            block_count=store.count_blocks(existing.id),
            inserted=False,
            bytes_fetched=0,
        )

    # Three OPS calls in sequence. We fetch all three on the first
    # ingest because they're cheap relative to the OAuth handshake
    # latency. If OPS returns 404 on biblio, we short-circuit — the
    # patent doesn't exist.
    try:
        biblio_xml = ops.biblio(slug)
    except OpsNotFound as e:
        raise NotFound(
            f"patent {parsed_id.display!r} not found at OPS",
            next="search(kind='patent', q='...') to find a different one",
        ) from e

    try:
        description_xml = ops.description(slug)
    except OpsNotFound:
        # Some EP applications publish biblio + claims but no full
        # description (e.g. early A1 applications). Treat as empty.
        description_xml = b""

    try:
        claims_xml = ops.claims(slug)
    except OpsNotFound:
        claims_xml = b""

    bytes_fetched = len(biblio_xml) + len(description_xml) + len(claims_xml)

    # Write each XML to disk before parsing — even if the parser
    # blows up later, we have the original artefacts for forensic
    # re-parse.
    disk_dir = _disk_dir(raw_root, parsed_id)
    _write_xml(disk_dir / "biblio.xml", biblio_xml)
    if description_xml:
        _write_xml(disk_dir / "description.xml", description_xml)
    if claims_xml:
        _write_xml(disk_dir / "claims.xml", claims_xml)

    parsed = parse_patent(
        biblio_xml=biblio_xml,
        description_xml=description_xml or None,
        claims_xml=claims_xml or None,
    )

    # Build block payloads. Description first (pos 0..N1), claims
    # after (pos N1+1..N2). Each gets density-classified; embeddings
    # are filled below if an embedder is configured.
    block_seeds: list[ParsedBlock] = []
    for txt in parsed.description_paragraphs:
        block_seeds.append(
            ParsedBlock(
                text=txt,
                embedding=None,
                density=classify_density(txt),
            )
        )
    for txt in parsed.claim_texts:
        block_seeds.append(
            ParsedBlock(
                text=txt,
                embedding=None,
                density=classify_density(txt),
            )
        )

    # Embed if we have an embedder. ``fill_embeddings`` is a no-op
    # for the empty list.
    if embedder is not None and block_seeds:
        block_seeds = fill_embeddings(block_seeds, embedder=embedder)

    # Build ref meta from the parsed structure. ``raw_meta`` keeps
    # the parsed view available without re-reading XML.
    # ``fair_use_bytes`` lets the watch runner sum a rolling 7-day
    # window via SQL without needing a side table — see
    # ``precis.jobs.patent_watch.compute_rolling_fair_use_bytes``.
    meta = _build_meta(parsed, parsed_id, fair_use_bytes=bytes_fetched)

    with store.tx() as conn:
        cid = store.ensure_corpus(corpus_slug)
        ref = store.insert_ref(
            corpus_id=cid,
            kind="patent",
            slug=slug,
            title=parsed.title,
            provider="epo_ops",
            meta=meta,
            conn=conn,
        )
        if block_seeds:
            inserts = [
                BlockInsert(
                    pos=i,
                    text=b.text,
                    embedding=b.embedding,
                    density=b.density,
                    token_count=len(b.text.split()),
                )
                for i, b in enumerate(block_seeds)
            ]
            store.insert_blocks(ref.id, inserts, conn=conn)

    # Auto-tags. Lowercase open prefixes — see
    # store/types.py::Tag.open() for the storage rule.
    _apply_auto_tags(store, ref.id, parsed, parsed_id)

    return PatentIngestResult(
        ref_id=ref.id,
        slug=slug,
        docdb=parsed_id,
        block_count=len(block_seeds),
        inserted=True,
        bytes_fetched=bytes_fetched,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_meta(
    parsed: ParsedPatent,
    docdb: DocDbId,
    *,
    fair_use_bytes: int = 0,
) -> dict:
    """Compose the ``refs.meta`` payload.

    Layout matches the spec at ``docs/patent-kind-spec.md``. We keep
    this in one place so the handler renderers can rely on stable
    keys.

    Args:
        fair_use_bytes: total raw OPS body bytes consumed to ingest
            this patent. Persisted so the watch runner can compute a
            rolling 7-day fair-use total via a single SQL aggregate.
    """
    return {
        "country": docdb.country,
        "kind_code": docdb.kind_full,
        "doc_number": docdb.number,
        "publication_date": parsed.publication_date,
        "application_date": parsed.application_date,
        "family_id": parsed.family_id,
        "applicants": parsed.applicants,
        "inventors": parsed.inventors,
        "cpc_classes": parsed.cpc_classes,
        "ipc_classes": parsed.ipc_classes,
        "abstract": parsed.abstract,
        "fair_use_bytes": fair_use_bytes,
    }


def _apply_auto_tags(
    store: Store,
    ref_id: int,
    parsed: ParsedPatent,
    docdb: DocDbId,
) -> None:
    """Drop the auto-tags onto the freshly-inserted ref.

    All tags are lowercase open prefixes; the ``ref_open_tags`` table
    enforces a CHECK constraint that the value is lowercase.
    """
    auto_tags: list[str] = [
        f"country:{docdb.country}",
        f"kind:{docdb.kind_full}",
    ]
    if parsed.family_id:
        auto_tags.append(f"family:{parsed.family_id.lower()}")
    for cpc in parsed.cpc_classes:
        auto_tags.append(f"cpc:{cpc.lower()}")
    for ipc in parsed.ipc_classes:
        # IPC values can include whitespace ('B01J 27/24') — slug it
        # by collapsing whitespace to nothing for storage.
        normalised = "".join(ipc.lower().split())
        if normalised:
            auto_tags.append(f"ipc:{normalised}")
    for app in parsed.applicants:
        name = app.get("name") if isinstance(app, dict) else None
        if isinstance(name, str) and name.strip():
            auto_tags.append(f"applicant:{slugify_applicant(name)}")

    for tag_str in auto_tags:
        try:
            tag = Tag.parse(tag_str)
            store.add_tag(ref_id, tag, set_by="system")
        except Exception:
            # Tags are best-effort metadata; never fail ingest on
            # one bad value.
            log.warning("patent ingest: skipped malformed tag %r", tag_str)


__all__ = ["PatentIngestResult", "ingest_patent"]
