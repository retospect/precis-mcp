"""Pure-function tests for the ``news`` kind + poller + briefing.

DB-backed end-to-end (mint → search → brief) is covered by the
integration suite; these lock the offline logic: URL canonicalization /
dedup-key alignment, feed-entry parsing, tag composition, and briefing
context rendering.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from precis.errors import BadInput
from precis.handlers._cache_base import CacheBackedHandler
from precis.handlers.news import article_blocks, canonical_url
from precis.workers import briefing, news_poll

# ── canonical_url ──────────────────────────────────────────────────────


def test_canonical_url_strips_tracking_and_fragment() -> None:
    raw = "HTTPS://Www.Example.COM/Story/?utm_source=rss&utm_campaign=x&id=42#top"
    assert canonical_url(raw) == "https://www.example.com/Story?id=42"


def test_canonical_url_sorts_query_for_stable_key() -> None:
    a = canonical_url("https://x.com/a?b=2&a=1")
    b = canonical_url("https://x.com/a?a=1&b=2")
    assert a == b == "https://x.com/a?a=1&b=2"


def test_canonical_url_drops_trailing_slash() -> None:
    assert canonical_url("https://x.com/a/") == "https://x.com/a"


def test_canonical_url_rejects_non_url() -> None:
    with pytest.raises(BadInput):
        canonical_url("not a url")


# ── dedup-key alignment between poller and on-demand get ───────────────


def test_request_hash_matches_cache_base_hash() -> None:
    """The poller must hash the canonical URL the same way the cache base
    does, so a poller-minted article and an on-demand ``get`` of the same
    URL collide on one cache row."""
    key = canonical_url("https://x.com/news/article?id=7")
    assert news_poll._request_hash(key) == CacheBackedHandler._hash(key)


# ── feed-entry parsing (lifted from old rss_ingest) ────────────────────


def test_entry_pub_date_from_struct_time() -> None:
    entry = SimpleNamespace(published_parsed=(2026, 6, 21, 9, 30, 0, 0, 0, 0))
    got = news_poll._entry_pub_date(entry)
    assert got == datetime(2026, 6, 21, 9, 30, tzinfo=UTC)


def test_entry_pub_date_missing_returns_none() -> None:
    assert news_poll._entry_pub_date(SimpleNamespace()) is None


def test_entry_tags_compose_and_dedup() -> None:
    entry = SimpleNamespace(published_parsed=(2026, 6, 21, 0, 0, 0, 0, 0, 0))
    tags = news_poll._entry_tags(entry, ["topic:tech", "category:news"], "bbc")
    assert tags[0] == "category:news"
    assert "source:bbc" in tags
    assert "topic:tech" in tags
    assert "published:2026-06-21" in tags
    # category:news passed in default_tags must not duplicate
    assert tags.count("category:news") == 1


# ── briefing context rendering ─────────────────────────────────────────


def test_format_context_renders_headlines() -> None:
    refs = [
        SimpleNamespace(
            title="Markets rally",
            slug="markets-rally",
            meta={"url": "https://x.com/m", "source": "bbc"},
            updated_at=datetime(2026, 6, 21, 8, 0, tzinfo=UTC),
        ),
    ]
    out = briefing._format_context(refs)
    assert "[bbc] Markets rally" in out
    assert "https://x.com/m" in out


def test_article_blocks_nonempty() -> None:
    blocks = article_blocks("# Heading\n\nA paragraph of news.", embedder=None)
    assert blocks
    assert all(b.embedding is None for b in blocks)  # deferred embedding


# ── RSS-content ingestion (feedparser-only, no trafilatura) ────────────


def test_strip_html_drops_tags_and_unescapes() -> None:
    out = news_poll._strip_html("<p>Hello <b>world</b></p><p>Second &amp; line</p>")
    assert "Hello world" in out
    assert "Second & line" in out
    assert "<" not in out


def test_entry_body_prefers_full_content() -> None:
    entry = SimpleNamespace(
        content=[{"value": "<p>Full article text.</p>"}], summary="just a blurb"
    )
    assert news_poll._entry_body(entry) == "Full article text."


def test_entry_body_falls_back_to_summary() -> None:
    entry = SimpleNamespace(summary="<p>A summary.</p>")
    assert news_poll._entry_body(entry) == "A summary."


def test_entry_body_empty_when_no_fields() -> None:
    assert news_poll._entry_body(SimpleNamespace()) == ""


# ── briefing delivery + help skill ─────────────────────────────────────


class _FakeRef:
    id = 99


class _FakeConn:
    def __init__(self, existing: tuple | None = None) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self._existing = existing

    def execute(self, sql: str, params: tuple = ()) -> _FakeConn:
        self.calls.append((sql, params))
        return self

    def fetchone(self) -> tuple | None:
        return self._existing


class _FakeDeliverStore:
    def __init__(self, existing: tuple | None = None) -> None:
        self.conn = _FakeConn(existing)
        self.inserted: list[dict] = []
        self.blocks: list[tuple] = []

    def tx(self):  # type: ignore[no-untyped-def]
        import contextlib

        @contextlib.contextmanager
        def _cm():  # type: ignore[no-untyped-def]
            yield self.conn

        return _cm()

    def insert_ref(self, **kw: object) -> _FakeRef:
        self.inserted.append(kw)
        return _FakeRef()

    def insert_blocks(
        self, ref_id: object, blocks: object, conn: object = None
    ) -> None:
        self.blocks.append((ref_id, blocks))


def test_deliver_queues_message_and_notifies() -> None:
    import json

    store = _FakeDeliverStore()
    briefing._deliver(store, "discord/1/2/2", "Brief text", "2026-06-23")  # type: ignore[arg-type]
    # a message ref was created, targeted + date-stamped for idempotency
    assert store.inserted and store.inserted[0]["kind"] == "message"
    assert store.inserted[0]["meta"]["target"] == "discord/1/2/2"
    assert store.inserted[0]["meta"]["briefing_date"] == "2026-06-23"
    assert store.blocks, "expected a message_body block"
    # and the precis.messages notify fired with the new ref id
    notifies = [c for c in store.conn.calls if "precis.messages" in c[0]]
    assert notifies, "expected a precis.messages pg_notify"
    payload = json.loads(notifies[0][1][0])
    assert payload == {"ref_id": 99, "target": "discord/1/2/2"}


def test_deliver_idempotent_skips_when_already_sent() -> None:
    # existence probe returns a row → today's brief already queued
    store = _FakeDeliverStore(existing=(1,))
    briefing._deliver(store, "discord/1/2/2", "Brief text", "2026-06-23")  # type: ignore[arg-type]
    assert not store.inserted, "must not create a second delivery message"
    assert not any("precis.messages" in c[0] for c in store.conn.calls)


def test_news_help_skill_present_and_shaped() -> None:
    import pathlib

    import precis

    p = pathlib.Path(precis.__file__).parent / "data" / "skills" / "precis-news-help.md"
    assert p.exists(), "precis-news-help skill file missing"
    text = p.read_text()
    assert "id: precis-news-help" in text
    assert "news_poll" in text and "briefing" in text
