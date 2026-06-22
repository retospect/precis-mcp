"""``news`` kind — multi-source news articles in the shared corpus.

A ``news`` ref is a single news article: its URL is the cache key, its
title the slug, its extracted body block-split + embedded like the
``web`` kind, so ``search(kind='news', q=...)`` lands hits inside
article text. Articles are minted two ways, both routing through the
same ``fetch_article`` + ``Store.put_cache_entry`` path:

* **on demand** — ``get(kind='news', id='https://…')`` fetches one URL;
* **scheduled** — the :mod:`precis.workers.news_poll` worker walks the
  ``news_sources`` feed registry and mints every new article.

This replaces the retired ``daily_briefing``/``rss_ingest`` monolith
tables: a news item is now a first-class, searchable, taggable ref
instead of a row in a bespoke ``news_items`` table. The morning
briefing (:mod:`precis.workers.briefing`) reads recent ``news`` refs
back out and summarizes them.

Cache TTL is ``None`` (pinned): a news article is a historical record,
not a TTL'd lookup that should silently re-fetch. Volume is bounded at
ingest by per-feed ``max_items`` caps, not by expiry.

Every article is stamped ``category:news`` + ``source:<slug>`` so it can
be filtered in or out of search by tag — no hard fence (per design
discussion 2026-06-21: news is meant to be queryable, and the briefing
needs to read it; a dedicated NEWS fence axis can land later if volume
starts crowding default cross-kind search).
"""

from __future__ import annotations

import logging
import os
from typing import Any, ClassVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from precis.errors import BadInput, Upstream
from precis.handlers._cache_base import (
    CacheBackedHandler,
    FetchResult,
    _format_cache_footer,
)
from precis.protocol import KindSpec
from precis.response import Response
from precis.store.types import BlockInsert
from precis.utils.block_ingest import to_block_inserts
from precis.utils.md_parse import block_meta, parse_markdown
from precis.utils.optional_deps import require_optional
from precis.utils.slug import slug_from_text

log = logging.getLogger(__name__)

_ATTRIBUTION = (
    "Source: news article; content © its publisher. Fetched and extracted "
    "with trafilatura. Verify quotes against the original; quote sparingly."
)

_DEFAULT_UA = "precis-mcp/2.0 (+https://github.com/retospect/precis-mcp)"

#: Query-string keys that are pure tracking noise — stripped during URL
#: canonicalization so the same article arriving via five feeds (each
#: with its own utm campaign) dedups to one ref.
_TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "igshid",
        "ref",
        "cmpid",
        "ito",
    }
)

#: Cap on extracted article chars kept (some pages run huge); the
#: block-splitter chunks this. Truncation is flagged in meta.
_MAX_ARTICLE_CHARS = 80_000


def canonical_url(url: str) -> str:
    """Normalize an article URL for stable dedup.

    Lowercases scheme + host, drops the fragment, and strips known
    tracking query params (utm_*, fbclid, …). Remaining query params
    are kept (some sites route article identity through them) but
    sorted so ordering differences don't fork the cache key.
    """
    parts = urlsplit((url or "").strip())
    if not parts.scheme or not parts.netloc:
        raise BadInput(
            f"not a fetchable article URL: {url!r}",
            next="get(kind='news', id='https://example.com/article')",
        )
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ]
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/") or "/",
            urlencode(sorted(query)),
            "",  # drop fragment
        )
    )


def article_blocks(body_text: str, *, embedder: Any) -> list[BlockInsert]:
    """Markdown body → embedded ``BlockInsert`` rows (shared ingest path).

    Mirrors :meth:`CacheBackedHandler._blocks_from_report` but as a free
    function so the poller can build blocks without a handler instance.
    ``embedder=None`` produces ``embedding=None`` rows; the embed worker
    vectorizes them later (the deferred path the poller uses so polling
    isn't blocked on embedding).
    """
    md_blocks = parse_markdown(body_text)
    if not md_blocks:
        return [BlockInsert(pos=0, text=body_text)]
    return to_block_inserts(md_blocks, embedder=embedder, meta_for=block_meta)


def fetch_article(url: str, *, embedder: Any = None) -> FetchResult:
    """Fetch + extract one article URL into a :class:`FetchResult`.

    Shared by :meth:`NewsHandler._fetch` (on-demand) and the news_poll
    worker (scheduled). Uses trafilatura markdown extraction — same
    engine as the ``web`` kind — behind the SSRF-guarded fetcher.
    """
    httpx = require_optional("httpx", extra="external")
    trafilatura = require_optional("trafilatura", extra="external")

    from precis.utils.safe_fetch import SsrfBlocked, safe_get

    ua = os.environ.get("WEB_USER_AGENT", _DEFAULT_UA)
    headers = {"User-Agent": ua, "Accept": "text/html,*/*;q=0.8"}

    try:
        with httpx.Client(
            timeout=30.0, follow_redirects=False, headers=headers
        ) as client:
            resp = safe_get(client, url)
    except SsrfBlocked as exc:
        raise Upstream(
            f"news fetch refused for {url}: {exc}",
            next="the URL resolves to a non-public address; use a public URL",
        ) from exc
    except httpx.HTTPError as exc:
        raise Upstream(
            f"news fetch failed for {url}: {exc}",
            next="check the URL is reachable; retry later",
        ) from exc

    if resp.status_code >= 400:
        raise Upstream(
            f"HTTP {resp.status_code} for {url}",
            next="check the URL or wait for the site to recover",
        )

    extracted = trafilatura.extract(
        resp.text,
        output_format="markdown",
        include_links=True,
        include_images=False,
        favor_recall=False,
    )
    try:
        meta = trafilatura.extract_metadata(resp.text)
        title = (getattr(meta, "title", None) if meta else None) or url
    except Exception:  # metadata extraction is best-effort, never fatal
        title = url

    truncated = False
    if extracted and len(extracted) > _MAX_ARTICLE_CHARS:
        extracted = extracted[:_MAX_ARTICLE_CHARS].rstrip() + "\n\n[…truncated]"
        truncated = True

    if not extracted or not extracted.strip():
        body_text = (
            f"(no readable content extracted from {url} — "
            f"page may require JS, login, or have non-article shape)"
        )
    else:
        body_text = extracted.strip()

    return FetchResult(
        title=str(title),
        body_blocks=article_blocks(body_text, embedder=embedder),
        cost_usd=None,  # bandwidth only
        meta={"url": url, "chars": len(body_text), "truncated": truncated},
    )


class NewsHandler(CacheBackedHandler):
    """``news`` — fetch + cache a news article (on-demand or poller-fed)."""

    spec: ClassVar[KindSpec] = KindSpec(
        kind="news",
        title="News",
        description=(
            "Multi-source news articles. get(kind='news', id='<url>') "
            "fetches + extracts + embeds one article; the news_poll worker "
            "mints them from the news_sources feed registry on a schedule. "
            "search(kind='news', q=...) lands hits inside article bodies. "
            "Pinned in cache; tagged category:news + source:<slug>. The "
            "morning briefing summarizes recent items. See ``precis-news-help``."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=False,
        id_required=True,
    )

    provider: ClassVar[str] = "news"
    ttl_seconds: ClassVar[int | None] = None  # pinned — articles are records
    attribution: ClassVar[str] = _ATTRIBUTION
    corpus_slug: ClassVar[str] = "default"
    example_query: ClassVar[str] = "https://www.bbc.com/news/articles/abc123"

    # ── cache key + slug ──────────────────────────────────────────────

    def _canonical_key(self, query: str) -> str:
        return canonical_url(query)

    def _slug_for(self, key: str) -> str:
        return slug_from_text(key, max_len=72) or "news-article"

    def _recover_key(self, ref, cache):  # type: ignore[no-untyped-def]
        return (cache.meta or {}).get("url")

    # ── upstream fetch ────────────────────────────────────────────────

    def _fetch(self, key: str) -> FetchResult:
        return fetch_article(key, embedder=self.embedder)

    # ── render: append source URL + cache footer ──────────────────────

    def _render(self, ref, cache, *, hit):  # type: ignore[no-untyped-def]
        resp = super()._render(ref, cache, hit=hit)
        url = (cache.meta or {}).get("url") or ""
        footer = f"  Article: {url}\n  Cache:   {_format_cache_footer(cache)}"
        return Response(body=resp.body + "\n" + footer, cost=resp.cost)


__all__ = ["NewsHandler", "article_blocks", "canonical_url", "fetch_article"]
