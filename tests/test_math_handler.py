"""Tests for :class:`precis.handlers.math.MathHandler` (Wolfram Alpha).

Originally Phase 4 (external stateless handlers, see CHANGELOG).  Split
out of ``test_phase4_external.py`` so YouTube and Math each have their
own test file.

Math uses a mocked ``wolframalpha`` client; no network calls.

Live smoke test for Wolfram Alpha is at the bottom of this file; it's
gated on ``PRECIS_TEST_WOLFRAM_LIVE=1`` and spends one real API call
per test, so it stays off in CI.
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
# Formatter (pure function)
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

    def test_internal_timeout_returns_clean_message(self):
        """Regression: when Wolfram's solve-engine times out (~20s)
        the API returns ``<queryresult timing="20.002" timedout=""
        numpods="0"/>`` with **no** ``success`` attribute.  The
        upstream ``Document.__getattr__`` then raises
        ``AttributeError("success")`` for ``res.success``, which
        bubbled up as ``ERROR [unexpected]: AttributeError: success``
        in production.  ``_format_result`` must detect the missing
        ``success`` attribute and return a readable timeout message.
        """
        import xmltodict
        from wolframalpha import Document

        # Real shape from a timed-out query, parsed via the same
        # postprocessor we use in ``_run_query``.

        xml = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<queryresult timing="20.002" timedout="" '
            b'timedoutpods="" numpods="0"></queryresult>'
        )
        doc = xmltodict.parse(xml, postprocessor=Document.make)
        res = doc["queryresult"]

        out = _format_result(res, "distance of the planets from the sun")
        # The point is *not* to crash with AttributeError.
        assert "timed out" in out.lower() or "no success" in out.lower()
        assert "wolframalpha.com" in out  # attribution still present

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


# ---------------------------------------------------------------------------
# Handler dispatch
# ---------------------------------------------------------------------------


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

        Also pins the request shape: the ``totaltimeout`` knob must
        be sent (else Wolfram defaults to 20s and broad queries fail
        — see ``test_run_query_sets_totaltimeout``).
        """
        from precis.handlers import math as math_handler

        captured: dict[str, Any] = {}

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
                captured["client_kwargs"] = kw

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, params=None):
                captured["url"] = url
                captured["params"] = dict(params)
                return _FakeResponse()

        import httpx

        monkeypatch.setattr(httpx, "Client", _FakeHttpClient)

        client = MagicMock()
        client.app_id = "STUB-APPID"
        client.url = "https://api.wolframalpha.com/v2/query"

        res = math_handler._run_query(client, "2+2")
        assert res.success is True
        assert captured["url"] == "https://api.wolframalpha.com/v2/query"
        assert captured["params"]["appid"] == "STUB-APPID"
        assert captured["params"]["input"] == "2+2"

    def test_run_query_sets_totaltimeout(self, monkeypatch):
        """Regression: Wolfram's per-query ``totaltimeout`` defaults
        to 20s; broader queries time out internally and return an
        empty ``<queryresult timedout=""/>`` (which surfaces as
        ``"Wolfram Alpha timed out internally"`` to the agent).  We
        push the knob to ~55s and keep the httpx budget slightly
        above so server-side timeouts still parse cleanly rather
        than tripping a transport-level ``ReadTimeout``.
        """
        from precis.handlers import math as math_handler

        captured: dict[str, Any] = {}

        sample_xml = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<queryresult success="true" numpods="1">'
            b'<pod title="Result"><subpod><plaintext>x</plaintext>'
            b"</subpod></pod></queryresult>"
        )

        class _FakeResponse:
            status_code = 200
            content = sample_xml
            text = sample_xml.decode()
            headers = {"Content-Type": "text/xml; charset=utf-8"}

        class _FakeHttpClient:
            def __init__(self, *a, **kw):
                captured["timeout"] = kw.get("timeout")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, params=None):
                captured["params"] = dict(params)
                return _FakeResponse()

        import httpx

        monkeypatch.setattr(httpx, "Client", _FakeHttpClient)

        client = MagicMock()
        client.app_id = "STUB-APPID"
        client.url = "https://api.wolframalpha.com/v2/query"

        math_handler._run_query(client, "anything")

        # totaltimeout must be sent and must exceed Wolfram's 20s default.
        assert "totaltimeout" in captured["params"]
        assert int(captured["params"]["totaltimeout"]) > 20
        # httpx read budget must be >= the totaltimeout we asked for, or
        # a server-side timeout would surface as a transport error
        # instead of a parsed empty ``<queryresult>`` (which our
        # formatter handles cleanly).
        assert captured["timeout"] is not None
        assert captured["timeout"] >= int(captured["params"]["totaltimeout"])


# ---------------------------------------------------------------------------
# Registry integration — math kind shows up in visible_kinds
# ---------------------------------------------------------------------------


class TestMathRegistration:
    @classmethod
    def setup_class(cls):
        # Force plugin discovery so KINDS / SCHEMES are populated.
        import precis.registry as reg

        reg._discover()

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


# ---------------------------------------------------------------------------
# Server dispatch — type='math' routes through _dispatch
# ---------------------------------------------------------------------------


class TestMathServerDispatch:
    def setup_method(self):
        clear_session_stats()
        clear_kinds_mask()
        clear_startup_warnings()

    def teardown_method(self):
        clear_session_stats()
        clear_kinds_mask()
        clear_startup_warnings()

    def test_type_math_builds_correct_uri(self):
        try:
            import wolframalpha  # noqa: F401
        except ImportError:
            pytest.skip("wolframalpha not installed")
        # Math has no bare-id auto-detection; you need type=.
        assert server._to_uri("2+2", kind="math") == "math:2+2"


# ---------------------------------------------------------------------------
# Attribution — legal / ToS requirements
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

    def test_attribution_prefills_iso_date(self, monkeypatch):
        """Regression: previously the citation template emitted the
        literal placeholder ``(accessed [date])`` which forced human
        copy-editors to fill it in.  We now prefill with today's UTC
        ISO-8601 date so the citation is paste-ready.  Monkey-patch
        the ``_today_iso`` seam to keep the test deterministic.
        """
        from precis.handlers import math as math_handler

        monkeypatch.setattr(
            math_handler, "_today_iso", lambda: "2026-04-25"
        )
        out = _math_attribution("2+2")
        assert "(accessed 2026-04-25)" in out
        assert "[date]" not in out  # placeholder must not leak

    def test_today_iso_returns_yyyy_mm_dd(self):
        """``_today_iso`` is the test seam for the access date — it
        must produce ``YYYY-MM-DD`` (10 chars, two dashes) so the
        citation field stays well-formed.
        """
        from precis.handlers.math import _today_iso

        out = _today_iso()
        assert len(out) == 10
        assert out[4] == "-"
        assert out[7] == "-"
        # Year sanity-bounds: pre-2025 means stale, post-2100 means
        # the wall clock has skipped — both warrant a noisy failure.
        year = int(out[:4])
        assert 2025 <= year <= 2099

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


# ---------------------------------------------------------------------------
# Live Wolfram Alpha smoke test (opt-in).
#
# Run with:
#     PRECIS_TEST_WOLFRAM_LIVE=1 WOLFRAM_APP_ID=<your-app-id> \
#         uv run pytest tests/test_math_handler.py::TestWolframLive -v
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
