"""Citation graph lookup via Semantic Scholar API."""

from __future__ import annotations

from typing import Any

from semanticscholar import SemanticScholar

from precis.utils.http import external_retry
from precis.utils.rate_limit import acquire as acquire_rate_limit


def citations(paper_id: str, api_key: str = "") -> dict[str, list[dict[str, Any]]]:
    """Fetch references and cited-by for a paper via S2.

    Args:
        paper_id: DOI, arxiv ID, S2 paper ID, or acatome paper_id.

    Returns:
        Dict with 'references' and 'cited_by' lists.
    """
    if not api_key:
        from precis.secrets import get_secret

        api_key = get_secret("SEMANTIC_SCHOLAR_API_KEY") or ""
    sch = SemanticScholar(api_key=api_key) if api_key else SemanticScholar()

    # Normalize acatome paper_id prefixes
    s2_id = _to_s2_id(paper_id)

    refs = _get_references(sch, s2_id)
    cited = _get_citations(sch, s2_id)

    return {
        "references": refs,
        "cited_by": cited,
    }


def _to_s2_id(paper_id: str) -> str:
    """Convert acatome paper_id to S2-compatible ID."""
    if paper_id.startswith("doi:"):
        return paper_id  # S2 accepts DOI: prefix
    if paper_id.startswith("arxiv:"):
        return f"ARXIV:{paper_id[6:]}"
    return paper_id


def _shape_neighbor(item: Any) -> dict[str, Any]:
    """Shape one S2 reference/citation object (per-paper ``sch.get_paper``
    nested field, or batch ``sch.get_papers`` nested field — same attribute
    surface either way) into the ``{title, doi, year, s2_id}`` dict every
    caller persists as a citation neighbour. The single source of truth for
    that shape, so per-paper and batch fetches agree exactly."""
    return {
        "title": getattr(item, "title", "") or "",
        "doi": (
            (item.externalIds or {}).get("DOI")
            if hasattr(item, "externalIds")
            else None
        ),
        "year": getattr(item, "year", None),
        "s2_id": getattr(item, "paperId", None),
    }


@external_retry()
def _get_references(sch: SemanticScholar, paper_id: str) -> list[dict[str, Any]]:
    """Get papers this paper cites.

    Raises after the retry budget on S2 failure — swallowing here would
    make a transient error indistinguishable from a truly reference-less
    paper, and callers persist/stamp the result as truth.
    """
    acquire_rate_limit("s2")
    paper = sch.get_paper(
        paper_id,
        fields=["references.title", "references.externalIds", "references.year"],
    )
    if not paper or not hasattr(paper, "references") or not paper.references:
        return []
    return [_shape_neighbor(r) for r in paper.references]


@external_retry()
def _get_citations(sch: SemanticScholar, paper_id: str) -> list[dict[str, Any]]:
    """Get papers that cite this paper.

    Raises after the retry budget on S2 failure — see ``_get_references``.
    """
    acquire_rate_limit("s2")
    paper = sch.get_paper(
        paper_id,
        fields=["citations.title", "citations.externalIds", "citations.year"],
    )
    if not paper or not hasattr(paper, "citations") or not paper.citations:
        return []
    return [_shape_neighbor(c) for c in paper.citations]


# ── batched form (POST /paper/batch) ─────────────────────────────────
#
# Purely additive — nothing below is wired into ``citations()`` or any
# existing caller (``materialize_citation_edges``, ``chase``, ``watch_poll``,
# ``stub_rank``). It's a standalone entry point for a future many-papers-at-
# once caller to opt into.

#: Nested batch fields for refs+cited_by, plus the top-level fields needed
#: to (a) match a returned ``Paper`` back to its input id without relying on
#: position (``get_papers`` drops not-found entries, so a plain zip against
#: the input list silently misaligns) and (b) detect the batch endpoint's
#: silent 10MB/9999-citation truncation of the nested arrays.
_BATCH_FIELDS = [
    "paperId",
    "externalIds",
    "referenceCount",
    "citationCount",
    "references.title",
    "references.externalIds",
    "references.year",
    "references.paperId",
    "citations.title",
    "citations.externalIds",
    "citations.year",
    "citations.paperId",
]

#: `get_papers` hard-rejects any request outside 1..500 ids.
_BATCH_CHUNK_SIZE = 500


@external_retry()
def _get_papers_batch(sch: SemanticScholar, s2_ids: list[str]) -> list[Any]:
    """POST /paper/batch for one chunk (<=500 ids).

    Raises after the retry budget on S2 failure — see ``_get_references``; a
    transient batch failure must stay distinguishable from a batch that
    genuinely resolved nothing.
    """
    acquire_rate_limit("s2")
    return sch.get_papers(s2_ids, fields=_BATCH_FIELDS)


def _build_match_index(input_ids: list[str]) -> dict[str, str]:
    """Map every S2-addressable key an input id could come back as (its
    normalized ``_to_s2_id`` form, plus a bare DOI/ArXiv key) to the
    *original* input id, so returned ``Paper`` objects can be matched back
    by identity rather than by (unreliable, since ``get_papers`` drops
    not-found entries) position."""
    index: dict[str, str] = {}
    for original in input_ids:
        norm = _to_s2_id(original)
        index[norm] = original
        if norm.startswith("doi:"):
            index[norm[4:].lower()] = original
        elif norm.startswith("ARXIV:"):
            index[f"arxiv:{norm[6:]}"] = original
    return index


def _lookup_input_id(paper: Any, index: dict[str, str]) -> str | None:
    """Resolve a batch-returned ``Paper`` back to the input id it answers,
    trying its S2 ``paperId`` then its DOI/ArXiv external ids against
    ``index`` — never by position."""
    paper_id = getattr(paper, "paperId", None)
    if paper_id and paper_id in index:
        return index[paper_id]
    ext = getattr(paper, "externalIds", None) or {}
    doi = ext.get("DOI")
    if doi and str(doi).lower() in index:
        return index[str(doi).lower()]
    arxiv = ext.get("ArXiv")
    if arxiv and f"arxiv:{arxiv}" in index:
        return index[f"arxiv:{arxiv}"]
    return None


@external_retry()
def _paginated_references(sch: SemanticScholar, paper_id: str) -> list[dict[str, Any]]:
    """The truncation-fallback path: unlike ``get_paper``'s nested-field
    fetch (also capped at the same 10MB/9999 aggregate), the dedicated
    paginated endpoint pages through up to 9999 results a page at a time, so
    it recovers references a truncated batch response dropped."""
    acquire_rate_limit("s2")
    results = sch.get_paper_references(
        paper_id,
        fields=[
            "citedPaper.title",
            "citedPaper.externalIds",
            "citedPaper.year",
            "citedPaper.paperId",
        ],
        limit=1000,
    )
    return [
        _shape_neighbor(item.paper)
        for item in results
        if getattr(item, "paper", None) is not None
    ]


@external_retry()
def _paginated_citations(sch: SemanticScholar, paper_id: str) -> list[dict[str, Any]]:
    """The truncation-fallback path for cited-by — see
    ``_paginated_references``."""
    acquire_rate_limit("s2")
    results = sch.get_paper_citations(
        paper_id,
        fields=[
            "citingPaper.title",
            "citingPaper.externalIds",
            "citingPaper.year",
            "citingPaper.paperId",
        ],
        limit=1000,
    )
    return [
        _shape_neighbor(item.paper)
        for item in results
        if getattr(item, "paper", None) is not None
    ]


def _neighbors_for_paper(
    sch: SemanticScholar, paper: Any, fallback_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """References + cited-by for one batch-returned paper, shaped exactly
    like the per-paper path. The batch endpoint silently truncates nested
    arrays at ~10MB/9999 citations aggregate with no continuation token —
    detected here by comparing the returned nested-array length to the
    requested ``referenceCount``/``citationCount``, and repaired by falling
    back to the paginated per-paper endpoints (``_paginated_references``/
    ``_paginated_citations``) so coverage matches what ``citations()`` would
    have fetched for the same paper."""
    refs = [_shape_neighbor(r) for r in (getattr(paper, "references", None) or [])]
    cited = [_shape_neighbor(c) for c in (getattr(paper, "citations", None) or [])]

    ref_count = getattr(paper, "referenceCount", None)
    if ref_count is not None and len(refs) < ref_count:
        refs = _paginated_references(sch, fallback_id)

    cit_count = getattr(paper, "citationCount", None)
    if cit_count is not None and len(cited) < cit_count:
        cited = _paginated_citations(sch, fallback_id)

    return refs, cited


def citations_batch(
    paper_ids: list[str], api_key: str = ""
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Batched refs+cited_by for many papers via ``POST /paper/batch``.

    Fetches ``len(paper_ids)`` papers' reference/citation graphs in chunks
    of <=500 ids, one ``/paper/batch`` request per chunk, instead of two
    ``get_paper`` round-trips per paper the way :func:`citations` does.

    Args:
        paper_ids: DOI, arxiv ID, S2 paper ID, or acatome paper_id, each.

    Returns:
        ``{input_id: {"references": [...], "cited_by": [...]}}`` — every
        input id gets an entry (empty lists for ids the batch dropped as
        not-found, or that genuinely have no references/citations). Papers
        whose nested arrays were truncated by the batch endpoint's cap fall
        back to the paginated per-paper endpoints so coverage matches
        :func:`citations`.
    """
    if not api_key:
        from precis.secrets import get_secret

        api_key = get_secret("SEMANTIC_SCHOLAR_API_KEY") or ""
    sch = SemanticScholar(api_key=api_key) if api_key else SemanticScholar()

    out: dict[str, dict[str, list[dict[str, Any]]]] = {
        pid: {"references": [], "cited_by": []} for pid in paper_ids
    }

    for start in range(0, len(paper_ids), _BATCH_CHUNK_SIZE):
        chunk = paper_ids[start : start + _BATCH_CHUNK_SIZE]
        index = _build_match_index(chunk)
        s2_ids = [_to_s2_id(pid) for pid in chunk]
        papers = _get_papers_batch(sch, s2_ids)
        for paper in papers:
            original = _lookup_input_id(paper, index)
            if original is None:
                continue
            fallback_id = getattr(paper, "paperId", None) or _to_s2_id(original)
            refs, cited = _neighbors_for_paper(sch, paper, fallback_id)
            out[original] = {"references": refs, "cited_by": cited}

    return out
