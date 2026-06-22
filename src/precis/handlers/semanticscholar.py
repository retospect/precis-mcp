"""Semantic Scholar — cache-backed paper search + citation-graph nav.

The ``semanticscholar`` kind wraps Semantic Scholar's Graph API. The
``id`` selects between three modes:

* ``get(kind='semanticscholar', id='<query>')`` — a paper *search*:
  the top-10 ranked hits for a natural-language query, as a structured
  markdown listing (same shape as perplexity-*).
* ``get(kind='semanticscholar', id='refs:<paper-id>')`` — the papers
  *this* paper cites (its reference list / bibliography).
* ``get(kind='semanticscholar', id='cites:<paper-id>')`` — the papers
  that cite this one (its forward citations).

The ``refs:`` / ``cites:`` modes are how you **navigate a known
paper's citation graph** to discover a primary source the corpus
doesn't hold yet: every returned row carries the cited/citing paper's
DOI / arXiv id, which feeds straight into a
``put(kind='paper', doi=…)`` acquisition stub. ``<paper-id>`` is any
S2-resolvable handle — a bare DOI (``10.x/y``), an arXiv id
(``2401.00001``), a raw S2 paper hash, or an explicitly-prefixed
``DOI:`` / ``ARXIV:`` / ``CorpusId:`` / ``PMID:`` form.

Optional ``SEMANTIC_SCHOLAR_API_KEY`` env var raises the rate limit
from the public tier (~1 req/s) to the partner tier; the handler
works without one but is slower. We surface the missing-key state
as a one-time hint rather than an init failure.

Cache TTL: 30 days. S2 indexes new papers continuously but the
top-10 for a query — and a paper's reference list — are stable on
that timescale.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, ClassVar

from precis.errors import BadInput, Upstream
from precis.handlers._cache_base import CacheBackedHandler, FetchResult
from precis.protocol import KindSpec
from precis.store.types import BlockInsert
from precis.utils.optional_deps import require_optional
from precis.utils.slug import slug_from_text

log = logging.getLogger(__name__)

_S2_PAPER_BASE = "https://api.semanticscholar.org/graph/v1/paper"
_S2_URL = f"{_S2_PAPER_BASE}/search"

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

#: Per-paper fields for the citation-graph (references / citations)
#: endpoints. The nested paper record shares the search field shape,
#: so ``_format_paper`` renders both — we only trim ``referenceCount``
#: (not useful one hop out).
_NAV_FIELDS = (
    "title,authors.name,year,abstract,externalIds,venue,"
    "citationCount,openAccessPdf"
)

#: Page size for a citation-graph walk. Reference lists run to
#: hundreds; cap the cached body so it stays a bounded, scannable
#: page (the agent is hunting for one missing source, not archiving
#: the whole bibliography). Truncation is surfaced in the meta.
_NAV_LIMIT = 50

#: ``id=`` prefixes that switch ``get`` from search to a graph walk.
#: ``refs`` → papers this one cites; ``cites`` → papers citing it.
_NAV_PREFIXES = ("refs", "cites")

#: Bare-arXiv-id shape (new-style ``2401.00001`` with optional ``vN``).
#: Used to auto-prefix a path id when the caller passes a naked id.
_ARXIV_RE = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")

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
            "query (top-10 hits with title, authors, year, DOI / arXiv "
            "id, venue, abstract, citation count), OR walk a known "
            "paper's citation graph: id='refs:<paper-id>' lists the "
            "papers it cites, id='cites:<paper-id>' the papers citing "
            "it — each row carrying the DOI to feed a "
            "put(kind='paper', doi=…) acquisition stub. One chunk per "
            "paper after the base-class auto-chunker splits it."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        is_numeric=False,
        id_required=True,
    )

    # Must match a row in the `providers` table. The Semantic Scholar
    # provider is registered under the slug `s2` (0001_initial seed) —
    # stamping the literal `semanticscholar` here violated the
    # refs.provider FK on every cache write, so the kind raised a
    # ForeignKeyViolation after a successful API fetch (gripe #39242).
    provider: ClassVar[str] = "s2"
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
        # different casings shares one cache row. Identifiers under a
        # nav prefix lower-case safely too (DOIs are case-insensitive,
        # arXiv ids numeric, S2 hashes lower-hex).
        low = " ".join(q.lower().split())
        for mode in _NAV_PREFIXES:
            prefix = f"{mode}:"
            if low.startswith(prefix):
                ident = low[len(prefix) :].strip()
                if not ident:
                    raise BadInput(
                        f"semanticscholar {prefix} needs a paper id "
                        "(DOI / arXiv / S2)",
                        next=(
                            f"get(kind='semanticscholar', "
                            f"id='{prefix}10.1038/nature12373')"
                        ),
                    )
                return f"{prefix}{ident}"
        return low

    @staticmethod
    def _parse_nav_key(key: str) -> tuple[str, str] | None:
        """Split a canonical key into ``(mode, ident)`` or ``None``.

        ``None`` is the plain-search path; ``('refs', '10.x/y')`` /
        ``('cites', '10.x/y')`` are the two graph-walk paths.
        """
        for mode in _NAV_PREFIXES:
            prefix = f"{mode}:"
            if key.startswith(prefix):
                return mode, key[len(prefix) :]
        return None

    @staticmethod
    def _s2_path_id(ident: str) -> str:
        """Map an agent-supplied paper id to an S2 graph path segment.

        S2's ``/paper/{id}`` accepts a bare hash or a prefixed handle
        (``DOI:`` / ``ARXIV:`` / ``CorpusId:`` / ``PMID:`` / …). We let
        an already-prefixed id through (normalising the two common
        casings) and auto-prefix a naked DOI or arXiv id; anything else
        is assumed to be a raw S2 paper hash.
        """
        r = ident.strip()
        low = r.lower()
        if low.startswith("doi:"):
            return "DOI:" + r[4:]
        if low.startswith("arxiv:"):
            return "ARXIV:" + r[6:]
        if low.startswith("s2:"):
            return r[3:]  # bare S2 paper hash, no prefix in the path
        if low.startswith(("corpusid:", "pmid:", "pmcid:", "mag:", "acl:", "url:")):
            return r  # S2 accepts these verbatim
        if r.startswith("10."):
            return "DOI:" + r
        if _ARXIV_RE.match(r):
            return "ARXIV:" + r
        return r  # assume a raw S2 paper hash

    def _slug_for(self, key: str) -> str:
        return slug_from_text(key, max_len=60) or "semanticscholar-query"

    def _recover_key(self, ref, cache):  # type: ignore[no-untyped-def]
        meta = cache.meta or {}
        # New rows stamp the canonical key directly; fall back to the
        # legacy ``query`` field for search rows written before that.
        return meta.get("key") or meta.get("query")

    # ── fetch + render ────────────────────────────────────────────────

    def _fetch(self, key: str) -> FetchResult:
        nav = self._parse_nav_key(key)
        if nav is not None:
            return self._fetch_graph(key, *nav)
        return self._fetch_search(key)

    @staticmethod
    def _s2_get_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
        """Issue one S2 Graph GET and return parsed JSON (or raise).

        Shared by the search and citation-graph paths so the
        rate-limit / auth / transport handling lives in one place.
        """
        httpx = require_optional("httpx", extra="external")
        api_key = (os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or "").strip()
        headers: dict[str, str] = {"User-Agent": "precis-mcp/1.0"}
        if api_key:
            headers["x-api-key"] = api_key
        try:
            with httpx.Client(timeout=30.0, headers=headers) as client:
                resp = client.get(url, params=params)
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
        if resp.status_code == 404:
            raise Upstream(
                "Semantic Scholar has no record for that paper id (HTTP 404). "
                "Check the DOI / arXiv / S2 id you passed after refs:/cites:.",
            )
        if resp.status_code != 200:
            raise Upstream(
                f"Semantic Scholar HTTP {resp.status_code}: {resp.text[:200]}"
            )

        try:
            return resp.json()
        except Exception as exc:
            raise Upstream(f"Semantic Scholar returned non-JSON: {exc}") from exc

    def _fetch_search(self, key: str) -> FetchResult:
        params = {"query": key, "fields": _S2_FIELDS, "limit": _S2_LIMIT}
        data = self._s2_get_json(_S2_URL, params)

        papers = data.get("data") or []
        if not papers:
            text = f'No Semantic Scholar results for "{key}".'
            return FetchResult(
                title=f"Semantic Scholar: {key}",
                body_blocks=[BlockInsert(pos=0, text=text)],
                cost_usd=None,
                meta={"key": key, "query": key, "result_count": 0},
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
                "key": key,
                "query": key,
                "result_count": len(papers),
                "total_available": data.get("total"),
            },
        )

    def _fetch_graph(self, key: str, mode: str, ident: str) -> FetchResult:
        """Walk one hop of a paper's citation graph.

        ``mode='refs'`` → ``/paper/{id}/references`` (papers this one
        cites); ``mode='cites'`` → ``/paper/{id}/citations`` (papers
        citing it). The endpoint returns the *neighbour* paper nested
        under ``citedPaper`` / ``citingPaper``; we lift it out and
        render each with the shared per-paper formatter.
        """
        endpoint = "references" if mode == "refs" else "citations"
        nested = "citedPaper" if mode == "refs" else "citingPaper"
        verb = "cited by" if mode == "refs" else "citing"
        path_id = self._s2_path_id(ident)
        url = f"{_S2_PAPER_BASE}/{path_id}/{endpoint}"
        data = self._s2_get_json(url, {"fields": _NAV_FIELDS, "limit": _NAV_LIMIT})

        rows = data.get("data") or []
        papers = [
            row[nested]
            for row in rows
            if isinstance(row, dict) and isinstance(row.get(nested), dict)
        ]
        if not papers:
            text = f"No {endpoint} found for {ident} on Semantic Scholar."
            return FetchResult(
                title=f"S2 {endpoint}: {ident}",
                body_blocks=[BlockInsert(pos=0, text=text)],
                cost_usd=None,
                meta={"key": key, "nav": mode, "paper": ident, "result_count": 0},
            )

        blocks = [BlockInsert(pos=i, text=_format_paper(p)) for i, p in enumerate(papers)]
        # Title says which way the hop runs + how many we kept, so a
        # capped page reads as "first N", not "the complete list".
        suffix = f" ({len(papers)} shown, capped at {_NAV_LIMIT})" if (
            len(papers) >= _NAV_LIMIT
        ) else f" ({len(papers)})"
        return FetchResult(
            title=f"S2 papers {verb} {ident}{suffix}",
            body_blocks=blocks,
            cost_usd=None,
            meta={
                "key": key,
                "nav": mode,
                "paper": ident,
                "result_count": len(papers),
                "capped": len(papers) >= _NAV_LIMIT,
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
