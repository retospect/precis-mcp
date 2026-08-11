"""Paper metadata enrichment — one Crossref (+OpenAlex) fetch per paper.

Legacy ``refs.authors`` rows are flat ``{"name"}`` strings in mixed
formats that no parser can reliably fix, but Crossref/OpenAlex return the
given/family split authoritatively for any paper with a DOI, and their
responses carry more than we currently keep: Crossref's document type
(``entry_type`` — ``journal-article``, ``proceedings-article``,
``book-chapter``, ``posted-content``, ``dissertation``, verbatim),
retraction/correction notices (``update-to``), ISSN, per-author ORCID
iDs, and (via OpenAlex) the PubMed/OpenAlex id cluster.
:func:`enrich_paper` is the per-ref choke point; :mod:`precis.workers.
paper_meta_enrich` is the scheduled batch sweep that claims rows and
calls it.

What lands where:

* **authors** — wholesale replaced via :func:`precis.utils.authors.
  normalize_authors` on the Crossref author list (also flushes junk
  entries) when a DOI resolves; a best-effort comma-split heuristic on
  the *existing* flat strings otherwise (no network). Skipped entirely
  when ``refs.human_verified_at`` is set — a hand correction is never
  clobbered (a verified paper still gets its other meta fields filled).
* **``meta.entry_type`` / ``meta.journal`` / ``meta.issn`` /
  ``meta.abstract``** — filled only when the ref doesn't already carry
  one (never clobbers a better existing value, or a human edit made via
  the web Meta tab's entry_type form on a paper this pass hasn't
  visited yet).
* **``ref_identifiers``** — OpenAlex's id cluster (``openalex``,
  ``pubmed``, ``mag``) is pulled *only* when a DOI resolved via Crossref
  (an OpenAlex-only fetch adds identifiers, nothing else in scope here)
  and only actually inserted for schemes the ref doesn't already carry
  (``insert_ref_identifiers`` is first-write-wins).
* **``refs.retraction_status/reason/url``** — a conservative read of the
  same Crossref response's ``update-to`` array (see
  :mod:`precis.ingest.provenance` for the full notice-ref-minting
  pipeline this deliberately doesn't replicate; this pass only sets the
  three columns).
* **per-author ORCID** — kept on the author dict, and a minimal
  ``kind='orcid'`` stub ref is minted (if missing) + linked to the paper
  via the ``authored`` relation (ADR 0039). Deliberately does **not**
  call the full ORCID Public API resolve
  (:func:`precis.ingest.orcid.fetch_record`) — that's a per-author
  network round-trip (and needs its own OAuth credentials) this pass's
  one-fetch-per-paper budget doesn't afford at corpus scale. A later
  ``get(kind='orcid', id=...)`` fleshes the stub out on demand.

Provenance + idempotency: every visited ref gets
``meta.authors_resolved_at`` stamped (regardless of outcome — a
Crossref miss/error still counts as "visited" so it isn't retried every
pass forever); ``meta.authors_source`` (``crossref`` | ``heuristic``) is
set only when authors were actually replaced. The worker pass selects on
``authors_resolved_at IS NULL``, so a second run over the same rows is a
no-op.

Ordering: the stamp is written **last**, only after the authors/meta
update, the ORCID mint+link, and the card rebuild have all completed —
not up front. ``_claim_batch`` re-selects on the same predicate, so a
ref left unstamped after a mid-way failure (network hiccup, a raised
exception in card rebuild, …) is simply retried next pass rather than
silently wedged half-enriched forever. Every side effect here is
itself safe to repeat (wholesale author replace, fill-blanks-only meta,
first-write-wins identifiers, get-or-create ORCID mint + idempotent
link, idempotent card rewrite) — a retry re-fetching Crossref once more
is the acceptable cost.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from precis.ingest import orcid as orcid_api
from precis.ingest.cards import ensure_abstract_card, rewrite_cards
from precis.ingest.crossref import _normalize as _crossref_normalize
from precis.ingest.crossref import fetch_message as _fetch_crossref_message
from precis.ingest.openalex_meta import _short_id as _short_openalex_id
from precis.ingest.openalex_meta import fetch_openalex_work
from precis.ingest.provenance import (
    RetractionStatus,
    classify_update_type,
    dominant_status,
)
from precis.store import Store
from precis.utils.authors import author_display, author_names, normalize_authors

log = logging.getLogger(__name__)

#: meta key marking a ref as visited by this pass — the idempotency gate.
#: Distinct from ``SOURCE_KEY`` because a human-verified paper is visited
#: (its other meta fields filled) without its authors ever being touched.
RESOLVED_AT_KEY = "authors_resolved_at"
#: meta key recording where the current ``refs.authors`` value came from.
SOURCE_KEY = "authors_source"

#: Raw (non-normalized) Crossref message fetch — injectable for tests.
CrossrefRawFn = Callable[[str, str], "dict[str, Any] | None"]
#: OpenAlex work-object fetch — injectable for tests.
OpenAlexFn = Callable[..., "dict[str, Any] | None"]


@dataclass
class EnrichOutcome:
    """What one :func:`enrich_paper` call actually did, for logging/tests."""

    ref_id: int
    authors_source: str | None = None
    entry_type: str | None = None
    retraction_status: str | None = None
    extra_identifiers: int = 0
    orcid_links: int = 0
    skipped_authors: bool = False
    cards_rebuilt: bool = False


def _orcid_lookup(msg: dict[str, Any]) -> dict[tuple[str, str], str]:
    """``{(family_lower, given_lower): bare_orcid}`` off a raw Crossref
    message's ``author`` array.

    Used to reattach ORCID iDs onto the already-normalized
    (:func:`precis.ingest.crossref._normalize`) author list without
    re-deriving its affiliation-filtering / editor-fallback logic here.
    """
    out: dict[tuple[str, str], str] = {}
    for a in msg.get("author") or []:
        family = (a.get("family") or "").strip().lower()
        given = (a.get("given") or "").strip().lower()
        orcid_url = (a.get("ORCID") or "").strip()
        if not orcid_url or (not family and not given):
            continue
        out[(family, given)] = orcid_url.rsplit("/", 1)[-1].strip()
    return out


def _extract_retraction(
    msg: dict[str, Any],
) -> tuple[RetractionStatus, str | None, str | None] | None:
    """Conservative retraction read off ``message.update-to``.

    Returns ``(status, reason, url)`` or ``None`` when no entry maps to a
    known ``refs.retraction_status`` value (see
    :func:`precis.ingest.provenance.classify_update_type` for the
    vocabulary) — never guesses.
    """
    statuses: list[RetractionStatus] = []
    notice_dois: list[str] = []
    for entry in msg.get("update-to") or []:
        raw_type = entry.get("type")
        if not raw_type:
            continue
        cls = classify_update_type(str(raw_type))
        if cls is None:
            continue
        _severity, status, _relation = cls
        if status is None:
            continue
        statuses.append(status)
        doi_val = entry.get("DOI")
        if doi_val:
            notice_dois.append(str(doi_val).strip().lower())
    if not statuses:
        return None
    applied = dominant_status(statuses)
    if applied is None:
        return None
    reason = "; ".join(notice_dois) if notice_dois else None
    url = f"https://doi.org/{notice_dois[0]}" if notice_dois else None
    return applied, reason, url


def _openalex_extra_ids(work: dict[str, Any]) -> list[tuple[str, str, str]]:
    """``(scheme, value, source)`` triples for identifiers only OpenAlex
    supplies here (PubMed, OpenAlex, MAG). The reconstructed abstract /
    topics land in ``meta.openalex`` via
    :func:`precis.ingest.openalex_meta.enrich_ref` separately (a distinct
    pass) — this only pulls the id cluster.
    """
    ids = work.get("ids") or {}
    out: list[tuple[str, str, str]] = []
    oa_id = _short_openalex_id(work.get("id") or ids.get("openalex"))
    if oa_id:
        out.append(("openalex", oa_id, "openalex"))
    pmid = ids.get("pmid")
    if pmid:
        bare = str(pmid).rstrip("/").rsplit("/", 1)[-1].strip()
        if bare:
            out.append(("pubmed", bare, "openalex"))
    mag = ids.get("mag")
    if mag:
        out.append(("mag", str(mag).strip(), "openalex"))
    return out


def _mint_and_link_orcid_authors(
    store: Store, paper_ref_id: int, authors: list[dict[str, Any]]
) -> int:
    """Mint a minimal ``kind='orcid'`` stub ref (if missing) per author
    carrying a fresh ``orcid`` id, and link it to the paper via the
    ``authored`` relation (src=author, dst=paper — ADR 0039), mirroring
    ``handlers/orcid.py::_link_authored``. See the module docstring for
    why this doesn't call the full ORCID Public API resolve.

    Explicitly get-or-create on both the mint (look up by slug before
    insert) and the link (``add_link`` is unique-constrained on
    ``(src, dst, relation)`` and no-ops on conflict) — a retry after a
    mid-way failure elsewhere in :func:`enrich_paper` (the
    ``authors_resolved_at`` stamp isn't written until every side effect
    succeeds) re-runs this safely: never a duplicate stub, never a
    duplicate edge.
    """
    linked = 0
    for a in authors:
        raw_orcid = a.get("orcid")
        if not isinstance(raw_orcid, str) or not raw_orcid.strip():
            continue
        try:
            orcid_id = orcid_api.normalize_orcid_id(raw_orcid)
        except Exception:
            continue  # malformed ORCID off Crossref — skip, don't fail the paper
        slug = orcid_api.slug_for(orcid_id)
        existing = store.get_ref(kind="orcid", id=slug)  # get-or-create: get
        if existing is None:
            display = author_display(a) or slug
            ref = store.insert_ref(  # get-or-create: create
                kind="orcid",
                slug=slug,
                title=display,
                provider="crossref",
                meta={"orcid_id": orcid_id},
            )
            orcid_ref_id = ref.id
        else:
            orcid_ref_id = existing.id
        if orcid_ref_id == paper_ref_id:
            continue
        # add_link is itself idempotent (ON CONFLICT DO UPDATE on the
        # unique (src, dst, relation) tuple) — a re-run never double-links.
        store.add_link(
            src_ref_id=orcid_ref_id,
            dst_ref_id=paper_ref_id,
            relation="authored",
            set_by="system",
            meta={"set_by": "paper-meta-enrich"},
        )
        linked += 1
    return linked


def _rebuild_cards(store: Store, ref_id: int) -> None:
    """Rewrite ``ref_id``'s derived search cards after an author/abstract
    change (mirrors ``workers/openalex_enrich.py::_rebuild_cards``)."""
    ref = store.fetch_refs_by_ids([ref_id]).get(ref_id)
    if ref is None:
        return
    meta = ref.meta or {}
    abstract = meta.get("abstract", "")
    abstract = abstract if isinstance(abstract, str) else ""
    authors_display = author_names(ref.authors)
    kw_raw = meta.get("keywords", [])
    keywords = list(kw_raw) if isinstance(kw_raw, list) else []
    with store.tx() as conn:
        rewrite_cards(
            conn,
            ref_id,
            title=ref.title or "",
            author_names=authors_display,
            abstract=abstract,
            keywords=keywords,
        )
        if abstract:
            ensure_abstract_card(conn, ref_id, set_by="system", abstract=abstract)


def enrich_paper(
    store: Store,
    ref_id: int,
    *,
    doi: str | None,
    mailto: str = "",
    email: str = "",
    now: datetime | None = None,
    crossref_fn: CrossrefRawFn | None = None,
    openalex_fn: OpenAlexFn | None = None,
    link_orcid: bool = True,
) -> EnrichOutcome | None:
    """Re-resolve one paper's metadata; returns ``None`` if the ref is gone.

    ``doi`` — the ref's stored DOI (or ``None``/blank for a DOI-less
    paper). One Crossref fetch when a DOI is present; OpenAlex is fetched
    *in addition* only when Crossref resolved (it contributes identifiers
    only, not a metadata fallback). A DOI that 404s/errors at Crossref
    degrades to the same heuristic path as a DOI-less paper — this
    pass's job is to converge the row, not retry forever; the
    ``authors_resolved_at`` stamp (always written) keeps it from being
    re-selected next pass either way.

    ``crossref_fn``/``openalex_fn`` default to ``None``, resolved to this
    module's ``_fetch_crossref_message`` / ``fetch_openalex_work`` *at
    call time* (not as a bound default value) so a test can
    ``monkeypatch`` those module-level names directly, mirroring
    ``workers/openalex_enrich.py``'s pattern.
    """
    fetch_crossref = crossref_fn or _fetch_crossref_message
    fetch_openalex = openalex_fn or fetch_openalex_work

    ref = store.fetch_refs_by_ids([ref_id]).get(ref_id)
    if ref is None:
        return None
    now = now or datetime.now(UTC)
    verified = ref.human_verified_at is not None
    current_meta = ref.meta or {}
    outcome = EnrichOutcome(ref_id=ref_id)

    # RESOLVED_AT_KEY is deliberately NOT seeded here — it's written last,
    # in its own small update after every side effect below has landed
    # (see the module docstring's "Ordering" note).
    meta_patch: dict[str, Any] = {}
    extra_ids: list[tuple[str, str, str]] = []
    retraction: tuple[RetractionStatus, str | None, str | None] | None = None
    crossref_authors: list[dict[str, Any]] | None = None
    crossref_orcid_authors: list[dict[str, Any]] = []

    msg: dict[str, Any] | None = None
    doi = (doi or "").strip() or None
    if doi:
        try:
            msg = fetch_crossref(doi, mailto)
        except Exception:
            log.exception(
                "paper_meta_enrich: crossref fetch failed for #%d (%s)", ref_id, doi
            )
            msg = None

    if doi and msg is not None:
        norm = _crossref_normalize(msg, doi)
        orcid_map = _orcid_lookup(msg)
        authors_raw = list(norm.get("authors") or [])
        for a in authors_raw:
            if isinstance(a, dict):
                key = (
                    (a.get("family") or "").strip().lower(),
                    (a.get("given") or "").strip().lower(),
                )
                orc = orcid_map.get(key)
                if orc:
                    a["orcid"] = orc
        candidate = normalize_authors(authors_raw)
        if candidate:
            crossref_authors = candidate
            crossref_orcid_authors = [a for a in candidate if a.get("orcid")]

        if norm.get("entry_type") and not current_meta.get("entry_type"):
            meta_patch["entry_type"] = norm["entry_type"]
            outcome.entry_type = norm["entry_type"]
        if norm.get("journal") and not current_meta.get("journal"):
            meta_patch["journal"] = norm["journal"]
        if norm.get("issn") and not current_meta.get("issn"):
            meta_patch["issn"] = norm["issn"]
        if norm.get("abstract") and not current_meta.get("abstract"):
            meta_patch["abstract"] = norm["abstract"]

        retraction = _extract_retraction(msg)
        if retraction is not None:
            outcome.retraction_status = retraction[0]

        try:
            work = fetch_openalex(doi, email=email)
        except Exception:
            work = None
        if work:
            extra_ids = _openalex_extra_ids(work)

    new_authors: list[dict[str, Any]] | None = None
    orcid_authors: list[dict[str, Any]] = []
    if verified:
        outcome.skipped_authors = True
    elif crossref_authors is not None:
        new_authors = crossref_authors
        outcome.authors_source = "crossref"
        orcid_authors = crossref_orcid_authors
    else:
        heuristic = normalize_authors(ref.authors)
        if heuristic:
            new_authors = heuristic
            outcome.authors_source = "heuristic"

    if outcome.authors_source:
        meta_patch[SOURCE_KEY] = outcome.authors_source

    with store.tx() as conn:
        store.update_paper_fields(
            ref_id,
            authors=new_authors,
            meta_patch=meta_patch,
            source="paper-meta-enrich",
            conn=conn,
        )
        if extra_ids:
            outcome.extra_identifiers = store.insert_ref_identifiers(
                ref_id, extra_ids, conn=conn
            )
        if retraction is not None:
            status, reason, url = retraction
            store.set_retraction_status(
                ref_id, status=status, reason=reason, url=url, conn=conn
            )

    if link_orcid and orcid_authors:
        outcome.orcid_links = _mint_and_link_orcid_authors(store, ref_id, orcid_authors)

    if new_authors or meta_patch.get("abstract"):
        _rebuild_cards(store, ref_id)
        outcome.cards_rebuilt = True

    # Stamp the idempotency marker LAST — see the module docstring's
    # "Ordering" note. If anything above raised, execution never reaches
    # here, the ref stays unstamped, and the next pass retries it (safe:
    # every side effect above is itself idempotent/get-or-create).
    store.update_paper_fields(
        ref_id,
        meta_patch={RESOLVED_AT_KEY: now.isoformat()},
        source="paper-meta-enrich",
    )

    return outcome


__all__ = [
    "RESOLVED_AT_KEY",
    "SOURCE_KEY",
    "EnrichOutcome",
    "enrich_paper",
]
