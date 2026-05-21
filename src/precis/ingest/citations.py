"""Citation graph lookup via Semantic Scholar API."""

from __future__ import annotations

import os
from typing import Any

from semanticscholar import SemanticScholar
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


def citations(paper_id: str, api_key: str = "") -> dict[str, list[dict[str, Any]]]:
    """Fetch references and cited-by for a paper via S2.

    Args:
        paper_id: DOI, arxiv ID, S2 paper ID, or acatome paper_id.

    Returns:
        Dict with 'references' and 'cited_by' lists.
    """
    api_key = api_key or os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")
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


@retry(
    wait=wait_exponential(min=1, max=60),
    stop=stop_after_attempt(5),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def _get_references(sch: SemanticScholar, paper_id: str) -> list[dict[str, Any]]:
    """Get papers this paper cites."""
    try:
        paper = sch.get_paper(
            paper_id,
            fields=["references.title", "references.externalIds", "references.year"],
        )
        if not paper or not hasattr(paper, "references") or not paper.references:
            return []
        return [
            {
                "title": getattr(r, "title", "") or "",
                "doi": (
                    (r.externalIds or {}).get("DOI")
                    if hasattr(r, "externalIds")
                    else None
                ),
                "year": getattr(r, "year", None),
                "s2_id": getattr(r, "paperId", None),
            }
            for r in paper.references
        ]
    except Exception:
        return []


@retry(
    wait=wait_exponential(min=1, max=60),
    stop=stop_after_attempt(5),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def _get_citations(sch: SemanticScholar, paper_id: str) -> list[dict[str, Any]]:
    """Get papers that cite this paper."""
    try:
        paper = sch.get_paper(
            paper_id,
            fields=["citations.title", "citations.externalIds", "citations.year"],
        )
        if not paper or not hasattr(paper, "citations") or not paper.citations:
            return []
        return [
            {
                "title": getattr(c, "title", "") or "",
                "doi": (
                    (c.externalIds or {}).get("DOI")
                    if hasattr(c, "externalIds")
                    else None
                ),
                "year": getattr(c, "year", None),
                "s2_id": getattr(c, "paperId", None),
            }
            for c in paper.citations
        ]
    except Exception:
        return []
