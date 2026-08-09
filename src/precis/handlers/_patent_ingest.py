"""Ingest pipeline for the ``patent`` kind.

Drives the fetch-as-ingest flow:

    parse_docdb_id(slug)
        ↓
    OpsClient.biblio(docdb)
        ↓
    parse_patent(biblio_xml=...) → family_id + priority_claims
        ↓
    simple-family stub decision (see below) → STUB or FULL
        ↓
    OpsClient.{description,claims}(docdb)  ← FULL path only
        ↓
    write XML to $PRECIS_PATENT_RAW_ROOT/<cc>/<num>/<kind>/*.xml
        ↓
    Store.insert_ref('patent', slug=..., title=...)
        ↓
    Store.insert_blocks([description blocks, claim blocks])  ← FULL path only
        ↓
    fill_embeddings(...)  ← reuses the bundle-side helper
        ↓
    Store.add_tag(...) for each auto-tag (cpc:, ipc:, applicant:, …)
        ↓
    return IngestResult

The pipeline is idempotent: re-ingesting an existing slug returns
the existing ref and skips OPS calls. Force-refresh is a future
flag; the spec keeps it out of phase 1.

**Patent-family mechanism** (Phase 2,
docs/backlog/patent-evidence-parity.md). Family identity is EPO-
authoritative data (the OPS biblio's DOCDB ``family-id`` attribute), not a
judged identity — so there's no hub-like node, just three light pieces:

1. ``meta['family_id']`` is stored on every patent ref whenever OPS's
   biblio carries one (:func:`_patent_xml._extract_family_id`); the key is
   simply absent — never ``null`` — when OPS didn't serve one, so every
   downstream reader must ``meta.get('family_id')``, never assume presence.
2. :mod:`precis.handlers._patent_family` is the deterministic read-side
   helper (family representative = earliest-published ingested member,
   cite-key-slug tiebreak) — reused by the cites view (Phase 3) and
   available to hub-refine, not wired into either yet.
3. **Simple-family stubbing**: a fresh ingest into a family that already
   has a fully-ingested (non-stub) member is ingested as a **stub** — refs
   row + full biblio meta, ``meta['family_stub'] = True``, NO description/
   claim blocks, plus a ``same-family-as`` link to the family's current
   representative — *iff* its priority-claim set is identical to the
   representative's (the DOCDB "simple family" test: same invention, no new
   matter). A differing priority-claim set (CIP/divisional — new matter,
   often later worked examples) or missing/unparseable priority data on
   either side always gets a FULL ingest: never stub on uncertainty, since
   losing worked examples is worse than some duplicate description text.
   Stubbing only fetches OPS's ``biblio`` endpoint (never ``description``/
   ``claims``) — no wasted OPS quota on text the ref won't store.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from precis.embedder import Embedder
from precis.errors import NotFound
from precis.handlers._patent_claims import (
    DESCRIPTION_BLOCK_META,
    claim_block_meta,
)
from precis.handlers._patent_family import family_members, family_representative
from precis.handlers._patent_ops import (
    OpsClientProto,
    OpsNotFound,
)
from precis.handlers._patent_slug import DocDbId, parse_docdb_id
from precis.handlers._patent_xml import ParsedPatent, parse_patent
from precis.ingest.blocks import ParsedBlock, classify_density
from precis.store import Ref, Store, Tag
from precis.store.types import BlockInsert

log = logging.getLogger(__name__)

#: Link relation from a stubbed patent ref to its family's current
#: representative (migration 0115). Symmetric, no inverse — see
#: ``store/types.py``'s ``Relation`` Literal entry for the write-door
#: FK-vocabulary note.
SAME_FAMILY_AS_RELATION = "same-family-as"

#: Ref meta flag marking a stub ingest (docstring above) — biblio only, no
#: description/claim blocks. Absent (never ``False``) on a normal full
#: ingest, matching the "absent means no" convention ``family_id`` uses.
FAMILY_STUB_META_KEY = "family_stub"


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


def _year_from_publication_date(publication_date: str | None) -> int | None:
    """Best-effort ``refs.year`` from a parsed ``YYYY-MM-DD`` publication
    date — the first-class column ``taproot/seniority.py`` orders evidence
    by (docs/backlog/patent-evidence-parity.md's "seniority gap" fix).
    Patent ingest used to omit ``year=`` on ``insert_ref``, so every patent
    ref sorted last (NULL) regardless of its real publication date.

    Returns ``None`` — never raises — when the date is absent or its
    leading 4 characters aren't a plausible year: a malformed OPS date
    string must degrade to "unknown year", not crash ingest.
    """
    if not publication_date or len(publication_date) < 4:
        return None
    candidate = publication_date[:4]
    if not candidate.isdigit():
        return None
    return int(candidate)


def _priority_claim_set(
    priority_claims: list[dict[str, str]] | None,
) -> frozenset[tuple[str, str, str]] | None:
    """Normalise parsed priority claims into a comparable set.

    Each entry becomes ``(country, doc_number, date)`` — uppercased
    country, ``date`` defaulting to ``""`` when OPS didn't carry one (a
    missing date still participates in the comparison; it just can't
    distinguish two claims that share a country + number).

    Returns ``None`` — "priority data missing/unparseable" — for an empty
    or falsy list, never an empty frozenset, so the simple-family stub
    decision (:func:`ingest_patent`) can tell "no priority claims on this
    biblio" apart from "priority claims present but the sets differ": both
    read as "not equal," but only the former is fine to compare as a
    *set*, and comparing two ``None``s must never spuriously match.
    """
    if not priority_claims:
        return None
    return frozenset(
        (
            (c.get("country") or "").upper(),
            c.get("doc_number") or "",
            c.get("date") or "",
        )
        for c in priority_claims
    )


def _find_stub_target(
    store: Store, *, family_id: str | None, priority_claims: list[dict[str, str]]
) -> Ref | None:
    """The family representative to stub this ingest against, or ``None``
    to force a full ingest.

    ``None`` whenever any of the "never stub on uncertainty" conditions
    hold: no ``family_id`` on the incoming biblio, no already-ingested
    *full* (non-stub) family member yet (this is the family's first
    ingested member — it always gets a full ingest), or either side's
    priority-claim set is missing/unparseable/different from the
    representative's. A non-``None`` return means "same DOCDB simple
    family as the representative" — the caller stubs against it.
    """
    if not family_id:
        return None
    members = family_members(store, family_id)
    full_members = [m for m in members if not (m.meta or {}).get(FAMILY_STUB_META_KEY)]
    if not full_members:
        return None
    representative = family_representative(store, family_id)
    if representative is None:
        return None
    new_set = _priority_claim_set(priority_claims)
    rep_set = _priority_claim_set((representative.meta or {}).get("priority_claims"))
    if new_set is None or rep_set is None or new_set != rep_set:
        return None
    return representative


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
    force: bool = False,
) -> PatentIngestResult:
    """Fetch a patent from OPS, store it, embed it, return the result.

    Idempotent on the patent's slug: if the ref already exists, the
    method short-circuits without any OPS call — **unless** ``force`` is
    set.

    Args:
        docdb:        Either the canonical lowercased slug or a
                      pre-parsed ``DocDbId``.
        store:        Connected ``Store``. The caller owns its lifetime.
        ops:          Live or fake OPS client. Must implement
                      ``OpsClientProto``.
        embedder:     Accepted for signature compatibility but unused.
                      Embeddings are now populated lazily by the
                      ``embed:bge-m3`` worker (derived-queue);
                      synchronous embed during ingest blocked the verb
                      and diverged from paper-ingest. Callers may pass
                      ``None``; existing callers keep working.
        raw_root:     Directory where raw XML lands on disk
                      (``$PRECIS_PATENT_RAW_ROOT``).
        corpus_slug:  Corpus to insert the ref into. ``"default"``
                      matches the rest of the kinds.
        force:        Re-fetch and **re-ingest an existing** patent —
                      re-run OPS + ``parse_patent`` and DELETE+re-INSERT
                      the ref's blocks (``insert_blocks(replace=True)``)
                      so they carry the current block metadata, notably
                      the slice-1 ``patent_block`` claim markers the
                      freedom-to-operate digest reads
                      (docs/backlog/patent-authoring-loop.md). Used by the
                      claim-marking backfill (``precis jobs
                      reingest-patents``) to re-mark patents ingested
                      before the marker existed. The ref itself is kept
                      (id, links, tags preserved); only its meta + blocks
                      are refreshed. No-op for a slug that doesn't exist
                      yet — it ingests fresh, same as ``force=False``.

    Raises:
        NotFound: OPS reports no such publication. No state mutated.
    """
    parsed_id = docdb if isinstance(docdb, DocDbId) else parse_docdb_id(docdb)
    slug = parsed_id.slug

    # Idempotency check — return early if we've already ingested this one,
    # unless the caller asked to re-ingest (``force``), in which case we
    # keep the ref but refresh its meta + blocks below.
    existing = store.get_ref(kind="patent", id=slug)
    reingest = existing is not None and force
    if existing is not None and not force:
        return PatentIngestResult(
            ref_id=existing.id,
            slug=slug,
            docdb=parsed_id,
            block_count=store.count_blocks(existing.id),
            inserted=False,
            bytes_fetched=0,
        )

    # Biblio always fetched first — every downstream decision (title, the
    # simple-family stub call below) reads it, and if OPS 404s here the
    # patent doesn't exist at all.
    try:
        biblio_xml = ops.biblio(slug)
    except OpsNotFound as e:
        raise NotFound(
            f"patent {parsed_id.display!r} not found at OPS",
            next="search(kind='patent', q='...') to find a different one",
        ) from e

    disk_dir = _disk_dir(raw_root, parsed_id)

    # Simple-family stubbing decision (docs/backlog/patent-evidence-
    # parity.md Phase 2, module docstring above). Only considered for a
    # genuinely fresh ingest — a ``force`` re-ingest of an existing ref
    # always takes the full path (explicit caller intent to (re)populate
    # blocks overrides the heuristic). Deciding from the biblio alone,
    # before fetching description/claims, means a stub never pays for OPS
    # full-text quota it's about to throw away.
    biblio_parsed = parse_patent(biblio_xml=biblio_xml)
    stub_target = (
        None
        if reingest
        else _find_stub_target(
            store,
            family_id=biblio_parsed.family_id,
            priority_claims=biblio_parsed.priority_claims,
        )
    )

    if stub_target is not None:
        bytes_fetched = len(biblio_xml)
        _write_xml(disk_dir / "biblio.xml", biblio_xml)
        # ``has_description=False, has_claims=False`` accurately reflects
        # that a stub carries no blocks — but with no ``fulltext_retry_at``
        # (the default), so the fulltext-sweep job never touches it. A
        # stub is a permanent, deliberate omission, not "OPS hasn't
        # indexed this yet" — those two must never share the retry queue.
        meta = _build_meta(
            biblio_parsed,
            parsed_id,
            fair_use_bytes=bytes_fetched,
            has_description=False,
            has_claims=False,
        )
        meta[FAMILY_STUB_META_KEY] = True
        with store.tx() as conn:
            ref = store.insert_ref(
                kind="patent",
                slug=slug,
                title=biblio_parsed.title,
                provider="epo_ops",
                meta=meta,
                year=_year_from_publication_date(biblio_parsed.publication_date),
                conn=conn,
            )
            ref_id = ref.id
            store.add_link(
                src_ref_id=ref_id,
                dst_ref_id=stub_target.id,
                relation=SAME_FAMILY_AS_RELATION,
                set_by="system",
                conn=conn,
            )
        _apply_auto_tags(store, ref_id, biblio_parsed, parsed_id)
        return PatentIngestResult(
            ref_id=ref_id,
            slug=slug,
            docdb=parsed_id,
            block_count=0,
            inserted=True,
            bytes_fetched=bytes_fetched,
        )

    # Full ingest: fetch description + claims too. We fetch both on the
    # first ingest because they're cheap relative to the OAuth handshake
    # latency.
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
    # are filled below if an embedder is configured. Each block also
    # carries a ``chunks.meta`` marker (``patent_block`` = description |
    # claim) so ``view='claims'`` can retrieve claims on their own and
    # the freedom-to-operate loop can address a single prior-art claim
    # (docs/backlog/patent-authoring-loop.md). Claim blocks additionally
    # record the derived independent/dependent structure.
    block_seeds: list[ParsedBlock] = []
    block_metas: list[dict[str, Any]] = []
    for txt in parsed.description_paragraphs:
        block_seeds.append(
            ParsedBlock(
                text=txt,
                embedding=None,
                density=classify_density(txt),
            )
        )
        block_metas.append(dict(DESCRIPTION_BLOCK_META))
    for claim_idx, txt in enumerate(parsed.claim_texts):
        block_seeds.append(
            ParsedBlock(
                text=txt,
                embedding=None,
                density=classify_density(txt),
            )
        )
        block_metas.append(claim_block_meta(txt, claim_idx + 1))

    # Embeddings are populated lazily by the embed:bge-m3 worker
    # (the derived queue / AGENTS.md ingest-guarantees). Patent ingest used
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

    # Re-ingest guard: never clobber a ref that another source already
    # populated. OPS DOCDB serves full text mainly for EP/WO/US; a CN/KR/JP
    # patent's body typically comes from the patents.google.com fallback
    # (``fetch_google_patents``), and OPS returns *empty* description+claims
    # for it. Overwriting that ref's meta with ``has_*=false`` +
    # re-scheduling an awaiting-fulltext retry would falsely mark a
    # fully-populated patent as missing (and churn the fulltext sweep). So
    # when a re-ingest produces no blocks, leave the existing ref entirely
    # as-is. (A *fresh* ingest with no blocks still records the stub —
    # that's the recent-application-awaiting-indexing case, handled below.)
    if reingest and not block_seeds:
        return PatentIngestResult(
            ref_id=existing.id,  # type: ignore[union-attr]
            slug=slug,
            docdb=parsed_id,
            block_count=store.count_blocks(existing.id),  # type: ignore[union-attr]
            inserted=False,
            bytes_fetched=bytes_fetched,
        )

    with store.tx() as conn:
        if reingest:
            # Keep the existing ref (id / links / history); refresh its
            # meta and swap its blocks in place. ``stamp_ref_meta`` is a
            # shallow merge, so freshly-served full text flips the stale
            # ``has_claims=false`` from an old stub-ingest to true.
            assert existing is not None  # narrowed by ``reingest``
            ref_id = existing.id
            if (existing.meta or {}).get(FAMILY_STUB_META_KEY):
                # A forced re-ingest of a former simple-family STUB just
                # produced real blocks (the ``block_seeds`` early-return
                # above already handled "still no content") — clear the
                # flag explicitly so the shallow merge below doesn't leave
                # a stale ``family_stub: true`` on a now fully-ingested
                # ref (``stamp_ref_meta`` only overlays keys present in
                # ``meta``; it can't unset one on its own).
                meta[FAMILY_STUB_META_KEY] = False
            store.stamp_ref_meta(ref_id, meta, conn=conn)
            # ``stamp_ref_meta`` only touches the ``meta`` JSONB, never the
            # first-class ``refs.year`` column -- so a ref ingested before
            # the seniority-gap fix (module docstring, docs/backlog/
            # patent-evidence-parity.md) stayed year=NULL forever, even
            # through an operator force-reingest meant to repair it.
            # ``update_paper_fields`` is the sole write path for that
            # column; a ``None`` year (unparseable/missing publication
            # date) leaves the existing value untouched via its own
            # COALESCE.
            store.update_paper_fields(
                ref_id,
                year=_year_from_publication_date(parsed.publication_date),
                source="patent-reingest",
                conn=conn,
            )
        else:
            ref = store.insert_ref(
                kind="patent",
                slug=slug,
                title=parsed.title,
                provider="epo_ops",
                meta=meta,
                year=_year_from_publication_date(parsed.publication_date),
                conn=conn,
            )
            ref_id = ref.id
        if block_seeds:
            inserts = [
                BlockInsert(
                    pos=i,
                    text=b.text,
                    embedding=b.embedding,
                    density=b.density,
                    token_count=len(b.text.split()),
                    meta=dict(block_metas[i]),
                )
                for i, b in enumerate(block_seeds)
            ]
            # ``replace=True`` on a re-ingest DELETEs the ref's existing
            # chunks first (cascading to embeddings/summaries/tags) so the
            # embed / keyword / classify workers re-derive over the newly
            # marked blocks — an in-place marker patch would leave stale
            # derived rows (AGENTS.md "don't mutate body chunks").
            store.insert_blocks(ref_id, inserts, replace=reingest, conn=conn)

    # Auto-tags. Lowercase open prefixes — see
    # store/types.py::Tag.open() for the storage rule.
    _apply_auto_tags(store, ref_id, parsed, parsed_id)

    # On a re-ingest that now has full text, clear the awaiting/unavailable
    # full-text tags left by an earlier stub ingest — the retry loop is
    # done for this patent.
    if reingest and not fulltext_missing:
        for tag_str in (AWAITING_FULLTEXT_TAG, FULLTEXT_UNAVAILABLE_TAG):
            try:
                store.remove_tag(ref_id, Tag.open(tag_str))
            except Exception:  # pragma: no cover — best-effort cleanup
                log.warning(
                    "patent reingest: failed to clear %s tag on %s",
                    tag_str,
                    slug,
                )

    # Queue an automatic full-text retry if either endpoint 404'd.
    # The sweep job (``precis.jobs.patent_fulltext_sweep``) picks
    # these up on its schedule, fetches the missing endpoints, and
    # replaces the placeholder blocks + tag on success.
    if fulltext_missing:
        try:
            store.add_tag(
                ref_id,
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
        ref_id=ref_id,
        slug=slug,
        docdb=parsed_id,
        block_count=len(block_seeds),
        inserted=not reingest,
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

    Layout matches the spec at ``docs/user-facing/patent-kind-spec.md``. We keep
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
        "applicants": parsed.applicants,
        "inventors": parsed.inventors,
        "cpc_classes": parsed.cpc_classes,
        "ipc_classes": parsed.ipc_classes,
        "abstract": parsed.abstract,
        "fair_use_bytes": fair_use_bytes,
        "has_description": has_description,
        "has_claims": has_claims,
    }
    # ``family_id`` / ``priority_claims`` are absent from meta entirely —
    # never a ``null`` / ``[]`` placeholder — when OPS's biblio didn't
    # carry them (design applications, some national-only filings, an
    # older/degraded OPS response). Every reader (auto-tags,
    # ``_patent_family.py``, the simple-family stub decision) already
    # goes through ``meta.get(...)``, so "absent" and "None" read
    # identically; keeping the key out entirely just keeps the row
    # compact for the common (both present) case, matching the
    # ``fulltext_retry_at`` convention below.
    if parsed.family_id:
        meta["family_id"] = parsed.family_id
    if parsed.priority_claims:
        meta["priority_claims"] = parsed.priority_claims
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

    **Removed 2026-06-16**: ``applicant:*``, ``cpc:*``, ``ipc:*`` used
    to be auto-tagged here too — denormalised indices for the OPS CQL
    lift. They clutter the global tag table (one row per Chinese-
    university applicant, one per IPC subclass like ``g06n3/12ai``) and
    the canonical data is already in ``refs.meta`` as structured JSONB.
    The CQL lift now queries ``meta @> '{...}'`` directly; see
    :mod:`precis.handlers._patent_cql`. ``country:``, ``kind:`` and
    ``family:`` stay — they're short, distinct, and useful as plain
    tag filters in the dashboard.
    """
    auto_tags: list[str] = [
        f"country:{docdb.country}",
        f"kind:{docdb.kind_full}",
    ]
    if parsed.family_id:
        auto_tags.append(f"family:{parsed.family_id.lower()}")

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
