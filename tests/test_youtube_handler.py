"""Tests for :class:`precis.handlers.youtube.YouTubeHandler`.

Originally Phase 4 (external stateless handlers, see CHANGELOG).  Split
out of ``test_phase4_external.py`` so YouTube and Math each have their
own test file.

Uses a mocked ``youtube-transcript-api`` — no network calls.  Live
smoke tests are not yet wired (the public API has no quota cost, so
they could be added cheaply if needed).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from precis import server
from precis.handlers.youtube import (
    YouTubeHandler,
    _extract_video_id,
    _parse_languages,
)
from precis.handlers.youtube import (
    _attribution as _yt_attribution,
)
from precis.protocol import ErrorCode, PrecisError
from precis.registry import (
    KINDS,
    SCHEMES,
    clear_kinds_mask,
    clear_session_stats,
    clear_startup_warnings,
    visible_kinds,
)

# ---------------------------------------------------------------------------
# id extraction helper (ported from tubescribe-mcp)
# ---------------------------------------------------------------------------


class TestExtractVideoId:
    def test_bare_11_char_id(self):
        assert _extract_video_id("79-bApI3GIU") == "79-bApI3GIU"

    def test_watch_url(self):
        url = "https://www.youtube.com/watch?v=79-bApI3GIU"
        assert _extract_video_id(url) == "79-bApI3GIU"

    def test_watch_url_with_extra_params(self):
        url = "https://www.youtube.com/watch?v=79-bApI3GIU&t=42s&feature=share"
        assert _extract_video_id(url) == "79-bApI3GIU"

    def test_short_url(self):
        assert _extract_video_id("https://youtu.be/79-bApI3GIU") == "79-bApI3GIU"

    def test_shorts_url(self):
        url = "https://www.youtube.com/shorts/79-bApI3GIU"
        assert _extract_video_id(url) == "79-bApI3GIU"

    def test_embed_url(self):
        url = "https://www.youtube.com/embed/79-bApI3GIU"
        assert _extract_video_id(url) == "79-bApI3GIU"

    def test_live_url(self):
        url = "https://www.youtube.com/live/79-bApI3GIU"
        assert _extract_video_id(url) == "79-bApI3GIU"

    def test_mobile_url(self):
        url = "https://m.youtube.com/watch?v=79-bApI3GIU"
        assert _extract_video_id(url) == "79-bApI3GIU"

    def test_invalid_url_raises(self):
        with pytest.raises(ValueError, match="cannot extract"):
            _extract_video_id("https://vimeo.com/123456")

    def test_too_short_id_raises(self):
        with pytest.raises(ValueError):
            _extract_video_id("abc")


class TestParseLanguages:
    def test_empty_defaults_to_en(self):
        assert _parse_languages("") == ["en"]

    def test_single_code(self):
        assert _parse_languages("fr") == ["fr"]

    def test_comma_separated(self):
        assert _parse_languages("en,es,fr") == ["en", "es", "fr"]

    def test_whitespace_trimmed(self):
        assert _parse_languages(" en , es ") == ["en", "es"]

    def test_all_empty_entries_fall_back_to_en(self):
        assert _parse_languages(",,,") == ["en"]


# ---------------------------------------------------------------------------
# Handler dispatch with a mocked API
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_yt_api(monkeypatch):
    """Return a fresh MagicMock stand-in for YouTubeTranscriptApi."""
    api = MagicMock()
    monkeypatch.setattr(
        "precis.handlers.youtube.YouTubeHandler._get_api",
        lambda self: api,
    )
    return api


class TestYouTubeHandler:
    def test_read_returns_transcript_text(self, mock_yt_api):
        # Mock snippets: each has a .text attr.
        s1 = MagicMock(text="Hello world")
        s2 = MagicMock(text="This is a test")
        mock_yt_api.fetch.return_value = [s1, s2]

        h = YouTubeHandler()
        out = h.read(
            path="79-bApI3GIU",
            selector=None,
            view=None,
            subview=None,
            query="",
            summarize=False,
            depth=0,
            page=1,
        )
        assert "Hello world" in out
        assert "This is a test" in out
        mock_yt_api.fetch.assert_called_once_with("79-bApI3GIU", languages=["en"])

    def test_read_with_language_preference(self, mock_yt_api):
        mock_yt_api.fetch.return_value = [MagicMock(text="Bonjour")]
        h = YouTubeHandler()
        out = h.read(
            path="79-bApI3GIU",
            selector=None,
            view=None,
            subview=None,
            query="fr,en",
            summarize=False,
            depth=0,
            page=1,
        )
        assert "Bonjour" in out
        mock_yt_api.fetch.assert_called_once_with("79-bApI3GIU", languages=["fr", "en"])

    def test_languages_view_lists_available(self, mock_yt_api):
        t1 = MagicMock(language="English", language_code="en", is_generated=False)
        t2 = MagicMock(language="Spanish", language_code="es", is_generated=True)
        mock_yt_api.list.return_value = [t1, t2]

        h = YouTubeHandler()
        out = h.read(
            path="79-bApI3GIU",
            selector=None,
            view="languages",
            subview=None,
            query="",
            summarize=False,
            depth=0,
            page=1,
        )
        assert "English" in out
        assert "Spanish" in out
        assert "en" in out
        assert "[auto]" in out  # Spanish is auto-generated
        assert "[human]" in out  # English is human-made

    def test_empty_path_raises_param_invalid(self, mock_yt_api):
        h = YouTubeHandler()
        with pytest.raises(PrecisError) as exc:
            h.read(
                path="",
                selector=None,
                view=None,
                subview=None,
                query="",
                summarize=False,
                depth=0,
                page=1,
            )
        assert exc.value.code == ErrorCode.PARAM_INVALID

    def test_malformed_id_raises_id_malformed(self, mock_yt_api):
        h = YouTubeHandler()
        with pytest.raises(PrecisError) as exc:
            h.read(
                path="https://vimeo.com/123",
                selector=None,
                view=None,
                subview=None,
                query="",
                summarize=False,
                depth=0,
                page=1,
            )
        assert exc.value.code == ErrorCode.ID_MALFORMED


# ---------------------------------------------------------------------------
# Registry integration — youtube kind shows up in visible_kinds
# ---------------------------------------------------------------------------


class TestYouTubeRegistration:
    @classmethod
    def setup_class(cls):
        # Force plugin discovery so KINDS / SCHEMES are populated.
        import precis.registry as reg

        reg._discover()

    def test_youtube_kind_registered_when_package_available(self):
        # youtube-transcript-api is an optional extra; if it imported at
        # module load, the kind registered.  This test checks the
        # registration outcome, not the availability of the package.
        try:
            import youtube_transcript_api  # noqa: F401
        except ImportError:
            pytest.skip("youtube-transcript-api not installed")
        assert "youtube" in KINDS
        assert "youtube" in SCHEMES

    def test_youtube_always_visible(self, monkeypatch):
        """No env requirement → kind is always in the enum."""
        try:
            import youtube_transcript_api  # noqa: F401
        except ImportError:
            pytest.skip("youtube-transcript-api not installed")
        names = {k.spec.name for k in visible_kinds("get")}
        assert "youtube" in names


# ---------------------------------------------------------------------------
# Server dispatch — type='youtube' routes through _dispatch
# ---------------------------------------------------------------------------


class TestYouTubeServerDispatch:
    def setup_method(self):
        clear_session_stats()
        clear_kinds_mask()
        clear_startup_warnings()

    def teardown_method(self):
        clear_session_stats()
        clear_kinds_mask()
        clear_startup_warnings()

    def test_type_youtube_builds_correct_uri(self):
        try:
            import youtube_transcript_api  # noqa: F401
        except ImportError:
            pytest.skip("youtube-transcript-api not installed")
        assert server._to_uri("79-bApI3GIU", kind="youtube") == "youtube:79-bApI3GIU"


# ---------------------------------------------------------------------------
# Attribution — legal / source-citation requirements
# ---------------------------------------------------------------------------


class TestYouTubeAttribution:
    """YouTube transcripts belong to video creators (or YouTube's auto-
    generator).  Downstream users must cite the original video; the
    handler surfaces the canonical watch URL so there's no excuse.
    """

    def test_attribution_contains_watch_url(self):
        out = _yt_attribution("79-bApI3GIU")
        assert "youtube.com/watch?v=79-bApI3GIU" in out

    def test_attribution_mentions_source_video(self):
        out = _yt_attribution("79-bApI3GIU")
        assert "79-bApI3GIU" in out
        assert "YouTube" in out.lower() or "youtube" in out

    def test_attribution_warns_about_verification(self):
        out = _yt_attribution("79-bApI3GIU")
        assert "verify" in out.lower() or "Cite" in out

    def test_transcript_fetch_has_attribution(self, monkeypatch):
        api = MagicMock()
        monkeypatch.setattr(
            "precis.handlers.youtube.YouTubeHandler._get_api",
            lambda self: api,
        )
        api.fetch.return_value = [MagicMock(text="Hello")]
        h = YouTubeHandler()
        out = h.read(
            path="79-bApI3GIU",
            selector=None,
            view=None,
            subview=None,
            query="",
            summarize=False,
            depth=0,
            page=1,
        )
        assert "Hello" in out
        assert "youtube.com/watch?v=79-bApI3GIU" in out

    def test_languages_view_has_attribution(self, monkeypatch):
        api = MagicMock()
        monkeypatch.setattr(
            "precis.handlers.youtube.YouTubeHandler._get_api",
            lambda self: api,
        )
        api.list.return_value = [
            MagicMock(language="English", language_code="en", is_generated=False)
        ]
        h = YouTubeHandler()
        out = h.read(
            path="79-bApI3GIU",
            selector=None,
            view="languages",
            subview=None,
            query="",
            summarize=False,
            depth=0,
            page=1,
        )
        assert "English" in out
        assert "youtube.com/watch?v=79-bApI3GIU" in out
