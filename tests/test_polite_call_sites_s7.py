"""gr493222 part A: the last uncovered external call sites.

Web fetch / news / Wikipedia / CFP / CIF / nanopub mirror (``polite_safe_get``),
Wolfram, reMarkable pairing, nanopub registry publish, OpenAI-compatible
transport (per-host, runtime internal-host skip), Discord webhook, and the
async core under the credential probes. For each: per-host pacing (one
limiter token per attempt), a 429 backs off with ``Retry-After`` parked, a
401/403 is never retried, and a non-idempotent POST is not re-sent after a
5xx.

Mocks only: scripted fakes, a recorder over ``rate_limit.require`` / ``park``
and an injected sleep. No network, no real sleeping, no DB, no threads.
"""

from __future__ import annotations

import asyncio
import urllib.error
from collections.abc import Iterator
from dataclasses import dataclass, field
from email.message import Message
from typing import Any

import httpx
import pytest

from precis.utils import http
from precis.utils.http import (
    POLICIES,
    VendorError,
    async_call_with_backoff,
    is_internal_host,
    polite_safe_get,
    reset_circuit,
)


@dataclass
class Rec:
    requires: list[str] = field(default_factory=list)
    parks: list[tuple[str, float]] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)


@pytest.fixture(autouse=True)
def _isolated() -> Iterator[None]:
    reset_circuit()
    yield
    reset_circuit()


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> Rec:
    r = Rec()
    monkeypatch.setattr(http, "_default_sleep", r.sleeps.append)
    monkeypatch.setattr(
        "precis.utils.rate_limit.require",
        lambda provider, **_kw: r.requires.append(provider),
    )
    monkeypatch.setattr(
        "precis.utils.rate_limit.park",
        lambda provider, seconds: r.parks.append((provider, seconds)),
    )
    return r


def _resp(status: int, url: str = "https://example.org/x", **headers: str) -> Any:
    return httpx.Response(
        status, headers=headers, text="body", request=httpx.Request("GET", url)
    )


class _scripted:
    """A ``safe_get`` stand-in answering a scripted status sequence."""

    def __init__(self, statuses: list[int], url: str = "https://example.org/x") -> None:
        self._seen = iter(statuses)
        self._url = url
        self.calls: list[int] = []

    def __call__(self, *_a: Any, **_kw: Any) -> Any:
        status = next(self._seen)
        self.calls.append(status)
        return _resp(
            status, self._url, **({"Retry-After": "7"} if status == 429 else {})
        )


_CLIENT: Any = object()


# ── polite_safe_get ────────────────────────────────────────────────────────


def test_safe_get_paces_each_attempt_and_backs_off_on_429(
    rec: Rec, monkeypatch: pytest.MonkeyPatch
) -> None:
    send = _scripted([429, 503, 200])
    monkeypatch.setattr("precis.utils.safe_fetch.safe_get", send)
    resp = polite_safe_get(_CLIENT, "https://example.org/x")
    assert resp.status_code == 200
    assert send.calls == [429, 503, 200]
    assert rec.requires == ["host:example.org"] * 3
    assert rec.parks == [("host:example.org", 7.0)]
    assert len(rec.sleeps) == 2


def test_safe_get_403_is_fatal_and_never_retried(
    rec: Rec, monkeypatch: pytest.MonkeyPatch
) -> None:
    send = _scripted([403, 200])
    monkeypatch.setattr("precis.utils.safe_fetch.safe_get", send)
    with pytest.raises(VendorError) as err:
        polite_safe_get(_CLIENT, "https://example.org/x")
    assert err.value.status == 403
    assert send.calls == [403]
    assert rec.sleeps == []


def test_safe_get_404_comes_back_unretried(
    rec: Rec, monkeypatch: pytest.MonkeyPatch
) -> None:
    send = _scripted([404])
    monkeypatch.setattr("precis.utils.safe_fetch.safe_get", send)
    assert polite_safe_get(_CLIENT, "https://example.org/x").status_code == 404
    assert send.calls == [404]


def test_hosts_have_separate_breakers(
    rec: Rec, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("precis.utils.safe_fetch.safe_get", _scripted([403]))
    with pytest.raises(VendorError):
        polite_safe_get(_CLIENT, "https://a.example.org/")
    monkeypatch.setattr("precis.utils.safe_fetch.safe_get", _scripted([200]))
    assert polite_safe_get(_CLIENT, "https://b.example.org/").status_code == 200
    assert rec.requires == ["host:a.example.org", "host:b.example.org"]


# ── web-ish handlers keep their error types ────────────────────────────────


def test_cif_403_is_a_refusal_not_a_retry(
    rec: Rec, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.structure.importers.cif import CifRefused, fetch_cif_url

    send = _scripted([403])
    monkeypatch.setattr("precis.utils.safe_fetch.safe_get", send)
    with pytest.raises(CifRefused):
        fetch_cif_url("https://example.org/a.cif")
    assert send.calls == [403]


def test_mirror_fetch_backs_off_on_429(
    rec: Rec, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.nanopub.mirror import _default_fetch

    send = _scripted([429, 200], "https://example.org/n.json")
    monkeypatch.setattr("precis.utils.safe_fetch.safe_get", send)
    assert _default_fetch("https://example.org/n.json") == b"body"
    assert send.calls == [429, 200]
    assert rec.requires == ["host:example.org"] * 2


# ── POST sites: non-idempotent, no re-send after a 5xx ─────────────────────


class _FakeClient:
    def __init__(self, statuses: list[int]) -> None:
        self.statuses = iter(statuses)
        self.calls = 0

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_a: object) -> None:
        return None

    def post(self, url: str, **_kw: Any) -> Any:
        self.calls += 1
        status = next(self.statuses)
        return _resp(status, url, **({"Retry-After": "3"} if status == 429 else {}))


def test_registry_publish_retries_429_but_not_5xx(
    rec: Rec, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.nanopub import registry

    client = _FakeClient([429, 200])
    monkeypatch.setattr("precis.utils.http.http_client", lambda **_kw: client)
    registry._default_post("https://registry.example.org/", b"x")
    assert client.calls == 2

    client = _FakeClient([503, 200])
    monkeypatch.setattr("precis.utils.http.http_client", lambda **_kw: client)
    with pytest.raises(VendorError) as err:
        registry._default_post("https://registry.example.org/", b"x")
    assert client.calls == 1
    # The caller still reads a 5xx as ambiguous, a 403 as definite.
    assert not registry._is_definite_failure(err.value)
    assert registry._is_definite_failure(
        VendorError("nanopub-registry", "x", status=403)
    )


def test_remarkable_pairing_403_is_rejected_once(
    rec: Rec, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.export import remarkable

    client = _FakeClient([403, 200])
    monkeypatch.setattr(httpx, "post", lambda url, **kw: client.post(url, **kw))
    with pytest.raises(remarkable.PairingError, match="rejected"):
        remarkable.register_device("abcdefgh")
    assert client.calls == 1
    # A per-code 403 must not open the service breaker.
    assert POLICIES["remarkable"].fatal_counts_toward_breaker is False


def test_wolfram_backs_off_and_hides_the_appid(
    rec: Rec, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.errors import Upstream
    from precis.handlers import math as wmath

    class Client(_FakeClient):
        def get(self, url: str, **kw: Any) -> Any:
            return self.post(url, **kw)

    client = Client([403])
    monkeypatch.setattr(wmath, "http_client", lambda **_kw: client)
    with pytest.raises(Upstream) as err:
        wmath._run_query("SECRET-APPID", "2+2")
    assert "SECRET-APPID" not in str(err.value)
    assert client.calls == 1


# ── OpenAI-compatible transport: per-host, internal hosts skip the limiter ─


def test_internal_host_table() -> None:
    for url in (
        "http://localhost:8000/v1",
        "http://127.0.0.1:8000/v1",
        "http://10.1.2.3:8000/v1",
        "http://llmbox:8000/v1",
        "http://gpu.lan/v1",
    ):
        assert is_internal_host(url), url
    for url in (
        "https://openrouter.ai/api/v1",
        "https://api.openai.com/v1",
        "http://8.8.8.8/v1",
    ):
        assert not is_internal_host(url), url


def test_openai_transport_polite_for_external_only(
    rec: Rec, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.utils.llm import openai_tools as ot

    def err(code: int, url: str) -> urllib.error.HTTPError:
        hdrs = Message()
        if code == 429:
            hdrs["Retry-After"] = "4"
        return urllib.error.HTTPError(url, code, "x", hdrs, None)

    plan: list[Any] = []

    def fake_read(url: str, *_a: Any) -> bytes:
        item = plan.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(ot, "_post_read", fake_read)

    url = "https://openrouter.ai/api/v1/chat/completions"
    plan[:] = [err(429, url), b"{}"]
    assert ot._UrllibTransport().post_json(url, {}, headers={}, timeout=5) == {}
    # outbound-policy: openrouter resolves to the rate-less llm-api
    # class — no token lane; the 429 still parks the shared row.
    assert rec.requires == []
    assert rec.parks == [("host:openrouter.ai", 4.0)]

    plan[:] = [err(401, url), b"{}"]
    with pytest.raises(urllib.error.HTTPError):
        ot._UrllibTransport().post_json(url, {}, headers={}, timeout=5)
    assert len(plan) == 1  # the 401 was not retried

    rec.requires.clear()
    local = "http://10.0.0.5:8000/v1/chat/completions"
    plan[:] = [b"{}"]
    ot._UrllibTransport().post_json(local, {}, headers={}, timeout=5)
    assert rec.requires == []


# ── Discord webhook ─────────────────────────────────────────────────────────


def test_discord_post_backs_off_on_429(
    rec: Rec, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.fixer import report

    calls: list[int] = []

    class Resp:
        def close(self) -> None:
            return None

    def fake_urlopen(req: Any, timeout: float = 0) -> Any:
        calls.append(1)
        assert req.get_header("User-agent").startswith("precis-mcp/")
        if len(calls) == 1:
            hdrs = Message()
            hdrs["Retry-After"] = "2"
            raise urllib.error.HTTPError(req.full_url, 429, "slow", hdrs, None)
        return Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    report._post_discord("https://discord.example.org/hook", "hello")
    assert len(calls) == 2
    assert rec.requires == ["host:discord.example.org"] * 2


# ── async core (credential probes) ──────────────────────────────────────────


def test_async_core_retries_429_never_403(
    rec: Rec, monkeypatch: pytest.MonkeyPatch
) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    def status_error(code: int) -> httpx.HTTPStatusError:
        return httpx.HTTPStatusError(
            "x", request=httpx.Request("GET", "https://e.org"), response=_resp(code)
        )

    seq: list[Any] = [status_error(429), status_error(503), "ok"]

    async def flaky() -> str:
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    out = asyncio.run(
        async_call_with_backoff(
            flaky,
            service="probe:test",
            policy=POLICIES["secret-probe"],
            provider=None,
            sleep=fake_sleep,
        )
    )
    assert out == "ok" and len(slept) == 2

    seq2: list[Any] = [status_error(403), "ok"]

    async def forbidden() -> str:
        item = seq2.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(
            async_call_with_backoff(
                forbidden,
                service="probe:test2",
                policy=POLICIES["secret-probe"],
                provider=None,
                sleep=fake_sleep,
            )
        )
    assert len(seq2) == 1


# ── pourbaix ion-reference pager: a mid-walk 429 retries only that page ────


def test_ion_reference_429_retries_only_that_page(
    rec: Rec, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.workers.job_types import pourbaix_bulk as pb
    from precis_dft import pourbaix_bulk as engine

    row = {"identifier": "i", "formula": "f", "data": {}}
    pages: list[int] = []
    plan = {1: [200], 2: [429, 200]}

    def fake_get(url: str, **kw: Any) -> Any:
        page = kw["params"]["page"]
        pages.append(page)
        status = plan[page].pop(0)
        return httpx.Response(
            status,
            json={"data": [row], "total_pages": 2},
            headers={"Retry-After": "2"} if status == 429 else {},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    captured: dict[str, Any] = {}

    def fake_fetch_entries(api_key: str, chemsys: str, **kw: Any) -> list[Any]:
        captured["rows"] = engine.fetch_ion_reference_data(
            api_key, get=kw["get"], user_agent=kw["user_agent"]
        )
        return []

    monkeypatch.setattr(engine, "fetch_entries", fake_fetch_entries)
    pb._mp_fetch("key", "Fe")
    assert pages == [1, 2, 2]  # page 1 is not re-fetched
    assert len(captured["rows"]) == 2
