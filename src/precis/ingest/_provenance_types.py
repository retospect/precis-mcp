"""Shared type aliases + :class:`Notice` for the provenance pipeline.

Split out of ``ingest/provenance.py`` 2026-06-05 so the sibling
modules (``_provenance_rw``, future ``_provenance_crossref``) can
import the canonical :class:`Notice` dataclass without inducing a
circular import.

Type aliases mirror what Crossref / RW emit and what
``refs.retraction_status`` (the CHECK constraint from
``0001_initial.sql:308``) accepts. The wider provenance pipeline
imports everything in :data:`__all__` from this module; downstream
callers should keep importing from ``precis.ingest.provenance``
(which re-exports) so the existing surface stays stable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

Severity = Literal["blocker", "review", "note", "info"]
RetractionStatus = Literal["retracted", "expression_of_concern", "corrected"]
LinkRelation = Literal["retracted-by", "corrected-by", "concern-raised-by"]


@dataclass(frozen=True, slots=True)
class Notice:
    """One ``update-to`` entry on a Crossref paper record."""

    update_type: str  # raw Crossref string ("retraction", "corrigendum", …)
    severity: Severity
    status: RetractionStatus | None  # None when type is informational only
    relation: LinkRelation | None  # link relation to use when writing
    notice_doi: str  # the notice's own DOI, canonicalised
    notice_date: datetime | None  # ``updated`` timestamp from Crossref
    notice_title: str | None  # populated when notice metadata is fetched
    notice_authors: list[dict[str, Any]] | None
    notice_year: int | None
    persisted_ref_id: int | None = None  # populated when auto-ingested
    # Phase 3: Retraction Watch reason codes joined from the local
    # cache. Empty list means either (a) no RW data for this paper-DOI,
    # or (b) RW data exists but the notice_doi didn't match any cached
    # row. The renderer treats both cases identically — just no
    # reasons surfaced.
    rw_reasons: list[str] = field(default_factory=list)
    # RW's own classification ("Retraction" / "Correction" / "Expression
    # of concern" / …). Mostly redundant with ``update_type`` from
    # Crossref but kept for cross-reference when the two disagree
    # (rare, but informative when investigating discrepancies).
    rw_notice_nature: str | None = None


__all__ = [
    "LinkRelation",
    "Notice",
    "RetractionStatus",
    "Severity",
]
