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
from precis.errors import BadInput, NotFound, Upstream
from precis.handlers.youtube import (
    YouTubeHandler,
    _extract_video_id,
    _parse_languages,
)
from precis.store import Store

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

    fake_errors.TranscriptsDisabled = TranscriptsDisabled  # type: ignore[attr-defined]
    fake_errors.NoTranscriptFound = NoTranscriptFound  # type: ignore[attr-defined]
    fake_errors.VideoUnavailable = VideoUnavailable  # type: ignore[attr-defined]

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


def test_languages_view_accepts_url(handler: YouTubeHandler) -> None:
    handler.get(id="https://youtu.be/dQw4w9WgXcQ", view="languages")
    assert _StubApi.list_calls == ["dQw4w9WgXcQ"]


# ── error mapping ────────────────────────────────────────────────────


def test_transcripts_disabled_raises_not_found(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from youtube_transcript_api._errors import (
        TranscriptsDisabled,  # type: ignore[import-not-found]
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
        NoTranscriptFound,  # type: ignore[import-not-found]
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
        VideoUnavailable,  # type: ignore[import-not-found]
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

        return httpx.Response(status, text=html, request=httpx.Request("GET", "https://www.youtube.com/"))

    def test_parses_og_title_description_channel_duration(
        self, monkeypatch
    ) -> None:
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
