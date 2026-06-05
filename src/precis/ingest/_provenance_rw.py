"""Retraction Watch cache lookup + Crossref-merge.

Split out of ``ingest/provenance.py`` 2026-06-05. Originally the
provenance pipeline carried both the Crossref orchestration and the
RW (Retraction Watch) local-cache integration in one 1.7K-LOC file.
This module covers the RW side end-to-end:

* :class:`_RWCacheRow` — value type mirroring one row of
  ``provenance_rw_cache``.
* :func:`_lookup_rw_cache` — DB query over ``provenance_rw_cache``,
  tolerant of the migration-not-applied state (returns ``[]``).
* :func:`_classify_rw_nature` — RW's notice-nature vocabulary →
  severity / status / relation triple.
* :func:`_rw_row_to_notice` — synthesise a :class:`Notice` from an
  unmatched RW row (Hwang stem-cell case: publisher never backfilled
  the Crossref ``update-to`` relation).
* :func:`_enrich_notices_with_rw` — Phase-3 back-compat wrapper.
* :func:`_merge_crossref_and_rw_notices` — Phase 6.1 merge; the main
  entry point used by the orchestrator.

All public names are re-exported from ``ingest/provenance.py`` so
existing imports (tests, ``handlers/_provenance_report.py``) keep
working unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

# Re-imported here for the triple-type signatures below; the canonical
# definitions live in ``ingest/provenance.py``. Importing from there
# would create a cycle, so we redeclare the Literal aliases — they
# carry the same string values either way.
from precis.ingest._provenance_types import (
    LinkRelation,
    Notice,
    RetractionStatus,
    Severity,
)
from precis.store import Store


@dataclass(frozen=True, slots=True)
class _RWCacheRow:
    """One row from ``provenance_rw_cache`` for a single paper DOI.

    Phase 6.1: carries paper-level metadata too (``paper_title``,
    ``journal``, ``retraction_date``) so the merge path can fall
    back when Crossref is unavailable or doesn't have the record.
    """

    notice_doi: str | None
    notice_nature: str
    reasons: list[str]
    retraction_date: datetime | None
    paper_title: str | None
    journal: str | None


def _lookup_rw_cache(store: Store, paper_doi: str) -> list[_RWCacheRow]:
    """Return every cached RW row for ``paper_doi``, or ``[]`` if none.

    Multiple rows are possible: a paper with both a correction *and*
    a later retraction has one row per notice. The caller matches
    each row to the corresponding Crossref ``update-to`` Notice by
    DOI; un-matched rows are surfaced via the merge path so RW
    reasons aren't lost when Crossref's view of the paper is thin.

    Returns ``[]`` when the cache table doesn't exist yet (e.g. on
    a deployment that hasn't run migration 0003) — the caller
    treats the absence as "no reasons available", same as an empty
    cache. This keeps the kind usable in degraded environments.

    Phase 6.1: also returns ``retraction_date``, ``paper_title``, and
    ``journal`` so the merge path can synthesise Notices and fall
    back paper metadata when Crossref is unavailable.
    """
    sql = (
        "SELECT notice_doi, notice_nature, reasons, "
        "       retraction_date, paper_title, journal "
        "FROM provenance_rw_cache WHERE paper_doi = %s"
    )
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(sql, (paper_doi,)).fetchall()
    except Exception:
        return []
    out: list[_RWCacheRow] = []
    for r in rows:
        d = r[3]
        # PG DATE → Python datetime.date; lift to datetime for shared type
        date_as_dt: datetime | None = None
        if d is not None:
            try:
                date_as_dt = datetime(d.year, d.month, d.day)
            except (AttributeError, ValueError):
                date_as_dt = None
        out.append(
            _RWCacheRow(
                notice_doi=(r[0] or None),
                notice_nature=str(r[1] or ""),
                reasons=list(r[2] or []),
                retraction_date=date_as_dt,
                paper_title=(r[4] or None),
                journal=(r[5] or None),
            )
        )
    return out


# RW's ``notice_nature`` vocabulary maps onto our internal severity
# triple. Distinct from Crossref's ``update_type`` map because the two
# data sources use different strings for the same concepts. See
# docs/design/provenance-kind-plan.md § "Severity classification".
_RW_NATURE_MAP: dict[
    str, tuple[Severity, RetractionStatus | None, LinkRelation | None]
] = {
    "retraction": ("blocker", "retracted", "retracted-by"),
    "partialretraction": ("blocker", "retracted", "retracted-by"),
    "withdrawal": ("blocker", "retracted", "retracted-by"),
    "removal": ("blocker", "retracted", "retracted-by"),
    "expressionofconcern": ("review", "expression_of_concern", "concern-raised-by"),
    "correction": ("note", "corrected", "corrected-by"),
    "corrigendum": ("note", "corrected", "corrected-by"),
    "erratum": ("note", "corrected", "corrected-by"),
    # 'Reinstatement' deliberately absent — RW uses it for retractions that
    # were later reversed. Mapping it to a notice would be misleading; the
    # paper is currently *not* retracted. Future enhancement: surface a
    # ``REINSTATED`` flag separately.
}


def _classify_rw_nature(
    notice_nature: str,
) -> tuple[Severity, RetractionStatus | None, LinkRelation | None] | None:
    """Classify a Retraction Watch ``notice_nature`` string.

    Returns the same triple shape as ``classify_update_type``. Returns
    ``None`` for natures we don't recognise (e.g. ``Reinstatement``,
    ``Unknown``), in which case the caller drops the notice rather
    than guessing.
    """
    key = re.sub(r"[^a-z]", "", notice_nature.lower())
    return _RW_NATURE_MAP.get(key)


def _rw_row_to_notice(row: _RWCacheRow) -> Notice | None:
    """Synthesise a ``Notice`` from a RW cache row not matched to Crossref.

    Used when:
    - Crossref returned no ``update-to`` entry for the paper (publisher
      never backfilled — the Hwang stem-cell case)
    - Crossref returned 404 entirely (paper not in their index)
    - Crossref timed out and we fell back to local-only data

    ``update_type`` is left empty to signal "RW-only" to the renderer;
    ``rw_notice_nature`` carries the human-readable nature string.
    Returns ``None`` when the RW nature doesn't classify (Reinstatement,
    Unknown, …) — caller drops un-classified rows.
    """
    cls = _classify_rw_nature(row.notice_nature)
    if cls is None:
        return None
    severity, status, relation = cls
    return Notice(
        update_type="",
        severity=severity,
        status=status,
        relation=relation,
        notice_doi=row.notice_doi or "",
        notice_date=row.retraction_date,
        notice_title=None,
        notice_authors=None,
        notice_year=None,
        persisted_ref_id=None,
        rw_reasons=list(row.reasons),
        rw_notice_nature=row.notice_nature or None,
    )


def _enrich_notices_with_rw(
    notices: list[Notice],
    cache_rows: list[_RWCacheRow],
) -> list[Notice]:
    """Match RW cache rows to Crossref notices and merge in the reasons.

    Phase 3 behaviour preserved for back-compat (single-direction
    enrichment, no synthesis). Phase 6.1 callers use
    :func:`_merge_crossref_and_rw_notices` instead, which extends
    this to also synthesise notices from RW-only rows.
    """
    merged, _consumed = _merge_crossref_and_rw_notices(
        notices, cache_rows, _synthesize_rw_only=False
    )
    return merged


def _merge_crossref_and_rw_notices(
    crossref_notices: list[Notice],
    cache_rows: list[_RWCacheRow],
    *,
    _synthesize_rw_only: bool = True,
) -> tuple[list[Notice], set[int]]:
    """Phase 6.1 merge: combine Crossref-derived notices with the local RW cache.

    Returns ``(merged_notices, consumed_row_indices)``:

    - Every Crossref notice is preserved. If the RW cache has a matching
      row (exact ``notice_doi`` match or single-nature fallback), the
      Crossref notice is enriched with the RW reasons + nature string.
    - When ``_synthesize_rw_only=True`` (the default), RW cache rows
      that did not match any Crossref notice are converted into
      synthesised ``Notice`` objects and appended. This is how we surface
      retractions Crossref doesn't know about (publisher never deposited
      the ``update-to`` relation) — the Hwang stem-cell case is the
      canonical example.
    - ``consumed_row_indices`` is the set of RW row indices the merge
      "used" (either by enriching a Crossref notice or by synthesising
      a new one). Returned so the caller can reason about coverage.

    The previous Phase 3 helper ``_enrich_notices_with_rw`` is now a
    thin wrapper around this with ``_synthesize_rw_only=False`` for
    back-compat with any callers that wanted pure enrichment.
    """
    if not cache_rows:
        return crossref_notices, set()

    # Index cache rows by exact notice_doi for the fast path. ``by_doi``
    # tracks (index, row) so the caller can mark indices as consumed.
    by_doi: dict[str, tuple[int, _RWCacheRow]] = {}
    for i, r in enumerate(cache_rows):
        if r.notice_doi:
            by_doi[r.notice_doi.lower()] = (i, r)

    # Build a nature → [(index, row), …] map for the fallback. Lowercase
    # + collapse whitespace so "Expression of Concern" matches
    # "expression_of_concern" off the Crossref side.
    def _norm_nature(s: str) -> str:
        return re.sub(r"[^a-z]", "", s.lower())

    by_nature: dict[str, list[tuple[int, _RWCacheRow]]] = {}
    for i, r in enumerate(cache_rows):
        by_nature.setdefault(_norm_nature(r.notice_nature), []).append((i, r))

    nature_for_status: dict[RetractionStatus, str] = {
        "retracted": "retraction",
        "expression_of_concern": "expressionofconcern",
        "corrected": "correction",
    }

    consumed: set[int] = set()
    merged: list[Notice] = []
    for n in crossref_notices:
        match: tuple[int, _RWCacheRow] | None = by_doi.get(n.notice_doi)
        if match is None and n.status is not None:
            candidates = by_nature.get(nature_for_status.get(n.status, ""), [])
            if len(candidates) == 1:
                match = candidates[0]
        if match is None:
            merged.append(n)
            continue
        idx, row = match
        consumed.add(idx)
        merged.append(
            Notice(
                update_type=n.update_type,
                severity=n.severity,
                status=n.status,
                relation=n.relation,
                notice_doi=n.notice_doi,
                notice_date=n.notice_date,
                notice_title=n.notice_title,
                notice_authors=n.notice_authors,
                notice_year=n.notice_year,
                persisted_ref_id=n.persisted_ref_id,
                rw_reasons=list(row.reasons),
                rw_notice_nature=row.notice_nature or None,
            )
        )

    # Synthesise notices for unmatched RW rows. Skip rows whose nature
    # we don't classify (Reinstatement, Unknown) — surfacing them as
    # severity=info would clutter the report without actionable signal.
    if _synthesize_rw_only:
        for i, row in enumerate(cache_rows):
            if i in consumed:
                continue
            synthesised = _rw_row_to_notice(row)
            if synthesised is None:
                continue
            merged.append(synthesised)
            consumed.add(i)

    return merged, consumed


__all__ = [
    "_RWCacheRow",
    "_classify_rw_nature",
    "_enrich_notices_with_rw",
    "_lookup_rw_cache",
    "_merge_crossref_and_rw_notices",
    "_rw_row_to_notice",
]
