"""Tests for `YouTubeHandler` — transcript fetching with caching.

The `youtube-transcript-api` package is mocked at the API surface
boundary (`YouTubeTranscriptApi.fetch` / `.list`) so tests don't talk
to the real YouTube. Cache flow is delegated to `CacheBackedHandler`
(covered in `test_cache_base.py`); these tests focus on:

- video id extraction from URL/short forms
- language preference parsing & cache-key inclusion
- ``view='languages'`` side query
- error mapping (TranscriptsDisabled / NoTranscriptFound /
  VideoUnavailable / generic)
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound, RateLimited, Unsupported, Upstream
from precis.handlers.youtube import (
    YouTubeHandler,
    _extract_video_id,
    _parse_languages,
)
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.store.types import ChunkInsert

# ── stub youtube-transcript-api at the import boundary ────────────────


class _Snippet:
    def __init__(self, text: str) -> None:
        self.text = text


class _Track:
    def __init__(self, code: str, lang: str, *, is_generated: bool) -> None:
        self.language_code = code
        self.language = lang
        self.is_generated = is_generated


class _StubApi:
    """Drop-in for ``YouTubeTranscriptApi()``.

    Subclassed in individual tests to override behaviour.
    """

    fetch_calls: list[tuple[str, list[str]]] = []
    list_calls: list[str] = []

    @classmethod
    def reset(cls) -> None:
        cls.fetch_calls = []
        cls.list_calls = []

    def fetch(self, video_id: str, languages: list[str]) -> Any:
        type(self).fetch_calls.append((video_id, languages))
        return [_Snippet("hello"), _Snippet("world")]

    def list(self, video_id: str) -> Any:
        type(self).list_calls.append(video_id)
        return [
            _Track("en", "English", is_generated=False),
            _Track("es", "Spanish", is_generated=True),
        ]


@pytest.fixture(autouse=True)
def _patch_yt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch `youtube_transcript_api.YouTubeTranscriptApi` to the stub."""
    import sys

    _StubApi.reset()

    fake_yt = type(sys)("youtube_transcript_api")
    fake_yt.YouTubeTranscriptApi = _StubApi  # type: ignore[attr-defined]

    fake_errors = type(sys)("youtube_transcript_api._errors")

    class _Boom(Exception):  # base for typed errors
        pass

    class TranscriptsDisabled(_Boom):
        def __init__(self, video_id: str = "") -> None:
            super().__init__(video_id)

    class NoTranscriptFound(_Boom):
        # Mirror the real youtube-transcript-api signature so tests can
        # raise it with the same args the library uses (the handler's
        # ``except NoTranscriptFound`` clause doesn't read these but the
        # caller-side raise needs them to type-check + run).
        def __init__(
            self,
            video_id: str = "",
            requested_language_codes: Any = None,
            transcript_data: Any = None,
        ) -> None:
            super().__init__(video_id)

    class VideoUnavailable(_Boom):
        def __init__(self, video_id: str = "") -> None:
            super().__init__(video_id)

    class RequestBlocked(_Boom):
        def __init__(self, video_id: str = "") -> None:
            super().__init__(video_id)

    class IpBlocked(_Boom):
        def __init__(self, video_id: str = "") -> None:
            super().__init__(video_id)

    fake_errors.TranscriptsDisabled = TranscriptsDisabled  # type: ignore[attr-defined]
    fake_errors.NoTranscriptFound = NoTranscriptFound  # type: ignore[attr-defined]
    fake_errors.VideoUnavailable = VideoUnavailable  # type: ignore[attr-defined]
    monkeypatch.setattr(fake_errors, "RequestBlocked", RequestBlocked, raising=False)
    monkeypatch.setattr(fake_errors, "IpBlocked", IpBlocked, raising=False)

    monkeypatch.setitem(sys.modules, "youtube_transcript_api", fake_yt)
    monkeypatch.setitem(sys.modules, "youtube_transcript_api._errors", fake_errors)


@pytest.fixture
def handler(hub: Hub) -> YouTubeHandler:
    return YouTubeHandler(hub=hub)


# ── basic flow ────────────────────────────────────────────────────────


def test_first_fetch_renders_transcript(handler: YouTubeHandler) -> None:
    resp = handler.get(id="dQw4w9WgXcQ")
    assert "hello\nworld" in resp.body
    assert "Source: YouTube" in resp.body
    assert "youtube.com/watch?v=dQw4w9WgXcQ" in resp.body
    assert resp.cost == "[cost: free]"


def test_cache_hit_on_second_call(handler: YouTubeHandler) -> None:
    handler.get(id="dQw4w9WgXcQ")
    handler.get(id="dQw4w9WgXcQ")
    assert len(_StubApi.fetch_calls) == 1


def test_cache_collapses_url_and_bare_id(handler: YouTubeHandler) -> None:
    handler.get(id="dQw4w9WgXcQ")
    handler.get(id="https://youtu.be/dQw4w9WgXcQ")
    handler.get(id="https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert len(_StubApi.fetch_calls) == 1


def test_different_languages_cache_separately(handler: YouTubeHandler) -> None:
    handler.get(id="dQw4w9WgXcQ", languages="en")
    handler.get(id="dQw4w9WgXcQ", languages="es")
    handler.get(id="dQw4w9WgXcQ", languages="es")  # this one hits cache
    # Two upstream calls (en, es); third call hits es cache.
    assert len(_StubApi.fetch_calls) == 2


# ── view='languages' ─────────────────────────────────────────────────


def test_languages_view_lists_tracks(handler: YouTubeHandler) -> None:
    resp = handler.get(id="dQw4w9WgXcQ", view="languages")
    assert "Available transcripts for dQw4w9WgXcQ" in resp.body
    assert "en" in resp.body and "English" in resp.body
    assert "es" in resp.body and "Spanish" in resp.body
    assert "[human]" in resp.body
    assert "[auto]" in resp.body
    # Languages query is not cached — fetch_calls untouched.
    assert _StubApi.fetch_calls == []
    assert _StubApi.list_calls == ["dQw4w9WgXcQ"]


def test_languages_view_hint_interpolates_first_real_code(
    handler: YouTubeHandler,
) -> None:
    """hint-audit item 6: the drill-down hint hardcoded
    ``args={'languages': 'LANG_CODE'}`` under a table of real codes —
    a copy-paste trap. Interpolate the first listed track's real code
    (``en``, per ``_StubApi.list``'s order) instead."""
    resp = handler.get(id="dQw4w9WgXcQ", view="languages")
    assert "LANG_CODE" not in resp.body
    assert "args={'languages': 'en'}" in resp.body
    # And the advertised call actually dispatches to that language —
    # ``args={'languages': ...}`` is the MCP-dispatch-layer shape
    # (unpacked to this handler's own ``languages=`` kwarg by the
    # runtime before the handler ever sees it; calling the handler
    # directly here exercises that same target kwarg).
    _StubApi.fetch_calls.clear()
    handler.get(id="dQw4w9WgXcQ", languages="en")
    assert _StubApi.fetch_calls == [("dQw4w9WgXcQ", ["en"])]


def test_languages_view_accepts_url(handler: YouTubeHandler) -> None:
    handler.get(id="https://youtu.be/dQw4w9WgXcQ", view="languages")
    assert _StubApi.list_calls == ["dQw4w9WgXcQ"]


# ── error mapping ────────────────────────────────────────────────────


def test_transcripts_disabled_raises_not_found(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from youtube_transcript_api._errors import (
        TranscriptsDisabled,
    )

    class Bad(_StubApi):
        def fetch(self, video_id: str, languages: list[str]) -> Any:
            raise TranscriptsDisabled("disabled")

    sys.modules["youtube_transcript_api"].YouTubeTranscriptApi = Bad  # type: ignore[attr-defined]
    h = YouTubeHandler(hub=Hub(store=store))
    with pytest.raises(NotFound, match="disabled"):
        h.get(id="dQw4w9WgXcQ")


def test_no_transcript_raises_not_found(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from youtube_transcript_api._errors import (
        NoTranscriptFound,
    )

    class Bad(_StubApi):
        def fetch(self, video_id: str, languages: list[str]) -> Any:
            raise NoTranscriptFound(
                video_id, requested_language_codes=languages, transcript_data=[]
            )

    sys.modules["youtube_transcript_api"].YouTubeTranscriptApi = Bad  # type: ignore[attr-defined]
    h = YouTubeHandler(hub=Hub(store=store))
    with pytest.raises(NotFound, match="no transcript"):
        h.get(id="dQw4w9WgXcQ")


def test_video_unavailable_raises_not_found(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from youtube_transcript_api._errors import (
        VideoUnavailable,
    )

    class Bad(_StubApi):
        def fetch(self, video_id: str, languages: list[str]) -> Any:
            raise VideoUnavailable("private")

    sys.modules["youtube_transcript_api"].YouTubeTranscriptApi = Bad  # type: ignore[attr-defined]
    h = YouTubeHandler(hub=Hub(store=store))
    with pytest.raises(NotFound, match="unavailable"):
        h.get(id="dQw4w9WgXcQ")


def test_generic_exception_raises_upstream(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    class Bad(_StubApi):
        def fetch(self, video_id: str, languages: list[str]) -> Any:
            raise RuntimeError("yt boom")

    sys.modules["youtube_transcript_api"].YouTubeTranscriptApi = Bad  # type: ignore[attr-defined]
    h = YouTubeHandler(hub=Hub(store=store))
    with pytest.raises(Upstream, match="YouTube API error"):
        h.get(id="dQw4w9WgXcQ")


# ── id extraction & language parsing (pure helpers) ──────────────────


class TestExtractVideoId:
    def test_bare_id(self) -> None:
        assert _extract_video_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_youtu_be(self) -> None:
        assert _extract_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_watch_url(self) -> None:
        assert (
            _extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
            == "dQw4w9WgXcQ"
        )

    def test_shorts(self) -> None:
        assert (
            _extract_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ")
            == "dQw4w9WgXcQ"
        )

    def test_embed(self) -> None:
        assert (
            _extract_video_id("https://www.youtube.com/embed/dQw4w9WgXcQ")
            == "dQw4w9WgXcQ"
        )

    def test_mobile(self) -> None:
        assert (
            _extract_video_id("https://m.youtube.com/watch?v=dQw4w9WgXcQ")
            == "dQw4w9WgXcQ"
        )

    def test_strips_extra_path(self) -> None:
        assert (
            _extract_video_id("https://youtu.be/dQw4w9WgXcQ/extra-trash")
            == "dQw4w9WgXcQ"
        )

    def test_garbage_raises(self) -> None:
        with pytest.raises(BadInput):
            _extract_video_id("not a youtube url")

    def test_bare_too_short_raises(self) -> None:
        with pytest.raises(BadInput):
            _extract_video_id("short")


class TestParseLanguages:
    def test_default_en(self) -> None:
        assert _parse_languages("") == ["en"]

    def test_single(self) -> None:
        assert _parse_languages("de") == ["de"]

    def test_multi(self) -> None:
        assert _parse_languages("en,es,fr") == ["en", "es", "fr"]

    def test_strips(self) -> None:
        assert _parse_languages(" en , es ") == ["en", "es"]

    def test_empty_pieces_default(self) -> None:
        assert _parse_languages(",,,") == ["en"]


# ---- watch-page meta scrape (T183) ---------------------------------


class TestScrapeWatchPageMeta:
    """The helper parses og:* + itemprop=* meta tags from the watch
    page HTML. We monkeypatch ``safe_get`` to return a fixture HTML
    blob so the tests stay offline."""

    def _stub_response(self, html: str, status: int = 200):
        import httpx

        return httpx.Response(
            status, text=html, request=httpx.Request("GET", "https://www.youtube.com/")
        )

    def test_parses_og_title_description_channel_duration(self, monkeypatch) -> None:
        from precis.handlers import youtube as yt_mod

        html = """
        <html><head>
          <meta property="og:title" content="Rick Astley - Never Gonna Give You Up">
          <meta property="og:description" content="The official music video for &quot;Never Gonna Give You Up&quot; by Rick Astley.">
          <meta property="og:image" content="https://i.ytimg.com/vi/dQw4w9WgXcQ/maxres.jpg">
          <meta property="og:video:duration" content="213">
          <meta itemprop="name" content="Rick Astley">
          <meta itemprop="datePublished" content="2009-10-25">
          <link itemprop="url" href="https://www.youtube.com/@RickAstleyYT">
        </head><body></body></html>
        """

        def _fake_get(client, url):
            return self._stub_response(html)

        monkeypatch.setattr(yt_mod, "safe_get", _fake_get, raising=False)
        # The function imports safe_get inside its body; patch the
        # imported symbol on the module directly so the import line
        # picks up our stub.
        import precis.utils.safe_fetch as _sf

        monkeypatch.setattr(_sf, "safe_get", _fake_get)
        meta = yt_mod._scrape_watch_page_meta("dQw4w9WgXcQ")
        assert meta["title"] == "Rick Astley - Never Gonna Give You Up"
        assert (
            meta["description"]
            == 'The official music video for "Never Gonna Give You Up" by Rick Astley.'
        )
        assert meta["thumbnail_url"] == "https://i.ytimg.com/vi/dQw4w9WgXcQ/maxres.jpg"
        assert meta["duration_s"] == 213
        assert meta["channel_name"] == "Rick Astley"
        assert meta["published_at"] == "2009-10-25"
        assert meta["channel_url"] == "https://www.youtube.com/@RickAstleyYT"
        assert meta["watch_url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    def test_empty_dict_on_network_failure(self, monkeypatch) -> None:
        import precis.utils.safe_fetch as _sf
        from precis.handlers import youtube as yt_mod

        def _boom(client, url):
            raise RuntimeError("network down")

        monkeypatch.setattr(_sf, "safe_get", _boom)
        meta = yt_mod._scrape_watch_page_meta("dQw4w9WgXcQ")
        assert meta == {}

    def test_empty_dict_on_non_200(self, monkeypatch) -> None:
        import precis.utils.safe_fetch as _sf
        from precis.handlers import youtube as yt_mod

        def _403(client, url):
            return self._stub_response("Forbidden", status=403)

        monkeypatch.setattr(_sf, "safe_get", _403)
        meta = yt_mod._scrape_watch_page_meta("dQw4w9WgXcQ")
        assert meta == {}

    def test_missing_tags_dont_raise(self, monkeypatch) -> None:
        import precis.utils.safe_fetch as _sf
        from precis.handlers import youtube as yt_mod

        def _bare(client, url):
            return self._stub_response("<html><body>no metadata</body></html>")

        monkeypatch.setattr(_sf, "safe_get", _bare)
        meta = yt_mod._scrape_watch_page_meta("dQw4w9WgXcQ")
        # Only watch_url should be present (the rest weren't parseable).
        assert meta == {"watch_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}


def test_generic_upstream_error_names_languages_view(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M3 burn-down (gr494831): an unexpected YT failure carries next=."""
    import sys

    class Bad(_StubApi):
        def fetch(self, video_id: str, languages: list[str]) -> Any:
            raise RuntimeError("yt boom")

    monkeypatch.setattr(
        sys.modules["youtube_transcript_api"], "YouTubeTranscriptApi", Bad
    )
    h = YouTubeHandler(hub=Hub(store=store))
    with pytest.raises(Upstream, match="YouTube API error") as exc:
        h.get(id="dQw4w9WgXcQ")
    assert exc.value.next is not None
    assert "view='languages'" in str(exc.value.next)


# ── cached-video discovery (gr498108) ────────────────────────────────


def _cache_video(
    store: Store,
    video_id: str,
    title: str,
    *,
    channel: str | None = None,
    duration_s: int | None = None,
    description: str | None = None,
    text: str = "transcript body",
):
    """Insert a youtube cache row directly — no upstream involved."""
    meta: dict[str, Any] = {"video_id": video_id, "languages": ["en"]}
    if channel is not None:
        meta["channel_name"] = channel
    if duration_s is not None:
        meta["duration_s"] = duration_s
    if description is not None:
        meta["description"] = description
    return store.put_cache_entry(
        kind="youtube",
        slug=video_id,
        title=title,
        body_blocks=[ChunkInsert(ord=0, text=text)],
        provider="youtube",
        request_hash=f"hash-{video_id}",
        ttl_seconds=30 * 24 * 3600,
        cache_meta=meta,
    )


class TestRecentListing:
    def test_lists_videos_with_channel_duration_date(
        self, handler: YouTubeHandler, store: Store
    ) -> None:
        _cache_video(
            store,
            "dQw4w9WgXcQ",
            "Rick Astley - Never Gonna Give You Up",
            channel="Rick Astley",
            duration_s=213,
        )
        resp = handler.get(id="/recent")
        assert "# cached youtube videos" in resp.body
        assert "`dQw4w9WgXcQ`" in resp.body
        assert "Rick Astley - Never Gonna Give You Up" in resp.body
        assert "Rick Astley" in resp.body
        assert "3m33s" in resp.body
        assert "CACHE:fresh" in resp.body
        assert "showing 1–1 of 1" in resp.body
        assert _StubApi.fetch_calls == []

    def test_bare_get_and_slash_are_the_same_listing(
        self, handler: YouTubeHandler, store: Store
    ) -> None:
        _cache_video(store, "dQw4w9WgXcQ", "T")
        for ident in (None, "", "/", "/recent"):
            resp = handler.get(id=ident)
            assert "`dQw4w9WgXcQ`" in resp.body, ident

    def test_paginates(self, handler: YouTubeHandler, store: Store) -> None:
        for i in range(25):
            _cache_video(store, f"vid{i:07d}xyz", f"Video {i}")
        p1 = handler.get(id="/recent", page_size=10)
        p2 = handler.get(id="/recent", page_size=10, page=2)
        assert "showing 1–10 of 25 (page 1 of 3)" in p1.body
        assert "showing 11–20 of 25 (page 2 of 3)" in p2.body
        assert "page': 2" in p1.body  # next-page hint
        p4 = handler.get(id="/recent", page_size=10, page=4)
        assert "no entries" in p4.body

    def test_page_on_a_transcript_read_is_refused(
        self, handler: YouTubeHandler
    ) -> None:
        with pytest.raises(BadInput, match="page"):
            handler.get(id="dQw4w9WgXcQ", page=2)

    def test_unknown_view_is_refused_with_options(
        self, handler: YouTubeHandler
    ) -> None:
        with pytest.raises(BadInput, match="unknown view") as exc:
            handler.get(id="dQw4w9WgXcQ", view="bogus")
        assert exc.value.options == ["languages", "body"]

    def test_no_fetch_reads_without_fetching(
        self, handler: YouTubeHandler, store: Store
    ) -> None:
        _cache_video(store, "dQw4w9WgXcQ", "T", text="the cached words")
        resp = handler.get(id="dQw4w9WgXcQ", no_fetch=True)
        assert "the cached words" in resp.body
        assert _StubApi.fetch_calls == []


class TestSearch:
    def test_chunk_hits_grouped_one_row_per_video(
        self, handler: YouTubeHandler, store: Store
    ) -> None:
        # Two hits in the SAME video must collapse to one row.
        store.put_cache_entry(
            kind="youtube",
            slug="vidAAAAAAA1",
            title="Ions talk",
            body_blocks=[
                ChunkInsert(ord=0, text="neighbouring ions repel each other"),
                ChunkInsert(ord=1, text="neighbouring ions also attract"),
            ],
            provider="youtube",
            request_hash="h1",
            ttl_seconds=3600,
            cache_meta={"video_id": "vidAAAAAAA1", "channel_name": "Ch"},
        )
        resp = handler.search(q="neighbouring ions")
        assert resp.body.count("`vidAAAAAAA1`") == 1
        assert "more transcript hit" in resp.body

    def test_title_match_surfaced(self, handler: YouTubeHandler, store: Store) -> None:
        _cache_video(
            store,
            "vidBBBBBBB2",
            "How neighbouring ions self-organise",
            channel="Ion Channel",
        )
        resp = handler.search(q="neighbouring ions")
        assert "`vidBBBBBBB2`" in resp.body
        assert "title/channel match" in resp.body

    def test_channel_match(self, handler: YouTubeHandler, store: Store) -> None:
        _cache_video(
            store,
            "vidCCCCCCC3",
            "A lecture",
            channel="Veritasium",
        )
        resp = handler.search(q="veritasium")
        assert "`vidCCCCCCC3`" in resp.body

    def test_empty_state_lists_closest_titles(
        self, handler: YouTubeHandler, store: Store
    ) -> None:
        _cache_video(store, "vidDDDDDDD4", "Quantum chromodynamics lecture")
        resp = handler.search(q="quantum chromodynamic")
        if "no cached youtube videos match" in resp.body:
            assert "closest cached titles" in resp.body
            assert "/recent" in resp.body
        else:
            assert "`vidDDDDDDD4`" in resp.body

    def test_per_paper_raises_chunk_cap(
        self, handler: YouTubeHandler, store: Store
    ) -> None:
        store.put_cache_entry(
            kind="youtube",
            slug="vidEEEEEEE5",
            title="T",
            body_blocks=[
                ChunkInsert(ord=0, text="alpha beta gamma"),
                ChunkInsert(ord=1, text="alpha beta delta"),
            ],
            provider="youtube",
            request_hash="h5",
            ttl_seconds=3600,
            cache_meta={"video_id": "vidEEEEEEE5"},
        )
        one = handler.search(q="alpha beta")
        two = handler.search(q="alpha beta", per_paper=2)
        assert two.body.count("alpha beta") > one.body.count("alpha beta")
        with pytest.raises(BadInput, match="per_paper"):
            handler.search(q="alpha beta", per_paper=0)

    def test_no_live_fetch_on_search(
        self, handler: YouTubeHandler, store: Store
    ) -> None:
        _cache_video(store, "vidFFFFFFF6", "T", text="hello world")
        handler.search(q="hello")
        assert _StubApi.fetch_calls == []
        assert _StubApi.list_calls == []

    def test_page_two_includes_later_title_hits(
        self, handler: YouTubeHandler, store: Store
    ) -> None:
        """The title leg must fetch through the requested page — a
        ``limit=page_size`` fetch would drop page-2 title matches and
        under-count the total."""
        for i in range(12):
            _cache_video(store, f"vid{i:07d}pqr", f"Ions lecture {i}")
        p1 = handler.search(q="ions lecture", page_size=10)
        p2 = handler.search(q="ions lecture", page_size=10, page=2)
        assert "10 of 12 video hits" in p1.body
        assert p2.body.count("## `") == 2
        assert "page 3" not in p2.body

    def test_channel_match_escapes_ilike_wildcards(
        self, handler: YouTubeHandler, store: Store
    ) -> None:
        """A literal '%' in q must not act as an ILIKE wildcard."""
        _cache_video(store, "vidIIIIIII9", "Chem A", channel="100% Chemistry")
        _cache_video(store, "vidJJJJJJJ0", "Chem B", channel="1000 Islands")
        resp = handler.search(q="100%")
        assert "100% Chemistry" in resp.body
        assert "1000 Islands" not in resp.body

    def test_prose_query_on_get_hints_at_search(self, handler: YouTubeHandler) -> None:
        with pytest.raises(BadInput) as exc:
            handler.get(q="neighbouring ions")
        assert "search(kind='youtube'" in str(exc.value.next)


class TestDispatchArgs:
    """The extras gate must forward the new kwargs end-to-end (gr498108:
    ``args={'per_paper': 1}`` was refused before it reached the handler)."""

    def test_per_paper_via_args_reaches_handler(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        _cache_video(store, "vidGGGGGGG7", "Ions", text="ions everywhere")
        out = runtime_with_store.dispatch(
            "search",
            {"kind": "youtube", "q": "ions", "__extras__": {"per_paper": 1}},
        )
        body = out.body if hasattr(out, "body") else str(out)
        assert "not accepted" not in body
        assert "`vidGGGGGGG7`" in body

    def test_no_fetch_via_args_reaches_handler(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        _cache_video(store, "vidHHHHHHH8", "T", text="cached bytes")
        out = runtime_with_store.dispatch(
            "get",
            {
                "kind": "youtube",
                "id": "vidHHHHHHH8",
                "__extras__": {"no_fetch": True},
            },
        )
        body = out.body if hasattr(out, "body") else str(out)
        assert "cached bytes" in body

    def test_page_via_args_pages_the_listing(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        for i in range(3):
            _cache_video(store, f"p{i:09d}x", f"V{i}")
        out = runtime_with_store.dispatch(
            "get",
            {
                "kind": "youtube",
                "id": "/recent",
                "__extras__": {"page": 2, "page_size": 2},
            },
        )
        body = out.body if hasattr(out, "body") else str(out)
        assert "page 2" in body


class TestEyeRefusal:
    def test_stored_ref_error_names_what_is_wrong(
        self, handler: YouTubeHandler, store: Store
    ) -> None:
        """gr498108: a cached youtube ref IS a stored ref — the eye
        refusal must say so (missing handle, not missing ref) and
        point at /recent."""
        _cache_video(store, "dQw4w9WgXcQ", "T")
        with pytest.raises(Unsupported) as exc:
            handler.eye(id="dQw4w9WgXcQ", extent="summary")
        assert "stored" in exc.value.cause
        assert "not a stored ref" not in exc.value.cause
        assert "handle" in exc.value.cause
        assert "/recent" in str(exc.value.next)


# ── rate-limit error mapping (gr531240) ──────────────────────────────


def test_request_blocked_maps_to_rate_limited(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """YouTube's "too many requests" surface (``RequestBlocked``) is a
    ``RateLimited``, not a generic ``Upstream`` — the next= carries a
    conservative wait and tells the caller to serialise fetches."""
    import sys

    from youtube_transcript_api._errors import RequestBlocked

    class Bad(_StubApi):
        def fetch(self, video_id: str, languages: list[str]) -> Any:
            raise RequestBlocked(video_id)

    monkeypatch.setattr(
        sys.modules["youtube_transcript_api"], "YouTubeTranscriptApi", Bad
    )
    h = YouTubeHandler(hub=Hub(store=store))
    with pytest.raises(RateLimited) as ei:
        h.get(id="dQw4w9WgXcQ")
    text = str(ei.value)
    assert "rate-limiting" in text
    assert "serialise" in text
    assert "wait" in text


def test_ip_blocked_maps_to_rate_limited(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from youtube_transcript_api._errors import IpBlocked

    class Bad(_StubApi):
        def fetch(self, video_id: str, languages: list[str]) -> Any:
            raise IpBlocked(video_id)

    monkeypatch.setattr(
        sys.modules["youtube_transcript_api"], "YouTubeTranscriptApi", Bad
    )
    h = YouTubeHandler(hub=Hub(store=store))
    with pytest.raises(RateLimited) as ei:
        h.get(id="dQw4w9WgXcQ")
    assert "serialise" in str(ei.value)


def test_shared_limiter_refusal_maps_to_rate_limited(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shared-limiter token refusal for ``host:www.youtube.com`` also
    surfaces as ``RateLimited`` with the serialise next=, not a bare
    ``Upstream`` the caller can't distinguish from an outage."""
    import precis.handlers.youtube as yt_mod
    from precis.utils.rate_limit import RateLimited as _LimiterRefused

    def _refuse(*a: Any, **kw: Any) -> Any:
        raise _LimiterRefused("host:www.youtube.com")

    monkeypatch.setattr(yt_mod, "call_with_backoff", _refuse)
    h = YouTubeHandler(hub=Hub(store=store))
    with pytest.raises(RateLimited) as ei:
        h.get(id="dQw4w9WgXcQ")
    assert "serialise" in str(ei.value)


# ── chunk-range reads for long transcripts (gr533135) ────────────────


def _long_transcript() -> str:
    return "\n".join(
        f"segment {i:03d}: the speaker explains the mechanism in detail"
        for i in range(200)
    )


def _patch_long_transcript(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    body = _long_transcript()

    class Long(_StubApi):
        def fetch(self, video_id: str, languages: list[str]) -> Any:
            return [_Snippet(body)]

    monkeypatch.setattr(
        sys.modules["youtube_transcript_api"], "YouTubeTranscriptApi", Long
    )


def test_long_transcript_walkable_in_chunk_ranges(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr533135: a >4 KB transcript — unreadable whole under the context
    budget — pages in full by walking ``<vid>~lo..hi`` ranges."""
    _patch_long_transcript(monkeypatch)
    h = YouTubeHandler(hub=Hub(store=store))
    resp = h.get(id="dQw4w9WgXcQ")
    # Whole-body render exceeds the context cap; the response carries
    # the literal first-range call the refusal prints as next=.
    assert len(resp.body.encode("utf-8")) > 4096
    assert "id='dQw4w9WgXcQ~0..3'" in (resp.result_budget_hint or "")

    ref = h.store.list_refs(kind="youtube", limit=1)[0]
    total = h.store.chunks.count_chunks(ref.id)
    assert total > 4

    seen = ""
    lo = 0
    while lo < total:
        hi = min(lo + 3, total - 1)
        page = h.get(id=f"dQw4w9WgXcQ~{lo}..{hi}")
        seen += page.body
        lo = hi + 1
    assert "segment 000" in seen
    assert "segment 199" in seen

    # Past-the-end refuses with the first-range call as next=.
    with pytest.raises(NotFound) as ei:
        h.get(id=f"dQw4w9WgXcQ~{total + 5}")
    assert "id='dQw4w9WgXcQ~0..3'" in str(ei.value)


def test_over_cap_get_refusal_names_range_call(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr533135 end-to-end: an over-budget whole-body get (head-outline:
    head + outline, not a refusal) still names the first chunk-range
    call, so the body stays reachable."""
    # The suite disables the context cap by default
    # (``PRECIS_TOOL_RESULT_CONTEXT_BYTES=0`` in conftest) — restore the
    # prod default so the over-cap path runs.
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "4096")
    _patch_long_transcript(monkeypatch)
    out, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "youtube", "id": "dQw4w9WgXcQ"}, fold=True
    )
    assert not is_error
    assert len(out.encode("utf-8")) <= 4096
    assert "dQw4w9WgXcQ~0..3" in out
