"""Semantic Scholar — cache-backed paper search.

The ``semanticscholar`` kind wraps Semantic Scholar's Graph API. A
``get(kind='semanticscholar', id='<query>')`` issues a paper search
and returns the top 10 hits as a structured markdown listing. The
``id`` is the natural-language query — same shape as perplexity-*.

Optional ``SEMANTIC_SCHOLAR_API_KEY`` env var raises the rate limit
from the public tier (~1 req/s) to the partner tier; the handler
works without one but is slower. We surface the missing-key state
as a one-time hint rather than an init failure.

Cache TTL: 30 days. S2 indexes new papers continuously but the
top-10 for a given query is stable on that timescale.
"""

from __future__ import annotations

import logging
import os
from typing import Any, ClassVar

from precis.errors import BadInput, Upstream
from precis.handlers._cache_base import CacheBackedHandler, FetchResult
from precis.protocol import KindSpec
from precis.store.types import BlockInsert
from precis.utils.optional_deps import require_optional
from precis.utils.slug import slug_from_text

log = logging.getLogger(__name__)

_S2_URL = "https://api.semanticscholar.org/graph/v1/paper/search"

#: Fields we request from the API. Trimmed to what we render — full
#: paper records carry citations/references which would inflate the
#: cached row 10x without a clear render use.
_S2_FIELDS = (
    "title,authors.name,year,abstract,externalIds,venue,"
    "citationCount,referenceCount,openAccessPdf"
)

#: How many top hits to retain. Keeps the body bounded and the
#: chunker-emitted chunks meaningful per-paper.
_S2_LIMIT = 10

_ATTRIBUTION = (
    "Source: Semantic Scholar (https://www.semanticscholar.org). "
    "Each cited paper is an external work — verify and cite the "
    "primary paper, not this aggregator query."
)


class SemanticScholarHandler(CacheBackedHandler):
    """``semanticscholar`` — paper search via the S2 Graph API."""

    spec: ClassVar[KindSpec] = KindSpec(
        kind="semanticscholar",
        title="Semantic Scholar paper search",
        description=(
            "Search Semantic Scholar's paper graph by natural-language "
            "query. Returns the top 10 ranked papers with title, "
            "authors, year, DOI / arXiv id, venue, abstract, and "
            "citation count. The body is one chunk per paper after "
            "the base-class auto-chunker splits it."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        is_numeric=False,
        id_required=True,
    )

    provider: ClassVar[str] = "semanticscholar"
    ttl_seconds: ClassVar[int | None] = 30 * 24 * 60 * 60  # 30 days
    attribution: ClassVar[str] = _ATTRIBUTION
    corpus_slug: ClassVar[str] = "default"
    example_query: ClassVar[str] = "carbon nanotube field-effect transistors"
    #: Per-call cost — the public tier is free, partner tier charges
    #: per query but it's tiny. Record None so the dashboard doesn't
    #: invent a per-call dollar figure.
    cost_per_call_usd: ClassVar[float] = 0.0

    # ── cache key + slug ──────────────────────────────────────────────

    def _canonical_key(self, query: str) -> str:
        q = (query or "").strip()
        if not q:
            raise BadInput(
                "semanticscholar requires a non-empty query",
                next="get(kind='semanticscholar', id='your search terms')",
            )
        # Lower-case + collapse whitespace so the same query in
        # different casings shares one cache row.
        return " ".join(q.lower().split())

    def _slug_for(self, key: str) -> str:
        return slug_from_text(key, max_len=60) or "semanticscholar-query"

    def _recover_key(self, ref, cache):  # type: ignore[no-untyped-def]
        return (cache.meta or {}).get("query")

    # ── fetch + render ────────────────────────────────────────────────

    def _fetch(self, key: str) -> FetchResult:
        httpx = require_optional("httpx", extra="external")
        api_key = (os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or "").strip()
        headers: dict[str, str] = {"User-Agent": "precis-mcp/1.0"}
        if api_key:
            headers["x-api-key"] = api_key
        params = {"query": key, "fields": _S2_FIELDS, "limit": _S2_LIMIT}
        try:
            with httpx.Client(timeout=30.0, headers=headers) as client:
                resp = client.get(_S2_URL, params=params)
        except httpx.HTTPError as exc:
            raise Upstream(f"Semantic Scholar transport error: {exc}") from exc

        if resp.status_code == 429:
            raise Upstream(
                "Semantic Scholar rate-limited (HTTP 429); the public tier "
                "is ~1 req/s. Set SEMANTIC_SCHOLAR_API_KEY for the partner "
                "tier or retry later.",
            )
        if resp.status_code == 401:
            raise Upstream(
                "Semantic Scholar rejected the API key (HTTP 401). "
                "Check SEMANTIC_SCHOLAR_API_KEY.",
            )
        if resp.status_code != 200:
            raise Upstream(
                f"Semantic Scholar HTTP {resp.status_code}: {resp.text[:200]}"
            )

        try:
            data = resp.json()
        except Exception as exc:
            raise Upstream(f"Semantic Scholar returned non-JSON: {exc}") from exc

        papers = data.get("data") or []
        if not papers:
            text = f'No Semantic Scholar results for "{key}".'
            return FetchResult(
                title=f"Semantic Scholar: {key}",
                body_blocks=[BlockInsert(pos=0, text=text)],
                cost_usd=None,
                meta={"query": key, "result_count": 0},
            )

        # One block per paper — the base-class auto-chunker would split
        # a single long blob anyway, but emitting per-paper blocks keeps
        # the chunk-level granularity meaningful for citation surface
        # (the chunk's text *is* a paper's entry, not a fragment of one).
        blocks: list[BlockInsert] = []
        for i, p in enumerate(papers):
            blocks.append(BlockInsert(pos=i, text=_format_paper(p)))

        return FetchResult(
            title=f"Semantic Scholar: {key}",
            body_blocks=blocks,
            cost_usd=None,
            meta={
                "query": key,
                "result_count": len(papers),
                "total_available": data.get("total"),
            },
        )


def _format_paper(p: dict[str, Any]) -> str:
    """Format one paper hit into a markdown-style block."""
    title = (p.get("title") or "(untitled)").strip()
    year = p.get("year") or "?"
    authors = p.get("authors") or []
    author_names = ", ".join(a.get("name", "") for a in authors[:6] if a.get("name"))
    if len(authors) > 6:
        author_names += f", et al. ({len(authors)} authors)"
    ext = p.get("externalIds") or {}
    doi = ext.get("DOI") or ""
    arxiv = ext.get("ArXiv") or ""
    venue = (p.get("venue") or "").strip()
    abstract = (p.get("abstract") or "").strip()
    cite_n = p.get("citationCount")
    oa = p.get("openAccessPdf") or {}
    oa_url = oa.get("url") if isinstance(oa, dict) else None

    lines: list[str] = [f"## {title} ({year})"]
    if author_names:
        lines.append(f"_Authors:_ {author_names}")
    if venue:
        lines.append(f"_Venue:_ {venue}")
    if cite_n is not None:
        lines.append(f"_Cited:_ {cite_n}")
    if doi:
        lines.append(f"_DOI:_ {doi} (https://doi.org/{doi})")
    if arxiv:
        lines.append(f"_arXiv:_ {arxiv} (https://arxiv.org/abs/{arxiv})")
    if oa_url:
        lines.append(f"_Open access PDF:_ {oa_url}")
    if abstract:
        lines.append("")
        lines.append(abstract)
    return "\n".join(lines)


__all__ = ["SemanticScholarHandler"]
