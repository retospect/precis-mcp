"""S3 batch 2: ORCID, PubChem and SEC EDGAR ride the shared politeness core
(gr493222). OPSIN/CIR already use the core's ``with_backoff`` via
``pcb._http`` with their own policies and are untouched.

Mocks only: an ``httpx.MockTransport`` origin, a recorder over
``rate_limit.require`` / ``park``, backoff sleeps on an injected clock.
No network, no real sleeping, no DB.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from precis.config import DEFAULT_EDGAR_USER_AGENT
from precis.errors import NotFound, Upstream
from precis.handlers import _chem_resolve
from precis.handlers._edgar_client import (
    EdgarClient,
    EdgarHttpError,
    EdgarNotFound,
    EdgarRateLimited,
    TokenBucket,
)
from precis.ingest import orcid
from precis.utils import http
from precis.utils.http import check_user_agent, reset_circuit
from precis.utils.rate_limit import RateLimited

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


class Server:
    def __init__(self) -> None:
        self.script: list[tuple[int, dict[str, str], Any]] = []
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, headers, body = self.script.pop(0)
        if isinstance(body, Exception):
            raise body
        return httpx.Response(status, headers=headers, json=body, request=request)


@pytest.fixture(autouse=True)
def _isolated() -> Iterator[None]:
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


# ── ORCID ──────────────────────────────────────────────────────────────────


@pytest.fixture
def orcid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orcid, "_get_token", lambda: "tok")


def test_orcid_429_retry_after_is_the_sleep_and_token_per_attempt(
    server: Server, limiter: Limiter, clock: Clock, orcid_token: None
) -> None:
    server.script = [(429, {"Retry-After": "7"}, {}), (200, {}, {"ok": 1})]
    assert orcid._api_get(f"{_ORCID_ID}/person") == {"ok": 1}
    assert len(server.requests) == 2
    assert clock.sleeps == [7.0]
    assert limiter.requires == ["orcid"] * 2
    assert limiter.parks == [("orcid", 7.0)]


def test_orcid_404_not_retried_and_is_not_found(
    server: Server, limiter: Limiter, clock: Clock, orcid_token: None
) -> None:
    server.script = [(404, {}, {})]
    with pytest.raises(NotFound):
        orcid._api_get(f"{_ORCID_ID}/person")
    assert len(server.requests) == 1
    assert clock.sleeps == []


def test_orcid_exhausted_429_is_upstream_not_not_found(
    server: Server, limiter: Limiter, clock: Clock, orcid_token: None
) -> None:
    server.script = [(429, {}, {})] * 5
    with pytest.raises(Upstream):
        orcid._api_get(f"{_ORCID_ID}/person")
    assert len(server.requests) == 5


def test_orcid_401_clears_token_cache_and_is_not_retried(
    server: Server, limiter: Limiter, clock: Clock, orcid_token: None
) -> None:
    orcid._token_cache["cid"] = ("tok", 1e18)
    server.script = [(401, {}, {})]
    with pytest.raises(Upstream, match="bearer token"):
        orcid._api_get(f"{_ORCID_ID}/person")
    assert len(server.requests) == 1
    assert orcid._token_cache == {}


def test_orcid_limiter_refusal_is_upstream(
    server: Server, clock: Clock, orcid_token: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(provider: str, **_kw: Any) -> None:
        raise RateLimited(provider)

    monkeypatch.setattr("precis.utils.rate_limit.require", refuse)
    with pytest.raises(Upstream):
        orcid._api_get(f"{_ORCID_ID}/person")
    assert server.requests == []


def test_orcid_user_agent_is_honest(
    server: Server, limiter: Limiter, clock: Clock, orcid_token: None
) -> None:
    server.script = [(200, {}, {})]
    orcid._api_get(f"{_ORCID_ID}/person")
    ua = server.requests[0].headers["user-agent"]
    assert check_user_agent(ua) == ua and "mailto:" in ua


def test_orcid_token_mint_429_retried_and_ua_honest(
    server: Server,
    limiter: Limiter,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orcid, "_credentials", lambda: ("cid", "secret"))
    server.script = [
        (429, {"Retry-After": "3"}, {}),
        (200, {}, {"access_token": "t", "expires_in": 3600}),
    ]
    assert orcid._get_token() == "t"
    assert clock.sleeps == [3.0]
    assert limiter.requires == ["orcid"] * 2
    ua = server.requests[0].headers["user-agent"]
    assert check_user_agent(ua) == ua


def test_orcid_one_overall_deadline_over_all_retries(
    server: Server,
    limiter: Limiter,
    orcid_token: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every attempt is a retryable 503 with Retry-After and the backoff
    sleep takes real time: only one deadline spanning the attempts can end
    the call before the 5 attempts do."""
    monkeypatch.setattr(http, "_default_sleep", lambda _s: time.sleep(0.25))
    server.script = [(503, {"Retry-After": "1"}, {})] * 5
    monkeypatch.setattr(orcid, "_DEADLINE_S", 0.4)
    t0 = time.monotonic()
    with pytest.raises(Upstream, match="transport"):
        orcid._api_get(f"{_ORCID_ID}/person")
    assert time.monotonic() - t0 < 1.0
    assert len(server.requests) >= 2  # it retried; the deadline spanned them
    # The abandoned deadline thread keeps retrying after the call returned;
    # left alone it takes limiter tokens inside whichever later test has the
    # recording limiter patched in. Let it unwind before this test ends.
    for t in threading.enumerate():
        if t.name.startswith("deadline-"):
            t.join(timeout=10)
            assert not t.is_alive()


# ── PubChem ────────────────────────────────────────────────────────────────

_PUBCHEM_URL = _chem_resolve.PUBCHEM_CIDS_URL.format(name="aspirin")


def test_pubchem_429_retry_after_is_the_sleep_and_token_per_attempt(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(429, {"Retry-After": "4"}, {}), (200, {}, {"IdentifierList": {}})]
    status, body = _chem_resolve.http_fetch()(_PUBCHEM_URL)
    assert status == 200 and "IdentifierList" in body
    assert len(server.requests) == 2
    assert clock.sleeps == [4.0]
    assert limiter.requires == ["pubchem"] * 2
    assert limiter.parks == [("pubchem", 4.0)]


def test_pubchem_404_returned_not_retried(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(404, {}, {})]
    status, _ = _chem_resolve.http_fetch()(_PUBCHEM_URL)
    assert status == 404
    assert len(server.requests) == 1
    assert clock.sleeps == []


def test_pubchem_exhausted_503_is_a_fault_in_the_tier(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(503, {}, {})] * 3
    fetch = _chem_resolve.http_fetch()
    with pytest.raises(_chem_resolve._Fault):
        _chem_resolve._call(fetch, _PUBCHEM_URL)
    assert len(server.requests) == 3


def test_pubchem_read_timeouts_are_retried_within_the_tight_policy(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(0, {}, httpx.ReadTimeout("slow"))] * 3
    with pytest.raises(_chem_resolve._Fault):
        _chem_resolve._call(_chem_resolve.http_fetch(), _PUBCHEM_URL)
    assert len(server.requests) == 3
    assert len(clock.sleeps) == 2 and all(0.0 <= x <= 8.0 for x in clock.sleeps)
    assert limiter.requires == ["pubchem"] * 3


def test_pubchem_attempts_one_disables_retry(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(503, {}, {})]
    with pytest.raises(_chem_resolve._Fault):
        _chem_resolve._call(_chem_resolve.http_fetch(attempts=1), _PUBCHEM_URL)
    assert len(server.requests) == 1
    assert clock.sleeps == []


def test_pubchem_403_is_a_fault_and_not_retried(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(403, {}, {})]
    with pytest.raises(_chem_resolve._Fault):
        _chem_resolve._call(_chem_resolve.http_fetch(), _PUBCHEM_URL)
    assert len(server.requests) == 1


def test_pubchem_exhausted_429_is_a_fault(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(429, {}, {})] * 3
    with pytest.raises(_chem_resolve._Fault):
        _chem_resolve._call(_chem_resolve.http_fetch(), _PUBCHEM_URL)
    assert len(server.requests) == 3


def test_pubchem_user_agent_is_honest(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(200, {}, {})]
    _chem_resolve.http_fetch()(_PUBCHEM_URL)
    ua = server.requests[0].headers["user-agent"]
    assert check_user_agent(ua) == ua and "mailto:" in ua


# ── SEC EDGAR ──────────────────────────────────────────────────────────────


class CountingBucket(TokenBucket):
    def __init__(self) -> None:
        super().__init__()
        self.acquired = 0

    def acquire(self, tokens: float = 1.0) -> None:
        self.acquired += 1


def _edgar(bucket: CountingBucket | None = None) -> EdgarClient:
    return EdgarClient(
        user_agent=DEFAULT_EDGAR_USER_AGENT,
        bucket=bucket,
    )


def test_edgar_429_retry_after_is_the_sleep_and_both_limiters_per_attempt(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    bucket = CountingBucket()
    server.script = [(429, {"Retry-After": "5"}, {}), (200, {}, {"cik": 1})]
    assert b"cik" in _edgar(bucket).submissions("320193")
    assert len(server.requests) == 2
    assert clock.sleeps == [5.0]
    assert limiter.requires == ["sec"] * 2
    assert limiter.parks == [("sec", 5.0)]
    assert bucket.acquired == 2  # the per-process 10 req/s bucket is kept


def test_edgar_404_not_retried(server: Server, limiter: Limiter, clock: Clock) -> None:
    server.script = [(404, {}, {})]
    with pytest.raises(EdgarNotFound):
        _edgar().submissions("320193")
    assert len(server.requests) == 1
    assert clock.sleeps == []


def test_edgar_403_is_rate_limited_and_not_retried(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(403, {}, {})]
    with pytest.raises(EdgarRateLimited):
        _edgar().submissions("320193")
    assert len(server.requests) == 1


def test_edgar_exhausted_429_is_rate_limited_not_found(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(429, {}, {})] * 5
    with pytest.raises(EdgarRateLimited):
        _edgar().submissions("320193")
    assert len(server.requests) == 5


def test_edgar_exhausted_5xx_is_http_error(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(503, {}, {})] * 5
    with pytest.raises(EdgarHttpError) as ei:
        _edgar().submissions("320193")
    assert ei.value.status == 503
    assert len(server.requests) == 5


def test_edgar_breaker_open_is_rate_limited_without_a_request(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    http._CIRCUITS["sec"] = http._Circuit(
        consecutive_failures=8, opened_at=time.monotonic(), cooldown=300.0
    )
    with pytest.raises(EdgarRateLimited):
        _edgar().submissions("320193")
    assert server.requests == []


def test_edgar_user_agent_passes_the_check(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(200, {}, {})]
    _edgar().submissions("320193")
    ua = server.requests[0].headers["user-agent"]
    assert check_user_agent(ua) == ua and "@" in ua


# ── already on the core / nothing to convert ───────────────────────────────


def test_opsin_polite_fetch_429_retry_after_is_the_sleep(
    server: Server, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("precis.utils.rate_limit.acquire", lambda *_a, **_k: True)
    _chem_resolve.clear_answer_cache()
    server.script = [(429, {"Retry-After": "2"}, {}), (200, {}, {"smiles": "C"})]
    fetch = _chem_resolve.polite_fetch("OPSIN", _chem_resolve.OPSIN_POLICY)
    status, _ = fetch(_chem_resolve.OPSIN_URL.format(name="methane"))
    assert status == 200
    assert len(server.requests) == 2
    assert clock.sleeps == [2.0]
