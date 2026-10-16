"""polite_fetch: the OPSIN/CIR legs' limiter, backoff, breaker, cache and UA.

OPSIN is mocked at ``safe_get``; time is a fake clock. No network.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from precis.handlers import _chem_resolve as cr
from precis.pcb import _http as vendor_http

URL = "https://www.ebi.ac.uk/opsin/ws/1%2C2-dichloroethane.json"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, secs: float) -> None:
        self.sleeps.append(secs)
        self.t += secs


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> Any:
    clock = Clock()
    vendor_http.reset_circuit()
    cr.clear_answer_cache()
    real = vendor_http.with_backoff

    def with_fake_time(send: Callable[[], httpx.Response], **kw: Any) -> Any:
        return real(send, sleep=clock.sleep, now=clock.now, **kw)

    monkeypatch.setattr(vendor_http, "with_backoff", with_fake_time)

    class Rig:
        responses: list[httpx.Response | Exception] = []
        calls: list[dict[str, Any]] = []
        acquired: list[str] = []

    rig = Rig()
    rig.clock = clock  # type: ignore[attr-defined]
    rig.responses = []
    rig.calls = []
    rig.acquired = []

    def fake_get(client: httpx.Client, url: str, /, **kw: Any) -> httpx.Response:
        rig.calls.append({"url": url, "ua": client.headers.get("User-Agent")})
        got = rig.responses.pop(0)
        if isinstance(got, Exception):
            raise got
        return got

    monkeypatch.setattr("precis.utils.safe_fetch.safe_get", fake_get)

    def fake_acquire(provider: str, **kw: Any) -> bool:
        rig.acquired.append(provider)
        return True

    monkeypatch.setattr("precis.utils.rate_limit.acquire", fake_acquire)
    yield rig
    vendor_http.reset_circuit()
    cr.clear_answer_cache()


def _resp(status: int, text: str = "{}", **headers: str) -> httpx.Response:
    return httpx.Response(status, text=text, headers=headers)


def test_user_agent_identifies_precis_without_a_personal_address(rig: Any) -> None:
    rig.responses = [_resp(200)]
    cr.polite_fetch("OPSIN", cr.OPSIN_POLICY)(URL)
    ua = rig.calls[0]["ua"]
    assert ua.startswith("precis-mcp/") and "https://github.com/" in ua
    assert "@" not in ua


def test_calls_are_spaced_by_the_policy_interval(rig: Any) -> None:
    rig.responses = [_resp(200), _resp(200), _resp(404)]
    fetch = cr.polite_fetch("OPSIN", cr.OPSIN_POLICY)
    for n in ("a", "b", "c"):
        fetch(URL.replace("1%2C2-dichloroethane", n))
    # Fake time only moves when with_backoff sleeps: two gaps >= 1 s.
    assert len(rig.clock.sleeps) == 2
    assert all(s >= 1.0 - 1e-9 for s in rig.clock.sleeps)
    assert rig.acquired == ["opsin"] * 3


def test_429_retry_after_is_honoured_and_capped(rig: Any) -> None:
    rig.responses = [_resp(429, **{"Retry-After": "120"}), _resp(200, "ok")]
    status, body = cr.polite_fetch("OPSIN", cr.OPSIN_POLICY)(URL)
    assert (status, body) == (200, "ok")
    # The server asked for 120 s; the policy cap (4 s) bounds the wait.
    assert max(rig.clock.sleeps) == cr.OPSIN_POLICY.cap


def test_503_backs_off_with_jitter_inside_the_cap(rig: Any) -> None:
    rig.responses = [_resp(503), _resp(503), _resp(200)]
    status, _ = cr.polite_fetch("OPSIN", cr.OPSIN_POLICY)(URL)
    assert status == 200
    assert all(0.0 <= s <= cr.OPSIN_POLICY.cap for s in rig.clock.sleeps)


def test_403_is_a_fault_not_a_miss(rig: Any) -> None:
    rig.responses = [_resp(403)]
    fetch = cr.polite_fetch("OPSIN", cr.OPSIN_POLICY)
    with pytest.raises(cr._Fault):
        cr._tier_opsin("aspirin", fetch, [])
    assert len(rig.calls) == 1  # never retried


def test_one_flaky_name_does_not_park_opsin_for_everyone(rig: Any) -> None:
    rig.responses = [_resp(503)] * 3 + [_resp(200, "ok")]
    fetch = cr.polite_fetch("OPSIN", cr.OPSIN_POLICY)
    with pytest.raises(vendor_http.VendorUnavailable):
        fetch(URL)
    assert fetch(URL + "?other") == (200, "ok")


def test_breaker_opens_and_later_calls_fail_without_network(rig: Any) -> None:
    rig.responses = [httpx.ConnectTimeout("tls")] * 5
    fetch = cr.polite_fetch("OPSIN", cr.OPSIN_POLICY)
    with pytest.raises(vendor_http.VendorUnavailable):
        fetch(URL)
    with pytest.raises(vendor_http.VendorUnavailable):
        fetch(URL + "?y")
    n_calls = len(rig.calls)
    with pytest.raises(vendor_http.VendorUnavailable, match="circuit"):
        fetch(URL + "?x")
    assert len(rig.calls) == n_calls  # parked: no request went out


def test_resolver_reports_breaker_failure_as_fault_not_miss(rig: Any) -> None:
    rig.responses = [httpx.ConnectTimeout("tls")] * 3
    fetch = cr.polite_fetch("OPSIN", cr.OPSIN_POLICY)
    with pytest.raises(cr._Fault):
        cr._tier_opsin("aspirin", fetch, [])


def test_cache_serves_hits_and_negative_answers(rig: Any) -> None:
    rig.responses = [_resp(200, "hit"), _resp(404, "nope")]
    fetch = cr.polite_fetch("OPSIN", cr.OPSIN_POLICY, cache=True)
    miss_url = URL.replace("1%2C2", "9%2C9")
    assert fetch(URL) == (200, "hit")
    assert fetch(miss_url) == (404, "nope")
    assert fetch(URL) == (200, "hit")
    assert fetch(miss_url) == (404, "nope")
    assert len(rig.calls) == 2


def test_cache_entries_expire(rig: Any) -> None:
    now = [0.0]
    rig.responses = [_resp(404, "a"), _resp(404, "b")]
    fetch = cr.polite_fetch("OPSIN", cr.OPSIN_POLICY, cache=True, clock=lambda: now[0])
    assert fetch(URL) == (404, "a")
    now[0] = cr._OPSIN_MISS_TTL_S + 1
    assert fetch(URL) == (404, "b")


def test_server_errors_are_never_cached(rig: Any) -> None:
    rig.responses = [_resp(500)] * 3 + [_resp(200, "fine")]
    fetch = cr.polite_fetch("OPSIN", cr.OPSIN_POLICY, cache=True)
    with pytest.raises(vendor_http.VendorUnavailable):
        fetch(URL)
    vendor_http.reset_circuit()
    assert fetch(URL) == (200, "fine")


def test_partial_sweep_names_the_shortfall(rig: Any) -> None:
    """3 probes answered, then OPSIN dies: the note says so and flags the
    list as incomplete; answered readings are kept."""
    from tests.test_chem_resolve import (
        Replay,
        _locant_probe_routes,
        _opsin,
    )

    routes = _locant_probe_routes()
    routes[_opsin("1,3-Dichloroethane")] = TimeoutError("read timed out")
    res = cr.resolve("dichloroethane", Replay(routes), cir_enabled=False)
    assert [c.name for c in res.candidates][:2] == [
        "1,1-Dichloroethane",
        "1,2-Dichloroethane",
    ]
    note = next(n for n in res.notes if "locant probes answered" in n)
    assert "2 of 3 locant probes answered" in note
    assert "INCOMPLETE" in note
    assert ("OPSIN-locants", "1 readings (2 of 3 probes answered)") in res.tiers
