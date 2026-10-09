"""``news`` kind — multi-source news articles in the shared corpus.

A ``news`` ref is a single news article: its URL is the cache key, its
title the slug, its extracted body block-split + embedded like the
``web`` kind, so ``search(kind='news', q=...)`` lands hits inside
article text. Articles are minted two ways, both routing through the
same ``fetch_article`` + ``Store.put_cache_entry`` path:

* **on demand** — ``get(kind='news', id='https://…')`` fetches one URL;
* **scheduled** — the :mod:`precis.workers.news_poll` worker walks the
  ``news_sources`` feed registry and mints every new article.

``put(kind='news', text='reddit:r/<name>' | 'mastodon:<user>@<instance>')``
registers a subreddit or a Mastodon account as a ``news_sources`` row —
both expose public, credential-free RSS (``/r/<name>/.rss``,
``/@<user>.rss``), so they ride the existing poller path (``safe_get`` →
feedparser → tier-0 injection scan) with no bespoke API client. The put
stores the resolved feed URL and does not fetch it; a typo'd name shows up
as the row's ``last_status`` after the first poll, then backs off.

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
import re
from dataclasses import dataclass
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
from precis.store.types import ChunkInsert
from precis.utils.chunk_ingest import to_chunk_inserts
from precis.utils.http import http_client, require_httpx
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

#: Source-spec forms ``put(kind='news', text=...)`` accepts. Reddit names are
#: 3-21 word chars (Reddit's own rule; 2-char legacy subs exist, allow 2);
#: Mastodon usernames are word chars plus ``.``/``-``, the instance a
#: hostname with at least one dot.
_REDDIT_SPEC_RE = re.compile(r"^reddit:(?:r/)?([A-Za-z0-9_]{2,21})/?$")
_MASTODON_SPEC_RE = re.compile(
    r"^mastodon:@?([A-Za-z0-9_.\-]{1,64})@"
    r"((?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63})$"
)
_SOURCE_SPEC_NEXT = (
    "put(kind='news', text='reddit:r/<name>') or "
    "put(kind='news', text='mastodon:<user>@<instance>')"
)


@dataclass(frozen=True, slots=True)
class NewsSourceSpec:
    """A resolved ``news_sources`` row candidate from a source spec."""

    category: str  # 'reddit' | 'mastodon'
    url: str  # the public RSS feed URL the poller fetches
    title: str  # human label ('r/python', '@user@instance')
    source_slug: str  # → source:<slug> tag


def parse_source_spec(spec: str) -> NewsSourceSpec:
    """``reddit:r/<name>`` / ``mastodon:<user>@<instance>`` → feed row.

    Only the two documented forms are accepted; a bare URL is refused
    here (operators add arbitrary feeds by SQL, ``docs/runbooks/news-ops.md``).
    """
    text = (spec or "").strip()
    if m := _REDDIT_SPEC_RE.match(text):
        name = m.group(1)
        return NewsSourceSpec(
            category="reddit",
            url=f"https://www.reddit.com/r/{name.lower()}/.rss",
            title=f"r/{name}",
            source_slug=f"reddit-{name.lower()}",
        )
    if m := _MASTODON_SPEC_RE.match(text):
        user, instance = m.group(1), m.group(2).lower()
        return NewsSourceSpec(
            category="mastodon",
            url=f"https://{instance}/@{user}.rss",
            title=f"@{user}@{instance}",
            source_slug=slug_from_text(f"mastodon-{user}-{instance}", max_len=72),
        )
    raise BadInput(
        f"not a news source spec: {spec!r}",
        next=_SOURCE_SPEC_NEXT,
    )


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


def article_blocks(body_text: str, *, embedder: Any) -> list[ChunkInsert]:
    """Markdown body → embedded ``ChunkInsert`` rows (shared ingest path).

    Mirrors :meth:`CacheBackedHandler._blocks_from_report` but as a free
    function so the poller can build blocks without a handler instance.
    ``embedder=None`` produces ``embedding=None`` rows; the embed worker
    vectorizes them later (the deferred path the poller uses so polling
    isn't blocked on embedding).
    """
    md_blocks = parse_markdown(body_text)
    if not md_blocks:
        return [ChunkInsert(ord=0, text=body_text)]
    return to_chunk_inserts(md_blocks, embedder=embedder, meta_for=block_meta)


def fetch_article(url: str, *, embedder: Any = None) -> FetchResult:
    """Fetch + extract one article URL into a :class:`FetchResult`.

    Shared by :meth:`NewsHandler._fetch` (on-demand) and the news_poll
    worker (scheduled). Uses trafilatura markdown extraction — same
    engine as the ``web`` kind — behind the SSRF-guarded fetcher.
    """
    httpx = require_httpx()
    trafilatura = require_optional("trafilatura")

    from precis.utils.safe_fetch import SsrfBlocked, safe_get

    ua = os.environ.get("WEB_USER_AGENT", _DEFAULT_UA)

    try:
        with http_client(
            timeout=30.0,
            headers={"Accept": "text/html,*/*;q=0.8"},
            user_agent=ua,
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
            "morning briefing summarizes recent items. put(kind='news', "
            "text='reddit:r/<name>' | 'mastodon:<user>@<instance>') registers "
            "a subreddit / Mastodon account as a feed source. See "
            "``precis-news-help``."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        supports_put=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=False,
        id_required=True,
        # gr311336: news is the only CacheBackedHandler subclass with a
        # CHUNK_CODES entry (``nc<id>``), so it's the only one that ever
        # hit the dispatcher's chunk-handle rewrite — this declares that
        # `get` now understands the resulting `slug~ord` selector
        # (CacheBackedHandler._render_chunk_selector).
        supports_chunk_selectors=True,
    )

    provider: ClassVar[str] = "news"
    ttl_seconds: ClassVar[int | None] = None  # pinned — articles are records
    attribution: ClassVar[str] = _ATTRIBUTION
    corpus_slug: ClassVar[str] = "default"
    example_query: ClassVar[str] = "https://www.bbc.com/news/articles/abc123"

    # ── cache key + slug ──────────────────────────────────────────────

    def _canonical_key(self, query: str, *, literal: bool = False) -> str:
        return canonical_url(query)

    def _slug_for(self, key: str) -> str:
        return slug_from_text(key, max_len=72) or "news-article"

    def _recover_key(self, ref, cache):
        return (cache.meta or {}).get("url")

    # ── upstream fetch ────────────────────────────────────────────────

    def _fetch(self, key: str) -> FetchResult:
        return fetch_article(key, embedder=self.embedder)

    # ── put: register a subreddit / Mastodon account as a feed source ──

    def put(
        self,
        *,
        text: str | None = None,
        id: str | None = None,
        title: str | None = None,
        tags: list[str] | None = None,
        **_kw: Any,
    ) -> Response:
        """Register ``reddit:r/<name>`` / ``mastodon:<user>@<instance>``.

        Writes (or re-enables) a ``news_sources`` row holding the resolved
        public RSS URL; the next ``news_poll`` tick ingests it through the
        shared ``safe_get`` → feedparser → tier-0 scan path. Idempotent on
        the feed URL. ``tags=`` become the row's ``default_tags`` (stamped
        on every article); ``title=`` overrides the human label.
        """
        spec_text = text if text is not None else id
        if not isinstance(spec_text, str) or not spec_text.strip():
            raise BadInput(
                "put(kind='news') registers a feed source and needs text=",
                next=_SOURCE_SPEC_NEXT,
            )
        spec = parse_source_spec(spec_text)
        label = (title or "").strip() or spec.title
        default_tags = [t for t in (tags or []) if t and t.strip()]
        with self.store.tx() as conn:
            row = conn.execute(
                "SELECT source_id, enabled FROM news_sources WHERE url = %s",
                (spec.url,),
            ).fetchone()
            if row is None:
                inserted = conn.execute(
                    "INSERT INTO news_sources "
                    "(url, title, source_slug, category, default_tags) "
                    "VALUES (%s, %s, %s, %s, %s) RETURNING source_id",
                    (spec.url, label, spec.source_slug, spec.category, default_tags),
                ).fetchone()
                assert inserted is not None  # RETURNING on a successful INSERT
                source_id = inserted[0]
                state = "registered"
            else:
                source_id, enabled = row
                if not enabled:
                    conn.execute(
                        "UPDATE news_sources SET enabled = true WHERE source_id = %s",
                        (source_id,),
                    )
                state = "already registered" + ("" if enabled else ", re-enabled")
        body = (
            f"news source {state}: {label} (source_id={source_id})\n"
            f"  feed: {spec.url}\n"
            f"  tag:  source:{spec.source_slug}\n"
            f"  next: the news_poll pass ingests it on its next tick; "
            f"search(kind='news', tags=['source:{spec.source_slug}'])"
        )
        return Response(body=body)

    # ── render: append source URL + cache footer ──────────────────────

    def _render(self, ref, cache, *, hit):
        resp = super()._render(ref, cache, hit=hit)
        url = (cache.meta or {}).get("url") or ""
        footer = f"  Article: {url}\n  Cache:   {_format_cache_footer(cache)}"
        return Response(body=resp.body + "\n" + footer, cost=resp.cost)


__all__ = [
    "NewsHandler",
    "NewsSourceSpec",
    "article_blocks",
    "canonical_url",
    "fetch_article",
    "parse_source_spec",
]
