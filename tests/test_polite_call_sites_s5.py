"""S3 batch 5 (gr493222): the last WEAK outbound leftovers.

Markup downloads (``fetch_oa._download_markup``) share the PDF leg's
per-host pacing; S2's swallowed 429 parks the shared row; ORCID's per-record
403 no longer opens its breaker. Also pins the core's per-provider behaviour:
each provider backs off on its own policy and breaker, and a 4xx (other than
429) is never retried.

Mocks only: scripted fakes, a recorder over ``rate_limit.require`` / ``park``
and an injected sleep. No network, no real sleeping, no DB.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from precis.ingest import orcid
from precis.ingest import semantic_scholar as s2
from precis.utils import http
from precis.utils.http import (
    POLICIES,
    VendorError,
    VendorUnavailable,
    reset_circuit,
    with_backoff,
)
from precis.workers import fetch_oa

_ORCID_ID = "0000-0002-1825-0097"


@dataclass
class Clock:
    sleeps: list[float] = field(default_factory=list)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


@dataclass
class Limiter:
    requires: list[str] = field(default_factory=list)
    parks: list[tuple[str, float]] = field(default_factory=list)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    reset_circuit()
    orcid._token_cache.clear()
    yield
    reset_circuit()
    orcid._token_cache.clear()


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


def _status_error(status: int, headers: dict[str, str] | None = None):
    request = httpx.Request("GET", "https://example.test/x")
    response = httpx.Response(status, headers=headers or {}, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


def _response(status: int, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        headers=headers or {},
        request=httpx.Request("GET", "https://example.test/x"),
    )


# ── fetch_oa._download_markup ──────────────────────────────────────────────


class _OkMarkup:
    headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self, chunk_size: int = 0):
        yield b"<article>" + b"x" * 32 + b"</article>"


class _MarkupStream:
    """Replays per-call outcomes for ``safe_stream``: an exception to raise or
    ``"ok"`` for a small XML body."""

    def __init__(self, *script: BaseException | str) -> None:
        self.script = list(script)
        self.urls: list[str] = []

    @contextlib.contextmanager
    def __call__(self, client: object, method: str, url: str, **_kw: object):
        self.urls.append(url)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        yield _OkMarkup()


@pytest.fixture
def markup_stream(monkeypatch):
    def install(*script: BaseException | str) -> _MarkupStream:
        stream = _MarkupStream(*script)

        @contextlib.contextmanager
        def fake_client(*a: object, **k: object):
            yield object()

        monkeypatch.setattr("precis.utils.http.http_client", fake_client)
        monkeypatch.setattr("precis.utils.safe_fetch.safe_stream", stream)
        return stream

    return install


def test_markup_download_takes_a_host_token_per_attempt_and_retries_5xx(
    tmp_path: Path, limiter, clock, markup_stream
):
    stream = markup_stream(_status_error(503), "ok")
    target = tmp_path / "a.xml"
    assert fetch_oa._download_markup("https://Pub.example.net/a.xml", target) > 0
    assert len(stream.urls) == 2
    assert limiter.requires == ["host:pub.example.net"] * 2
    assert len(clock.sleeps) == 1 and 0.0 <= clock.sleeps[0] <= 1.0
    assert target.read_bytes().startswith(b"<article>")


def test_markup_download_429_retry_after_parks_that_host(
    tmp_path: Path, limiter, clock, markup_stream
):
    markup_stream(_status_error(429, {"Retry-After": "6"}), "ok")
    fetch_oa._download_markup("https://pub.example.net/a.xml", tmp_path / "a.xml")
    assert limiter.parks == [("host:pub.example.net", 6.0)]
    assert clock.sleeps == [6.0]


@pytest.mark.parametrize("status", [400, 404, 410])
def test_markup_download_4xx_is_final_no_retry(
    tmp_path: Path, limiter, clock, markup_stream, status
):
    stream = markup_stream(_status_error(status))
    with pytest.raises(httpx.HTTPStatusError):
        fetch_oa._download_markup("https://pub.example.net/a.xml", tmp_path / "a.xml")
    assert len(stream.urls) == 1
    assert clock.sleeps == []


def test_markup_download_is_not_retried_forever_on_5xx(
    tmp_path: Path, limiter, clock, markup_stream
):
    stream = markup_stream(*[_status_error(503)] * 3)
    with pytest.raises(httpx.HTTPStatusError):
        fetch_oa._download_markup("https://pub.example.net/a.xml", tmp_path / "a.xml")
    assert len(stream.urls) == 3
    assert not (tmp_path / "a.xml").exists()


# ── S2 swallowed 429 ───────────────────────────────────────────────────────


class _RefusedSch:
    def search_paper(self, query: str, limit: int = 0):
        raise ConnectionRefusedError("429")


def test_s2_fast_fail_429_parks_the_s2_row_and_does_not_retry(limiter, clock):
    out = s2._search_fast_fail(_RefusedSch(), "graphene", 5)
    assert out is s2._RATE_LIMITED
    assert limiter.requires == ["s2"]
    assert limiter.parks == [("s2", s2._RATE_LIMITED_PARK_S)]
    assert clock.sleeps == []


# ── ORCID: a per-record 403 is not a provider outage ───────────────────────


def test_orcid_403_run_does_not_open_the_breaker(limiter, clock):
    policy = POLICIES["orcid"]
    for _ in range(policy.breaker_threshold + 3):
        with pytest.raises(VendorError) as err:
            with_backoff(lambda: _response(403), service="orcid")
        assert not isinstance(err.value, VendorUnavailable)
    # Still callable: a healthy response goes through.
    assert with_backoff(lambda: _response(200), service="orcid").status_code == 200


def test_other_providers_403_run_still_opens_the_breaker(limiter, clock):
    policy = POLICIES["openalex"]
    assert policy.fatal_counts_toward_breaker
    for _ in range(policy.breaker_threshold):
        with pytest.raises(VendorError):
            with_backoff(lambda: _response(403), service="openalex")
    with pytest.raises(VendorUnavailable):
        with_backoff(lambda: _response(200), service="openalex")


# ── core: per-provider backoff, 4xx is fatal ───────────────────────────────


def _next_of(it: Iterator[httpx.Response]) -> Callable[[], httpx.Response]:
    return lambda: next(it)


def test_backoff_waits_follow_each_providers_own_policy(limiter, clock):
    """arXiv's base is 3 s, Crossref's default 1 s: the full-jitter ceiling
    for the first retry differs, and each provider takes its own token."""

    # Seeded rng stub: uniform() returns the upper bound, so the sleep IS the
    # ceiling min(cap, base * 2**attempt).
    class Top:
        def uniform(self, a: float, b: float) -> float:
            return b

    for service in ("arxiv", "crossref"):
        script = iter([_response(503), _response(200)])
        with_backoff(_next_of(script), service=service, rng=Top())
    assert clock.sleeps == [
        POLICIES["arxiv"].base,
        POLICIES["crossref"].base,
    ]
    assert clock.sleeps[0] != clock.sleeps[1]
    assert limiter.requires == ["arxiv", "arxiv", "crossref", "crossref"]


def test_one_providers_outage_does_not_open_another_providers_breaker(limiter, clock):
    with pytest.raises(VendorUnavailable):
        with_backoff(
            lambda: _response(503),
            service="pubchem",
            policy=http.Policy(attempts=1, breaker_threshold=1, provider="pubchem"),
        )
    with pytest.raises(VendorUnavailable, match="circuit"):
        with_backoff(lambda: _response(200), service="pubchem")
    assert with_backoff(lambda: _response(200), service="datacite").status_code == 200


@pytest.mark.parametrize("status", [400, 404, 410, 422])
def test_core_4xx_other_than_429_is_returned_after_one_attempt(limiter, clock, status):
    seen = {"n": 0}

    def send() -> httpx.Response:
        seen["n"] += 1
        return _response(status)

    assert with_backoff(send, service="europepmc").status_code == status
    assert seen["n"] == 1
    assert clock.sleeps == []


@pytest.mark.parametrize("status", [401, 403])
def test_core_auth_failures_are_fatal_without_retry(limiter, clock, status):
    seen = {"n": 0}

    def send() -> httpx.Response:
        seen["n"] += 1
        return _response(status)

    with pytest.raises(VendorError):
        with_backoff(send, service="datacite")
    assert seen["n"] == 1
    assert clock.sleeps == []


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_call_with_backoff_does_not_retry_4xx(limiter, clock, status):
    seen = {"n": 0}

    def fn() -> None:
        seen["n"] += 1
        raise _status_error(status)

    with pytest.raises(httpx.HTTPStatusError):
        http.call_with_backoff(fn, service="core")
    assert seen["n"] == 1
    assert clock.sleeps == []
