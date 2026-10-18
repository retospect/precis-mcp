"""S3 batch 4: supply vendors (DigiKey, Farnell, Mouser), the OpenRouter
catalogue fetches and the /secrets probes ride the shared politeness core
(gr493222).

Mocks only: an ``httpx.MockTransport`` origin, a recorder over
``rate_limit.require`` / ``park``, backoff sleeps on an injected clock, a fake
monotonic clock for the probe interval. No network, no real sleeping, no DB.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, cast

import httpx
import pytest

from precis.supply.base import SupplierError
from precis.supply.digikey import DigiKeyAdapter
from precis.supply.farnell import FarnellAdapter
from precis.supply.mouser import MouserAdapter
from precis.utils import http
from precis.utils.http import check_user_agent, reset_circuit
from precis.workers import llm_reconcile

pytest.importorskip("fastapi")

from precis import secret_status


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

    def ua(self, i: int = 0) -> str:
        ua = self.requests[i].headers["user-agent"]
        assert check_user_agent(ua) == ua
        return ua


@pytest.fixture(autouse=True)
def _isolated() -> Iterator[None]:
    reset_circuit()
    secret_status._probed.clear()
    yield
    reset_circuit()
    secret_status._probed.clear()


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
    client = httpx.Client(transport=httpx.MockTransport(srv))
    monkeypatch.setattr(httpx, "get", client.get)
    monkeypatch.setattr(httpx, "post", client.post)
    monkeypatch.setattr("precis.supply.farnell.get_secret", lambda name: "k")
    monkeypatch.setattr("precis.supply.mouser.get_secret", lambda name: "k")
    monkeypatch.setattr(
        DigiKeyAdapter, "_credentials", staticmethod(lambda: ("id", "secret"))
    )
    return srv


_FARNELL_OK = {"keywordSearchReturn": {"numberOfResults": 0}}
_MOUSER_OK: dict[str, Any] = {"SearchResults": {"Parts": []}}
_DK_SEARCH_OK: dict[str, Any] = {"Products": []}
_DK_TOKEN = {"access_token": "tok", "expires_in": 600}


# ── Farnell ────────────────────────────────────────────────────────────────


def test_farnell_429_retry_after_is_the_sleep_and_token_per_attempt(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(429, {"Retry-After": "7"}, {}), (200, {}, _FARNELL_OK)]
    assert FarnellAdapter().search("M4") == []
    assert len(server.requests) == 2
    assert clock.sleeps == [7.0]
    assert limiter.requires == ["farnell"] * 2
    assert limiter.parks == [("farnell", 7.0)]
    server.ua()


def test_farnell_404_not_retried_keeps_its_mapped_error(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(404, {}, {})]
    with pytest.raises(SupplierError) as err:
        FarnellAdapter().search("M4")
    assert (err.value.stage, err.value.status) == ("search", 404)
    assert len(server.requests) == 1 and clock.sleeps == []


def test_farnell_exhausted_429_is_a_429_error_never_not_found(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(429, {}, {})] * 2
    with pytest.raises(SupplierError) as err:
        FarnellAdapter().search("M4")
    assert err.value.status == 429
    assert len(server.requests) == 2


@pytest.mark.parametrize("status", [401, 403])
def test_farnell_auth_failure_keeps_status_and_is_not_retried(
    server: Server, limiter: Limiter, clock: Clock, status: int
) -> None:
    server.script = [(status, {}, {"error": "k"})]
    with pytest.raises(SupplierError) as err:
        FarnellAdapter().search("M4")
    assert (err.value.stage, err.value.status) == ("search", status)
    assert len(server.requests) == 1 and clock.sleeps == []


def test_farnell_transport_error_is_retried(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(0, {}, httpx.ConnectTimeout("slow")), (200, {}, _FARNELL_OK)]
    assert FarnellAdapter().search("M4") == []
    assert len(clock.sleeps) == 1


# ── Mouser ─────────────────────────────────────────────────────────────────


def test_mouser_429_retry_after_is_the_sleep_and_token_per_attempt(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(429, {"Retry-After": "4"}, {}), (200, {}, _MOUSER_OK)]
    assert MouserAdapter().search("M4") == []
    assert clock.sleeps == [4.0]
    assert limiter.requires == ["mouser"] * 2
    assert server.requests[0].method == "POST"
    server.ua()


def test_mouser_exhausted_429_and_404_and_403(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(429, {}, {})] * 2
    with pytest.raises(SupplierError) as err:
        MouserAdapter().search("M4")
    assert err.value.status == 429
    server.script = [(404, {}, {})]
    with pytest.raises(SupplierError) as err:
        MouserAdapter().search("M4")
    assert err.value.status == 404
    server.script = [(403, {}, {"error": "k"})]
    with pytest.raises(SupplierError) as err:
        MouserAdapter().search("M4")
    assert (err.value.stage, err.value.status) == ("search", 403)


# ── DigiKey ────────────────────────────────────────────────────────────────


def test_digikey_search_429_retry_after_and_token_per_attempt(
    server: Server, limiter: Limiter, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(DigiKeyAdapter, "_access_token", lambda self: "tok")
    server.script = [(429, {"Retry-After": "3"}, {}), (200, {}, _DK_SEARCH_OK)]
    assert DigiKeyAdapter().search("M4") == []
    assert clock.sleeps == [3.0]
    assert limiter.requires == ["digikey"] * 2
    server.ua()
    assert server.requests[0].headers["authorization"] == "Bearer tok"


def test_digikey_oauth_token_post_is_retried_with_polite_ua(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [
        (503, {"Retry-After": "2"}, {}),
        (200, {}, _DK_TOKEN),
        (200, {}, _DK_SEARCH_OK),
    ]
    assert DigiKeyAdapter().search("M4") == []
    assert str(server.requests[0].url).endswith("/v1/oauth2/token")
    assert str(server.requests[1].url).endswith("/v1/oauth2/token")
    assert clock.sleeps == [2.0]
    server.ua(0)
    server.ua(2)


@pytest.mark.parametrize("status", [401, 403])
def test_digikey_token_auth_failure_keeps_mapped_error(
    server: Server, limiter: Limiter, clock: Clock, status: int
) -> None:
    server.script = [(status, {}, {"error": "bad"})]
    with pytest.raises(SupplierError) as err:
        DigiKeyAdapter().search("M4")
    assert (err.value.stage, err.value.status) == ("token", status)
    assert len(server.requests) == 1 and clock.sleeps == []


def test_digikey_404_not_retried_and_exhausted_429_never_not_found(
    server: Server, limiter: Limiter, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(DigiKeyAdapter, "_access_token", lambda self: "tok")
    server.script = [(404, {}, {"detail": "none"})]
    with pytest.raises(SupplierError) as err:
        DigiKeyAdapter().search("M4")
    assert err.value.status == 404 and len(server.requests) == 1
    server.script = [(429, {}, {})] * 2
    with pytest.raises(SupplierError) as err:
        DigiKeyAdapter().search("M4")
    assert err.value.status == 429


def test_digikey_categories_ride_the_core(
    server: Server, limiter: Limiter, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(DigiKeyAdapter, "_access_token", lambda self: "tok")
    server.script = [(429, {"Retry-After": "1"}, {}), (200, {}, {"Categories": []})]
    assert DigiKeyAdapter().categories().categories == []
    assert clock.sleeps == [1.0]


# ── OpenRouter catalogue fetches ───────────────────────────────────────────


def test_openrouter_models_fetch_retries_429_with_retry_after(
    server: Server, limiter: Limiter, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def direct(client: httpx.Client, url: str, **kw: Any) -> httpx.Response:
        return client.get(url, **kw)

    monkeypatch.setattr("precis.utils.safe_fetch.safe_get", direct)
    real = httpx.Client

    def factory(**kw: Any) -> httpx.Client:
        kw["transport"] = httpx.MockTransport(server)
        return real(**kw)

    monkeypatch.setattr(httpx, "Client", factory)
    server.script = [
        (429, {"Retry-After": "5"}, {}),
        (200, {}, {"data": [{"id": "acme/model-x"}]}),
    ]
    out = llm_reconcile.fetch_openrouter_models()
    assert out is not None and len(out) == 1
    assert clock.sleeps == [5.0]
    assert limiter.requires == ["host:openrouter.ai"] * 2
    server.ua()


# ── /secrets probes ────────────────────────────────────────────────────────


def _store() -> Any:
    return cast("Any", object())


def _install_probes(
    monkeypatch: pytest.MonkeyPatch, groups: list[str], probe: Any
) -> None:
    monkeypatch.setattr(
        secret_status, "_PROBE_GROUP_MEMBERS", {g: (g,) for g in groups}
    )
    monkeypatch.setattr(secret_status, "is_available", lambda n, store=None: True)
    monkeypatch.setattr(secret_status, "get_secret", lambda n, store=None: "v-" + n)
    for g in groups:
        monkeypatch.setitem(secret_status._PROBES, g, probe)


def test_probe_concurrency_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    live = 0
    peak = 0

    async def probe(
        client: httpx.AsyncClient, values: dict[str, str]
    ) -> secret_status.CheckResult:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        live -= 1
        return secret_status.CheckResult("ok", "verified")

    groups = [f"g{i}" for i in range(8)]
    _install_probes(monkeypatch, groups, probe)
    results = asyncio.run(secret_status.run_checks(_store()))
    assert len(results) == 8
    assert secret_status._MAX_CONCURRENT_PROBES == 3
    assert 1 < peak <= 3


def test_force_within_min_interval_reuses_the_result_then_reprobes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def probe(
        client: httpx.AsyncClient, values: dict[str, str]
    ) -> secret_status.CheckResult:
        calls.append("x")
        return secret_status.CheckResult("ok", f"verified {len(calls)}")

    _install_probes(monkeypatch, ["anthropic"], probe)
    now = [1000.0]
    monkeypatch.setattr(secret_status, "_clock", lambda: now[0])
    monkeypatch.setattr(secret_status, "_last", {})
    monkeypatch.setattr(secret_status, "_checked_at", None)

    async def scenario() -> None:
        first = dict(await secret_status.get_results(_store(), force=True))
        now[0] += 10.0  # a force storm inside 30 s
        for _ in range(5):
            again = await secret_status.get_results(_store(), force=True)
            assert again == first
        assert len(calls) == 1
        now[0] += 25.0  # 35 s after the real probe
        later = await secret_status.get_results(_store(), force=True)
        assert len(calls) == 2
        assert later["anthropic"].detail == "verified 2"

    asyncio.run(scenario())


def test_changed_credential_bypasses_the_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def probe(
        client: httpx.AsyncClient, values: dict[str, str]
    ) -> secret_status.CheckResult:
        calls.append(values["anthropic"])
        return secret_status.CheckResult("ok", "verified")

    _install_probes(monkeypatch, ["anthropic"], probe)
    monkeypatch.setattr(secret_status, "_clock", lambda: 1000.0)
    asyncio.run(secret_status.run_checks(_store()))
    monkeypatch.setattr(secret_status, "get_secret", lambda n, store=None: "new")
    asyncio.run(secret_status.run_checks(_store()))
    assert calls == ["v-anthropic", "new"]


def test_probe_client_sends_an_honest_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    async def probe(
        client: httpx.AsyncClient, values: dict[str, str]
    ) -> secret_status.CheckResult:
        seen.append(client.headers["user-agent"])
        return secret_status.CheckResult("ok", "verified")

    _install_probes(monkeypatch, ["anthropic"], probe)
    asyncio.run(secret_status.run_checks(_store()))
    assert seen and check_user_agent(seen[0]) == seen[0]


# ── review fixes: detail kept, no fake 503, honest checked_at ──────────────


def test_digikey_403_detail_is_kept_and_no_key_leaks(
    server: Server,
    limiter: Limiter,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from precis.supply.digikey import _error_detail

    monkeypatch.setattr(DigiKeyAdapter, "_access_token", lambda self: "tok-secret")
    body = {"detail": "Parametric Filtering Not Supported", "error": "tok-secret"}
    server.script = [(403, {}, body)]
    with pytest.raises(SupplierError) as err:
        DigiKeyAdapter().search("M4")
    expected = _error_detail(httpx.Response(403, json=body))
    assert expected == "Parametric Filtering Not Supported"
    assert err.value.detail == expected
    assert str(err.value) == "HTTP 403 at search"
    assert "tok-secret" not in str(err.value) + err.value.detail + caplog.text


def test_digikey_exhausted_429_keeps_the_vendor_detail(
    server: Server, limiter: Limiter, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(DigiKeyAdapter, "_access_token", lambda self: "tok")
    server.script = [(429, {}, {"detail": "slow down"})] * 2
    with pytest.raises(SupplierError) as err:
        DigiKeyAdapter().search("M4")
    assert (err.value.status, err.value.detail) == (429, "slow down")


def test_url_key_never_reaches_message_or_log(
    server: Server,
    limiter: Limiter,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr("precis.supply.farnell.get_secret", lambda name: "sekrit-key")
    server.script = [(403, {}, {"error": "sekrit-key"})]
    with pytest.raises(SupplierError) as err:
        FarnellAdapter().search("M4")
    assert "sekrit-key" not in str(err.value) + err.value.detail + caplog.text


def test_surviving_transport_error_is_not_posed_as_http_503(
    server: Server, limiter: Limiter, clock: Clock
) -> None:
    server.script = [(0, {}, httpx.ConnectTimeout("slow"))] * 2
    with pytest.raises(SupplierError) as err:
        FarnellAdapter().search("M4")
    assert err.value.status is None
    assert str(err.value) == "transport error at search"
    assert "503" not in str(err.value)


def test_open_breaker_reads_as_paused_not_http_503(
    server: Server,
    limiter: Limiter,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        http.POLICIES, "farnell", http.Policy(breaker_threshold=1, provider="farnell")
    )
    server.script = [(401, {}, {})]
    with pytest.raises(SupplierError) as first:
        FarnellAdapter().search("M4")
    assert first.value.status == 401
    with pytest.raises(SupplierError) as err:
        FarnellAdapter().search("M4")
    assert err.value.status is None
    assert str(err.value) == "temporarily paused after repeated failures at search"
    assert len(server.requests) == 1


def test_cached_force_does_not_restamp_checked_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def probe(
        client: httpx.AsyncClient, values: dict[str, str]
    ) -> secret_status.CheckResult:
        return secret_status.CheckResult("ok", "verified")

    _install_probes(monkeypatch, ["anthropic"], probe)
    now = [1000.0]
    monkeypatch.setattr(secret_status, "_clock", lambda: now[0])
    monkeypatch.setattr(secret_status, "_last", {})
    monkeypatch.setattr(secret_status, "_checked_at", None)

    async def scenario() -> None:
        await secret_status.get_results(_store(), force=True)
        stamped = secret_status.checked_at()
        assert stamped is not None
        now[0] += 10.0
        await secret_status.get_results(_store(), force=True)
        assert secret_status.checked_at() == stamped
        now[0] += 25.0
        await secret_status.get_results(_store(), force=True)
        later = secret_status.checked_at()
        assert later is not None and later > stamped

    asyncio.run(scenario())
