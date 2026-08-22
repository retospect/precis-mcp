"""Retraction state — and DOI completeness/validity — of the papers a
draft cites.

One walk, two consumers — the export gate (`precis_web/routes/drafts.py`)
and the draft's retraction-watch button — so the two can never disagree
about what a draft cites or how bad it is.

**DOI completeness rides the same walk** (docs/backlog/
draft-doi-completeness-check.md, decided 2026-08-12: one walk, not a
parallel module). DOI *presence* — does this cite carry a DOI at all, and
if not, is it at least resolvable from another identifier (arxiv/pubmed/
s2)? — is a pure read off ``ref_identifiers``, same cost profile as
reading ``retraction_status``. DOI *validity* — does the DOI actually
resolve upstream? — is a network check that mirrors the retraction split
exactly, down to its own ``doi_validated_at`` stamp
(``ingest/provenance.check_ref_doi_validity``), riding the *same*
``select_for_check``-picked subset in one press of the watch button so a
user gets both signals refreshed for the price of one wait. It does **not**
spend a second Crossref round-trip to get there: wherever the retraction
check above already hit the network for a cite, its answer also tells us
whether the DOI resolves (``RetractionCheck.doi_signal``), and
:func:`draft_retraction_report` stamps that directly; ``check_refs_doi_validity``
only runs for cites the retraction check itself skipped this press (its own
TTL still fresh), so the two checks never both fetch the same DOI. Both DOI
signals are advisory only — see :attr:`CitedPaper.doi_fetchable` /
:attr:`DraftRetractionReport.missing_doi` — never :attr:`blocks_export`.

The split that matters here is **read vs check**. Export is a read: it
reports whatever the triggers happened to stamp and never touches the
network, because discovering a minute of Crossref latency inside a
download click is the wrong place to find out. The button is the check:
it is the user deliberately waiting, and it is where stale cites get
re-fetched (TTL-gated, see ``ingest/provenance.check_ref_retraction``).

A caller that can't afford to walk every cite in one press narrows the
network side with ``check_slugs`` — :func:`select_for_check` picks the
neediest — while the report still covers the whole draft. Bounding the
*report* instead would be the same bug in a different place: a partly
walked draft that reads as fully checked.

Coverage is sparse by design, so "we never looked" is a first-class
outcome here, distinct from "we looked and it was fine" — see
:attr:`DraftRetractionReport.unchecked`. Consumers surface it rather
than rounding it down to clean.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from precis.ingest.provenance import (
    DOI_VALIDATION_TTL_DAYS,
    RETRACTION_TTL_DAYS,
    DoiValidationCheck,
    check_refs_doi_validity,
    check_refs_retraction,
)

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

#: Non-DOI identifier schemes a missing DOI can plausibly be resolved
#: from (phase 2's "fetch missing DOIs" target set) — the three the
#: backlog item names explicitly. Preference order only matters for which
#: one :func:`identifier_kind_for` reports when a ref happens to carry
#: more than one.
_FETCHABLE_ID_KINDS: tuple[str, ...] = ("arxiv", "pubmed", "s2")


def identifier_kind_for(ids: Mapping[str, str]) -> str | None:
    """The best non-DOI persistent identifier scheme present in ``ids``
    (an :meth:`Store.identifiers_for_refs` bucket), or ``None`` when the
    ref carries none of them.

    Callers check DOI presence separately — this only answers "is there
    something else fetchable from" for the DOI-missing case. Shared by
    :func:`draft_retraction_report` and the cheaper chunk-mined callers
    (``handlers/draft.py``'s Hygiene footer, ``_citations_view.py``) so
    the presence classification can't drift between them.
    """
    for kind in _FETCHABLE_ID_KINDS:
        if ids.get(kind):
            return kind
    return None


def summarize_doi_completeness(
    ref_ids: Sequence[int],
    identifiers: Mapping[int, Mapping[str, str]],
    refs_by_id: Mapping[int, Any],
) -> str:
    """One line: DOI completeness + validity over an arbitrary set of
    paper ``ref_id``\\ s, given batched lookups the caller already has
    (:meth:`Store.identifiers_for_refs`, :meth:`Store.fetch_refs_by_ids`).

    The authoritative walk for the retraction pane / export summary /
    watch button is :func:`draft_retraction_report` (see this module's
    docstring). This is the cheaper variant for callers that already have
    their own notion of "what this draft cites" (chunk-token mining, e.g.
    the Hygiene footer's existing taproot-hub scoreboard) and shouldn't
    pay for a second body render just to get the DOI line — it shares
    :func:`identifier_kind_for`'s classification so the two can't
    disagree on wording.
    """
    ids_list = list(ref_ids)
    if not ids_list:
        return ""
    missing = fetchable = no_identifier = unvalidated = invalid = 0
    for rid in ids_list:
        ids = identifiers.get(rid, {})
        doi = ids.get("doi")
        if not doi:
            missing += 1
            if identifier_kind_for(ids) is not None:
                fetchable += 1
            else:
                no_identifier += 1
            continue
        ref = refs_by_id.get(rid)
        if getattr(ref, "doi_status", None) == "not_found":
            invalid += 1
        if getattr(ref, "doi_validated_at", None) is None:
            unvalidated += 1
    bits = []
    if missing:
        bits.append(
            f"{missing} missing DOI ({fetchable} fetchable, "
            f"{no_identifier} no identifier)"
        )
    if invalid:
        bits.append(f"{invalid} DOI invalid")
    if unvalidated:
        bits.append(f"{unvalidated} DOI never validated")
    if not bits:
        return f"all {len(ids_list)} cited paper(s) have a validated DOI"
    return f"{len(ids_list)} cited paper(s) — " + ", ".join(bits)


@dataclass(frozen=True, slots=True)
class CitedPaper:
    """One paper a draft cites, with its retraction state and DOI
    completeness/validity (docs/backlog/draft-doi-completeness-check.md)."""

    ref_id: int
    slug: str
    title: str
    status: str | None = None
    #: ``None`` when this report was read rather than checked.
    outcome: str | None = None
    checked_at: Any = None
    #: The ref's DOI, or ``None`` when it doesn't carry one.
    doi: str | None = None
    #: Best non-DOI identifier scheme (``'arxiv'``/``'pubmed'``/``'s2'``)
    #: when ``doi`` is ``None`` and one exists; ``None`` otherwise —
    #: including when ``doi`` is set (nothing to fall back to).
    identifier_kind: str | None = None
    #: ``'valid'`` / ``'not_found'`` once checked, ``None`` before the
    #: first validity check (mirrors ``status`` above).
    doi_status: str | None = None
    #: ``None`` when this DOI has never been validated — a first-class
    #: state, not rounded down to "valid" (mirrors ``checked_at``).
    doi_validated_at: Any = None

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

    @property
    def has_doi(self) -> bool:
        return bool(self.doi)

    @property
    def doi_fetchable(self) -> bool:
        """No DOI, but another persistent identifier (arxiv/pubmed/s2)
        exists to resolve one from — phase 2's "fetch missing DOIs"
        target set."""
        return not self.has_doi and self.identifier_kind is not None

    @property
    def doi_missing_no_identifier(self) -> bool:
        """No DOI and no other persistent identifier either — typically
        a whole-paper ``[pa]`` stub imported from a bare title (the worst
        of the three presence buckets)."""
        return not self.has_doi and self.identifier_kind is None

    @property
    def doi_presence_label(self) -> str:
        """ "" when clean, else the human-facing presence bucket —
        ``"no DOI (fetchable from arxiv)"`` or ``"no persistent
        identifier"`` (acceptance-criteria wording)."""
        if self.has_doi:
            return ""
        if self.identifier_kind is not None:
            return f"no DOI (fetchable from {self.identifier_kind})"
        return "no persistent identifier"

    @property
    def doi_never_validated(self) -> bool:
        """True only when this ref carries a DOI nobody has ever asked
        Crossref about — the DOI-validity twin of ``never_checked``."""
        return self.has_doi and self.doi_validated_at is None

    @property
    def doi_invalid(self) -> bool:
        return self.doi_status == "not_found"


@dataclass(frozen=True, slots=True)
class DraftRetractionReport:
    """What the draft's cited papers look like, retraction- and
    DOI-completeness-wise (docs/backlog/draft-doi-completeness-check.md)."""

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
    def missing_doi(self) -> list[CitedPaper]:
        """Cites with no DOI at all — the union of :attr:`doi_fetchable`
        and :attr:`doi_no_identifier`."""
        return [p for p in self.papers if not p.has_doi]

    @property
    def doi_fetchable(self) -> list[CitedPaper]:
        """The missing-DOI subset resolvable from another identifier —
        phase 2's "fetch missing DOIs" target set."""
        return [p for p in self.papers if p.doi_fetchable]

    @property
    def doi_no_identifier(self) -> list[CitedPaper]:
        """The missing-DOI subset with no persistent identifier at
        all — the worst presence bucket."""
        return [p for p in self.papers if p.doi_missing_no_identifier]

    @property
    def doi_unvalidated(self) -> list[CitedPaper]:
        """Cites with a DOI nobody has ever validated — reported rather
        than rounded down to "valid", same as :attr:`unchecked`."""
        return [p for p in self.papers if p.doi_never_validated]

    @property
    def doi_invalid(self) -> list[CitedPaper]:
        """Cites with a DOI that was checked and does not resolve."""
        return [p for p in self.papers if p.doi_invalid]

    @property
    def blocks_export(self) -> bool:
        return bool(self.retracted)

    def summary(self) -> str:
        """One line for a flash message / job log.

        Missing/invalid/unvalidated DOI bits are advisory — they never
        affect :attr:`blocks_export` — but are still surfaced here (never
        rounded into "all clean") for the same reason ``unchecked`` is:
        the sparse coverage model means silence is not evidence of
        cleanliness.
        """
        bits = []
        if self.retracted:
            bits.append(f"{len(self.retracted)} retracted")
        if self.soft:
            bits.append(f"{len(self.soft)} with notices")
        if self.missing_doi:
            bits.append(f"{len(self.missing_doi)} missing DOI")
        if self.doi_invalid:
            bits.append(f"{len(self.doi_invalid)} DOI invalid")
        if self.unchecked:
            bits.append(f"{len(self.unchecked)} never checked")
        if self.doi_unvalidated:
            bits.append(f"{len(self.doi_unvalidated)} DOI never validated")
        if not bits:
            return f"{len(self.papers)} cited papers, all clean"
        return f"{len(self.papers)} cited papers — " + ", ".join(bits)


# store stays Any: when cited_slugs is omitted this forwards into
# latex.render_body (Store-typed), but tests drive draft_retraction_report
# through the monkeypatched `cited_paper_refs` seam and pass bare
# `object()` as store — no protocol could accept that.
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


def select_for_check(refs: Sequence[Any], limit: int) -> list[Any]:
    """The ``limit`` cites most in need of a network re-check, neediest
    first: never-checked before checked, then oldest stamp first.

    A caller that can only afford so many Crossref round-trips per press
    must select by *need*, not by position. A head slice
    (``refs[:limit]``) re-picks the same cites forever: they come back
    TTL-fresh, so every later press is a no-op and cites past ``limit``
    are unreachable through that button at all. Ordering by need makes
    the cap a per-press budget instead of a horizon — each press pushes
    what it checked to the back, so repeated presses walk the whole
    draft.
    """
    if limit <= 0 or len(refs) <= limit:
        return list(refs)
    # Stable sort: cites with equal need keep their cited order.
    ordered = sorted(refs, key=_check_need_key)
    return ordered[:limit]


def _check_need_key(r: Any) -> tuple[bool, Any]:
    """Sort key for :func:`select_for_check` — ``False`` sorts first, so
    never-checked leads, then oldest ``retraction_checked_at``."""
    at = getattr(r, "retraction_checked_at", None)
    return (at is not None, at)


# store stays Any: forwards into cited_paper_refs, which stays Any for the
# same reason (tests pass bare `object()` — see that function's comment).
def draft_retraction_report(
    store: Any,
    ref: Any,
    *,
    cited_slugs: list[str] | None = None,
    check: bool = False,
    check_slugs: Collection[str] | None = None,
    force: bool = False,
    ttl_days: int = RETRACTION_TTL_DAYS,
    doi_ttl_days: int = DOI_VALIDATION_TTL_DAYS,
    mailto: str | None = None,
) -> DraftRetractionReport:
    """Retraction state — and DOI completeness/validity — of everything
    ``ref`` cites (docs/backlog/draft-doi-completeness-check.md).

    ``check=False`` (the export path) reads stored state only — no
    network. ``check=True`` (the button) re-checks each cite through the
    TTL gate; ``force=True`` additionally ignores the TTL, which is what
    makes pressing the button twice in a day do something. DOI presence
    is always a pure read (:func:`identifier_kind_for` off
    :meth:`Store.identifiers_for_refs`) regardless of ``check`` — it costs
    no network either way. DOI *validity* follows the same read-vs-check
    split as retraction, but shares the retraction check's own Crossref
    round-trip rather than spending a second one: for every cite whose
    retraction check hit the network this press, its
    ``RetractionCheck.doi_signal`` is stamped straight through
    :meth:`Store.set_doi_validation`; only the cites the retraction check
    skipped this press (still TTL-fresh) fall through to a dedicated call to
    :func:`~precis.ingest.provenance.check_refs_doi_validity`. Either way one
    press of the button refreshes both signals for one round-trip per cite —
    never two GETs to ``api.crossref.org/works/{doi}`` for the same DOI.

    ``check_slugs`` narrows the *network* walk to a subset while the
    report still covers every cite — that split is what lets a caller
    spend a bounded per-press budget (see :func:`select_for_check`)
    without truncating what it reports. Truncating the report instead
    hides the tail: the pane's "N of M never checked" prompt reads off
    these totals, so a report scoped to the checked subset always looks
    complete.
    """
    refs, unresolved = cited_paper_refs(store, ref, cited_slugs=cited_slugs)

    to_check = refs
    if check_slugs is not None:
        wanted = set(check_slugs)
        to_check = [r for r in refs if (getattr(r, "slug", "") or "") in wanted]

    results: dict[int, Any] = {}
    doi_results: dict[int, Any] = {}
    if check and to_check:
        # ``Ref.id`` is the ref_id column — the dataclass renamed it in
        # migration 0001; there is no ``.ref_id`` attribute.
        retraction_checks = check_refs_retraction(
            store,
            [r.id for r in to_check],
            force=force,
            ttl_days=ttl_days,
            mailto=mailto,
        )
        results = {c.ref_id: c for c in retraction_checks}

        # DOI validity rides the SAME Crossref round-trip as the retraction
        # check above wherever that check actually reached the network
        # (``RetractionCheck.doi_signal`` — outcome in {"checked",
        # "unchecked"}). Pre-ship-review fix: this used to call
        # check_refs_doi_validity over the whole ``to_check`` set
        # unconditionally, GETting api.crossref.org/works/{doi} a second
        # time for every cite the line above had just resolved — doubling
        # the button's 40-cap/90s budget for no new information. Only cites
        # the retraction check skipped this press (outcome == "fresh", its
        # own TTL not yet due) still get a dedicated validity fetch below —
        # for those, retraction spent no round-trip, so one here doesn't
        # double anything.
        network_attempted = {
            c.ref_id for c in retraction_checks if c.outcome in ("checked", "unchecked")
        }
        for c in retraction_checks:
            signal = getattr(c, "doi_signal", None)
            if signal is None:
                continue
            store.set_doi_validation(c.ref_id, status=signal)
            doi_results[c.ref_id] = DoiValidationCheck(
                ref_id=c.ref_id,
                outcome="checked",
                status=signal,
                validated_at=datetime.now(UTC),
            )
        still_need = [r for r in to_check if r.id not in network_attempted]
        if still_need:
            doi_results.update(
                {
                    c.ref_id: c
                    for c in check_refs_doi_validity(
                        store,
                        [r.id for r in still_need],
                        force=force,
                        ttl_days=doi_ttl_days,
                        mailto=mailto,
                    )
                }
            )

    identifiers = store.identifiers_for_refs([r.id for r in refs]) if refs else {}

    papers: list[CitedPaper] = []
    for r in refs:
        checked = results.get(r.id)
        doi_checked = doi_results.get(r.id)
        ids = identifiers.get(r.id, {})
        doi = ids.get("doi") or None
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
                doi=doi,
                identifier_kind=identifier_kind_for(ids) if not doi else None,
                doi_status=(
                    doi_checked.status
                    if doi_checked is not None
                    else getattr(r, "doi_status", None)
                ),
                doi_validated_at=(
                    doi_checked.validated_at
                    if doi_checked is not None
                    else getattr(r, "doi_validated_at", None)
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
    "identifier_kind_for",
    "select_for_check",
    "summarize_doi_completeness",
]
