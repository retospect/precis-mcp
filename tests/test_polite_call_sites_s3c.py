"""S3 batch 3: Google Patents, YouTube, news feeds, Materials Project and
Catalysis-Hub ride the shared politeness core (gr493222).

Mocks only: an ``httpx.MockTransport`` origin, a recorder over
``rate_limit.require`` / ``park``, backoff sleeps on an injected clock.
No network, no real sleeping, no DB.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import requests
import youtube_transcript_api._errors as yt_errors

from precis.errors import Upstream
from precis.handlers import youtube
from precis.structure.importers import catalysis_hub
from precis.utils import http
from precis.utils.http import (
    VendorUnavailable,
    check_user_agent,
    netloc_provider,
    reset_circuit,
)
from precis.utils.rate_limit import RateLimited
from precis.workers import fetch_google_patents as gp
from precis.workers import news_poll
from precis.workers.job_types import pourbaix_bulk as pb_job

_GP_HTML = (
    '<section itemprop="abstract">Abstract A thing.</section>'
    '<section itemprop="claims"><claim><claim-text>1. A thing.</claim-text>'
    "</claim></section>"
)
_RSS = (
    '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
    "<item><title>a</title><link>https://example.org/a</link></item>"
    "</channel></rss>"
)


@dataclass
class Clock:
    sleeps: list[float] = field(default_factory=list)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


@dataclass
class Limiter:
    requires: list[str] = field(default_factory=list)
    parks: list[tuple[str, float]] = field(default_factory=list)


class Server:
    def __init__(self) -> None:
        self.script: list[tuple[int, dict[str, str], Any]] = []
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, headers, body = self.script.pop(0)
        if isinstance(body, Exception):
            raise body
        if isinstance(body, str):
            return httpx.Response(
                status, headers=headers, content=body.encode(), request=request
            )
        return httpx.Response(status, headers=headers, json=body, request=request)

    def ua(self, i: int = 0) -> str:
        ua = self.requests[i].headers["user-agent"]
        assert check_user_agent(ua) == ua
        return ua


@pytest.fixture(autouse=True)
def _isolated() -> Iterator[None]:
    reset_circuit()
    yield
    reset_circuit()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(http, "_default_sleep", c.sleep)
    return c


@pytest.fixture
def limiter(monkeypatch: pytest.MonkeyPatch) -> Limiter:
    rec = Limiter()
    monkeypatch.setattr(
        "precis.utils.rate_limit.require",
        lambda provider, **_kw: rec.requires.append(provider),
    )
    monkeypatch.setattr(
        "precis.utils.rate_limit.park",
        lambda provider, seconds: rec.parks.append((provider, seconds)),
    )
    return rec


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Server:
    srv = Server()
    real = httpx.Client

    def factory(**kw: Any) -> httpx.Client:
        kw["transport"] = httpx.MockTransport(srv)
        return real(**kw)

    def direct_get(client: httpx.Client, url: str, **kw: Any) -> httpx.Response:
        return client.get(url, **kw)

    monkeypatch.setattr(httpx, "Client", factory)
    monkeypatch.setattr("precis.utils.safe_fetch.safe_get", direct_get)
    return srv


def _status_error(status: int, retry_after: str | None = None) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://example.org/x")
    headers = {"Retry-After": retry_after} if retry_after else {}
    resp = httpx.Response(status, headers=headers, request=req)
    return httpx.HTTPStatusError("boom", request=req, response=resp)


# ── Google Patents ─────────────────────────────────────────────────────────


def test_gp_429_retry_after_is_the_sleep_and_token_per_attempt(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(429, {"Retry-After": "7"}, ""), (200, {}, _GP_HTML)]
    status, html, _n = gp._fetch_one("US1234567B2")
    assert status == "ok" and html == _GP_HTML
    assert len(server.requests) == 2
    assert clock.sleeps == [7.0]
    assert limiter.requires == ["google-patents"] * 2
    assert limiter.parks == [("google-patents", 7.0)]
    server.ua()


def test_gp_404_not_retried_is_not_found(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(404, {}, "")]
    assert gp._fetch_one("US1234567B2")[0] == "not-found"
    assert len(server.requests) == 1 and clock.sleeps == []


def test_gp_exhausted_429_is_http_error_never_not_found(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(429, {}, "")] * 5
    status, detail, _n = gp._fetch_one("US1234567B2")
    assert status == "http-error" and detail is not None
    assert len(server.requests) == 5


def test_gp_transport_error_is_retried_then_ok(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(0, {}, httpx.ConnectTimeout("slow")), (200, {}, _GP_HTML)]
    assert gp._fetch_one("US1234567B2")[0] == "ok"
    assert len(clock.sleeps) == 1


def _refuse(provider: str, **_kw: Any) -> None:
    raise RateLimited(provider)


def test_gp_limiter_refusal_is_deferred_not_http_error(
    server: Server, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("precis.utils.rate_limit.require", _refuse)
    status, detail, _n = gp._fetch_one("US1234567B2")
    assert status == "deferred" and detail is not None
    assert server.requests == []


def test_gp_limiter_refusing_the_retry_is_deferred(
    server: Server, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken: list[str] = []

    def second_refused(provider: str, **_kw: Any) -> None:
        taken.append(provider)
        if len(taken) > 1:
            raise RateLimited(provider)

    monkeypatch.setattr("precis.utils.rate_limit.require", second_refused)
    server.script = [(503, {}, "")]
    assert gp._fetch_one("US1234567B2")[0] == "deferred"
    assert len(server.requests) == 1


def test_gp_open_breaker_is_deferred_but_the_tripping_call_is_http_error(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    # 5 failures, then 3 more trip the breaker (threshold 8) mid-call: that
    # call really failed upstream.
    server.script = [(503, {}, "")] * 5
    assert gp._fetch_one("US1234567B2")[0] == "http-error"
    server.script = [(503, {}, "")] * 3
    assert gp._fetch_one("US1234567B2")[0] == "http-error"
    n = len(server.requests)
    # Now the breaker is open: our own cooldown, no request goes out.
    assert gp._fetch_one("US1234567B2")[0] == "deferred"
    assert len(server.requests) == n


def test_gp_real_5xx_exhausted_stays_http_error(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(503, {}, "")] * 5
    status, detail, _n = gp._fetch_one("US1234567B2")
    assert status == "http-error" and "503" in (detail or "")


@pytest.mark.parametrize("code", [401, 403])
def test_gp_fatal_auth_status_is_http_error_not_retried(
    server: Server, limiter: Limiter, clock: Clock, code: int
) -> None:
    server.script = [(code, {}, "")]
    assert gp._fetch_one("US1234567B2")[0] == "http-error"
    assert len(server.requests) == 1 and clock.sleeps == []


# ── News feeds ─────────────────────────────────────────────────────────────

_FEED = "https://example.org/feed.xml"


def test_news_feed_429_retry_after_and_polite_ua(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(429, {"Retry-After": "4"}, ""), (200, {}, _RSS)]
    parsed = news_poll._default_parse_feed(_FEED)
    assert len(parsed.entries) == 1
    assert clock.sleeps == [4.0]
    assert limiter.requires == [netloc_provider(_FEED)] * 2
    assert "Mozilla" not in server.ua()


def test_news_feed_304_is_returned_not_retried(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(304, {}, "")]
    parsed = news_poll._default_parse_feed(_FEED, etag="x")
    assert parsed.status == 304 and parsed.entries == []
    assert len(server.requests) == 1


def test_news_feed_404_not_retried_raises_http_status(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(404, {}, "")]
    with pytest.raises(httpx.HTTPStatusError):
        news_poll._default_parse_feed(_FEED)
    assert len(server.requests) == 1 and clock.sleeps == []


def test_news_feed_exhausted_429_raises_not_empty(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(429, {}, "")] * 5
    with pytest.raises(VendorUnavailable):
        news_poll._default_parse_feed(_FEED)


def test_news_dead_host_breaker_does_not_block_another_feed(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    dead = "https://dead.example.net/feed.xml"
    for _ in range(2):
        server.script = [(503, {}, "")] * 5
        with pytest.raises(VendorUnavailable):
            news_poll._default_parse_feed(dead)
    n = len(server.requests)
    with pytest.raises(VendorUnavailable, match="circuit open"):
        news_poll._default_parse_feed(dead)
    assert len(server.requests) == n  # breaker open, nothing sent
    server.script = [(200, {}, _RSS)]
    assert len(news_poll._default_parse_feed(_FEED).entries) == 1


# ── YouTube ────────────────────────────────────────────────────────────────

_WATCH_HTML = '<meta property="og:title" content="A Title">'
_YT_ROW = "host:www.youtube.com"


def test_youtube_watch_scrape_429_retry_after_token_per_attempt_ua(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(429, {"Retry-After": "5"}, ""), (200, {}, _WATCH_HTML)]
    meta = youtube._scrape_watch_page_meta("abcdefghijk")
    assert meta["title"] == "A Title"
    assert clock.sleeps == [5.0]
    assert limiter.requires == [_YT_ROW] * 2
    server.ua()


def test_youtube_watch_scrape_404_not_retried_and_empty(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(404, {}, "")]
    assert youtube._scrape_watch_page_meta("abcdefghijk") == {}
    assert len(server.requests) == 1 and clock.sleeps == []


def test_youtube_watch_scrape_exhausted_429_degrades_to_empty(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(429, {}, "")] * 2
    assert youtube._scrape_watch_page_meta("abcdefghijk") == {}


class _Snippet:
    text = "hello"


def _fake_transcript_modules(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[Any]
) -> list[int]:
    calls: list[int] = []

    class Api:
        def fetch(self, video_id: str, languages: list[str]) -> list[_Snippet]:
            calls.append(1)
            out = outcomes.pop(0)
            if isinstance(out, Exception):
                raise out
            return [_Snippet()]

    yt = types.SimpleNamespace(YouTubeTranscriptApi=Api)
    errs = yt_errors
    mods = {"youtube_transcript_api": yt, "youtube_transcript_api._errors": errs}
    monkeypatch.setattr(youtube, "require_optional", lambda name: mods[name])
    monkeypatch.setattr(youtube, "_scrape_watch_page_meta", lambda _v: {})
    return calls


def _yt_fetch() -> Any:
    handler = youtube.YouTubeHandler.__new__(youtube.YouTubeHandler)
    return handler._fetch("vid:en")


def test_youtube_transcript_transport_error_retried_token_per_attempt(
    monkeypatch: pytest.MonkeyPatch, limiter: Limiter, clock: Clock
) -> None:
    # The library surfaces connection failures from ``requests`` unwrapped;
    # it wraps HTTP statuses (429 included) in YouTubeRequestFailed with only
    # a string reason, so a status-driven retry cannot happen on this path.
    calls = _fake_transcript_modules(
        monkeypatch, [requests.exceptions.ConnectionError("reset"), "ok"]
    )
    res = _yt_fetch()
    assert res.body_blocks[0].text == "hello"
    assert len(calls) == 2
    assert len(clock.sleeps) == 1
    assert limiter.requires == [_YT_ROW] * 2


def test_youtube_transcript_request_failed_not_retried_stays_upstream(
    monkeypatch: pytest.MonkeyPatch, limiter: Limiter, clock: Clock
) -> None:
    failed = yt_errors.YouTubeRequestFailed("vid", requests.exceptions.HTTPError("429"))
    calls = _fake_transcript_modules(monkeypatch, [failed])
    with pytest.raises(Upstream):
        _yt_fetch()
    assert len(calls) == 1 and clock.sleeps == []


def test_youtube_transcript_exhausted_transport_errors_are_upstream_bounded(
    monkeypatch: pytest.MonkeyPatch, limiter: Limiter, clock: Clock
) -> None:
    calls = _fake_transcript_modules(
        monkeypatch, [requests.exceptions.ConnectionError("reset")] * 5
    )
    with pytest.raises(Upstream):
        _yt_fetch()
    assert len(calls) == 2  # interactive policy: attempts=2


def test_youtube_exhausted_scrapes_do_not_open_the_transcript_breaker(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    for _ in range(10):
        server.script = [(503, {}, "")] * 2
        assert youtube._scrape_watch_page_meta("abcdefghijk") == {}
    assert http._CIRCUITS.get("youtube") is None or (
        http._CIRCUITS["youtube"].opened_at is None
    )
    assert http._CIRCUITS["youtube-scrape"].opened_at is not None


# ── Catalysis-Hub ──────────────────────────────────────────────────────────

_CH_OK: dict[str, Any] = {"data": {"reactions": {"edges": []}}}


def test_cathub_429_retry_after_token_per_attempt_and_polite_ua(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(429, {"Retry-After": "9"}, {}), (200, {}, _CH_OK)]
    assert catalysis_hub.fetch_config(surface_composition="Pd", facet="111") == []
    assert len(server.requests) == 2
    assert clock.sleeps == [9.0]
    assert limiter.requires == [netloc_provider(catalysis_hub.GRAPHQL_URL)] * 2
    server.ua()


def test_cathub_404_not_retried_raises_http_status(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(404, {}, {})]
    with pytest.raises(httpx.HTTPStatusError):
        catalysis_hub.fetch_config()
    assert len(server.requests) == 1 and clock.sleeps == []


def test_cathub_sends_x_api_key_only_when_the_secret_is_set(
    server: Server,
    limiter: Limiter,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("precis.secrets.get_secret", lambda *a, **k: None)
    server.script = [(200, {}, _CH_OK)]
    catalysis_hub.fetch_config()
    assert "x-api-key" not in server.requests[0].headers

    monkeypatch.setattr("precis.secrets.get_secret", lambda *a, **k: "ch-secret")
    server.script = [(200, {}, _CH_OK)]
    catalysis_hub.fetch_config()
    assert server.requests[1].headers["x-api-key"] == "ch-secret"


@pytest.mark.parametrize("keyed", [False, True])
def test_cathub_401_is_a_clean_auth_refusal_not_http_status(
    server: Server,
    limiter: Limiter,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    keyed: bool,
) -> None:
    monkeypatch.setattr(
        "precis.secrets.get_secret", lambda *a, **k: "ch-secret" if keyed else None
    )
    server.script = [(401, {}, {})]
    with pytest.raises(catalysis_hub.CatalysisHubAuthRequired) as ei:
        catalysis_hub.fetch_config()
    assert ei.value.keyed is keyed
    assert "ch-secret" not in str(ei.value)
    assert len(server.requests) == 1 and clock.sleeps == []


def test_cathub_exhausted_429_raises_not_empty_result(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(429, {}, {})] * 2
    with pytest.raises(VendorUnavailable):
        catalysis_hub.fetch_config()
    assert len(server.requests) == 2  # interactive policy: attempts=2


# ── Materials Project ──────────────────────────────────────────────────────


def _fake_engine(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[Any], seen: list[Any]
) -> None:
    def fetch_entries(
        api_key: str,
        chemsys: str,
        *,
        user_agent: str | None = None,
        pace: Any = None,
        get: Any = None,
    ) -> Any:
        seen.append(user_agent)
        out = outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    def database_version(api_key: str) -> str:
        seen.append(None)
        out = outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return str(out)

    engine = types.SimpleNamespace(
        fetch_entries=fetch_entries, database_version=database_version
    )
    monkeypatch.setitem(sys.modules, "precis_dft.pourbaix_bulk", engine)


def test_mp_fetch_429_retry_after_token_per_attempt_and_polite_ua(
    monkeypatch: pytest.MonkeyPatch, limiter: Limiter, clock: Clock
) -> None:
    seen: list[Any] = []
    _fake_engine(monkeypatch, [_status_error(429, "8"), ["entry"]], seen)
    assert pb_job._mp_fetch("key", "Cu") == ["entry"]
    assert clock.sleeps == [8.0]
    assert limiter.requires == ["materialsproject"] * 2
    assert len(seen) == 2 and check_user_agent(seen[0]) == seen[0]


def test_mp_version_404_not_retried(
    monkeypatch: pytest.MonkeyPatch, limiter: Limiter, clock: Clock
) -> None:
    seen: list[Any] = []
    _fake_engine(monkeypatch, [_status_error(404)], seen)
    with pytest.raises(httpx.HTTPStatusError):
        pb_job._mp_version("key")
    assert len(seen) == 1 and clock.sleeps == []


def test_mp_version_exhausted_429_reraises_original(
    monkeypatch: pytest.MonkeyPatch, limiter: Limiter, clock: Clock
) -> None:
    seen: list[Any] = []
    _fake_engine(monkeypatch, [_status_error(429)] * 5, seen)
    with pytest.raises(httpx.HTTPStatusError):
        pb_job._mp_version("key")
    assert len(seen) == 5


def test_mp_ion_reference_rest_call_sends_the_polite_ua(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis_dft import pourbaix_bulk as engine

    got: list[dict[str, str]] = []

    def fake_get(url: str, **kw: Any) -> httpx.Response:
        got.append(kw["headers"])
        return httpx.Response(
            200,
            json={"data": [], "total_pages": 1},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    engine.fetch_ion_reference_data("k", user_agent=http.polite_user_agent())
    assert got[0]["x-api-key"] == "k"
    assert check_user_agent(got[0]["User-Agent"]) == got[0]["User-Agent"]
    got.clear()
    engine.fetch_ion_reference_data("k")
    assert got[0] == {"x-api-key": "k"}
