"""Fetch missing DOIs — phase 2 of the draft DOI-completeness check (gr690484).

Phase 1 (``precis.export.retraction``) *reports* which cited papers carry no
DOI and which of those are fetchable. This is the fix action, mirroring the
retraction button: for a ref with no DOI but an ``arxiv`` / ``pubmed`` /
``s2`` identifier, resolve a DOI and write it back onto the ref.

Resolution order, first hit wins:

1. **Identifier lookup** (``arxiv``, ``pubmed``): OpenAlex by that id
   (:func:`precis.ingest.openalex_meta.fetch_openalex_work_by_identifier`);
   the work's DOI is taken verbatim.
2. **Title + first-author search** (every fetchable kind, and the only route
   for ``s2``): Crossref ``/works`` via the provenance candidate search, then
   OpenAlex search as a second source. A hit is accepted only when its title
   is a near-exact token match for the ref's own title and the first-author
   surname agrees (when both sides have one). A near miss is *not* written.

A ref with no persistent identifier (title-only, bare stub) is never
touched — it stays flagged "no persistent identifier" in the report.

TTL: every attempt that reached a conclusion (resolved or definitively
unresolved) stamps ``meta.doi_fetch_attempted_at``; inside
:data:`DOI_FETCH_TTL_DAYS` a re-press is a no-network no-op unless forced.
A transport failure stamps nothing, so it is retried next press.

Preprint, dataset and thesis entry types are not exempt: an unresolved one
reports an ``entry_type`` note so the author decides whether a DOI is owed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from precis.errors import BadInput
from precis.identity import is_placeholder_title
from precis.ingest._text_norm import best_jaccard, surname_matches
from precis.ingest.provenance import validate_doi
from precis.store import Store
from precis.utils.authors import author_names
from precis.utils.rate_limit import RateLimited

log = logging.getLogger(__name__)

#: Default window before an unresolved ref is searched again.
DOI_FETCH_TTL_DAYS = 14

#: ``refs.meta`` key holding the ISO timestamp of the last conclusive attempt.
FETCH_ATTEMPTED_KEY = "doi_fetch_attempted_at"

#: Non-DOI id kinds that make a ref a fetch target (the report's
#: ``identifier_kind_for`` set).
FETCHABLE_ID_KINDS: tuple[str, ...] = ("arxiv", "pubmed", "s2")

#: Title token-Jaccard a search hit must clear to be written.
_TITLE_ACCEPT = 0.85

#: Crossref ``type`` / ``meta.entry_type`` values where a missing DOI is
#: plausibly legitimate — flagged with a note, never silently exempted.
_NOTE_ENTRY_TYPES = frozenset(
    {
        "posted-content",
        "preprint",
        "dataset",
        "dissertation",
        "thesis",
        "phdthesis",
        "mastersthesis",
    }
)

DoiFetchOutcome = Literal[
    "fresh",
    "resolved",
    "unresolved",
    "unchecked",
    "has_doi",
    "no_identifier",
    "missing",
]

#: Injectable seams (tests): ``(identifier, email) -> OpenAlex work | None``,
#: ``(title, first_author, mailto) -> [{"doi", "title", "first_author", "year"}]``.
IdentifierFn = Callable[[str, str], "dict[str, Any] | None"]
SearchFn = Callable[[str, "str | None", "str | None"], "Iterable[dict[str, Any]]"]


@dataclass(frozen=True, slots=True)
class DoiFetchCheck:
    """What one :func:`fetch_missing_doi` call did."""

    ref_id: int
    outcome: DoiFetchOutcome
    doi: str | None = None
    #: Which route produced ``doi``: ``'openalex-id'`` / ``'crossref-title'``
    #: / ``'openalex-title'``.
    source: str | None = None
    error: str | None = None
    #: Entry-type caveat on an unresolved ref (preprint/dataset/thesis).
    note: str | None = None


def _openalex_doi(work: dict[str, Any] | None) -> str | None:
    raw = (work or {}).get("doi")
    return validate_doi(raw) if isinstance(raw, str) and raw else None


def _by_identifier(email: str, ids: dict[str, str], fetch: IdentifierFn) -> str | None:
    for kind in ("arxiv", "pubmed"):
        value = ids.get(kind)
        if value:
            identifier = f"pmid:{value}" if kind == "pubmed" else f"arxiv:{value}"
            doi = _openalex_doi(fetch(identifier, email))
            if doi:
                return doi
    return None


def _default_identifier_fn(identifier: str, email: str) -> dict[str, Any] | None:
    from precis.ingest.openalex_meta import fetch_openalex_work_by_identifier

    return fetch_openalex_work_by_identifier(identifier, email=email)


def _default_search_fn(
    title: str, author: str | None, mailto: str | None
) -> Iterable[dict[str, Any]]:
    """Crossref candidates first, OpenAlex as the second source."""
    from precis.ingest.provenance import (
        _extract_authors,
        _extract_title,
        _extract_year,
        _search_crossref_works,
    )

    crossref_error = None
    try:
        items = _search_crossref_works(
            title=title, author=author, mailto=mailto, rows=5, raise_on_error=True
        )
    except RateLimited:
        raise
    except Exception as exc:
        crossref_error = exc
        items = []
    for item in items:
        doi = validate_doi(str(item.get("DOI") or ""))
        if not doi:
            continue
        names = [a.get("name", "") for a in _extract_authors(item) or []]
        yield {
            "doi": doi,
            "title": _extract_title(item) or "",
            "first_author": names[0].split(",")[0].strip() if names else "",
            "year": _extract_year(item),
            "source": "crossref-title",
        }
    from precis.ingest.openalex_meta import _authorships, search_openalex_works

    for work in search_openalex_works(title, per_page=5, email=mailto or ""):
        doi = _openalex_doi(work)
        if not doi:
            continue
        ships = _authorships(work)
        # OpenAlex display names read "Given Family": the surname is last.
        words = ships[0]["name"].split() if ships else []
        first = words[-1] if words else ""
        yield {
            "doi": doi,
            "title": str(work.get("title") or work.get("display_name") or ""),
            "first_author": first,
            "year": work.get("publication_year"),
            "source": "openalex-title",
        }
    if crossref_error is not None:
        raise crossref_error


def _first_surname(ref: Any) -> str | None:
    names = author_names(getattr(ref, "authors", None), order="sortable")
    return names[0].split(",")[0].strip() or None if names else None


def _accept(ref: Any, cand: dict[str, Any], surname: str | None) -> bool:
    title = str(getattr(ref, "title", "") or "")
    if not cand.get("title") or best_jaccard(title, cand["title"])[0] < _TITLE_ACCEPT:
        return False
    if surname and cand.get("first_author"):
        if not surname_matches(surname, cand["first_author"]):
            return False
    year, cyear = getattr(ref, "year", None), cand.get("year")
    # Preprint -> journal years legitimately differ by a little.
    return not (year and cyear and abs(int(year) - int(cyear)) > 2)


def _by_title(ref: Any, mailto: str | None, search: SearchFn) -> tuple[str, str] | None:
    title = str(getattr(ref, "title", "") or "").strip()
    if not title or is_placeholder_title(title):
        return None
    surname = _first_surname(ref)
    for cand in search(title, surname, mailto):
        if _accept(ref, cand, surname):
            return cand["doi"], str(cand.get("source") or "crossref-title")
    return None


def _attempted_at(ref: Any) -> datetime | None:
    raw = (getattr(ref, "meta", None) or {}).get(FETCH_ATTEMPTED_KEY)
    if not isinstance(raw, str):
        return None
    try:
        at = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return at if at.tzinfo is not None else None


def doi_fetch_need_key(ref: Any) -> tuple[bool, datetime | None]:
    """Prioritize never-attempted cites, then the oldest DOI fetch attempts."""
    at = _attempted_at(ref)
    return (at is not None, at)


def missing_doi_entry_type_note(meta: dict[str, Any]) -> str | None:
    """Flag entry types for which a missing DOI may be legitimate."""
    entry_type = str(meta.get("entry_type") or "").lower()
    if entry_type in _NOTE_ENTRY_TYPES:
        return f"Entry type {entry_type}: a DOI may not exist. Author decides."
    return None


def fetch_missing_doi(
    store: Store,
    ref_id: int,
    *,
    force: bool = False,
    ttl_days: int = DOI_FETCH_TTL_DAYS,
    mailto: str | None = None,
    identifier_fn: IdentifierFn | None = None,
    search_fn: SearchFn | None = None,
) -> DoiFetchCheck:
    """Resolve and write back one ref's missing DOI (see module docstring)."""
    ref = store.fetch_refs_by_ids([ref_id]).get(ref_id)
    if ref is None:
        return DoiFetchCheck(ref_id=ref_id, outcome="missing")
    ids = store.identifiers_for_refs([ref_id]).get(ref_id, {})
    if ids.get("doi"):
        return DoiFetchCheck(ref_id=ref_id, outcome="has_doi", doi=ids["doi"])
    if not any(ids.get(k) for k in FETCHABLE_ID_KINDS):
        return DoiFetchCheck(ref_id=ref_id, outcome="no_identifier")

    meta = getattr(ref, "meta", None) or {}
    at = _attempted_at(ref)
    if (
        not force
        and at is not None
        and datetime.now(UTC) - at < timedelta(days=ttl_days)
    ):
        return DoiFetchCheck(ref_id=ref_id, outcome="fresh")

    by_id = identifier_fn or _default_identifier_fn
    search = search_fn or _default_search_fn
    try:
        doi = _by_identifier(mailto or "", ids, by_id)
        source = "openalex-id" if doi else None
        if doi is None:
            hit = _by_title(ref, mailto, search)
            doi, source = hit if hit else (None, None)
    except RateLimited:
        raise
    except Exception as exc:
        log.warning("doi_fetch: lookup failed for #%d", ref_id, exc_info=True)
        return DoiFetchCheck(ref_id=ref_id, outcome="unchecked", error=str(exc)[:200])

    wrote = False
    error = None
    if doi is not None:
        try:
            wrote = store.set_ref_identifier(
                ref_id, "doi", doi, source="doi-fetch", only_if_missing=True
            )
        except BadInput as exc:
            error = str(exc)[:200]
        else:
            if not wrote:
                error = "not written"
    store.update_paper_fields(
        ref_id,
        meta_patch={FETCH_ATTEMPTED_KEY: datetime.now(UTC).isoformat()},
        source="doi-fetch",
    )
    if not wrote:
        return DoiFetchCheck(
            ref_id=ref_id,
            outcome="unresolved",
            error=error,
            note=missing_doi_entry_type_note(meta),
        )
    return DoiFetchCheck(ref_id=ref_id, outcome="resolved", doi=doi, source=source)


def fetch_missing_dois(
    store: Store,
    ref_ids: Sequence[int],
    *,
    force: bool = False,
    ttl_days: int = DOI_FETCH_TTL_DAYS,
    mailto: str | None = None,
    identifier_fn: IdentifierFn | None = None,
    search_fn: SearchFn | None = None,
) -> list[DoiFetchCheck]:
    """:func:`fetch_missing_doi` over a list, in order, deduped."""
    seen: set[int] = set()
    out: list[DoiFetchCheck] = []
    for ref_id in ref_ids:
        if ref_id in seen:
            continue
        seen.add(ref_id)
        out.append(
            fetch_missing_doi(
                store,
                ref_id,
                force=force,
                ttl_days=ttl_days,
                mailto=mailto,
                identifier_fn=identifier_fn,
                search_fn=search_fn,
            )
        )
    return out


__all__ = [
    "DOI_FETCH_TTL_DAYS",
    "FETCHABLE_ID_KINDS",
    "DoiFetchCheck",
    "fetch_missing_doi",
    "fetch_missing_dois",
]
