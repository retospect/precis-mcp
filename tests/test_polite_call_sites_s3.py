"""The WEAK outbound call sites ride the shared politeness core (gr493222 S3,
batch 1): Crossref (habanero + httpx), OpenAlex content download, arXiv PDF
download, Europe PMC and DataCite.

Mocks only: HTTP is an ``httpx.MockTransport`` (or a scripted fake client),
the limiter is a recorder patched over ``rate_limit.require`` / ``park``
(http.py imports ``rate_limit`` lazily), and backoff sleeps land on an
injected clock (``http._default_sleep``). No network, no real sleeping, no DB.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import requests

from precis.ingest import (
    crossref,
    crossref_citation,
    doi_registry,
    provenance,
    si_discovery,
)
from precis.utils import http
from precis.utils.http import check_user_agent, reset_circuit
from precis.utils.rate_limit import RateLimited
from precis.workers import bib_parse, fetch_oa


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

    def fake_require(provider: str, **_kw: Any) -> None:
        rec.requires.append(provider)

    def fake_park(provider: str, seconds: float) -> None:
        rec.parks.append((provider, seconds))

    monkeypatch.setattr("precis.utils.rate_limit.require", fake_require)
    monkeypatch.setattr("precis.utils.rate_limit.park", fake_park)
    return rec


# ── Crossref via habanero (requests exceptions) ────────────────────────────


def _requests_error(
    status: int, headers: dict[str, str] | None = None
) -> requests.exceptions.HTTPError:
    resp = requests.Response()
    resp.status_code = status
    resp.headers.update(headers or {})
    return requests.exceptions.HTTPError(f"HTTP {status}", response=resp)


class FakeCrossref:
    """habanero ``Crossref`` stand-in: ``works`` replays a script."""

    script: list[Any] = []
    calls = 0
    init_kwargs: dict[str, Any] = {}

    def __init__(self, **kw: Any) -> None:
        type(self).init_kwargs = kw

    def works(self, **_kw: Any) -> Any:
        type(self).calls += 1
        item = type(self).script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture
def fake_crossref(monkeypatch: pytest.MonkeyPatch) -> type[FakeCrossref]:
    import habanero

    FakeCrossref.script = []
    FakeCrossref.calls = 0
    FakeCrossref.init_kwargs = {}
    monkeypatch.setattr(habanero, "Crossref", FakeCrossref)
    monkeypatch.setattr(crossref, "Crossref", FakeCrossref)
    return FakeCrossref


_OK_WORK = {"message": {"DOI": "10.1/x", "title": ["T"]}}


def test_habanero_fetch_message_429_retry_after_is_the_sleep(
    fake_crossref, limiter, clock
) -> None:
    fake_crossref.script = [_requests_error(429, {"Retry-After": "7"}), _OK_WORK]
    assert crossref.fetch_message("10.1/x") == _OK_WORK["message"]
    assert clock.sleeps == [7.0]
    assert limiter.requires == ["crossref", "crossref"]  # a token per attempt
    assert limiter.parks == [("crossref", 7.0)]


def test_habanero_fetch_message_404_is_none_and_not_retried(
    fake_crossref, limiter, clock
) -> None:
    fake_crossref.script = [_requests_error(404)]
    assert crossref.fetch_message("10.1/x") is None
    assert fake_crossref.calls == 1 and clock.sleeps == []


def test_habanero_fetch_message_exhausted_429_is_not_a_not_found(
    fake_crossref, limiter, clock
) -> None:
    fake_crossref.script = [_requests_error(429, {"Retry-After": "1"})] * 5
    with pytest.raises(RateLimited):
        crossref.fetch_message("10.1/x")
    assert fake_crossref.calls == 5
    assert clock.sleeps == [1.0] * 4


def test_habanero_lookup_crossref_takes_one_token_per_call(
    fake_crossref, limiter, clock
) -> None:
    fake_crossref.script = [_OK_WORK]
    assert crossref.lookup_crossref("10.1/x", mailto="ops@lab.org") is not None
    assert limiter.requires == ["crossref"]  # not doubled by the lookup wrapper


def test_habanero_ua_is_honest(fake_crossref, limiter, clock) -> None:
    fake_crossref.script = [_OK_WORK]
    crossref.fetch_message("10.1/x", mailto="ops@lab.org")
    kw = fake_crossref.init_kwargs
    assert kw["mailto"] == "ops@lab.org"
    assert check_user_agent(kw["ua_string"]) == kw["ua_string"]
    assert "mailto:ops@lab.org" in kw["ua_string"]


def test_provenance_fetch_message_429_retried_404_not(
    fake_crossref, limiter, clock
) -> None:
    fake_crossref.script = [_requests_error(429, {"Retry-After": "5"}), _OK_WORK]
    assert provenance._fetch_crossref_message("10.1/x", None) == _OK_WORK["message"]
    assert clock.sleeps == [5.0] and limiter.requires == ["crossref"] * 2

    fake_crossref.script = [_requests_error(404)]
    fake_crossref.calls = 0
    assert provenance._fetch_crossref_message("10.1/x", None) is None
    assert fake_crossref.calls == 1


def test_provenance_fetch_message_exhausted_429_surfaces_the_error(
    fake_crossref, limiter, clock
) -> None:
    """The caller maps a non-404 status to ``check_failed``; it must see the
    429, not a swallowed ``None`` (= unknown DOI)."""
    fake_crossref.script = [_requests_error(429, {"Retry-After": "1"})] * 5
    with pytest.raises(requests.exceptions.HTTPError):
        provenance._fetch_crossref_message("10.1/x", None)


def test_provenance_search_uses_the_crossref_list_lane(
    fake_crossref, limiter, clock
) -> None:
    fake_crossref.script = [
        _requests_error(429, {"Retry-After": "2"}),
        {"message": {"items": [{"DOI": "10.1/y"}]}},
    ]
    items = provenance._search_crossref_works(title="t", author=None, mailto=None)
    assert items == [{"DOI": "10.1/y"}]
    assert limiter.requires == ["crossref-list"] * 2
    assert clock.sleeps == [2.0] and limiter.parks == [("crossref-list", 2.0)]
    ua = fake_crossref.init_kwargs["ua_string"]
    assert check_user_agent(ua) == ua


# ── httpx sites through a MockTransport ────────────────────────────────────


class Server:
    """Scripted origin: replays ``script`` (status, headers, json) and records
    every request."""

    def __init__(self) -> None:
        self.script: list[tuple[int, dict[str, str], Any]] = []
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, headers, body = self.script.pop(0)
        return httpx.Response(status, headers=headers, json=body, request=request)


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
    # safe_get does DNS classification; the MockTransport client is the seam.
    monkeypatch.setattr("precis.utils.safe_fetch.safe_get", direct_get)
    monkeypatch.setattr(doi_registry, "safe_get", direct_get)
    return srv


_CR_ITEMS = {"message": {"items": [{"DOI": "10.1/x"}]}}
_CR_WORK = {"message": {"title": ["T"], "DOI": "10.1/x"}}
_DATACITE = {"data": {"attributes": {"titles": [{"title": "T"}]}}}
_EPMC = {"resultList": {"result": [{"isOpenAccess": "Y", "pmcid": "PMC1"}]}}
_CR_LINKS = {
    "message": {"link": [{"URL": "https://p.test/a.pdf", "content-type": "x/pdf"}]}
}


@dataclass
class Case:
    name: str
    call: Callable[[], Any]
    service: str
    ok_body: Any
    ok_value: Callable[[Any], bool]
    #: What a 404 means at this site: a value, or an exception type raised.
    miss: Any


def _cases() -> list[Case]:
    return [
        Case(
            "citation",
            lambda: crossref_citation._fetch_items("Smith 2020 Nature", "a@lab.org"),
            "crossref-list",
            _CR_ITEMS,
            lambda r: r == [{"DOI": "10.1/x"}],
            [],
        ),
        Case(
            "bib_parse",
            lambda: bib_parse._crossref_query("Smith 2020", mailto="a@lab.org"),
            "crossref-list",
            _CR_ITEMS,
            lambda r: r == [{"DOI": "10.1/x"}],
            None,
        ),
        Case(
            "registry-crossref",
            lambda: doi_registry._crossref_meta("10.1/x", mailto="a@lab.org"),
            "crossref",
            _CR_WORK,
            lambda r: r is not None and r["title"] == "T",
            None,
        ),
        Case(
            "registry-datacite",
            lambda: doi_registry._datacite_meta("10.1/x"),
            "datacite",
            _DATACITE,
            lambda r: r is not None and r["title"] == "T",
            None,
        ),
        Case(
            "doi-validity",
            lambda: provenance._fetch_doi_validity("10.1/x", mailto="a@lab.org"),
            "crossref",
            {},
            lambda r: r == "valid",
            "not_found",
        ),
        Case(
            "europepmc",
            lambda: fetch_oa._query_europepmc_oa_pmcid("10.1/x"),
            "europepmc",
            _EPMC,
            lambda r: r == "PMC1",
            httpx.HTTPStatusError,
        ),
        Case(
            "fetch_oa-crossref-links",
            lambda: fetch_oa._query_crossref_pdf_links("10.1/x", email="a@lab.org"),
            "crossref",
            _CR_LINKS,
            lambda r: r == ["https://p.test/a.pdf"],
            httpx.HTTPStatusError,
        ),
    ]


CASES = _cases()
IDS = [c.name for c in CASES]


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_429_retry_after_is_retried_with_that_sleep(
    case: Case, server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(429, {"Retry-After": "7"}, {}), (200, {}, case.ok_body)]
    assert case.ok_value(case.call())
    assert len(server.requests) == 2
    assert clock.sleeps == [7.0]
    assert limiter.requires == [case.service] * 2  # a token per attempt
    assert limiter.parks == [(case.service, 7.0)]


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_404_is_not_retried(
    case: Case, server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(404, {}, {})]
    if isinstance(case.miss, type):
        with pytest.raises(case.miss):
            case.call()
    else:
        assert case.call() == case.miss
    assert len(server.requests) == 1
    assert clock.sleeps == []


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_user_agent_is_honest(
    case: Case, server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(200, {}, case.ok_body)]
    case.call()
    ua = server.requests[0].headers["user-agent"]
    assert check_user_agent(ua) == ua
    assert ua.startswith("precis-mcp/") and "mailto:" in ua


def test_registry_exhausted_429_is_a_fault_not_a_miss(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(429, {"Retry-After": "1"}, {})] * 5
    with pytest.raises(doi_registry.RegistryLookupError):
        doi_registry._datacite_meta("10.1/x")
    assert len(server.requests) == 5


def test_registry_limiter_refusal_is_a_fault_not_a_miss(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(provider: str, **_kw: Any) -> None:
        raise RateLimited(provider)

    monkeypatch.setattr("precis.utils.rate_limit.require", refuse)
    with pytest.raises(doi_registry.RegistryLookupError):
        doi_registry._crossref_meta("10.1/x")
    assert server.requests == []


def test_doi_validity_exhausted_429_is_none_not_not_found(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(429, {"Retry-After": "1"}, {})] * 5
    assert provenance._fetch_doi_validity("10.1/x", mailto=None) is None


def test_bib_parse_exhausted_429_is_none_not_unmatched(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(429, {"Retry-After": "1"}, {})] * 5
    assert bib_parse._crossref_query("Smith 2020") is None


# ── si_discovery Crossref leg (HttpResult fetch) ───────────────────────────


def _si_fetch(cr_script: list[si_discovery.HttpResult], seen: list[str]):
    def fetch(url: str) -> si_discovery.HttpResult:
        seen.append(url)
        if url.startswith(si_discovery._CROSSREF_API):
            return cr_script.pop(0)
        return si_discovery.HttpResult(status=404, text="", url=url, headers={})

    return fetch


def test_si_discovery_crossref_429_retry_after_and_404(
    limiter: Limiter, clock: Clock
) -> None:
    seen: list[str] = []
    cr_url = f"{si_discovery._CROSSREF_API}/10.1/x"
    # default_fetch lowercases header keys; the core must still read it.
    script = [
        si_discovery.HttpResult(status=429, headers={"retry-after": "4"}),
        si_discovery.HttpResult(status=404),
    ]
    si_discovery.discover("10.1/x", _si_fetch(script, seen))
    assert seen.count(cr_url) == 2
    assert clock.sleeps == [4.0]
    assert limiter.requires == ["crossref"] * 2
    assert limiter.parks == [("crossref", 4.0)]


def test_si_discovery_crossref_404_not_retried(limiter: Limiter, clock: Clock) -> None:
    seen: list[str] = []
    script = [si_discovery.HttpResult(status=404)]
    res = si_discovery.discover("10.1/x", _si_fetch(script, seen))
    assert seen.count(f"{si_discovery._CROSSREF_API}/10.1/x") == 1
    assert clock.sleeps == []
    assert any(m["source"] == si_discovery.SOURCE_CROSSREF for m in res.misses)


def test_si_discovery_exhausted_429_stays_a_status_miss(
    limiter: Limiter, clock: Clock
) -> None:
    seen: list[str] = []
    script = [si_discovery.HttpResult(status=429, headers={"retry-after": "1"})] * 5
    res = si_discovery.discover("10.1/x", _si_fetch(script, seen))
    assert seen.count(f"{si_discovery._CROSSREF_API}/10.1/x") == 5
    cr_misses = [m for m in res.misses if m["source"] == si_discovery.SOURCE_CROSSREF]
    assert len(cr_misses) == 1 and "429" in cr_misses[0]["reason"]


# ── PDF downloads under a named service (arXiv, OpenAlex content) ──────────


def _http_status_error(status: int, headers: dict[str, str] | None = None):
    request = httpx.Request("GET", "https://x.test/f.pdf")
    response = httpx.Response(status, headers=headers or {}, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


class Once:
    """Scripted ``_download_pdf_once``."""

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.calls = 0

    def __call__(self, url: str, target: Path, extra: Any) -> int:
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return int(item)


@pytest.mark.parametrize("service", ["arxiv", "openalex-content"])
def test_named_service_download_429_retry_after_and_token_per_attempt(
    service: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, limiter, clock
) -> None:
    once = Once(_http_status_error(429, {"Retry-After": "3"}), 512)
    monkeypatch.setattr(fetch_oa, "_download_pdf_once", once)
    size = fetch_oa._download_pdf(
        "https://arxiv.org/pdf/2401.12345.pdf", tmp_path / "a.pdf", service=service
    )
    assert size == 512 and once.calls == 2
    assert clock.sleeps == [3.0]
    # One limiter token per attempt, on the service's own row (not host:*).
    assert limiter.requires == [service] * 2
    assert limiter.parks == [(service, 3.0)]


@pytest.mark.parametrize("service", ["arxiv", "openalex-content"])
def test_named_service_download_404_not_retried(
    service: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, limiter, clock
) -> None:
    once = Once(_http_status_error(404))
    monkeypatch.setattr(fetch_oa, "_download_pdf_once", once)
    with pytest.raises(httpx.HTTPStatusError):
        fetch_oa._download_pdf(
            "https://x.test/f.pdf", tmp_path / "a.pdf", service=service
        )
    assert once.calls == 1 and clock.sleeps == []
    assert limiter.requires == [service]


def test_try_arxiv_takes_a_token_for_each_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, limiter, clock
) -> None:
    """Previously one limiter token covered all three tries."""
    once = Once(_http_status_error(503), _http_status_error(503), 256)
    monkeypatch.setattr(fetch_oa, "_download_pdf_once", once)
    stub = fetch_oa.StubRef(
        ref_id=1, doi=None, arxiv="2401.12345", s2_id=None, cite_key="ax"
    )
    out = fetch_oa._try_arxiv(stub, inbox_dir=tmp_path)
    assert out is not None and out.event == "fetch_ok"
    assert once.calls == 3
    assert limiter.requires == ["arxiv"] * 3
    assert len(clock.sleeps) == 2  # full-jitter, in [0, base * 2**attempt]
    assert 0.0 <= clock.sleeps[0] <= 3.0 and 0.0 <= clock.sleeps[1] <= 6.0


def test_try_arxiv_limiter_refusal_is_rate_limited_outcome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def refuse(provider: str, **_kw: Any) -> None:
        raise RateLimited(provider)

    monkeypatch.setattr("precis.utils.rate_limit.require", refuse)
    once = Once()
    monkeypatch.setattr(fetch_oa, "_download_pdf_once", once)
    stub = fetch_oa.StubRef(
        ref_id=1, doi=None, arxiv="2401.12345", s2_id=None, cite_key="ax"
    )
    out = fetch_oa._try_arxiv(stub, inbox_dir=tmp_path)
    assert out is not None and out.event == "rate_limited"
    assert out.payload["provider"] == "arxiv" and once.calls == 0


def test_download_headers_ua_is_honest() -> None:
    ua = fetch_oa._download_headers()["User-Agent"]
    assert check_user_agent(ua) == ua


def test_habanero_one_overall_deadline_over_all_retries(
    fake_crossref, limiter, clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wedged Crossref holds the caller one deadline, not attempts x deadline."""
    import threading
    import time

    release = threading.Event()
    calls = {"n": 0}

    def wedged(self: Any, **_kw: Any) -> Any:
        calls["n"] += 1
        release.wait(5.0)
        return _OK_WORK

    monkeypatch.setattr(FakeCrossref, "works", wedged)
    monkeypatch.setattr(crossref, "_DEADLINE_S", 0.3)
    start = time.monotonic()
    try:
        assert crossref.fetch_message("10.1/x") is None
    finally:
        release.set()
    assert time.monotonic() - start < 2.0
    assert calls["n"] == 1  # no retry started after the overall deadline
