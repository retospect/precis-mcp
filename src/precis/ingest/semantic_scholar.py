"""Semantic Scholar metadata lookup with tenacity retry."""

from __future__ import annotations

from typing import Any

from semanticscholar import SemanticScholar
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


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
    return sch.search_paper(title, limit=limit)


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
    }
