"""Semantic Scholar metadata lookup with tenacity retry."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from semanticscholar import SemanticScholar
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from precis.utils.rate_limit import acquire as acquire_rate_limit


def lookup_s2(title: str, api_key: str = "", limit: int = 3) -> dict[str, Any] | None:
    """Search Semantic Scholar by title, return best match metadata.

    Args:
        title: Paper title to search for.
        api_key: Optional S2 API key for higher rate limits.
        limit: Max results to consider.

    Returns:
        Normalized metadata dict or None if not found.
    """
    sch = SemanticScholar(api_key=api_key) if api_key else SemanticScholar()
    results = _search_with_retry(sch, title, limit)
    if not results or not results.items:
        return None

    paper = results.items[0]
    return _normalize(paper)


@retry(
    wait=wait_exponential(min=1, max=60),
    stop=stop_after_attempt(5),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def _search_with_retry(sch: SemanticScholar, title: str, limit: int) -> Any:
    """Search S2 with exponential backoff on 429."""
    acquire_rate_limit("s2")
    return sch.search_paper(title, limit=limit)


def search_s2_papers(
    query: str, limit: int = 3, api_key: str = ""
) -> list[dict[str, Any]]:
    """Free-text S2 search, return up to ``limit`` normalized metadata dicts.

    Unlike :func:`lookup_s2` (best match only, used for title-matching an
    existing paper), this is a quest lit-search primitive: it hands back the
    whole result page so a caller can acquire several candidates per query.
    Degrades to ``[]`` on any error (bad query, network hiccup, rate limit
    exhaustion) — a lit-search step must never blow up a quest tick.
    """
    try:
        sch = SemanticScholar(api_key=api_key) if api_key else SemanticScholar()
        results = _search_with_retry(sch, query, limit)
    except Exception:
        return []
    if not results or not results.items:
        return []
    return [_normalize(paper) for paper in results.items[:limit]]


#: S2's batch endpoint (``POST /paper/batch``) caps at 500 ids per call
#: (the underlying ``semanticscholar`` lib raises ``ValueError`` above
#: this) — :func:`get_papers_batch` chunks any longer ``ids`` list.
_BATCH_CHUNK_SIZE = 500


def get_papers_batch(ids: list[str], api_key: str = "") -> list[dict[str, Any] | None]:
    """Batch-resolve S2 metadata for ``ids``, one slot per input id.

    ``ids`` may mix S2 paper-id shas with the lib's prefixed forms
    (``DOI:10.1234/...``, ``ARXIV:2401.12345``, ``CorpusId:...``, ...) —
    see :meth:`semanticscholar.SemanticScholar.get_papers`'s own
    docstring for the full id vocabulary. Chunks internally at
    :data:`_BATCH_CHUNK_SIZE` (the API's per-call cap) so a caller can
    hand this an arbitrarily long list.

    Returns a list the same length as ``ids``, same order: each slot is
    a normalized metadata dict (see :func:`_normalize`) for a resolved
    id, or ``None`` for one S2 couldn't resolve — mirroring
    :func:`get_paper_by_id`'s graceful-degrade-to-None contract so a
    caller enriching many stubs at once doesn't lose the whole batch to
    one bad id, and a whole-chunk API failure (network blip, exhausted
    retry budget) degrades every id in that chunk to ``None`` rather
    than raising.

    S2's batch response is a JSON array positionally aligned with the
    request, with ``null`` for an unresolved id — the ``semanticscholar``
    lib's ``get_papers`` already drops those nulls (so the returned
    ``Paper`` list can be *shorter* than the request), and separately
    reports which ids didn't resolve via ``return_not_found=True``. That
    leaves matching each surviving ``Paper`` back to *which* requested id
    it answers — a paper resolved via ``DOI:...`` doesn't carry that
    string as its ``paperId``. We build a lookup keyed on every identifier
    form a ``Paper`` actually carries (its ``paperId`` plus each
    ``"<prefix>:<value>"`` in ``externalIds``, all lower-cased) and probe
    it with each requested id lower-cased — the same identifier space the
    lib itself matches against (see its private ``_get_not_found_ids``).
    """
    if not ids:
        return []
    sch = SemanticScholar(api_key=api_key) if api_key else SemanticScholar()
    out: list[dict[str, Any] | None] = []
    for start in range(0, len(ids), _BATCH_CHUNK_SIZE):
        chunk = ids[start : start + _BATCH_CHUNK_SIZE]
        try:
            papers, _not_found = _get_papers_with_retry(sch, chunk)
        except Exception:
            out.extend([None] * len(chunk))
            continue
        lookup: dict[str, Any] = {}
        for paper in papers:
            for key in _id_keys_for_paper(paper):
                lookup.setdefault(key, paper)
        for raw_id in chunk:
            paper = lookup.get(raw_id.strip().lower())
            out.append(_normalize(paper) if paper is not None else None)
    return out


@retry(
    wait=wait_exponential(min=1, max=60),
    stop=stop_after_attempt(5),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def _get_papers_with_retry(
    sch: SemanticScholar, paper_ids: list[str]
) -> tuple[list[Any], list[str]]:
    """Batch-get with exponential backoff on 429."""
    acquire_rate_limit("s2")
    return sch.get_papers(paper_ids, return_not_found=True)


def _id_keys_for_paper(paper: Any) -> set[str]:
    """Every lower-cased identifier form ``paper`` can be looked up by.

    Includes the bare ``paperId`` plus one ``"<prefix>:<value>"`` key per
    ``externalIds`` entry (``DOI``, ``ArXiv``, ``PubMed``, ...) — matching
    the lower-cased-prefix form :func:`get_papers_batch` probes with, so
    a request id like ``"DOI:10.1234/x"`` or ``"ARXIV:2401.12345"``
    round-trips regardless of the case S2 echoes the prefix back in.
    """
    keys: set[str] = set()
    paper_id = getattr(paper, "paperId", None)
    if paper_id:
        keys.add(str(paper_id).lower())
    external = getattr(paper, "externalIds", None) or {}
    for prefix, value in external.items():
        if prefix and value is not None:
            keys.add(f"{prefix}:{value}".lower())
    return keys


def get_paper_by_id(paper_id: str, api_key: str = "") -> dict[str, Any] | None:
    """Fetch a single paper by S2 paper ID, DOI, or arxiv ID."""
    sch = SemanticScholar(api_key=api_key) if api_key else SemanticScholar()
    try:
        paper = _get_with_retry(sch, paper_id)
    except Exception:
        return None
    if not paper:
        return None
    return _normalize(paper)


@retry(
    wait=wait_exponential(min=1, max=60),
    stop=stop_after_attempt(5),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def _get_with_retry(sch: SemanticScholar, paper_id: str) -> Any:
    """Get paper with exponential backoff on 429."""
    acquire_rate_limit("s2")
    return sch.get_paper(paper_id)


def _normalize(paper: Any) -> dict[str, Any]:
    """Normalize S2 paper object to acatome header format."""
    authors = []
    if hasattr(paper, "authors") and paper.authors:
        for a in paper.authors:
            name = getattr(a, "name", None) or str(a)
            authors.append({"name": name})

    # Capture the FULL externalIds cluster verbatim (S2 keys: DOI,
    # ArXiv, PubMed, PubMedCentralID, MAG, DBLP, CorpusId, OpenAlex)
    # so the precis ingest path can write a `ref_identifiers` row per
    # alias. Two papers will frequently share a paper-id cluster but
    # carry a journal DOI, an arXiv DOI, a PubMed id, and an OpenAlex
    # id all at once — capturing all of them up front means the alias
    # index is fully populated at ingest time without any second S2
    # round-trip. Older bundles missing this field are handled
    # gracefully by precis (empty dict default).
    raw_external = getattr(paper, "externalIds", None) or {}
    external_ids: dict[str, str] = {}
    if raw_external:
        for k, v in raw_external.items():
            if not k or v is None:
                continue
            sv = str(v).strip()
            if sv:
                external_ids[str(k)] = sv

    return {
        "title": getattr(paper, "title", "") or "",
        "authors": authors,
        "year": getattr(paper, "year", None),
        "doi": external_ids.get("DOI"),
        "arxiv_id": external_ids.get("ArXiv"),
        "s2_id": getattr(paper, "paperId", None),
        "external_ids": external_ids,
        "journal": getattr(paper, "venue", "") or "",
        "abstract": getattr(paper, "abstract", "") or "",
        "entry_type": "article",
        "source": "s2",
        # Additive (stub_rank enrich, docs/backlog): S2's field-of-study
        # classification, deduped. ``s2FieldsOfStudy`` (structured,
        # ``[{"category": ..., "source": ...}]``) is preferred — it's the
        # model-scored classification, vs. the legacy ``fieldsOfStudy``
        # (a bare category list, often sparser); we fold in both so a
        # paper carrying only the legacy field still gets a non-empty
        # list. Order is stable (first-seen wins) so a caller diffing
        # across enrich passes sees a deterministic list, not set-order
        # churn.
        "fields": _dedup_fields(paper),
        # Citation count rides along free (already in the default
        # ``SEARCH_FIELDS`` fetched by every call in this module) — the
        # stub_rank pass wants it without a second S2 round-trip.
        "citation_count": getattr(paper, "citationCount", None),
    }


def s2_stub_meta(resolved: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    """The ``refs.meta`` patch to write at MINT time for a stub that already
    holds a normalized S2 dict (this module's :func:`_normalize` shape).

    This is the mint-time twin of ``stub_rank._merge_enrich_meta``'s
    resolved-case patch: stamping ``s2_enriched_at`` here is what makes
    ``stub_rank._claim_enrich_candidates``'s ``meta->>'s2_enriched_at' IS
    NULL`` predicate skip this stub — a caller that already paid for an
    S2 round-trip at mint time shouldn't force ``stub_rank`` to pay for a
    second one later. Includes ``abstract`` only when ``resolved`` carries
    a non-empty one (a caller merging this into a fresh mint's meta has no
    "existing abstract" to protect, unlike ``stub_rank``'s never-clobber
    merge).
    """
    patch: dict[str, Any] = {
        "s2_enriched_at": now.isoformat(),
        "s2_fields": resolved.get("fields") or [],
        "s2_citation_count": resolved.get("citation_count"),
    }
    abstract = resolved.get("abstract")
    if isinstance(abstract, str) and abstract.strip():
        patch["abstract"] = abstract
    return patch


def s2_stub_meta_if_present(
    resolved: dict[str, Any], *, now: datetime
) -> dict[str, Any] | None:
    """:func:`s2_stub_meta`, but only when ``resolved`` actually carries
    something beyond the bare title/doi/year/s2_id shape.

    Some S2 call sites (e.g. the citations endpoint, called with a
    deliberately narrow ``fields=``) hand back a dict with no
    ``abstract``/``fields``/``citation_count`` at all. Building (and
    stamping) the mint-time S2 patch from such a dict would set
    ``s2_enriched_at`` on empty data — which would permanently skip
    ``stub_rank``'s real enrich pass for that stub (its ``meta->>
    's2_enriched_at' IS NULL`` claim predicate would never fire again).
    This guard is the shared home for that check — was hand-copied at
    ``workers/chase.py::_ref_to_target`` and ``workers/inbound_chase.py::
    _resolve_or_ingest_citer`` before this helper existed. Returns
    ``None`` when ``resolved`` has no non-empty ``abstract``, no truthy
    ``fields``, and ``citation_count is None``; otherwise the same patch
    :func:`s2_stub_meta` would build.
    """
    abstract = resolved.get("abstract")
    has_abstract = isinstance(abstract, str) and bool(abstract.strip())
    if not (
        has_abstract
        or resolved.get("fields")
        or resolved.get("citation_count") is not None
    ):
        return None
    return s2_stub_meta(resolved, now=now)


def _dedup_fields(paper: Any) -> list[str]:
    """Deduped field-of-study category strings for ``paper``, order-stable."""
    seen: set[str] = set()
    out: list[str] = []
    s2_fields = getattr(paper, "s2FieldsOfStudy", None) or []
    for entry in s2_fields:
        category = entry.get("category") if isinstance(entry, dict) else None
        if category and category not in seen:
            seen.add(category)
            out.append(category)
    legacy_fields = getattr(paper, "fieldsOfStudy", None) or []
    for category in legacy_fields:
        if category and category not in seen:
            seen.add(category)
            out.append(category)
    return out
