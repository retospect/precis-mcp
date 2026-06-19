"""``wikipedia`` kind — on-demand Wikipedia article fetch (Tier 1).

The on-demand alternative to bulk-embedding a Wikipedia dump. A
``get(kind='wikipedia', id='<query>')`` runs the MediaWiki **search**
API (CirrusSearch / Elasticsearch backend) to resolve the best-matching
article, then fetches its **plain-text extract** (the TextExtracts
``prop=extracts&explaintext`` surface — no wikitext parsing) and caches
it. The cached body is block-split + embedded by the standard pipeline,
so ``search(kind='wikipedia', q=...)`` lands hits inside the articles
you've already pulled.

Nothing is resident until asked: a handful of articles per fetch, TTL-
expired, instead of ~30M dump chunks permanently diluting the corpus.

Two upstream hops, both free, no API key:

1. ``action=query&list=search`` → top article title for the query.
2. ``action=query&prop=extracts&explaintext`` → clean article prose.

Wikimedia **requires a descriptive User-Agent with contact info** —
generic/empty UAs get hard-blocked. Default carries the project repo
URL; override with ``PRECIS_WIKIPEDIA_UA``. Language defaults to English
(``en.wikipedia.org``); override with ``PRECIS_WIKIPEDIA_LANG``.

Cache TTL: 7 days. Articles mutate, and a short TTL also keeps the
on-demand corpus from accreting — stale rows re-fetch on next miss.
"""

from __future__ import annotations

import logging
import os
from typing import Any, ClassVar
from urllib.parse import quote

from precis.errors import BadInput, Upstream
from precis.handlers._cache_base import (
    CacheBackedHandler,
    FetchResult,
    _format_cache_footer,
)
from precis.protocol import KindSpec
from precis.response import Response
from precis.store._tag_filter import WIKI_TAG, is_wiki_tag
from precis.store.types import BlockInsert
from precis.utils.optional_deps import require_optional
from precis.utils.slug import slug_from_text

log = logging.getLogger(__name__)


_ATTRIBUTION = (
    "Source: Wikipedia (https://www.wikipedia.org), text under "
    "CC BY-SA 4.0. Tertiary encyclopedic summary — verify against "
    "primary sources and cite those, not this fetch."
)

#: Wikimedia's robot policy requires a descriptive UA that identifies
#: the client and a contact (a URL or email). Generic browser UAs and
#: empty UAs are rate-limited or blocked outright. Operators can swap
#: in their own contact via ``PRECIS_WIKIPEDIA_UA``.
_DEFAULT_UA = "precis-mcp/2.0 (+https://github.com/retospect/precis-mcp)"

#: Max characters of extract to keep. Long articles run to tens of KB;
#: the base auto-chunker splits this into ~800-char blocks. We cap so a
#: single pathological article (some run >100 KB) can't dominate a
#: TTL-bounded cache row. Truncation is flagged in meta.
_MAX_EXTRACT_CHARS = 60_000


class WikipediaHandler(CacheBackedHandler):
    """``wikipedia`` — resolve + fetch one article's plain-text extract."""

    spec: ClassVar[KindSpec] = KindSpec(
        kind="wikipedia",
        title="Wikipedia (on-demand article fetch)",
        description=(
            "Resolve a query to the best-matching Wikipedia article via "
            "the MediaWiki search API, then fetch and cache its plain-text "
            "extract. Cached 7 days; the body is block-parsed and embedded "
            "so search(kind='wikipedia', q=...) lands hits inside fetched "
            "articles. On-demand by design — no bulk dump, always current. "
            "Tag to keep (e.g. add=['bookmark']) and link to memory / paper "
            "refs. See precis-wikipedia-help."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=False,
        id_required=True,
    )

    provider: ClassVar[str] = "wikipedia"
    ttl_seconds: ClassVar[int | None] = 7 * 24 * 60 * 60  # 7 days
    attribution: ClassVar[str] = _ATTRIBUTION
    corpus_slug: ClassVar[str] = "default"
    example_query: ClassVar[str] = "CRISPR gene editing"

    # ── cache key + slug ──────────────────────────────────────────────

    def _canonical_key(self, query: str) -> str:
        q = " ".join((query or "").lower().split())
        if not q:
            raise BadInput(
                "wikipedia requires a non-empty query",
                next="get(kind='wikipedia', id='your topic')",
            )
        return q

    def _slug_for(self, key: str) -> str:
        return slug_from_text(key, max_len=60) or "wikipedia-query"

    def _recover_key(self, ref, cache):  # type: ignore[no-untyped-def]
        return (cache.meta or {}).get("query")

    # ── provenance stamp ──────────────────────────────────────────────

    def _apply_tag_ops_if_any(self, ref_id, tags, untags):  # type: ignore[no-untyped-def]
        """Always stamp ``ORIGIN:wikipedia`` (plus any caller tags).

        Every cache write — fresh create *and* in-place refresh — routes
        through this hook in the base ``get`` flow, so stamping here
        guarantees the provenance fence-tag rides on the ref without
        the caller having to remember it. Idempotent: the tag layer
        dedups, so re-fetches don't pile up duplicate rows. The stamp is
        what keeps Wikipedia prose out of default / cross-kind search
        (see ``store/_tag_filter.wiki_fence``); never auto-removed.
        """
        merged = [WIKI_TAG, *(tags or [])]
        # Guard against a caller passing untags=['ORIGIN:wikipedia'] —
        # the provenance fence is not theirs to drop on this kind.
        clean_untags = [t for t in (untags or []) if not is_wiki_tag(t)]
        super()._apply_tag_ops_if_any(ref_id, merged, clean_untags)

    # ── lang / UA config ──────────────────────────────────────────────

    @staticmethod
    def _lang() -> str:
        return (os.environ.get("PRECIS_WIKIPEDIA_LANG") or "en").strip() or "en"

    @staticmethod
    def _user_agent() -> str:
        return (os.environ.get("PRECIS_WIKIPEDIA_UA") or "").strip() or _DEFAULT_UA

    # ── upstream fetch ────────────────────────────────────────────────

    def _fetch(self, key: str) -> FetchResult:
        httpx = require_optional("httpx", extra="external")

        from precis.utils.safe_fetch import SsrfBlocked

        lang = self._lang()
        api = f"https://{lang}.wikipedia.org/w/api.php"
        headers = {"User-Agent": self._user_agent(), "Accept": "application/json"}

        # follow_redirects=False — safe_get walks the chain itself,
        # revalidating each hop against the SSRF blocklist. (Same guard
        # the `web` kind uses; api.php is a public host so this is belt-
        # and-suspenders, but no raw redirect-following on agent input.)
        try:
            with httpx.Client(
                timeout=30.0, follow_redirects=False, headers=headers
            ) as client:
                title = self._resolve_title(client, api, key)
                if title is None:
                    return _no_results(key, lang)
                article = self._fetch_extract(client, api, title)
        except SsrfBlocked as exc:
            raise Upstream(f"wikipedia fetch refused: {exc}") from exc
        except httpx.HTTPError as exc:
            raise Upstream(
                f"wikipedia transport error: {exc}",
                next="check connectivity to wikipedia.org; retry later",
            ) from exc

        resolved_title, pageid, extract = article
        truncated = len(extract) > _MAX_EXTRACT_CHARS
        if truncated:
            extract = extract[:_MAX_EXTRACT_CHARS].rstrip() + "\n\n[…truncated]"

        url = _article_url(lang, resolved_title)
        body = extract.strip() or (
            f"(article '{resolved_title}' returned no extractable prose)"
        )
        return FetchResult(
            title=resolved_title,
            # One block; the base-class auto-chunker splits it at
            # ~800 chars and the embed worker vectorizes per ADR 0007
            # (ingest writes embedding NULL; the worker fills it).
            body_blocks=[BlockInsert(pos=0, text=body)],
            cost_usd=None,  # free, bandwidth only
            meta={
                "query": key,
                "title": resolved_title,
                "pageid": pageid,
                "lang": lang,
                "url": url,
                "extract_chars": len(body),
                "truncated": truncated,
            },
        )

    def _resolve_title(
        self, client: Any, api: str, query: str
    ) -> str | None:
        """Run CirrusSearch; return the top-hit article title or None."""
        from precis.utils.safe_fetch import safe_get

        params = {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": 1,
            "srnamespace": 0,  # main/article namespace only
            "format": "json",
            "formatversion": 2,
        }
        resp = safe_get(client, api, params=params)
        if resp.status_code != 200:
            raise Upstream(f"wikipedia search HTTP {resp.status_code}")
        return _pick_title(resp.json())

    def _fetch_extract(
        self, client: Any, api: str, title: str
    ) -> tuple[str, int | None, str]:
        """Fetch the plain-text extract for an exact article title."""
        from precis.utils.safe_fetch import safe_get

        params = {
            "action": "query",
            "prop": "extracts",
            "explaintext": 1,
            "exsectionformat": "plain",
            "redirects": 1,  # follow article redirects to the canonical page
            "titles": title,
            "format": "json",
            "formatversion": 2,
        }
        resp = safe_get(client, api, params=params)
        if resp.status_code != 200:
            raise Upstream(f"wikipedia extract HTTP {resp.status_code}")
        return _parse_extract(resp.json(), fallback_title=title)

    # ── render: append source URL + cache footer ──────────────────────

    def _render(self, ref, cache, *, hit):  # type: ignore[no-untyped-def]
        resp = super()._render(ref, cache, hit=hit)
        url = (cache.meta or {}).get("url") or ""
        cache_state = _format_cache_footer(cache)
        footer = f"  Article: {url}\n  Cache:   {cache_state}"
        return Response(body=resp.body + "\n" + footer, cost=resp.cost)


# ---------------------------------------------------------------------------
# Pure helpers (offline-testable — no network)
# ---------------------------------------------------------------------------


def _pick_title(search_json: dict[str, Any]) -> str | None:
    """Top article title from a ``list=search`` (formatversion=2) body."""
    hits = ((search_json or {}).get("query") or {}).get("search") or []
    for hit in hits:
        title = (hit or {}).get("title")
        if title:
            return str(title)
    return None


def _parse_extract(
    pages_json: dict[str, Any], *, fallback_title: str
) -> tuple[str, int | None, str]:
    """Pull ``(title, pageid, extract)`` from a ``prop=extracts`` body.

    formatversion=2 shapes ``query.pages`` as a list. A missing page
    (deleted between search and fetch) yields an empty extract rather
    than raising — the caller renders a stub.
    """
    pages = ((pages_json or {}).get("query") or {}).get("pages") or []
    for page in pages:
        if page.get("missing"):
            continue
        title = str(page.get("title") or fallback_title)
        pageid = page.get("pageid")
        extract = str(page.get("extract") or "")
        return title, pageid, extract
    return fallback_title, None, ""


def _article_url(lang: str, title: str) -> str:
    """Canonical desktop URL for an article title."""
    return f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}"


def _no_results(query: str, lang: str) -> FetchResult:
    """FetchResult for a query that matched no article."""
    return FetchResult(
        title=f"Wikipedia: no match for {query!r}",
        body_blocks=[
            BlockInsert(
                pos=0,
                text=(
                    f"No {lang}.wikipedia.org article matched {query!r}. "
                    "Try different terms, or a more specific entity name."
                ),
            )
        ],
        cost_usd=None,
        meta={"query": query, "lang": lang, "result_count": 0},
    )


__all__ = ["WikipediaHandler"]
