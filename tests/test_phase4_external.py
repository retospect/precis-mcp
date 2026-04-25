"""Phase 4 — external stateless handlers (Math, YouTube).

Math uses a mocked wolframalpha client; YouTube uses a mocked
youtube-transcript-api.  Both paths exercise the handler → registry →
server dispatch chain.  No network calls.

Live smoke test for Wolfram Alpha is at the bottom of this file; it's
gated on ``PRECIS_TEST_WOLFRAM_LIVE=1`` and spends one real API call,
so it stays off in CI.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock

import pytest

from precis import server
from precis.handlers.math import (
    MathHandler,
    _format_result,
)
from precis.handlers.math import (
    _attribution as _math_attribution,
)
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
# YouTube — id extraction helper (ported from tubescribe-mcp)
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
# YouTube — handler dispatch with a mocked API
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
# Math — formatter (pure function)
# ---------------------------------------------------------------------------


class TestFormatResult:
    def _pod(self, title: str, plaintext: str) -> dict:
        return {
            "@title": title,
            "subpod": [{"plaintext": plaintext}],
        }

    def test_successful_query(self):
        res = MagicMock()
        res.success = True
        res.pods = [self._pod("Input", "2 + 2"), self._pod("Result", "4")]
        out = _format_result(res, "2+2")
        assert "## Input" in out
        assert "## Result" in out
        assert "4" in out

    def test_failed_query_no_tips(self):
        res = MagicMock()
        res.success = False
        res.didyoumeans = None
        out = _format_result(res, "asdfasdf")
        assert "failed" in out.lower()
        assert "'asdfasdf'" in out

    def test_failed_query_with_did_you_mean(self):
        res = MagicMock()
        res.success = False
        res.didyoumeans = [{"#text": "2+2"}, {"#text": "two plus two"}]
        out = _format_result(res, "to plus to")
        assert "Did you mean" in out
        assert "2+2" in out

    def test_successful_but_no_text(self):
        res = MagicMock()
        res.success = True
        res.pods = []
        out = _format_result(res, "x")
        assert "no displayable text" in out

    def test_pod_with_string_subpod_ignored(self):
        # Defensive: sometimes the wolframalpha lib returns a string
        # where we expect a dict; formatter must not crash.
        res = MagicMock()
        res.success = True
        res.pods = [
            {
                "@title": "Junk",
                "subpod": ["string-subpod", {"plaintext": "real"}],
            }
        ]
        out = _format_result(res, "q")
        assert "## Junk" in out
        assert "real" in out

    def test_single_subpod_as_dict_extracted(self):
        """Regression: ``xmltodict`` collapses a one-element ``<subpod>``
        list into a dict (not a one-element list).  The Wolfram API
        returns this shape for the common ``2+2`` style answer where a
        pod has exactly one subpod, so the formatter must coerce dict
        → [dict] rather than iterating dict keys (which yielded
        strings, all silently skipped, producing the empty
        "no displayable text" output observed in production).
        """
        res = MagicMock()
        res.success = True
        res.pods = [
            {
                "@title": "Result",
                "subpod": {"plaintext": "4"},  # dict, not list
            }
        ]
        out = _format_result(res, "2+2")
        assert "## Result" in out
        assert "4" in out
        assert "no displayable text" not in out


class TestMathHandler:
    def test_missing_env_raises_kind_unavailable(self, monkeypatch):
        # Force the wolframalpha import to succeed, but unset the env var.
        monkeypatch.delenv("WOLFRAM_APP_ID", raising=False)
        h = MathHandler()
        with pytest.raises(PrecisError) as exc:
            h.read(
                path="2+2",
                selector=None,
                view=None,
                subview=None,
                query="",
                summarize=False,
                depth=0,
                page=1,
            )
        # Either missing package OR missing env var → KIND_UNAVAILABLE.
        assert exc.value.code == ErrorCode.KIND_UNAVAILABLE

    def test_empty_query_raises_param_invalid(self, monkeypatch):
        # Stub the client so we don't trip the env-missing path first.
        monkeypatch.setenv("WOLFRAM_APP_ID", "stub")
        h = MathHandler()
        h._client = MagicMock()  # bypass _get_client
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

    def test_read_forwards_to_run_query(self, monkeypatch):
        monkeypatch.setenv("WOLFRAM_APP_ID", "stub")
        h = MathHandler()
        h._client = MagicMock()

        fake_res = MagicMock()
        fake_res.success = True
        fake_res.pods = [
            {"@title": "Result", "subpod": [{"plaintext": "4"}]},
        ]
        captured: dict[str, Any] = {}

        def _fake_run_query(client, expression):
            captured["client"] = client
            captured["expression"] = expression
            return fake_res

        monkeypatch.setattr(
            "precis.handlers.math._run_query", _fake_run_query
        )

        out = h.read(
            path="2+2",
            selector=None,
            view=None,
            subview=None,
            query="",
            summarize=False,
            depth=0,
            page=1,
        )
        assert captured["expression"] == "2+2"
        assert captured["client"] is h._client
        assert "4" in out

    def test_upstream_exception_raised_as_upstream_error(self, monkeypatch):
        monkeypatch.setenv("WOLFRAM_APP_ID", "stub")
        h = MathHandler()
        h._client = MagicMock()

        def _boom(client, expression):
            raise RuntimeError("network down")

        monkeypatch.setattr(
            "precis.handlers.math._run_query", _boom
        )

        with pytest.raises(PrecisError) as exc:
            h.read(
                path="2+2",
                selector=None,
                view=None,
                subview=None,
                query="",
                summarize=False,
                depth=0,
                page=1,
            )
        assert exc.value.code == ErrorCode.UPSTREAM_ERROR

    def test_read_works_inside_running_event_loop(self, monkeypatch):
        """Regression: ``wolframalpha.Client.query`` calls
        :func:`asyncio.run` internally, which raises when invoked
        inside an active loop (the MCP server's runtime).  We bypass
        the broken upstream method entirely; this test asserts that
        ``read`` succeeds while a loop is already running.
        """
        import asyncio

        monkeypatch.setenv("WOLFRAM_APP_ID", "stub")

        fake_res = MagicMock()
        fake_res.success = True
        fake_res.pods = [{"@title": "Result", "subpod": [{"plaintext": "4"}]}]
        monkeypatch.setattr(
            "precis.handlers.math._run_query",
            lambda client, expr: fake_res,
        )

        h = MathHandler()
        h._client = MagicMock()

        async def _drive():
            return h.read(
                path="2+2",
                selector=None,
                view=None,
                subview=None,
                query="",
                summarize=False,
                depth=0,
                page=1,
            )

        out = asyncio.run(_drive())
        assert "4" in out
        assert "wolframalpha.com" in out

    def test_run_query_handles_real_world_content_type(self, monkeypatch):
        """Regression: upstream ``aquery`` asserts the response
        Content-Type matches ``'text/xml;charset=utf-8'`` (no space),
        but the real Wolfram API returns ``'text/xml; charset=utf-8'``
        (with a space).  Our ``_run_query`` must not impose that
        assertion — it just parses the body.
        """
        from precis.handlers import math as math_handler

        captured_url: dict[str, str] = {}

        sample_xml = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<queryresult success="true" error="false" numpods="1">'
            b'<pod title="Result"><subpod><plaintext>4</plaintext>'
            b"</subpod></pod></queryresult>"
        )

        class _FakeResponse:
            status_code = 200
            content = sample_xml
            text = sample_xml.decode()
            headers = {"Content-Type": "text/xml; charset=utf-8"}

        class _FakeHttpClient:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, params=None):
                captured_url["url"] = url
                captured_url["appid"] = dict(params).get("appid")
                return _FakeResponse()

        import httpx

        monkeypatch.setattr(httpx, "Client", _FakeHttpClient)

        client = MagicMock()
        client.app_id = "STUB-APPID"
        client.url = "https://api.wolframalpha.com/v2/query"

        res = math_handler._run_query(client, "2+2")
        assert res.success is True
        assert captured_url["url"] == "https://api.wolframalpha.com/v2/query"
        assert captured_url["appid"] == "STUB-APPID"


# ---------------------------------------------------------------------------
# Registry integration — Math and YouTube show up in visible_kinds
# ---------------------------------------------------------------------------


class TestPhase4Registration:
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

    def test_math_kind_registered_when_package_available(self):
        try:
            import wolframalpha  # noqa: F401
        except ImportError:
            pytest.skip("wolframalpha not installed")
        assert "math" in KINDS
        assert "math" in SCHEMES

    def test_math_hidden_without_wolfram_env(self, monkeypatch):
        try:
            import wolframalpha  # noqa: F401
        except ImportError:
            pytest.skip("wolframalpha not installed")
        import precis.registry as reg

        monkeypatch.delenv("WOLFRAM_APP_ID", raising=False)
        reg._ENV_WARNED.discard("math")  # allow warning to re-fire
        names = {k.spec.name for k in visible_kinds("get")}
        assert "math" not in names

    def test_math_visible_with_wolfram_env(self, monkeypatch):
        try:
            import wolframalpha  # noqa: F401
        except ImportError:
            pytest.skip("wolframalpha not installed")
        monkeypatch.setenv("WOLFRAM_APP_ID", "stub-for-visibility")
        names = {k.spec.name for k in visible_kinds("get")}
        assert "math" in names

    def test_youtube_always_visible(self, monkeypatch):
        """No env requirement → kind is always in the enum."""
        try:
            import youtube_transcript_api  # noqa: F401
        except ImportError:
            pytest.skip("youtube-transcript-api not installed")
        names = {k.spec.name for k in visible_kinds("get")}
        assert "youtube" in names


# ---------------------------------------------------------------------------
# Server dispatch — type='youtube' / type='math' route through _dispatch
# ---------------------------------------------------------------------------


class TestPhase4ServerDispatch:
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

    def test_type_math_builds_correct_uri(self):
        try:
            import wolframalpha  # noqa: F401
        except ImportError:
            pytest.skip("wolframalpha not installed")
        # Math has no bare-id auto-detection; you need type=.
        assert server._to_uri("2+2", kind="math") == "math:2+2"


# ---------------------------------------------------------------------------
# Attribution — legal / ToS requirements for external-data handlers
# ---------------------------------------------------------------------------


class TestWolframAttribution:
    """Wolfram Alpha's ToU (https://www.wolframalpha.com/termsofuse) makes
    attribution mandatory.  These tests enforce that every code path
    emits the required footer with a deep-link URL + copyright marker.
    """

    def test_attribution_contains_wolfram_link(self):
        out = _math_attribution("2+2")
        assert "wolframalpha.com" in out
        assert "Computed by" in out
        assert "Wolfram|Alpha" in out

    def test_attribution_deep_links_to_query(self):
        out = _math_attribution("2+2")
        # `+` becomes `%2B` after URL-encoding.
        assert "i=2%2B2" in out

    def test_attribution_urlencodes_spaces(self):
        out = _math_attribution("integrate sin(x)")
        assert "integrate+sin" in out  # quote_plus encodes space as '+'
        # Parens must survive for readability of the URL.
        assert "%28x%29" in out

    def test_attribution_mentions_copyright(self):
        out = _math_attribution("x")
        assert "Wolfram Alpha LLC" in out

    def test_attribution_includes_academic_citation_format(self):
        # Wolfram's recommended form for paper citations per
        # https://support.wolfram.com/23498 — include query + access date.
        out = _math_attribution("2+2")
        assert "WolframAlpha" in out
        assert "accessed" in out.lower()

    def test_successful_result_has_attribution(self):
        res = MagicMock()
        res.success = True
        res.pods = [{"@title": "Result", "subpod": [{"plaintext": "4"}]}]
        out = _format_result(res, "2+2")
        assert "Wolfram|Alpha" in out
        assert "wolframalpha.com" in out

    def test_failed_result_has_attribution(self):
        # Even on failure the query was sent to Wolfram; the footer
        # still carries the deep link so the user can verify.
        res = MagicMock()
        res.success = False
        res.didyoumeans = None
        out = _format_result(res, "asdfasdf")
        assert "wolframalpha.com" in out

    def test_empty_result_has_attribution(self):
        res = MagicMock()
        res.success = True
        res.pods = []
        out = _format_result(res, "x")
        assert "wolframalpha.com" in out

    def test_did_you_mean_has_attribution(self):
        res = MagicMock()
        res.success = False
        res.didyoumeans = [{"#text": "2+2"}]
        out = _format_result(res, "to plus to")
        assert "Wolfram|Alpha" in out
        assert "Did you mean" in out


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


# ---------------------------------------------------------------------------
# Live Wolfram Alpha smoke test (opt-in).
#
# Run with:
#     PRECIS_TEST_WOLFRAM_LIVE=1 WOLFRAM_APP_ID=<your-app-id> \
#         uv run pytest tests/test_phase4_external.py::TestWolframLive -v
#
# Skipped by default in CI so we don't burn Wolfram's free-tier quota
# (2000/month) on every merge.  The suite is worth running locally
# after:
#   - bumping ``wolframalpha`` pin in pyproject.toml
#   - changing anything in ``precis/handlers/math.py`` that touches
#     the response-shape parsing (``_format_result``)
#   - renewing the Wolfram App ID (smoke-test the new credential)
# ---------------------------------------------------------------------------


_LIVE_WOLFRAM = os.environ.get("PRECIS_TEST_WOLFRAM_LIVE") == "1"
_WOLFRAM_KEY_SET = bool(os.environ.get("WOLFRAM_APP_ID", "").strip())


@pytest.mark.skipif(
    not _LIVE_WOLFRAM,
    reason="PRECIS_TEST_WOLFRAM_LIVE=1 not set — live API test opt-in",
)
@pytest.mark.skipif(
    not _WOLFRAM_KEY_SET,
    reason="WOLFRAM_APP_ID not set — live API test needs a real key",
)
class TestWolframLive:
    """Live smoke tests against the real Wolfram Alpha API.

    Each test costs one API call.  Keep the test count low and the
    queries cheap / uncacheable-by-us (Wolfram caches on their side so
    re-running is usually free to them).
    """

    def setup_method(self):
        try:
            import wolframalpha  # noqa: F401
        except ImportError:
            pytest.skip("wolframalpha package not installed")
        self.h = MathHandler()

    def test_live_arithmetic_returns_four(self):
        """``2+2`` must return a result containing ``4``.

        This is the canary test.  If the ``wolframalpha`` client or
        the Wolfram API response shape changes, ``_format_result`` will
        either fail or return an empty body — we want to see both.
        """
        out = self.h.read(
            path="2+2",
            selector=None,
            view=None,
            subview=None,
            query="",
            summarize=False,
            depth=0,
            page=1,
        )
        assert "4" in out, f"expected '4' in live Wolfram result:\n{out}"

    def test_live_response_carries_attribution_footer(self):
        """Every live response must include the mandatory Wolfram
        footer so downstream agents have the deep-link URL + copyright
        marker required by Wolfram's ToU.
        """
        out = self.h.read(
            path="speed of light",
            selector=None,
            view=None,
            subview=None,
            query="",
            summarize=False,
            depth=0,
            page=1,
        )
        assert "wolframalpha.com" in out, (
            f"live response missing attribution link:\n{out}"
        )
        assert "Wolfram|Alpha" in out or "Wolfram Alpha" in out

    def test_live_nonsense_query_does_not_crash(self):
        """An unparseable query should return a clean empty / did-you-
        mean path, never a stack trace or unhandled exception.

        Both paths in ``_format_result`` are exercised: failed-result
        (``res.success == False``) and empty-pods (``res.pods == []``).
        Either one is acceptable here; the goal is "doesn't crash".
        """
        out = self.h.read(
            path="asdfasdfqwertyzxcvbnm",
            selector=None,
            view=None,
            subview=None,
            query="",
            summarize=False,
            depth=0,
            page=1,
        )
        # Footer always present regardless of success/failure branch.
        assert "wolframalpha.com" in out
