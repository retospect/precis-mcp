"""Preprint → published upgrade (gr690851).

A cited preprint is a permanent hole in provenance: it carries no Crossref
DOI (only an arXiv DataCite mint, or none), so the Crossref-keyed
retraction watch can never form an opinion on it — it stays "never
checked" forever. When the peer-reviewed version exists we want it: an
authoritative DOI, final metadata, and the retraction/correction record.
This module is the discovery-and-upgrade half of that fix; the read half
lives in :mod:`precis.taproot.cite` (hub cite resolution resolves a
superseded preprint's cite_key through the ``supersedes`` link to the
published paper) and :mod:`precis.export.retraction` (the draft's
retraction walk follows the same link, so the watch checks the published
DOI).

Ruled defaults (Reto, 2026-11-19):

* **Detection** — a paper ref is a preprint candidate when it carries an
  arXiv identifier, when its only DOI is the ``10.48550/arXiv.*`` DataCite
  mint, or when it has no DOI at all.
* **Discovery, two lanes only** — (a) S2's explicit linkage: batch-resolve
  the ref by its S2/arXiv id and take the real DOI off the record's
  ``externalIds``; (b) a high-confidence title match reusing
  :func:`precis.ingest.metadata_resolve._resolve_by_title`'s gates
  (trigram sim ≥ 0.85, year-compatible, body-corroboration and DOI-owner
  checks included). Never a weak match: a wrong upgrade silently rewrites
  a citation.
* **Upgrade** — find-or-mint the published paper as a first-class paper
  ref (``Store.upsert_stub_paper`` collapses on the DOI, so a re-run never
  mints a duplicate), link ``published --supersedes--> preprint``, and
  soft-retire the preprint: a ``STATUS:superseded`` tag plus the link, the
  ref itself kept — provenance, chunks and historical chains stay
  resolvable. The preprint keeps its arXiv id and mint DOI; the published
  DOI is never written onto it (that would re-create the single-ref
  ambiguity this split exists to end).
* **What does NOT move** — the draft's chunk anchors stay on the
  preprint's held text; only the bibliographic citation resolves through
  the supersede link. Re-pinning anchors to the published paper's chunks
  is a deliberate authoring step, never automatic.

Idempotence: every attempted ref is stamped ``meta.preprint_upgrade_checked_at``
(so a no-discovery preprint is not re-probed every pass) and upgraded refs
are excluded by their incoming ``supersedes`` edge; the link and tag
writes are conflict-idempotent. The stamp re-arms after
:data:`_RECHECK_DAYS` so a preprint published *later* still upgrades.
The sweep defers wholesale on S2 rate-limit exhaustion (the next pass
retries), never counting a refusal as a failure (gr503080's contract).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Any

from precis.errors import Conflict
from precis.identity import (
    arxiv_id_of_doi,
    doi_twins,
    is_placeholder_title,
    normalize_doi,
)
from precis.ingest.crossref import _normalize, fetch_message
from precis.ingest.doi_backfill import BatchFn, _is_real_doi, _request_id
from precis.ingest.metadata_resolve import (
    _AUTO_SIM,
    S2Fn,
    _resolve_by_title,
    _similarity,
    _years_compatible,
)
from precis.ingest.pdf_sidecar import is_garbage_title, is_pii
from precis.ingest.semantic_scholar import S2RateLimited, get_papers_batch, lookup_s2
from precis.liveness import drain_sleep
from precis.store import Store, Tag
from precis.utils.rate_limit import RateLimited

log = logging.getLogger(__name__)

#: Source tag / event source stamped on every write this module makes.
SOURCE = "preprint-upgrade"

#: The soft-retire marker — a STATUS tag on the kept preprint ref (the
#: ruling's "STATUS tag plus supersede link", ref NOT retired).
SUPERSEDED_TAG = Tag.closed("STATUS", "superseded")

#: How long a no-discovery stamp holds before the sweep re-probes the ref
#: (a preprint published later still upgrades).
_RECHECK_DAYS = 30
_BATCH_LOOKUP = partial(get_papers_batch, propagate=True)
_PUBLISHED_TYPES = frozenset({"journal-article", "proceedings-article", "book-chapter"})
CrossrefWorkFn = Callable[[str], dict[str, Any] | None]

#: Outcome verbs for one ref's upgrade attempt.
V_UPGRADED = "upgraded"
V_NO_PUBLISHED = "no-published-version"
V_SKIPPED = "skipped"
V_REVIEW = "review"
V_DEFERRED = "deferred"
V_ERROR = "error"


@dataclass(frozen=True)
class UpgradeOutcome:
    """One ref's upgrade attempt."""

    ref_id: int
    verdict: str  # one of the V_* verbs
    published_ref_id: int | None = None
    doi: str | None = None
    reason: str = ""

    def line(self) -> str:
        got = f" -> pa{self.published_ref_id} ({self.doi})" if self.doi else ""
        return f"[{self.verdict}] #{self.ref_id} {self.reason}{got}"


@dataclass
class SweepResult:
    """Per-outcome tallies + the per-ref detail for one sweep."""

    outcomes: list[UpgradeOutcome] = field(default_factory=list)

    def count(self, verdict: str) -> int:
        return sum(1 for o in self.outcomes if o.verdict == verdict)


@dataclass(frozen=True)
class _Discovery:
    """The published version found for one preprint."""

    doi: str
    title: str
    year: int | None
    authors: list[dict[str, str]]
    metadata: dict[str, Any]
    via: str  # 's2-external-ids' | 's2-title-match'


def is_preprint(ids: Mapping[str, str]) -> bool:
    """True when an identifier bucket marks its ref as a preprint candidate
    (gr690851's detection rule): an arXiv identifier, only a
    ``10.48550/arXiv.*`` mint DOI, or no DOI at all. A ref holding any
    real (Crossref-usable) DOI alongside the mint is not a hole — the
    retraction watch can already check it."""
    if ids.get("arxiv"):
        return True
    doi = (ids.get("doi") or "").strip()
    if not doi:
        return True
    return not _is_real_doi(doi)


def _usable_title(ref: Any) -> str | None:
    """The ref's stored title when it can serve as a trusted title-match
    query — independent ground truth, so query-vs-hit similarity genuinely
    validates the hit (the metadata_resolve Track-2 contract)."""
    t = (getattr(ref, "title", None) or "").strip()
    if not t or is_placeholder_title(t) or is_garbage_title(t) or is_pii(t):
        return None
    return t


def _published_work(
    doi: str, via: str, crossref_fn: CrossrefWorkFn
) -> _Discovery | None:
    canonical = normalize_doi(doi)
    if canonical is None or not _is_real_doi(canonical):
        return None
    doi = canonical
    work = crossref_fn(doi)
    if (
        not work
        or work.get("type") not in _PUBLISHED_TYPES
        or normalize_doi(str(work.get("DOI") or "")) != doi
    ):
        return None
    meta = _normalize(work, doi)
    return _Discovery(
        doi=doi,
        title=meta["title"],
        year=meta["year"],
        authors=meta["authors"],
        metadata={
            key: meta[key]
            for key in (
                "journal",
                "issn",
                "entry_type",
                "volume",
                "issue",
                "pages",
                "publisher",
                "isbn",
                "editors",
                "container_title",
            )
            if meta.get(key)
        },
        via=via,
    )


def _discover(
    store: Store,
    ref: Any,
    *,
    api_key: str,
    batch_fn: BatchFn,
    s2_fn: S2Fn,
    crossref_fn: CrossrefWorkFn,
    call_timeout: float,
) -> tuple[_Discovery | None, str]:
    """Hunt the published version. Returns ``(discovery, reason)`` —
    ``(None, '')`` when a lane is simply inapplicable (no id / no title),
    ``(None, <why>)`` when the lanes ran and found nothing usable, and a
    :class:`_Discovery` on success. ``S2RateLimited`` propagates so the
    sweep can defer the whole pass."""

    # ── Lane A: S2's explicit id linkage ────────────────────────────────
    rid = _request_id(store, ref.id)
    if not rid:
        ids = store.identifiers_for_refs([ref.id]).get(ref.id, {})
        arxiv = arxiv_id_of_doi(ids.get("doi"))
        if arxiv:
            rid = f"ARXIV:{arxiv}"
    if rid:
        metas = batch_fn([rid], api_key)
        meta = metas[0] if metas else None
        doi = (meta or {}).get("doi")
        if _is_real_doi(doi):
            work = _published_work(str(doi), "s2-external-ids", crossref_fn)
            if work is not None:
                return work, ""

    # ── Lane B: high-confidence title match (metadata_resolve gates) ────
    title = _usable_title(ref)
    if not title:
        return None, "no-usable-title"
    res = _resolve_by_title(
        store,
        ref,
        candidates=[title],
        trusted_title=True,
        s2_fn=s2_fn,
        s2_api_key=api_key,
        call_timeout=call_timeout,
    )
    if res.verdict == "auto" and _is_real_doi(res.doi):
        work = _published_work(str(res.doi), "s2-title-match", crossref_fn)
        if (
            work is not None
            and work.title
            and _similarity(store, title, work.title) >= _AUTO_SIM
            and _years_compatible(ref.year, work.year)
        ):
            return work, ""
        return None, "no-confirmed-title-match"
    if res.verdict in ("review", "parked"):
        return None, f"title-match-not-auto:{res.reason}"
    return None, "no-published-version"


def upgrade_preprint(
    store: Store,
    ref_id: int,
    *,
    apply: bool = True,
    api_key: str = "",
    batch_fn: BatchFn = _BATCH_LOOKUP,
    s2_fn: S2Fn = lookup_s2,
    crossref_fn: CrossrefWorkFn = fetch_message,
    call_timeout: float = 20.0,
) -> UpgradeOutcome:
    """Attempt the upgrade for one preprint ref. ``apply=False`` plans
    (network + verdicts) without writing; ``apply=True`` performs the
    find-or-mint + link + tag + stamps in one transaction.

    Idempotent: the published ref collapses on its DOI, the link and tag
    are conflict-stable, and a no-discovery attempt only (re)stamps the
    checked marker."""
    ref = store.fetch_refs_by_ids([ref_id], include_deleted=False).get(ref_id)
    if ref is None or ref.kind != "paper":
        return UpgradeOutcome(ref_id, V_SKIPPED, reason="not-live")
    existing = store.supersede_targets_bulk([ref_id]).get(ref_id)
    if existing is not None:
        return UpgradeOutcome(ref_id, V_SKIPPED, existing, reason="already-superseded")
    if getattr(ref, "human_verified_at", None) is not None:
        # A human vetted this identity — the cite upgrade is a deliberate
        # authoring step for them too, not a sweep decision.
        return UpgradeOutcome(ref_id, V_SKIPPED, reason="human-verified")
    ids = store.identifiers_for_refs([ref_id]).get(ref_id, {})
    if not is_preprint(ids):
        return UpgradeOutcome(ref_id, V_SKIPPED, reason="has-real-doi")

    discovery, reason = _discover(
        store,
        ref,
        api_key=api_key,
        batch_fn=batch_fn,
        s2_fn=s2_fn,
        crossref_fn=crossref_fn,
        call_timeout=call_timeout,
    )
    if discovery is None:
        if apply:
            store.stamp_ref_meta(
                ref_id, {"preprint_upgrade_checked_at": datetime.now(UTC).isoformat()}
            )
        return UpgradeOutcome(ref_id, V_NO_PUBLISHED, reason=reason)

    if not apply:
        return UpgradeOutcome(
            ref_id, V_UPGRADED, doi=discovery.doi, reason=f"planned via {discovery.via}"
        )

    now = datetime.now(UTC).isoformat()
    try:
        with store.tx() as conn:
            # A second statement sees the winner's committed link after the
            # row lock is acquired, even if discovery raced outside the lock.
            live = conn.execute(
                "SELECT human_verified_at FROM refs WHERE ref_id = %s "
                "AND kind = 'paper' AND retired_at IS NULL FOR NO KEY UPDATE",
                (ref_id,),
            ).fetchone()
            if live is None or live[0] is not None:
                return UpgradeOutcome(ref_id, V_SKIPPED, reason="ineligible")
            existing_link = conn.execute(
                "SELECT src_ref_id FROM links WHERE relation = 'supersedes' "
                "AND dst_ref_id = %s ORDER BY src_ref_id LIMIT 1",
                (ref_id,),
            ).fetchone()
            if existing_link is not None:
                return UpgradeOutcome(
                    ref_id,
                    V_SKIPPED,
                    int(existing_link[0]),
                    reason="already-superseded",
                )
            # Distinct preprints can independently find the same DOI. Lock
            # the DOI family before the stub's identifier probe and mint.
            doi_key = min((discovery.doi, *doi_twins(discovery.doi)))
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"{SOURCE}:{doi_key}",),
            )
            published_ref_id, created = store.upsert_stub_paper(
                identifiers=[("doi", discovery.doi)],
                title=discovery.title or None,
                year=discovery.year,
                authors=discovery.authors or None,
                set_by="system",
                conn=conn,
            )
            if published_ref_id == ref_id:
                store.stamp_ref_meta(
                    ref_id, {"preprint_upgrade_checked_at": now}, conn=conn
                )
                return UpgradeOutcome(
                    ref_id, V_SKIPPED, reason="preprint-owns-published-doi"
                )
            target = conn.execute(
                "SELECT 1 FROM refs r JOIN ref_identifiers i USING (ref_id) "
                "WHERE r.ref_id = %s AND r.kind = 'paper' AND r.retired_at IS NULL "
                "AND i.id_kind = 'doi' AND i.id_value = %s",
                (published_ref_id, discovery.doi),
            ).fetchone()
            if target is None:
                raise Conflict(
                    "published DOI does not resolve to a live paper",
                    next="inspect the DOI owner before retrying",
                )
            store.add_link(
                src_ref_id=published_ref_id,
                dst_ref_id=ref_id,
                relation="supersedes",
                set_by="system",
                meta={
                    "why": "preprint upgraded to its published version",
                    "via": discovery.via,
                },
                conn=conn,
            )
            store.add_tag(ref_id, SUPERSEDED_TAG, set_by="system", conn=conn)
            store.stamp_ref_meta(
                ref_id,
                {
                    "preprint_upgraded_to": published_ref_id,
                    "preprint_upgrade_via": discovery.via,
                    "preprint_upgrade_checked_at": now,
                },
                conn=conn,
            )
            if created:
                store.update_paper_fields(
                    published_ref_id,
                    authors=discovery.authors or None,
                    authors_source="crossref",
                    source=SOURCE,
                    meta_patch={
                        **discovery.metadata,
                        "preprint_upgrade_of": ref_id,
                        "preprint_upgrade_via": discovery.via,
                    },
                    conn=conn,
                )
            store.append_event(
                published_ref_id,
                source=SOURCE,
                event="upgraded_from_preprint",
                payload={
                    "preprint_ref_id": ref_id,
                    "doi": discovery.doi,
                    "via": discovery.via,
                },
                conn=conn,
            )
            store.append_event(
                ref_id,
                source=SOURCE,
                event="superseded_by_publication",
                payload={
                    "published_ref_id": published_ref_id,
                    "doi": discovery.doi,
                    "via": discovery.via,
                },
                conn=conn,
            )
    except Conflict as exc:
        # The DOI owner exists but its title disputes the discovery — a
        # human looks; never force an upgrade onto a contested identity.
        log.info("preprint-upgrade: #%s deferred to review: %s", ref_id, exc)
        store.stamp_ref_meta(ref_id, {"preprint_upgrade_checked_at": now})
        return UpgradeOutcome(
            ref_id, V_REVIEW, doi=discovery.doi, reason="doi-owner-conflict"
        )

    log.info(
        "preprint-upgrade: %s",
        UpgradeOutcome(
            ref_id, V_UPGRADED, published_ref_id, discovery.doi, discovery.via
        ).line(),
    )
    return UpgradeOutcome(
        ref_id,
        V_UPGRADED,
        published_ref_id=published_ref_id,
        doi=discovery.doi,
        reason=discovery.via,
    )


def _cohort(store: Store, *, limit: int | None, recheck_days: int) -> list[int]:
    """The cited preprint candidates worth attempting: live paper refs that

    * look like preprints (arXiv id, only a ``10.48550`` mint DOI, or no
      DOI — :func:`is_preprint` mirrored in SQL),
    * are **used** somewhere (incoming ``cites`` edge, a taproot evidence
      edge, or a resolved bibliography entry) — the sweep's whole purpose
      is closing the retraction hole for *cited* preprints, not re-deriving
      every stub in the corpus,
    * are not already upgraded (no incoming ``supersedes`` edge), and
    * have not been attempted within ``recheck_days``.

    Never-checked refs go first, then the oldest attempts."""
    sql = (
        "SELECT r.ref_id FROM refs r "
        "WHERE r.kind='paper' AND r.retired_at IS NULL AND r.human_verified_at IS NULL "
        # Detection mirror (id_value is trigger-lowercased on write).
        "  AND ( EXISTS (SELECT 1 FROM ref_identifiers a "
        "                WHERE a.ref_id=r.ref_id AND a.id_kind='arxiv') "
        "        OR NOT EXISTS (SELECT 1 FROM ref_identifiers d "
        "                       WHERE d.ref_id=r.ref_id AND d.id_kind='doi') "
        "        OR NOT EXISTS (SELECT 1 FROM ref_identifiers d2 "
        "                       WHERE d2.ref_id=r.ref_id AND d2.id_kind='doi' "
        "                         AND d2.id_value NOT LIKE '10.48550/arxiv%%') ) "
        # Already upgraded → done; the link is the durable record.
        "  AND NOT EXISTS (SELECT 1 FROM links sl "
        "                  WHERE sl.relation='supersedes' AND sl.dst_ref_id=r.ref_id) "
        # Used somewhere: cited, evidence-bearing, or bibliography-resolved.
        "  AND ( EXISTS (SELECT 1 FROM links c "
        "                WHERE c.relation='cites' AND c.dst_ref_id=r.ref_id) "
        "        OR EXISTS (SELECT 1 FROM links e "
        "                   WHERE e.relation IN ('establishes','corroborates','contradicts') "
        "                     AND e.src_ref_id=r.ref_id) "
        "        OR EXISTS (SELECT 1 FROM paper_bib_entries b "
        "                   WHERE b.held_ref_id=r.ref_id) ) "
        # The no-discovery stamp re-arms after the recheck window.
        "  AND ( r.meta->>'preprint_upgrade_checked_at' IS NULL "
        "        OR (r.meta->>'preprint_upgrade_checked_at')::timestamptz < "
        "            now() - make_interval(days => %s)) "
        "ORDER BY (r.meta->>'preprint_upgrade_checked_at')::timestamptz NULLS FIRST, "
        "r.ref_id" + (" LIMIT %s" if limit is not None else "")
    )
    with store.pool.connection() as conn:
        rows = conn.execute(
            sql, ((recheck_days, limit) if limit is not None else (recheck_days,))
        ).fetchall()
    return [int(r[0]) for r in rows]


def sweep_preprint_upgrades(
    store: Store,
    *,
    apply: bool = True,
    limit: int | None = None,
    api_key: str = "",
    call_timeout: float = 20.0,
    delay: float = 0.5,
    recheck_days: int = _RECHECK_DAYS,
    batch_fn: BatchFn = _BATCH_LOOKUP,
    s2_fn: S2Fn = lookup_s2,
    crossref_fn: CrossrefWorkFn = fetch_message,
) -> SweepResult:
    """Walk the cohort and attempt each ref. A rate-limit refusal defers
    the rest of the pass (they keep their stamps; the next pass retries)
    rather than erroring them (gr503080)."""
    result = SweepResult()
    for ref_id in _cohort(store, limit=limit, recheck_days=recheck_days):
        try:
            outcome = upgrade_preprint(
                store,
                ref_id,
                apply=apply,
                api_key=api_key,
                batch_fn=batch_fn,
                s2_fn=s2_fn,
                crossref_fn=crossref_fn,
                call_timeout=call_timeout,
            )
        except (S2RateLimited, RateLimited) as exc:
            log.info("preprint-upgrade: deferring the pass (%s)", exc)
            result.outcomes.append(
                UpgradeOutcome(ref_id, V_DEFERRED, reason="rate-limited")
            )
            break
        except Exception:
            log.exception("preprint-upgrade: ref #%s failed", ref_id)
            result.outcomes.append(UpgradeOutcome(ref_id, V_ERROR, reason="exception"))
            continue
        result.outcomes.append(outcome)
        if delay > 0 and outcome.verdict in (V_UPGRADED, V_NO_PUBLISHED):
            if drain_sleep(delay):
                break  # draining — return what we have
    return result


__all__ = [
    "SOURCE",
    "SUPERSEDED_TAG",
    "SweepResult",
    "UpgradeOutcome",
    "is_preprint",
    "sweep_preprint_upgrades",
    "upgrade_preprint",
]
