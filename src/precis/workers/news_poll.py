"""news_poll — scheduled RSS/Atom ingestion for the ``news`` kind.

The news analog of :mod:`precis.workers.watch_poll`: instead of growing
the paper corpus along the citation graph, it grows a ``news`` corpus
along a curated feed list. Each pass:

1. reads every enabled row from the ``news_sources`` registry (a feed in
   error backoff is skipped — see ``_enabled_sources``);
2. parses each feed (``feedparser``) with a **conditional GET** (the
   stored ``etag`` / ``last_modified``) — a ``304 Not Modified`` short-
   circuits the whole feed, polite + cheap. Otherwise up to ``max_items``;
3. for each entry, dedups twice: first on the feed's ``<guid>`` (the
   outlet's stable per-story id, source-scoped — so a story re-published
   under a *changed URL* isn't ingested twice), then on the canonical URL
   hash (also the on-demand ``get`` cache key). Either hit → skip;
4. otherwise takes the article body straight from the feed entry
   (``content``/``summary``, HTML-stripped — feedparser only, no page
   fetch / trafilatura) and mints a pinned ``news`` ref via
   :meth:`Store.put_cache_entry`, stamped ``category:news`` +
   ``source:<slug>`` (+ the feed's ``default_tags`` and a
   ``published:<date>`` tag when the entry carries a date). Full-page
   extraction is opt-in via the ``fetch`` arg (needs ``[external]``);
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
import html
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from precis.handlers._link_tag_ops import apply_tag_ops
from precis.handlers.news import article_blocks, canonical_url
from precis.store import Store
from precis.utils.slug import slug_from_text

log = logging.getLogger(__name__)

#: Hard ceiling per feed regardless of the row's ``max_items`` — a
#: misconfigured feed can't mint thousands of refs in one pass.
_ABS_MAX_ITEMS = 200

#: Per-item body cap (chars). RSS bodies are short, but a feed shipping
#: full-text content[] can run long; the block-splitter chunks this.
_MAX_BODY_CHARS = 40_000

#: Exponential-backoff schedule for failing feeds (minutes). A source with
#: N consecutive errors waits ``_BACKOFF_BASE_MIN * 2^(N-1)`` before its
#: next poll, capped at ``_BACKOFF_CAP_MIN`` (~1 day). Base ≈ the nominal
#: 30-min poll cadence, so one error skips ~one tick, doubling thereafter.
_BACKOFF_BASE_MIN = 30
_BACKOFF_CAP_MIN = 1440

#: Feed parser: ``(url, *, etag=None, modified=None) -> parsed feed`` with
#: ``.entries`` and (on a conditional GET) ``.status`` (304 = unchanged) +
#: ``.etag`` / ``.modified`` validators to persist for next poll.
FeedParser = Callable[..., Any]
#: Optional full-page fetcher (url -> FetchResult). When supplied, the
#: poller fetches+extracts the article page instead of using the feed's
#: own content — opt-in only, since it pulls in the trafilatura/httpx
#: ``[external]`` stack. Default is feed-content ingestion (feedparser
#: alone), so the poller needs no page-fetch dependency.
ArticleFetcher = Callable[[str], Any]

_BREAK_RE = re.compile(r"<br\s*/?>|</p\s*>|</li\s*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]*\n[ \t]*(?:\n[ \t]*)+")


def _request_hash(canonical: str) -> str:
    """Mirror ``CacheBackedHandler._hash`` so the poller and on-demand
    ``get(kind='news', id=url)`` dedup against the same cache key."""
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _strip_html(raw: str) -> str:
    """Light HTML → text, no trafilatura. Block tags become newlines,
    remaining tags drop, entities unescape. Good enough for RSS bodies
    (feedparser already sanitizes scripts/styles out of ``summary``)."""
    text = _BREAK_RE.sub("\n", raw)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    return _WS_RE.sub("\n\n", text).strip()


def _entry_body(entry: Any) -> str:
    """Article body straight from the feed entry — ``content[].value`` if
    the feed ships full text, else ``summary``. Lifted from the retired
    rss_ingest ``extract_content``; HTML-stripped to plain text."""
    raw = ""
    content = getattr(entry, "content", None)
    if content:
        first = content[0]
        raw = (
            first.get("value")
            if isinstance(first, dict)
            else getattr(first, "value", "")
        ) or ""
    if not raw:
        raw = getattr(entry, "summary", "") or ""
    return _strip_html(raw)[:_MAX_BODY_CHARS]


def _entry_pub_date(entry: Any) -> datetime | None:
    """Best-effort publication date from a feedparser entry (lifted)."""
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            try:
                y, mo, d, h, mi, s = parsed[:6]
                return datetime(y, mo, d, h, mi, s, tzinfo=UTC)
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
    # Exponential backoff for failing feeds: a source with N consecutive
    # errors is skipped until `base * 2^(N-1)` minutes after its last poll
    # (capped at _BACKOFF_CAP_MIN). A healthy feed (errors=0) is never
    # held back. Keeps a broken/parked feed from being re-hit every tick
    # while still self-healing once it recovers — no manual disable needed.
    sql = (
        "SELECT source_id, url, title, source_slug, default_tags, max_items, "
        "       etag, last_modified "
        "FROM news_sources "
        "WHERE enabled = true "
        "  AND (consecutive_errors = 0 OR last_polled_at IS NULL "
        "       OR now() - last_polled_at >= make_interval(mins => "
        f"            least({_BACKOFF_BASE_MIN} * power(2, consecutive_errors - 1), "
        f"                  {_BACKOFF_CAP_MIN})::int)) "
        "ORDER BY source_id"
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
            "etag": r[6],
            "last_modified": r[7],
        }
        for r in rows
    ]


def _record_status(
    store: Store,
    source_id: int,
    status: str,
    *,
    etag: str | None = None,
    modified: str | None = None,
) -> None:
    err = 0 if status == "ok" else 1
    # COALESCE keeps the prior validator when this poll didn't supply a new
    # one (e.g. an error, or a feed that doesn't send etag/last-modified).
    with store.tx() as conn:
        conn.execute(
            "UPDATE news_sources SET last_polled_at = now(), last_status = %s, "
            "consecutive_errors = CASE WHEN %s = 0 THEN 0 "
            "ELSE consecutive_errors + 1 END, "
            "etag = COALESCE(%s, etag), "
            "last_modified = COALESCE(%s, last_modified) "
            "WHERE source_id = %s",
            (status[:200], err, etag, modified, source_id),
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

    By default the article body comes straight from the feed entry
    (feedparser only — no page fetch, no trafilatura). Pass ``fetch`` to
    enable full-page extraction instead (opt-in, needs ``[external]``).

    ``parse_feed`` / ``fetch`` are injectable for tests.
    """
    parse = parse_feed or _default_parse_feed

    sources = _enabled_sources(store, limit_sources)
    claimed = 0
    minted = 0
    failed = 0

    for src in sources:
        claimed += 1
        try:
            feed = parse(
                src["url"], etag=src.get("etag"), modified=src.get("last_modified")
            )
        except Exception as exc:  # feedparser is permissive, but be safe
            log.warning("news_poll: feed %s parse failed: %s", src["title"], exc)
            _record_status(store, src["source_id"], f"error: {exc}"[:200])
            failed += 1
            continue

        # Conditional GET: 304 = unchanged since last poll → nothing to pull.
        # Polite to the outlet (no body re-download) and cheap for us.
        if getattr(feed, "status", None) == 304:
            _record_status(store, src["source_id"], "ok")
            log.info("news_poll: %s → 304 not modified", src["title"])
            continue

        entries = list(getattr(feed, "entries", []) or [])[: src["max_items"]]
        new_here = 0
        for entry in entries:
            link = (getattr(entry, "link", "") or "").strip()
            # The feed's <guid> / Atom <id> — the outlet's stable per-story
            # identity. Scoped per source so a non-unique guid can't dedup
            # across outlets (we want the same syndicated story from two
            # outlets as two refs).
            guid = (getattr(entry, "id", "") or "").strip()
            guid_id = f"{src['source_slug']}:{guid}" if guid else None

            key: str | None = None
            if link:
                try:
                    key = canonical_url(link)
                except Exception:
                    key = None
            if key is None and guid_id is None:
                continue  # nothing identifies this item — skip

            # GUID dedup first: catches the same story re-published under a
            # changed URL (which the URL hash would miss → a duplicate).
            if (
                guid_id is not None
                and store.find_ref_by_identifier("guid", guid_id, kind="news")
                is not None
            ):
                continue

            # URL dedup: also the on-demand get(kind='news', id=url) cache key.
            rh = _request_hash(key) if key else _request_hash(f"guid:{guid_id}")
            if store.get_cache_entry(provider="news", request_hash=rh) is not None:
                continue  # already have this article

            title = (getattr(entry, "title", "") or key or guid).strip()
            if fetch is not None and key is not None:
                # Opt-in full-page extraction.
                try:
                    fr = fetch(key)
                except Exception as exc:
                    log.info("news_poll: full fetch failed for %s: %s", key, exc)
                    continue
                title = (fr.title or title).strip()
                body_blocks = fr.body_blocks
                extra_meta = {**(fr.meta or {})}
            else:
                # Default: body straight from the feed entry, no page fetch.
                body = (
                    _entry_body(entry) or f"(no body in feed)\n\n{title}\n{key or ''}"
                )
                body_blocks = article_blocks(body, embedder=None)
                extra_meta = {"url": key, "via": "rss"}

            # Slug from the canonical URL (the stable address), matching
            # NewsHandler._slug_for — NOT the title, which collides across
            # distinct articles sharing a headline and would clobber via
            # put_cache_entry's (kind, slug) replace. The article's handle
            # (ADR 0036) is its identity; URL/guid are metadata.
            slug = slug_from_text(key or guid_id or title, max_len=72) or "news-article"
            ref, _cache = store.put_cache_entry(
                kind="news",
                slug=slug,
                title=title,
                body_blocks=body_blocks,
                provider="news",
                request_hash=rh,
                ttl_seconds=None,  # pinned — articles are records
                ref_meta={
                    "url": key,
                    "guid": guid or None,
                    "source": src["source_slug"],
                },
                cache_meta={**extra_meta, "source": src["source_slug"]},
            )
            # Record the source-scoped guid so a later poll dedups on it even
            # if the article's URL changes (the case the URL hash misses).
            if guid_id is not None:
                store.insert_ref_identifiers(ref.id, [("guid", guid_id, "rss")])
            apply_tag_ops(
                store,
                "news",
                ref.id,
                tags=_entry_tags(entry, src["default_tags"], src["source_slug"]),
                untags=None,
            )
            new_here += 1
            minted += 1

        _record_status(
            store,
            src["source_id"],
            "ok",
            etag=getattr(feed, "etag", None),
            modified=getattr(feed, "modified", None),
        )
        log.info("news_poll: %s → %d new articles", src["title"], new_here)

    log.info(
        "news_poll pass: %d feeds, %d new articles, %d failed",
        claimed,
        minted,
        failed,
    )
    return {"claimed": claimed, "ok": minted, "failed": failed}


def _default_parse_feed(
    url: str, *, etag: str | None = None, modified: str | None = None
) -> Any:
    from precis.utils.optional_deps import require_optional

    feedparser = require_optional("feedparser", extra="external")
    # feedparser sends If-None-Match / If-Modified-Since from these and sets
    # ``.status == 304`` (with empty ``.entries``) when the feed is unchanged.
    return feedparser.parse(url, etag=etag or None, modified=modified or None)


__all__ = ["run_news_pass"]
