"""news_poll — scheduled RSS/Atom ingestion for the ``news`` kind.

The news analog of :mod:`precis.workers.watch_poll`: instead of growing
the paper corpus along the citation graph, it grows a ``news`` corpus
along a curated feed list. Each pass:

1. reads every enabled row from the ``news_sources`` registry;
2. parses each feed (``feedparser``), taking up to ``max_items`` entries;
3. for each entry, canonicalizes the article URL and skips it if a
   ``news`` ref already caches that URL (idempotent — re-polls are cheap);
4. otherwise fetches + extracts the article (:func:`fetch_article`) and
   mints a pinned ``news`` ref via :meth:`Store.put_cache_entry`, stamped
   ``category:news`` + ``source:<slug>`` (+ the feed's ``default_tags``
   and a ``published:<date>`` tag when the entry carries a date);
5. records ``last_polled_at`` / ``last_status`` on the source row.

Embedding is deferred: blocks are written with ``embedding=None`` and
the embed worker vectorizes them, so a slow embedder never stalls
polling.

This replaces the retired ``rss_ingest.py`` from the daily_briefing
monolith — the feedparser/date/dedup logic is lifted, but items land as
first-class searchable refs instead of rows in a bespoke ``news_items``
table.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from precis.handlers._link_tag_ops import apply_tag_ops
from precis.handlers.news import canonical_url, fetch_article
from precis.store import Store
from precis.utils.slug import slug_from_text

log = logging.getLogger(__name__)

#: Hard ceiling per feed regardless of the row's ``max_items`` — a
#: misconfigured feed can't mint thousands of refs in one pass.
_ABS_MAX_ITEMS = 200

FeedParser = Callable[[str], Any]
ArticleFetcher = Callable[[str], Any]


def _request_hash(canonical: str) -> str:
    """Mirror ``CacheBackedHandler._hash`` so the poller and on-demand
    ``get(kind='news', id=url)`` dedup against the same cache key."""
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _entry_pub_date(entry: Any) -> datetime | None:
    """Best-effort publication date from a feedparser entry (lifted)."""
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=UTC)
            except (TypeError, ValueError):
                pass
    for attr in ("published", "updated"):
        raw = getattr(entry, attr, None)
        if raw:
            try:
                return parsedate_to_datetime(raw)
            except (TypeError, ValueError):
                pass
    return None


def _entry_tags(entry: Any, default_tags: list[str], source_slug: str) -> list[str]:
    """Compose the tag set stamped on a minted article."""
    tags = ["category:news", f"source:{source_slug}", *default_tags]
    pub = _entry_pub_date(entry)
    if pub is not None:
        tags.append(f"published:{pub.date().isoformat()}")
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _enabled_sources(store: Store, limit: int | None) -> list[dict[str, Any]]:
    sql = (
        "SELECT source_id, url, title, source_slug, default_tags, max_items "
        "FROM news_sources WHERE enabled = true ORDER BY source_id"
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    with store.tx() as conn:
        rows = conn.execute(sql).fetchall()
    return [
        {
            "source_id": r[0],
            "url": r[1],
            "title": r[2],
            "source_slug": r[3],
            "default_tags": list(r[4] or []),
            "max_items": min(int(r[5] or 50), _ABS_MAX_ITEMS),
        }
        for r in rows
    ]


def _record_status(store: Store, source_id: int, status: str) -> None:
    err = 0 if status == "ok" else 1
    with store.tx() as conn:
        conn.execute(
            "UPDATE news_sources SET last_polled_at = now(), last_status = %s, "
            "consecutive_errors = CASE WHEN %s = 0 THEN 0 "
            "ELSE consecutive_errors + 1 END WHERE source_id = %s",
            (status[:200], err, source_id),
        )


def run_news_pass(
    store: Store,
    *,
    limit_sources: int | None = None,
    parse_feed: FeedParser | None = None,
    fetch: ArticleFetcher | None = None,
) -> dict[str, int]:
    """Poll enabled news_sources; mint new articles as ``news`` refs.

    Returns ``{claimed, ok, failed}``: ``claimed`` = feeds polled,
    ``ok`` = new articles minted, ``failed`` = feeds that errored.

    ``parse_feed`` / ``fetch`` are injectable for tests; defaults call
    feedparser and the live :func:`fetch_article`.
    """
    parse = parse_feed or _default_parse_feed
    fetch_one = fetch or (lambda url: fetch_article(url, embedder=None))

    sources = _enabled_sources(store, limit_sources)
    claimed = 0
    minted = 0
    failed = 0

    for src in sources:
        claimed += 1
        try:
            feed = parse(src["url"])
        except Exception as exc:  # feedparser is permissive, but be safe
            log.warning("news_poll: feed %s parse failed: %s", src["title"], exc)
            _record_status(store, src["source_id"], f"error: {exc}"[:200])
            failed += 1
            continue

        entries = list(getattr(feed, "entries", []) or [])[: src["max_items"]]
        new_here = 0
        for entry in entries:
            link = (getattr(entry, "link", "") or "").strip()
            if not link:
                continue
            try:
                key = canonical_url(link)
            except Exception:
                continue  # unparseable URL — skip, don't fail the feed
            rh = _request_hash(key)
            if store.get_cache_entry(provider="news", request_hash=rh) is not None:
                continue  # already have this article

            try:
                fr = fetch_one(key)
            except Exception as exc:
                log.info("news_poll: fetch failed for %s: %s", key, exc)
                continue

            title = (fr.title or getattr(entry, "title", "") or key).strip()
            slug = slug_from_text(title, max_len=72) or slug_from_text(key, max_len=72)
            ref, _cache = store.put_cache_entry(
                kind="news",
                slug=slug or "news-article",
                title=title,
                body_blocks=fr.body_blocks,
                provider="news",
                request_hash=rh,
                ttl_seconds=None,  # pinned — articles are records
                ref_meta={"url": key, "source": src["source_slug"]},
                cache_meta={**(fr.meta or {}), "source": src["source_slug"]},
            )
            apply_tag_ops(
                store,
                "news",
                ref.id,
                tags=_entry_tags(entry, src["default_tags"], src["source_slug"]),
                untags=None,
            )
            new_here += 1
            minted += 1

        _record_status(store, src["source_id"], "ok")
        log.info("news_poll: %s → %d new articles", src["title"], new_here)

    log.info(
        "news_poll pass: %d feeds, %d new articles, %d failed",
        claimed,
        minted,
        failed,
    )
    return {"claimed": claimed, "ok": minted, "failed": failed}


def _default_parse_feed(url: str) -> Any:
    from precis.utils.optional_deps import require_optional

    feedparser = require_optional("feedparser", extra="external")
    return feedparser.parse(url)


__all__ = ["run_news_pass"]
