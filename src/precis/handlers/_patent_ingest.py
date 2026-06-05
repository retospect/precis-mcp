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
from datetime import UTC, datetime, timedelta
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
from precis.ingest.blocks import ParsedBlock, classify_density
from precis.store import Store, Tag
from precis.store.types import BlockInsert

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Deferred full-text retry schedule
# ---------------------------------------------------------------------------

#: Open tag applied when OPS 404s description or claims at ingest.
#: The sweep job (``precis.jobs.patent_fulltext_sweep``) polls for this
#: tag and retries the missing endpoints on the schedule below.
AWAITING_FULLTEXT_TAG: str = "awaiting-fulltext"

#: Open tag applied when the sweep job has exhausted its retry budget
#: (publication older than :data:`FULLTEXT_GIVEUP_DAYS`). EPO rarely
#: back-fills full text after six months, so we stop polling to
#: preserve OPS quota for live work.
FULLTEXT_UNAVAILABLE_TAG: str = "fulltext-unavailable"

#: Base delay in days before the first retry after an ingest 404.
FULLTEXT_RETRY_BASE_DAYS: int = 7

#: Cap on the exponential-backoff window, in days. The sequence is
#: 7d → 14d → 28d → 56d and then stays at 56d.
FULLTEXT_RETRY_MAX_DAYS: int = 56

#: If the patent is this many days past its publication date and OPS
#: is *still* 404-ing, swap the awaiting tag for
#: :data:`FULLTEXT_UNAVAILABLE_TAG` and stop scheduling retries.
FULLTEXT_GIVEUP_DAYS: int = 183  # ~6 months


def next_fulltext_retry_at(*, now: datetime, retry_count: int) -> datetime:
    """Return the next retry timestamp given the current attempt count.

    ``retry_count`` is 0 on first scheduling (i.e. right after the
    ingest itself returned 404). The delay doubles each failed retry
    up to :data:`FULLTEXT_RETRY_MAX_DAYS`.
    """
    days = FULLTEXT_RETRY_BASE_DAYS * (2**retry_count)
    if days > FULLTEXT_RETRY_MAX_DAYS:
        days = FULLTEXT_RETRY_MAX_DAYS
    return now + timedelta(days=days)


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
        embedder:     Accepted for signature compatibility but unused.
                      Embeddings are now populated lazily by the
                      ``embed:bge-m3`` worker (ADR 0007 derived-queue);
                      synchronous embed during ingest blocked the verb
                      and diverged from paper-ingest. Callers may pass
                      ``None``; existing callers keep working.
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

    # Embeddings are populated lazily by the embed:bge-m3 worker
    # (ADR 0007 / AGENTS.md ingest-guarantees). Patent ingest used
    # to call ``fill_embeddings`` inline here; the synchronous path
    # blocked the verb and diverged from the paper-ingest flow.

    # Build ref meta from the parsed structure. ``raw_meta`` keeps
    # the parsed view available without re-reading XML.
    # ``fair_use_bytes`` lets the watch runner sum a rolling 7-day
    # window via SQL without needing a side table — see
    # ``precis.jobs.patent_watch.compute_rolling_fair_use_bytes``.
    # ``has_description`` / ``has_claims`` record whether OPS served
    # the full-text endpoints; some recent US / CN applications 404
    # on those until indexing completes (weeks to months post-
    # publication). When either is missing we schedule an automatic
    # retry via the awaiting-fulltext tag + sweep job (see
    # ``precis.jobs.patent_fulltext_sweep``).
    fulltext_missing = not description_xml or not claims_xml
    fulltext_retry_at: str | None = None
    if fulltext_missing:
        fulltext_retry_at = next_fulltext_retry_at(
            now=datetime.now(UTC),
            retry_count=0,
        ).isoformat()
    meta = _build_meta(
        parsed,
        parsed_id,
        fair_use_bytes=bytes_fetched,
        has_description=bool(description_xml),
        has_claims=bool(claims_xml),
        fulltext_retry_at=fulltext_retry_at,
        fulltext_retry_count=0 if fulltext_missing else None,
    )

    with store.tx() as conn:
        ref = store.insert_ref(
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

    # Queue an automatic full-text retry if either endpoint 404'd.
    # The sweep job (``precis.jobs.patent_fulltext_sweep``) picks
    # these up on its schedule, fetches the missing endpoints, and
    # replaces the placeholder blocks + tag on success.
    if fulltext_missing:
        try:
            store.add_tag(
                ref.id,
                Tag.open(AWAITING_FULLTEXT_TAG),
                set_by="system",
            )
        except Exception:
            # Best-effort — a failed tag write shouldn't roll back an
            # otherwise-successful ingest. The sweep job falls back to
            # a meta-only scan if the tag table is empty.
            log.warning(
                "patent ingest: failed to apply awaiting-fulltext tag to %s",
                slug,
            )

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
    has_description: bool = True,
    has_claims: bool = True,
    fulltext_retry_at: str | None = None,
    fulltext_retry_count: int | None = None,
) -> dict:
    """Compose the ``refs.meta`` payload.

    Layout matches the spec at ``docs/patent-kind-spec.md``. We keep
    this in one place so the handler renderers can rely on stable
    keys.

    Args:
        fair_use_bytes: total raw OPS body bytes consumed to ingest
            this patent. Persisted so the watch runner can compute a
            rolling 7-day fair-use total via a single SQL aggregate.
        has_description: True when OPS served the description
            endpoint. Renderers use this to explain an otherwise-
            opaque "0 blocks" on recent applications.
        has_claims: True when OPS served the claims endpoint.
        fulltext_retry_at: ISO-8601 timestamp at which the sweep job
            should next retry the missing full-text endpoints.
            ``None`` when full text is already present (no retry
            needed).
        fulltext_retry_count: Number of retries already attempted for
            this patent's full text. Drives the exponential backoff
            in :func:`next_fulltext_retry_at`. ``None`` when full
            text is already present.
    """
    meta: dict = {
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
        "has_description": has_description,
        "has_claims": has_claims,
    }
    # Retry bookkeeping only lands in meta when relevant — keeps the
    # row compact for fully-ingested patents (the common case).
    if fulltext_retry_at is not None:
        meta["fulltext_retry_at"] = fulltext_retry_at
    if fulltext_retry_count is not None:
        meta["fulltext_retry_count"] = fulltext_retry_count
    return meta


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


__all__ = [
    "AWAITING_FULLTEXT_TAG",
    "FULLTEXT_GIVEUP_DAYS",
    "FULLTEXT_RETRY_BASE_DAYS",
    "FULLTEXT_RETRY_MAX_DAYS",
    "FULLTEXT_UNAVAILABLE_TAG",
    "PatentIngestResult",
    "ingest_patent",
    "next_fulltext_retry_at",
]
