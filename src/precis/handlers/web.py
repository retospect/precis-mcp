"""``web`` kind — fetch and cache the readable content of a URL.

Phase 4 ships **page-fetch mode only**: ``get(kind='web', id=URL)``
fetches the URL, extracts the main content with `trafilatura`, and
caches the markdown text. Bookmark mode (durable, user-curated refs)
and Wayback-archive integration are deferred to phase 4b — they
need `put` support (phase 5+) and a richer ref schema.

Cache key: the canonical URL. Different URL forms of the same page
(scheme case, default ports, tracking params, trailing slashes,
fragments on non-SPA hosts) collapse onto a single cache row.

Free (no auth, just bandwidth). 7-day TTL — pages mutate often enough
that long-lived caching produces stale answers.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from precis.errors import BadInput, Upstream
from precis.handlers._cache_base import (
    CacheBackedHandler,
    FetchResult,
    _format_cache_footer,
)
from precis.protocol import KindSpec
from precis.response import Response
from precis.utils.http import http_client, require_httpx
from precis.utils.optional_deps import require_optional
from precis.utils.url import canonical_url, host_of, is_http_url, slug_from_url

log = logging.getLogger(__name__)


_WEB_BASE_ATTRIBUTION = (
    "Source: web page; content © its publisher. Fetched and extracted "
    "with trafilatura. Verify quotes against the original; quote sparingly."
)


# Generous default UA so brittle anti-bot middleware doesn't 403 us.
# Real users can override via `WEB_USER_AGENT` env var.
_DEFAULT_UA = (
    "Mozilla/5.0 (compatible; precis-mcp/2.0; +https://github.com/retospect/precis-mcp)"
)


class WebHandler(CacheBackedHandler):
    """``web`` — fetch+extract a URL. Page-fetch mode only in phase 4."""

    spec: ClassVar[KindSpec] = KindSpec(
        kind="web",
        title="Web (page fetch)",
        description=(
            "Fetch a web page and return its readable content as markdown. "
            "Cached for 7 days; tracking params and fragments collapse "
            "onto a single cache row. Fetched pages are block-parsed and "
            "embedded so search(kind='web', q=...) lands hits inside "
            "page content. Tag to bookmark (e.g. add=['bookmark']) and "
            "link to memory / paper refs for cross-referencing."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=False,
        id_required=True,
    )

    provider: ClassVar[str] = "web"
    ttl_seconds: ClassVar[int | None] = 7 * 24 * 60 * 60  # 7 days
    attribution: ClassVar[str] = _WEB_BASE_ATTRIBUTION
    corpus_slug: ClassVar[str] = "default"
    example_query: ClassVar[str] = "https://example.com/article"

    # ── canonicalization & cache key ──────────────────────────────────

    def _canonical_key(self, query: str) -> str:
        """The canonical URL is the cache key. Reject non-http(s) input."""
        try:
            url = canonical_url(query)
        except ValueError as exc:
            raise BadInput(
                f"not a valid URL: {query!r}",
                next="get(kind='web', id='https://example.com/article')",
            ) from exc
        if not is_http_url(url):
            raise BadInput(
                f"web kind requires http(s) URL; got: {query!r}",
                next="get(kind='web', id='https://example.com/article')",
            )
        return url

    def _slug_for(self, key: str) -> str:
        """Use the readable slug derived from the canonical URL.

        Two distinct pages on the same host with different paths get
        distinct slugs deterministically — no hash suffix needed for
        most cases. Truncated to 60 chars; collisions (rare for typical
        bookmarks) cascade-replace the older entry per
        ``put_cache_entry``'s upsert behaviour.
        """
        slug = slug_from_url(key)
        return slug or "web-fetch"

    def _recover_key(self, ref, cache):  # type: ignore[no-untyped-def]
        """Return the canonical URL stored in cache meta.

        Lets ``mode='refresh'`` work when the caller addressed by
        slug (e.g. the maintenance driver iterating ``WATCH:daily``
        web bookmarks). (gripe:3681 phase 4.)
        """
        return (cache.meta or {}).get("url")

    # ── upstream fetch + extract ──────────────────────────────────────

    def _fetch(self, key: str) -> FetchResult:
        httpx = require_httpx()
        trafilatura = require_optional("trafilatura", extra="external")

        import os

        from precis.utils.safe_fetch import SsrfBlocked, safe_get

        ua = os.environ.get("WEB_USER_AGENT", _DEFAULT_UA)

        try:
            # follow_redirects=False (http_client's default) — safe_get
            # walks the redirect chain itself, revalidating each Location
            # against the SSRF blocklist. A True default here would let an
            # agent-supplied URL chain into 169.254.169.254 / 127.0.0.1 /
            # RFC1918.
            with http_client(
                timeout=30.0,
                headers={"Accept": "text/html,*/*;q=0.8"},
                user_agent=ua,
            ) as client:
                resp = safe_get(client, key)
        except SsrfBlocked as exc:
            raise Upstream(
                f"fetch refused for {key}: {exc}",
                next=(
                    "the URL (or a redirect target) resolves to a "
                    "non-public address; use a public-internet URL"
                ),
            ) from exc
        except httpx.HTTPError as exc:
            raise Upstream(
                f"fetch failed for {key}: {exc}",
                next="check the URL is reachable; retry later",
            ) from exc

        if resp.status_code >= 400:
            raise Upstream(
                f"HTTP {resp.status_code} for {key}",
                next="check the URL or wait for the site to recover",
            )

        html = resp.text
        # Trafilatura's `extract` returns markdown when configured; we
        # prefer markdown so blocks read naturally and link-grep works.
        extracted = trafilatura.extract(
            html,
            output_format="markdown",
            include_links=True,
            include_images=False,
            favor_recall=False,
        )

        title = _extract_title(html) or host_of(key) or key

        if not extracted or not extracted.strip():
            # Trafilatura sometimes returns empty for low-text pages
            # (login walls, JS shells). Fall back to a stub note rather
            # than caching nothing.
            body_text = (
                f"(no readable content extracted from {key} - "
                f"page may require JS, login, or have non-article shape)"
            )
        else:
            body_text = extracted.strip()

        # Block-parse + embed via the shared cache-base helper —
        # this is what makes ``search(kind='web', q=...)`` land
        # lexical + semantic hits inside cached pages. Previously
        # we stored one un-embedded monolithic block, which meant
        # a user's bookmark corpus was opaque to both the block-
        # level fused search and to ``random`` (which only draws
        # from ``embedding IS NOT NULL`` rows). Paragraph-level
        # blocks also surface more useful previews in search
        # results and in the random-pick response body.
        return FetchResult(
            title=title,
            body_blocks=self._blocks_from_report(body_text),
            cost_usd=None,  # bandwidth only
            meta={
                "url": key,
                "host": host_of(key),
                "status_code": resp.status_code,
                "content_type": resp.headers.get("content-type", ""),
                "extracted_chars": len(body_text),
            },
        )

    # ── render: append source URL + fetched-at line ───────────────────

    def _render(self, ref, cache, *, hit):  # type: ignore[no-untyped-def]
        resp = super()._render(ref, cache, hit=hit)
        url = (cache.meta or {}).get("url") or ""
        fetched = cache.fetched_at.date().isoformat() if cache.fetched_at else "?"
        # Surface cache age + freshness state. The MCP critic flagged
        # the absence of the ``(web cache · age Nd · CACHE:fresh)``
        # annotation that ``precis-cache`` documents — a caller had
        # no way to tell a cached vs fresh response, and so no way
        # to decide whether to force a refetch. Using
        # :func:`_format_cache_footer` keeps the wording consistent
        # across every cache-backed kind. (Critic MINOR #11.)
        cache_state = _format_cache_footer(cache)
        deep_link = f"  Source: {url}\n  Fetched: {fetched}\n  Cache:  {cache_state}"
        return Response(body=resp.body + "\n" + deep_link, cost=resp.cost)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_title(html: str) -> str | None:
    """Cheap <title> grab. Trafilatura also exposes title metadata,
    but a single regex avoids a second pass through the HTML."""
    import re

    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    title = re.sub(r"\s+", " ", m.group(1)).strip()
    return title or None
