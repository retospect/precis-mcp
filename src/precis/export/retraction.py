"""Retraction state of the papers a draft cites.

One walk, two consumers — the export gate (`precis_web/routes/drafts.py`)
and the draft's retraction-watch button — so the two can never disagree
about what a draft cites or how bad it is.

The split that matters here is **read vs check**. Export is a read: it
reports whatever the triggers happened to stamp and never touches the
network, because discovering a minute of Crossref latency inside a
download click is the wrong place to find out. The button is the check:
it is the user deliberately waiting, and it is where stale cites get
re-fetched (TTL-gated, see ``ingest/provenance.check_ref_retraction``).

Coverage is sparse by design, so "we never looked" is a first-class
outcome here, distinct from "we looked and it was fine" — see
:attr:`DraftRetractionReport.unchecked`. Consumers surface it rather
than rounding it down to clean.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from precis.ingest.provenance import RETRACTION_TTL_DAYS, check_refs_retraction

#: Statuses that block an export outright. Narrow on purpose: a
#: corrigendum fixing an affiliation is not a reason to refuse to
#: produce a PDF.
BLOCKING_STATUSES = frozenset({"retracted"})

#: Statuses that annotate but never block.
SOFT_STATUSES = frozenset({"corrected", "expression_of_concern"})

#: Human-facing labels, keyed by ``refs.retraction_status``.
STATUS_LABELS = {
    "retracted": "RETRACTED",
    "corrected": "corrected",
    "expression_of_concern": "expression of concern",
}


@dataclass(frozen=True, slots=True)
class CitedPaper:
    """One paper a draft cites, with its retraction state."""

    ref_id: int
    slug: str
    title: str
    status: str | None = None
    #: ``None`` when this report was read rather than checked.
    outcome: str | None = None
    checked_at: Any = None

    @property
    def label(self) -> str:
        """``RETRACTED`` / ``corrected`` / … or "" when clean."""
        return STATUS_LABELS.get(self.status or "", "")

    @property
    def blocks(self) -> bool:
        return (self.status or "") in BLOCKING_STATUSES

    @property
    def never_checked(self) -> bool:
        return self.checked_at is None


@dataclass(frozen=True, slots=True)
class DraftRetractionReport:
    """What the draft's cited papers look like, retraction-wise."""

    papers: list[CitedPaper] = field(default_factory=list)
    #: Cited slugs that resolve to no ref at all. Not a retraction
    #: problem, but the walk is the natural place to notice them.
    unresolved: list[str] = field(default_factory=list)
    #: True when the papers were re-checked upstream, False when the
    #: report is a read of stored state.
    checked: bool = False

    @property
    def retracted(self) -> list[CitedPaper]:
        return [p for p in self.papers if p.blocks]

    @property
    def soft(self) -> list[CitedPaper]:
        return [p for p in self.papers if (p.status or "") in SOFT_STATUSES]

    @property
    def unchecked(self) -> list[CitedPaper]:
        """Cites nobody has ever asked about.

        Under the two-trigger model this is normally most of them, which
        is why it is reported rather than silently treated as clean.
        """
        return [p for p in self.papers if p.never_checked]

    @property
    def blocks_export(self) -> bool:
        return bool(self.retracted)

    def summary(self) -> str:
        """One line for a flash message / job log."""
        bits = []
        if self.retracted:
            bits.append(f"{len(self.retracted)} retracted")
        if self.soft:
            bits.append(f"{len(self.soft)} with notices")
        if self.unchecked:
            bits.append(f"{len(self.unchecked)} never checked")
        if not bits:
            return f"{len(self.papers)} cited papers, all clean"
        return f"{len(self.papers)} cited papers — " + ", ".join(bits)


def cited_paper_refs(
    store: Any, ref: Any, *, cited_slugs: list[str] | None = None
) -> tuple[list[Any], list[str]]:
    """``(refs, unresolved_slugs)`` for the sources a draft cites.

    ``cited_slugs`` is reused from an already-run export when the caller
    has it (``ExportResult.cited_slugs``); otherwise the body is rendered
    once to compute it — same contract as
    :func:`precis.export.sources.collect_cited_sources`, which this
    deliberately mirrors rather than calls (that one also resolves PDFs
    off disk, which a retraction walk has no use for).
    """
    # Local imports: both modules import each other's neighbours at load.
    from precis.export.latex import render_body
    from precis.export.sources import _resolve_source_ref

    if cited_slugs is None:
        cited_slugs = render_body(store, ref).cited_slugs

    refs: list[Any] = []
    unresolved: list[str] = []
    seen: set[str] = set()
    for slug in cited_slugs:
        if slug in seen:
            continue
        seen.add(slug)
        sref = _resolve_source_ref(store, slug)
        if sref is None:
            unresolved.append(slug)
        else:
            refs.append(sref)
    return refs, unresolved


def draft_retraction_report(
    store: Any,
    ref: Any,
    *,
    cited_slugs: list[str] | None = None,
    check: bool = False,
    force: bool = False,
    ttl_days: int = RETRACTION_TTL_DAYS,
    mailto: str | None = None,
) -> DraftRetractionReport:
    """Retraction state of everything ``ref`` cites.

    ``check=False`` (the export path) reads stored state only — no
    network. ``check=True`` (the button) re-checks each cite through the
    TTL gate; ``force=True`` additionally ignores the TTL, which is what
    makes pressing the button twice in a day do something.
    """
    refs, unresolved = cited_paper_refs(store, ref, cited_slugs=cited_slugs)

    results: dict[int, Any] = {}
    if check and refs:
        # ``Ref.id`` is the ref_id column — the dataclass renamed it in
        # migration 0001; there is no ``.ref_id`` attribute.
        results = {
            c.ref_id: c
            for c in check_refs_retraction(
                store,
                [r.id for r in refs],
                force=force,
                ttl_days=ttl_days,
                mailto=mailto,
            )
        }

    papers: list[CitedPaper] = []
    for r in refs:
        checked = results.get(r.id)
        papers.append(
            CitedPaper(
                ref_id=r.id,
                slug=getattr(r, "slug", "") or "",
                title=getattr(r, "title", "") or "",
                status=(
                    checked.status
                    if checked is not None
                    else getattr(r, "retraction_status", None)
                ),
                outcome=checked.outcome if checked is not None else None,
                checked_at=(
                    checked.checked_at
                    if checked is not None
                    else getattr(r, "retraction_checked_at", None)
                ),
            )
        )
    return DraftRetractionReport(papers=papers, unresolved=unresolved, checked=check)


__all__ = [
    "BLOCKING_STATUSES",
    "SOFT_STATUSES",
    "STATUS_LABELS",
    "CitedPaper",
    "DraftRetractionReport",
    "cited_paper_refs",
    "draft_retraction_report",
]
