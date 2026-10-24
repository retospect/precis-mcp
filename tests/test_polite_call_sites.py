"""The BAD outbound call sites ride the shared politeness core (gr493222 S2).

Semantic Scholar ingest (``precis.ingest.semantic_scholar``), the S2 handler's
``complete=True`` walk, and ``fetch_oa._download_pdf``. Mocks only: HTTP is a
scripted fake, the limiter is a recorder patched over ``rate_limit.require`` /
``park`` (http.py imports ``rate_limit`` lazily), and the backoff sleep is an
injected clock (``http._default_sleep``). No network, no real sleeping, no DB.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from precis.errors import Upstream
from precis.handlers.semanticscholar import SemanticScholarHandler
from precis.ingest import semantic_scholar as s2
from precis.utils import http, rate_limit
from precis.utils.http import DEFAULT_HOST_ROW, VendorUnavailable, reset_circuit
from precis.workers import fetch_oa


@dataclass
class Clock:
    sleeps: list[float] = field(default_factory=list)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


@dataclass
class Limiter:
    requires: list[tuple[str, tuple[int, float] | None]] = field(default_factory=list)
    parks: list[tuple[str, float]] = field(default_factory=list)

    def providers(self) -> list[str]:
        return [p for p, _ in self.requires]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[Clock]:
    reset_circuit()
    clock = Clock()
    monkeypatch.setattr(http, "_default_sleep", clock.sleep)
    yield clock
    reset_circuit()


@pytest.fixture
def clock(_isolated: Clock) -> Clock:
    return _isolated


@pytest.fixture
def limiter(monkeypatch: pytest.MonkeyPatch) -> Limiter:
    rec = Limiter()

    def fake_require(
        provider: str,
        *,
        n: int = 1,
        max_wait_s: float = 30.0,
        seed_row: tuple[int, float] | None = None,
    ) -> None:
        rec.requires.append((provider, seed_row))

    def fake_park(provider: str, seconds: float) -> None:
        rec.parks.append((provider, seconds))

    monkeypatch.setattr("precis.utils.rate_limit.require", fake_require)
    monkeypatch.setattr("precis.utils.rate_limit.park", fake_park)
    return rec


def _status_error(status: int, headers: dict[str, str] | None = None):
    request = httpx.Request("GET", "https://example.test/x")
    response = httpx.Response(status, headers=headers or {}, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


# ── Semantic Scholar ingest ────────────────────────────────────────────────


class _Sch:
    """``SemanticScholar`` stand-in: ``get_paper`` replays a script."""

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.calls = 0

    def get_paper(self, paper_id: str, **_kw: Any) -> Any:
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def test_s2_one_limiter_token_per_attempt_and_backoff_is_jittered(limiter, clock):
    sch = _Sch(_status_error(503), _status_error(503), "paper")
    assert s2._get_with_retry(sch, "x") == "paper"
    assert sch.calls == 3
    assert limiter.providers() == ["s2", "s2", "s2"]
    # Full jitter: each wait lies in [0, min(cap, base * 2**attempt)], base 2 s.
    assert len(clock.sleeps) == 2
    assert 0.0 <= clock.sleeps[0] <= 2.0
    assert 0.0 <= clock.sleeps[1] <= 4.0


def test_s2_429_retry_after_parks_the_s2_row_and_is_the_wait(limiter, clock):
    sch = _Sch(_status_error(429, {"Retry-After": "7"}), "paper")
    assert s2._get_with_retry(sch, "x") == "paper"
    assert limiter.parks == [("s2", 7.0)]
    assert clock.sleeps == [7.0]


@pytest.mark.parametrize("status", [400, 401, 404])
def test_s2_4xx_is_not_retried(limiter, clock, status):
    err = _status_error(status)
    sch = _Sch(err)
    with pytest.raises(httpx.HTTPStatusError):
        s2._get_with_retry(sch, "x")
    assert sch.calls == 1
    assert clock.sleeps == []
    assert limiter.parks == []


def test_s2_programming_error_is_not_retried(limiter, clock):
    sch = _Sch(TypeError("bug"))
    with pytest.raises(TypeError):
        s2._get_with_retry(sch, "x")
    assert sch.calls == 1


def test_s2_lib_404_is_not_retried(limiter, clock):
    from semanticscholar.SemanticScholarException import ObjectNotFoundException

    sch = _Sch(ObjectNotFoundException("nope"))
    with pytest.raises(ObjectNotFoundException):
        s2._get_with_retry(sch, "x")
    assert sch.calls == 1


def test_s2_fast_fail_429_returns_at_once_without_retry(limiter, clock):
    class _Limited:
        calls = 0

        def search_paper(self, *a: Any, **k: Any) -> Any:
            self.calls += 1
            raise ConnectionRefusedError("429")

    sch = _Limited()
    assert s2._search_fast_fail(sch, "q", 3) is s2._RATE_LIMITED
    assert sch.calls == 1
    assert clock.sleeps == []


def test_s2_breaker_opens_after_consecutive_failures(limiter, clock):
    sch = _Sch(*[httpx.ConnectError("down")] * 20)
    with pytest.raises(httpx.ConnectError):
        s2._get_with_retry(sch, "x")  # 5 failures
    with pytest.raises(httpx.ConnectError):
        s2._get_with_retry(sch, "x")  # failure 8 trips the breaker mid-call
    seen = sch.calls
    assert seen == 8
    with pytest.raises(VendorUnavailable, match="circuit open"):
        s2._get_with_retry(sch, "x")
    assert sch.calls == seen  # open breaker: no request made


def test_s2_fails_open_with_a_log_line_when_limiter_store_is_down(
    monkeypatch, caplog, clock
):
    monkeypatch.setattr(rate_limit, "_rate_limit_enabled", lambda: True)
    monkeypatch.setattr(rate_limit, "_fail_open_warned", {})

    def boom() -> None:
        raise RuntimeError("no limiter table yet")

    monkeypatch.setattr(rate_limit, "_get_conn", boom)
    sch = _Sch("paper")
    with caplog.at_level(logging.WARNING, logger=rate_limit.log.name):
        assert s2._get_with_retry(sch, "x") == "paper"
    assert any("failing open" in r.getMessage() for r in caplog.records)


# ── S2 handler: complete=True walk ─────────────────────────────────────────


def _handler(monkeypatch, *responses: httpx.Response):
    handler = SemanticScholarHandler.__new__(SemanticScholarHandler)
    queue = list(responses)
    calls = {"n": 0}

    def raw(url: str, params: dict) -> httpx.Response:
        calls["n"] += 1
        return queue.pop(0)

    monkeypatch.setattr(handler, "_s2_raw_get", raw)
    return handler, calls


def test_handler_walk_takes_s2_token_per_attempt_and_parks_retry_after(
    monkeypatch, limiter, clock
):
    handler, calls = _handler(
        monkeypatch,
        httpx.Response(429, headers={"Retry-After": "3"}),
        httpx.Response(200, json={"data": []}),
    )
    data, attempts = handler._s2_get_json_backoff("https://x/author/1/papers", {})
    assert data == {"data": []}
    assert attempts == 2 == calls["n"]
    assert limiter.providers() == ["s2", "s2"]
    assert limiter.parks == [("s2", 3.0)]
    assert clock.sleeps == [3.0]


def test_handler_walk_exhausted_429_raises_upstream(monkeypatch, limiter, clock):
    handler, calls = _handler(monkeypatch, *[httpx.Response(429)] * 5)
    with pytest.raises(Upstream, match="retries exhausted"):
        handler._s2_get_json_backoff("https://x/author/1/papers", {})
    assert calls["n"] == 5
    assert len(clock.sleeps) == 4
    assert all(0.0 <= s <= 60.0 for s in clock.sleeps)


def test_handler_walk_401_and_404_are_not_retried(monkeypatch, limiter, clock):
    handler, calls = _handler(monkeypatch, httpx.Response(401))
    with pytest.raises(Upstream, match="rejected the API key"):
        handler._s2_get_json_backoff("https://x/y", {})
    assert calls["n"] == 1

    handler, calls = _handler(monkeypatch, httpx.Response(404))
    with pytest.raises(Upstream, match="no record"):
        handler._s2_get_json_backoff("https://x/y", {})
    assert calls["n"] == 1
    assert clock.sleeps == []


# ── fetch_oa._download_pdf ─────────────────────────────────────────────────


class _PdfStream:
    """Replays per-call outcomes for ``safe_stream``: an exception to raise or
    ``"ok"`` for a tiny valid PDF."""

    def __init__(self, *script: BaseException | str) -> None:
        self.script = list(script)
        self.urls: list[str] = []

    @contextlib.contextmanager
    def __call__(self, client: object, method: str, url: str, **_kw: object):
        self.urls.append(url)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        yield _OkResp()


class _OkResp:
    headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self, chunk_size: int = 0):
        yield b"%PDF-1.7\n" + b"x" * 64


@pytest.fixture
def pdf_stream(monkeypatch):
    def install(*script: BaseException | str) -> _PdfStream:
        stream = _PdfStream(*script)

        @contextlib.contextmanager
        def fake_client(*a: object, **k: object):
            yield object()

        monkeypatch.setattr(fetch_oa, "http_client", fake_client)
        monkeypatch.setattr(fetch_oa, "safe_stream", stream)
        return stream

    return install


def test_download_paces_per_host_one_token_per_attempt(
    tmp_path: Path, limiter, clock, pdf_stream
):
    pdf_stream(httpx.ConnectTimeout("blip"), "ok")
    fetch_oa._download_pdf("https://Arxiv.org/pdf/1.pdf", tmp_path / "a.pdf")
    pdf_stream("ok")
    fetch_oa._download_pdf("https://pub.example.net/x.pdf", tmp_path / "b.pdf")
    assert limiter.requires == [
        ("host:arxiv.org", DEFAULT_HOST_ROW),
        ("host:arxiv.org", DEFAULT_HOST_ROW),
        ("host:pub.example.net", DEFAULT_HOST_ROW),
    ]
    assert len(clock.sleeps) == 1 and 0.0 <= clock.sleeps[0] <= 1.0


def test_download_429_retry_after_parks_that_hosts_row(
    tmp_path: Path, limiter, clock, pdf_stream
):
    pdf_stream(_status_error(429, {"Retry-After": "5"}), "ok")
    target = tmp_path / "a.pdf"
    assert fetch_oa._download_pdf("https://pub.example.net/x.pdf", target) > 0
    assert limiter.parks == [("host:pub.example.net", 5.0)]
    assert clock.sleeps == [5.0]  # Retry-After is the wait, not the jitter
    assert target.read_bytes().startswith(b"%PDF-")


def test_download_429_without_retry_after_backs_off_with_jitter(
    tmp_path: Path, limiter, clock, pdf_stream
):
    stream = pdf_stream(_status_error(429), _status_error(429), _status_error(429))
    with pytest.raises(httpx.HTTPStatusError):
        fetch_oa._download_pdf("https://pub.example.net/x.pdf", tmp_path / "a.pdf")
    assert len(stream.urls) == 3  # _DOWNLOAD_RETRY_ATTEMPTS
    assert limiter.parks == []
    assert len(clock.sleeps) == 2 and all(0.0 <= s <= 8.0 for s in clock.sleeps)


@pytest.mark.parametrize("status", [403, 404, 410])
def test_download_4xx_is_not_retried(
    tmp_path: Path, limiter, clock, pdf_stream, status
):
    stream = pdf_stream(_status_error(status))
    with pytest.raises(httpx.HTTPStatusError):
        fetch_oa._download_pdf("https://pub.example.net/x.pdf", tmp_path / "a.pdf")
    assert len(stream.urls) == 1
    assert clock.sleeps == [] and limiter.parks == []


def test_download_non_pdf_body_is_not_retried(
    tmp_path: Path, limiter, clock, pdf_stream
):
    stream = pdf_stream(ValueError("response is not a PDF"))
    with pytest.raises(ValueError):
        fetch_oa._download_pdf("https://pub.example.net/x.pdf", tmp_path / "a.pdf")
    assert len(stream.urls) == 1


def test_download_breaker_is_per_host(tmp_path: Path, limiter, clock, pdf_stream):
    bad = "https://down.example.net/x.pdf"
    stream = pdf_stream(*[httpx.ConnectError("down")] * 30)
    for _ in range(3):  # 3 + 3 + 2 failures reach the threshold of 8
        with contextlib.suppress(httpx.ConnectError, VendorUnavailable):
            fetch_oa._download_pdf(bad, tmp_path / "a.pdf")
    seen = len(stream.urls)
    assert seen == 8
    with pytest.raises(VendorUnavailable, match="circuit open"):
        fetch_oa._download_pdf(bad, tmp_path / "a.pdf")
    assert len(stream.urls) == seen  # open breaker: no request
    # Another host is unaffected.
    stream.script[:] = ["ok"]
    assert fetch_oa._download_pdf("https://fine.example.org/x.pdf", tmp_path / "b.pdf")


def test_download_fails_open_with_a_log_line_when_limiter_store_is_down(
    tmp_path: Path, monkeypatch, caplog, clock, pdf_stream
):
    monkeypatch.setattr(rate_limit, "_rate_limit_enabled", lambda: True)
    monkeypatch.setattr(rate_limit, "_fail_open_warned", {})

    def boom() -> None:
        raise RuntimeError("no limiter table yet")

    monkeypatch.setattr(rate_limit, "_get_conn", boom)
    pdf_stream("ok")
    with caplog.at_level(logging.WARNING, logger=rate_limit.log.name):
        assert fetch_oa._download_pdf(
            "https://pub.example.net/x.pdf", tmp_path / "a.pdf"
        )
    assert any("failing open" in r.getMessage() for r in caplog.records)


def test_si_fetch_treats_an_open_breaker_as_transient():
    from precis.workers import si_fetch

    assert "VendorUnavailable" in si_fetch._TRANSIENT_ERRORS
