"""The politeness core in ``precis.utils.http`` (gr493222) and the limiter
pieces it leans on (``rate_limit`` ``seed_row=``/``park``/fail-open warning).

Mocks only: ``sleep``/``now``/``uniform``/``rng``/``wall`` are injected, the
limiter is observed through recorders patched over ``rate_limit.require`` /
``rate_limit.park`` (http.py imports ``rate_limit`` lazily inside its
helpers, so patching the module attribute is seen), and the DB-facing tests
use a fake connection. No network, no real sleeping.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from precis.liveness import request_drain
from precis.utils import http, rate_limit
from precis.utils.http import (
    DEFAULT_HOST_ROW,
    DEFAULT_POLICY,
    FALLBACK_CONTACT,
    POLICIES,
    Policy,
    VendorError,
    VendorUnavailable,
    call_with_backoff,
    check_user_agent,
    netloc_provider,
    policy_for,
    polite_user_agent,
    reset_circuit,
    retry_after_seconds,
    with_backoff,
)

# ── fixtures and helpers ────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_circuits():
    reset_circuit()
    yield
    reset_circuit()


@dataclass
class Clock:
    t: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


class StubRng:
    """``random.Random`` stand-in: ``uniform(lo, hi)`` returns ``hi * frac``
    and records the arguments."""

    def __init__(self, frac: float = 0.5) -> None:
        self.frac = frac
        self.calls: list[tuple[float, float]] = []

    def uniform(self, lo: float, hi: float) -> float:
        self.calls.append((lo, hi))
        return hi * self.frac


@dataclass
class Limiter:
    requires: list[dict[str, Any]] = field(default_factory=list)
    parks: list[tuple[str, float]] = field(default_factory=list)
    refuse: bool = False


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
        rec.requires.append(
            {"provider": provider, "max_wait_s": max_wait_s, "seed_row": seed_row}
        )
        if rec.refuse:
            raise rate_limit.RateLimited(provider)

    def fake_park(provider: str, seconds: float) -> None:
        rec.parks.append((provider, seconds))

    monkeypatch.setattr("precis.utils.rate_limit.require", fake_require)
    monkeypatch.setattr("precis.utils.rate_limit.park", fake_park)
    return rec


def _resp(status: int, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, headers=headers or {})


def _scripted(*responses: httpx.Response | Exception):
    """A ``send`` thunk replaying ``responses`` (exceptions are raised);
    ``.calls`` counts invocations."""
    queue = list(responses)

    class _Send:
        calls = 0

        def __call__(self) -> httpx.Response:
            self.calls += 1
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    return _Send()


def _status_error(status: int, headers: dict[str, str] | None = None):
    request = httpx.Request("GET", "https://example.test/x")
    response = httpx.Response(status, headers=headers or {}, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


def _half(lo: float, hi: float) -> float:
    return hi * 0.5


def _failing(exc: Exception):
    class _Fn:
        calls = 0

        def __call__(self) -> Any:
            self.calls += 1
            raise exc

    return _Fn()


# ── 1. per-service policy ───────────────────────────────────────────────────


@pytest.mark.parametrize("key", sorted(POLICIES))
def test_per_service_policy_one_token_per_attempt_and_full_jitter(key, limiter):
    policy = POLICIES[key]
    clock = Clock()
    rng = StubRng()
    send = _scripted(_resp(503), _resp(200))

    resp = with_backoff(
        send,
        service=key,
        sleep=clock.sleep,
        now=clock.now,
        rng=rng,
    )

    assert resp.status_code == 200
    assert send.calls == 2
    backoff_draw = (0.0, min(policy.cap, policy.base))
    if policy.provider is None:
        # easyeda: no shared limiter row -> no token taken.
        assert limiter.requires == []
    else:
        assert [r["provider"] for r in limiter.requires] == [policy.provider] * 2
    if policy.min_interval > 0:
        # Spacing before the first attempt, then one backoff sleep.
        assert rng.calls == [(0.0, policy.spacing_jitter), backoff_draw]
        assert clock.sleeps == [
            policy.min_interval + policy.spacing_jitter * 0.5,
            backoff_draw[1] * 0.5,
        ]
    else:
        assert rng.calls == [backoff_draw]
        assert clock.sleeps == [backoff_draw[1] * 0.5]


def test_policy_for_unknown_service_is_default():
    assert policy_for("no-such-service") is DEFAULT_POLICY
    assert policy_for("s2") is POLICIES["s2"]


# ── 2. Retry-After ──────────────────────────────────────────────────────────


def test_retry_after_seconds_honoured_and_parked(limiter):
    policy = Policy(attempts=3, cap=60.0, provider="p", breaker_threshold=100)
    clock = Clock()
    send = _scripted(_resp(429, {"Retry-After": "7"}), _resp(200))
    resp = with_backoff(
        send, service="ra-sec", policy=policy, sleep=clock.sleep, now=clock.now
    )
    assert resp.status_code == 200
    assert clock.sleeps == [7.0]
    assert limiter.parks == [("p", 7.0)]


def test_retry_after_http_date_honoured_with_injected_wall(limiter):
    policy = Policy(attempts=3, cap=60.0, provider="p", breaker_threshold=100)
    clock = Clock()
    wall_now = datetime(2026, 10, 21, 7, 27, 50, tzinfo=UTC)
    send = _scripted(
        _resp(503, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), _resp(200)
    )
    with_backoff(
        send,
        service="ra-date",
        policy=policy,
        sleep=clock.sleep,
        now=clock.now,
        wall=lambda: wall_now,
    )
    assert clock.sleeps == [10.0]
    assert limiter.parks == [("p", 10.0)]


def test_retry_after_large_value_clamped_to_cap_for_sleep_and_900_for_park(limiter):
    policy = Policy(attempts=3, cap=5.0, provider="p", breaker_threshold=100)
    clock = Clock()
    send = _scripted(_resp(429, {"Retry-After": "99999"}), _resp(200))
    with_backoff(
        send, service="ra-big", policy=policy, sleep=clock.sleep, now=clock.now
    )
    assert clock.sleeps == [5.0]
    assert limiter.parks == [("p", 900.0)]


def test_retry_after_without_limiter_row_does_not_park(limiter):
    policy = Policy(attempts=3, cap=60.0, breaker_threshold=100)  # provider None
    clock = Clock()
    send = _scripted(_resp(429, {"Retry-After": "4"}), _resp(200))
    with_backoff(
        send, service="ra-norow", policy=policy, sleep=clock.sleep, now=clock.now
    )
    assert clock.sleeps == [4.0]
    assert limiter.parks == []


def test_call_with_backoff_honours_retry_after_from_exception_response(limiter):
    policy = Policy(attempts=3, cap=60.0, provider="p", breaker_threshold=100)
    clock = Clock()
    results: list[Any] = [_status_error(429, {"Retry-After": "6"}), "ok"]

    def fn() -> str:
        item = results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    assert (
        call_with_backoff(
            fn, service="ra-call", policy=policy, sleep=clock.sleep, now=clock.now
        )
        == "ok"
    )
    assert clock.sleeps == [6.0]
    assert limiter.parks == [("p", 6.0)]


@pytest.mark.parametrize("raw", ["", "soon", "nan"])
def test_retry_after_seconds_unparseable_is_none(raw):
    assert retry_after_seconds({"Retry-After": raw}) is None


def test_retry_after_seconds_absent_headers_is_none():
    assert retry_after_seconds(None) is None
    assert retry_after_seconds({}) is None


def test_retry_after_seconds_past_date_floors_at_zero():
    wall_now = datetime(2026, 10, 21, 7, 30, 0, tzinfo=UTC)
    got = retry_after_seconds(
        {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, wall=lambda: wall_now
    )
    assert got == 0.0


# ── 3. classification ───────────────────────────────────────────────────────


@pytest.mark.parametrize("status", [400, 404])
def test_non_retryable_client_status_is_returned_without_retry(status, limiter):
    clock = Clock()
    send = _scripted(_resp(status))
    resp = with_backoff(send, service=f"c-{status}", sleep=clock.sleep, now=clock.now)
    assert resp.status_code == status
    assert send.calls == 1
    assert clock.sleeps == []


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_raises_vendor_error_never_retried(status, limiter):
    clock = Clock()
    send = _scripted(_resp(status))
    with pytest.raises(VendorError) as ei:
        with_backoff(send, service=f"f-{status}", sleep=clock.sleep, now=clock.now)
    assert not isinstance(ei.value, VendorUnavailable)
    assert ei.value.status == status
    assert send.calls == 1
    assert clock.sleeps == []


def test_call_with_backoff_reraises_type_error_on_first_attempt(limiter):
    clock = Clock()
    exc = TypeError("bug")
    fn = _failing(exc)
    with pytest.raises(TypeError) as ei:
        call_with_backoff(fn, service="cls-type", sleep=clock.sleep, now=clock.now)
    assert ei.value is exc
    assert fn.calls == 1
    assert clock.sleeps == []


def test_call_with_backoff_reraises_404_on_first_attempt(limiter):
    clock = Clock()
    exc = _status_error(404)
    fn = _failing(exc)
    with pytest.raises(httpx.HTTPStatusError) as ei:
        call_with_backoff(fn, service="cls-404", sleep=clock.sleep, now=clock.now)
    assert ei.value is exc
    assert fn.calls == 1


@pytest.mark.parametrize(
    "make",
    [
        lambda: httpx.ConnectError("down"),
        lambda: _status_error(503),
    ],
    ids=["connect-error", "http-503"],
)
def test_call_with_backoff_retries_transient_then_succeeds(make, limiter):
    clock = Clock()
    results: list[Any] = [make(), "ok"]

    def fn() -> str:
        item = results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    assert (
        call_with_backoff(
            fn,
            service="cls-retry",
            sleep=clock.sleep,
            now=clock.now,
            uniform=_half,
        )
        == "ok"
    )
    assert len(clock.sleeps) == 1


def test_call_with_backoff_reraises_original_after_attempts_exhausted(limiter):
    policy = Policy(attempts=3, breaker_threshold=0)
    clock = Clock()
    exc = _status_error(503)
    fn = _failing(exc)
    with pytest.raises(httpx.HTTPStatusError) as ei:
        call_with_backoff(
            fn,
            service="cls-exhaust",
            policy=policy,
            sleep=clock.sleep,
            now=clock.now,
            uniform=_half,
        )
    assert ei.value is exc
    assert fn.calls == 3
    assert len(clock.sleeps) == 2


def test_with_backoff_exhaustion_is_vendor_unavailable(limiter):
    policy = Policy(attempts=2, breaker_threshold=0)
    clock = Clock()
    send = _scripted(_resp(503), _resp(502))
    with pytest.raises(VendorUnavailable) as ei:
        with_backoff(
            send,
            service="cls-exhaust-resp",
            policy=policy,
            sleep=clock.sleep,
            now=clock.now,
            rng=StubRng(),
        )
    assert ei.value.status == 502
    assert send.calls == 2


# ── 4. idempotent=False ─────────────────────────────────────────────────────


def test_non_idempotent_call_retries_connect_error(limiter):
    clock = Clock()
    results: list[Any] = [httpx.ConnectError("refused"), "ok"]

    def fn() -> str:
        item = results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    out = call_with_backoff(
        fn,
        service="ni-connect",
        idempotent=False,
        sleep=clock.sleep,
        now=clock.now,
        uniform=_half,
    )
    assert out == "ok"


def test_non_idempotent_call_does_not_retry_read_timeout(limiter):
    clock = Clock()
    exc = httpx.ReadTimeout("slow")
    fn = _failing(exc)
    with pytest.raises(httpx.ReadTimeout) as ei:
        call_with_backoff(
            fn,
            service="ni-read",
            idempotent=False,
            sleep=clock.sleep,
            now=clock.now,
        )
    assert ei.value is exc
    assert fn.calls == 1
    assert clock.sleeps == []


def test_non_idempotent_call_retries_429(limiter):
    clock = Clock()
    results: list[Any] = [_status_error(429), "ok"]

    def fn() -> str:
        item = results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    out = call_with_backoff(
        fn,
        service="ni-429",
        idempotent=False,
        sleep=clock.sleep,
        now=clock.now,
        uniform=_half,
    )
    assert out == "ok"


def test_non_idempotent_call_does_not_retry_503(limiter):
    clock = Clock()
    exc = _status_error(503)
    fn = _failing(exc)
    with pytest.raises(httpx.HTTPStatusError) as ei:
        call_with_backoff(
            fn,
            service="ni-503",
            idempotent=False,
            sleep=clock.sleep,
            now=clock.now,
        )
    assert ei.value is exc
    assert fn.calls == 1


def test_non_idempotent_with_backoff_503_raises_vendor_error(limiter):
    clock = Clock()
    send = _scripted(_resp(503))
    with pytest.raises(VendorError) as ei:
        with_backoff(
            send,
            service="ni-send-503",
            idempotent=False,
            sleep=clock.sleep,
            now=clock.now,
        )
    assert not isinstance(ei.value, VendorUnavailable)
    assert send.calls == 1


# ── 5. limiter resolution ───────────────────────────────────────────────────


def test_explicit_provider_none_disables_limiter(limiter):
    clock = Clock()
    policy = Policy(provider="p")
    send = _scripted(_resp(200))
    with_backoff(
        send,
        service="lim-off",
        policy=policy,
        provider=None,
        url="https://api.example.org/x",
        sleep=clock.sleep,
        now=clock.now,
    )
    assert limiter.requires == []


def test_url_host_row_is_seeded_with_default_host_row(limiter):
    clock = Clock()
    send = _scripted(_resp(200))
    with_backoff(
        send,
        service="lim-host",
        policy=DEFAULT_POLICY,
        url="https://Api.Example.org/x",
        sleep=clock.sleep,
        now=clock.now,
    )
    assert len(limiter.requires) == 1
    assert limiter.requires[0]["provider"] == "host:api.example.org"
    assert limiter.requires[0]["seed_row"] == DEFAULT_HOST_ROW
    assert limiter.requires[0]["max_wait_s"] == DEFAULT_POLICY.limiter_wait_s


def test_policy_provider_wins_over_url_and_is_not_seeded(limiter):
    clock = Clock()
    send = _scripted(_resp(200))
    with_backoff(
        send,
        service="lim-policy",
        policy=Policy(provider="crossref"),
        url="https://api.crossref.org/works",
        sleep=clock.sleep,
        now=clock.now,
    )
    assert [(r["provider"], r["seed_row"]) for r in limiter.requires] == [
        ("crossref", None)
    ]


def test_explicit_provider_wins_over_policy(limiter):
    clock = Clock()
    send = _scripted(_resp(200))
    with_backoff(
        send,
        service="lim-explicit",
        policy=Policy(provider="crossref"),
        provider="other",
        sleep=clock.sleep,
        now=clock.now,
    )
    assert [r["provider"] for r in limiter.requires] == ["other"]


def test_no_provider_and_no_url_means_no_limiter(limiter):
    clock = Clock()
    send = _scripted(_resp(200))
    with_backoff(send, service="lim-none", sleep=clock.sleep, now=clock.now)
    assert limiter.requires == []


def test_resolve_provider_direct():
    assert http._resolve_provider(None, Policy(provider="p"), "https://a.b/") == (
        None,
        None,
    )
    assert http._resolve_provider(http._UNSET, Policy(provider="p"), None) == (
        "p",
        None,
    )
    assert http._resolve_provider(http._UNSET, Policy(), "https://A.B:8080/") == (
        "host:a.b:8080",
        DEFAULT_HOST_ROW,
    )
    assert http._resolve_provider(http._UNSET, Policy(), None) == (None, None)


def test_llm_api_policy_seeds_the_metered_host_row(limiter):
    """gr507343: an LLM API host (the ``openai-tools`` policy, what
    ``openai_tools._polite`` routes OpenRouter through) is seeded with
    ``LLM_HOST_ROW`` — a wide bucket the provider's 429/Retry-After parks —
    not the 1 req/s website default; a scraped host keeps the default."""
    assert http.LLM_HOST_ROW[1] > DEFAULT_HOST_ROW[1]
    assert http._resolve_provider(
        http._UNSET, POLICIES["openai-tools"], "https://openrouter.ai/api/v1/x"
    ) == ("host:openrouter.ai", http.LLM_HOST_ROW)
    assert http._resolve_provider(
        http._UNSET, POLICIES["web-fetch"], "https://openrouter.ai/docs"
    ) == ("host:openrouter.ai", DEFAULT_HOST_ROW)
    clock = Clock()
    call_with_backoff(
        lambda: "ok",
        service="host:openrouter.ai",
        policy=POLICIES["openai-tools"],
        url="https://openrouter.ai/api/v1/chat/completions",
        sleep=clock.sleep,
        now=clock.now,
    )
    assert [(r["provider"], r["seed_row"]) for r in limiter.requires] == [
        ("host:openrouter.ai", http.LLM_HOST_ROW)
    ]


def test_netloc_provider():
    assert netloc_provider("https://Api.Example.org/x") == "host:api.example.org"
    assert netloc_provider("not a url") is None


# ── 6. RateLimited propagates ───────────────────────────────────────────────


def test_with_backoff_propagates_rate_limited_without_sending(limiter):
    limiter.refuse = True
    clock = Clock()
    send = _scripted(_resp(200))
    with pytest.raises(rate_limit.RateLimited):
        with_backoff(
            send,
            service="rl-send",
            policy=Policy(provider="p"),
            sleep=clock.sleep,
            now=clock.now,
        )
    assert send.calls == 0


def test_call_with_backoff_propagates_rate_limited_without_calling(limiter):
    limiter.refuse = True
    clock = Clock()
    fn = _failing(RuntimeError("must not run"))
    with pytest.raises(rate_limit.RateLimited):
        call_with_backoff(
            fn,
            service="rl-call",
            policy=Policy(provider="p"),
            sleep=clock.sleep,
            now=clock.now,
        )
    assert fn.calls == 0


# ── 7. breaker ──────────────────────────────────────────────────────────────


def test_call_with_backoff_breaker_opens_then_fails_fast(limiter):
    policy = Policy(attempts=5, breaker_threshold=2, breaker_cooldown=300.0)
    clock = Clock()
    exc = httpx.ConnectError("down")
    fn = _failing(exc)

    with pytest.raises(httpx.ConnectError) as ei:
        call_with_backoff(
            fn,
            service="brk",
            policy=policy,
            sleep=clock.sleep,
            now=clock.now,
            uniform=_half,
        )
    assert ei.value is exc
    assert fn.calls == 2  # breaker opened on the 2nd failure, no 3rd try

    before = fn.calls
    with pytest.raises(VendorUnavailable):
        call_with_backoff(
            fn,
            service="brk",
            policy=policy,
            sleep=clock.sleep,
            now=clock.now,
            uniform=_half,
        )
    assert fn.calls == before


def test_with_backoff_breaker_opens_and_next_call_fails_fast(limiter):
    policy = Policy(attempts=5, breaker_threshold=2)
    clock = Clock()
    send = _scripted(_resp(503), _resp(503))
    with pytest.raises(VendorUnavailable):
        with_backoff(
            send,
            service="brk-send",
            policy=policy,
            sleep=clock.sleep,
            now=clock.now,
            rng=StubRng(),
        )
    assert send.calls == 2
    again = _scripted(_resp(200))
    with pytest.raises(VendorUnavailable):
        with_backoff(
            again, service="brk-send", policy=policy, sleep=clock.sleep, now=clock.now
        )
    assert again.calls == 0


# ── 8. drain ────────────────────────────────────────────────────────────────
# tests/conftest.py's autouse ``_reset_drain_flag`` clears the drain flag
# after every test, so request_drain() here cannot leak.


def test_with_backoff_stops_after_first_failure_when_draining(limiter):
    request_drain()
    clock = Clock()
    send = _scripted(_resp(503), _resp(200))
    with pytest.raises(VendorUnavailable) as ei:
        with_backoff(
            send,
            service="drain-send",
            policy=Policy(attempts=5, breaker_threshold=100),
            sleep=clock.sleep,
            now=clock.now,
            rng=StubRng(),
        )
    assert "draining" in str(ei.value)
    assert send.calls == 1


def test_call_with_backoff_reraises_original_when_draining(limiter):
    request_drain()
    clock = Clock()
    exc = httpx.ConnectError("down")
    fn = _failing(exc)
    with pytest.raises(httpx.ConnectError) as ei:
        call_with_backoff(
            fn,
            service="drain-call",
            policy=Policy(attempts=5, breaker_threshold=100),
            sleep=clock.sleep,
            now=clock.now,
            uniform=_half,
        )
    assert ei.value is exc
    assert fn.calls == 1


# ── 9. User-Agent ───────────────────────────────────────────────────────────


def test_polite_user_agent_names_product_and_real_contact():
    ua = polite_user_agent()
    assert "precis-mcp/" in ua
    assert "mailto:" in ua
    match = re.search(r"mailto:([^\s);]+)", ua)
    assert match is not None
    address = match.group(1)
    assert "@" in address
    assert not any(m in address.lower() for m in ("noreply", "no-reply", "example."))


def test_polite_user_agent_placeholder_contact_falls_back(monkeypatch):
    monkeypatch.setattr("precis.settings.get_str", lambda *a, **k: None)
    ua = polite_user_agent("noreply@example.com")
    assert f"mailto:{FALLBACK_CONTACT}" in ua
    assert "noreply" not in ua


def test_polite_user_agent_uses_a_real_explicit_contact(monkeypatch):
    monkeypatch.setattr("precis.settings.get_str", lambda *a, **k: None)
    assert "mailto:ops@lab.org" in polite_user_agent("ops@lab.org")


@pytest.mark.parametrize(
    "ua",
    [
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        "python-requests/2.31",
        "precis-mcp/1 (+https://x; mailto:noreply@example.com)",
        "",
    ],
    ids=["browser-spoof", "library-default", "placeholder-contact", "empty"],
)
def test_check_user_agent_rejects(ua):
    with pytest.raises(ValueError):
        check_user_agent(ua)


def test_check_user_agent_accepts_honest_ua():
    ua = "precis-mcp/1 (+https://github.com/x; mailto:ops@lab.org)"
    assert check_user_agent(ua) == ua


# ── 10. rate_limit: seed_row, park, fail-open warning ───────────────────────


class _Cursor:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row

    def fetchall(self) -> list[tuple[Any, ...]]:
        return [self._row] if self._row is not None else []


class _FakeConn:
    """Records every ``execute``; ``fetchone`` results come from per-SQL
    scripted queues (missing/exhausted queue -> ``None``). ``seed`` scripts
    the ``RETURNING`` row of each seed insert (``None`` = cap hit / already
    there), ``evict`` the row an eviction retires, ``occupancy`` the
    ``(count, idle)`` the cap-full check reads."""

    closed = False

    def __init__(self, consume=(), state=(), seed=(), evict=(), occupancy=()) -> None:
        self.executed: list[tuple[str, Any]] = []
        self._consume = list(consume)
        self._state = list(state)
        self._seed = list(seed)
        self._evict = list(evict)
        self._occupancy = list(occupancy)

    def execute(self, sql: str, params: Any = None) -> _Cursor:
        self.executed.append((sql, params))
        if sql == rate_limit._CONSUME_SQL:
            return _Cursor(self._consume.pop(0) if self._consume else None)
        if sql == rate_limit._SELECT_STATE_SQL:
            return _Cursor(self._state.pop(0) if self._state else None)
        if sql == rate_limit._SEED_SQL:
            return _Cursor(self._seed.pop(0) if self._seed else None)
        if sql == rate_limit._EVICT_IDLE_HOST_SQL:
            return _Cursor(self._evict.pop(0) if self._evict else None)
        if sql == rate_limit.HOST_ROW_OCCUPANCY_SQL:
            return _Cursor(self._occupancy.pop(0) if self._occupancy else None)
        return _Cursor(None)


@pytest.fixture
def rl_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PRECIS_RATE_LIMIT", raising=False)
    monkeypatch.setattr(rate_limit, "_fail_open_warned", {})
    monkeypatch.setattr(rate_limit, "_silent_warned", {})


def test_acquire_seeds_missing_row_once_then_reconsumes(monkeypatch, rl_env):
    # consume: refused, then granted after the seed; state: row missing.
    conn = _FakeConn(consume=[None, (0.0,)], state=[None], seed=[("host:a.b",)])
    monkeypatch.setattr(rate_limit, "_get_conn", lambda: conn)

    assert rate_limit.acquire("host:a.b", seed_row=(1, 1.0)) is True

    sqls = [s for s, _ in conn.executed]
    assert sqls == [
        rate_limit._CONSUME_SQL,
        rate_limit._SELECT_STATE_SQL,
        rate_limit._SEED_SQL,
        rate_limit._CONSUME_SQL,
    ]
    assert "ON CONFLICT (provider) DO NOTHING" in rate_limit._SEED_SQL
    assert conn.executed[2][1] == (
        "host:a.b",
        1,
        1.0,
        1,
        rate_limit.MAX_SEEDED_HOST_ROWS,
    )


def test_seed_at_the_cap_evicts_idle_host_rows_then_seeds(monkeypatch, rl_env, caplog):
    """gr507345: the first seed inserts nothing (cap hit) → the longest-idle
    ``host:`` rows past the idle threshold are evicted → the seed is retried
    and lands → the consume is granted. The new host IS paced."""
    conn = _FakeConn(
        consume=[None, (0.0,)],
        state=[None],
        seed=[None, ("host:new.example",)],
        evict=[("host:stale.example",)],
    )
    monkeypatch.setattr(rate_limit, "_get_conn", lambda: conn)

    with caplog.at_level(logging.INFO, logger=rate_limit.log.name):
        assert rate_limit.acquire("host:new.example", seed_row=(1, 1.0)) is True

    sqls = [s for s, _ in conn.executed]
    assert sqls == [
        rate_limit._CONSUME_SQL,
        rate_limit._SELECT_STATE_SQL,
        rate_limit._SEED_SQL,
        rate_limit._EVICT_IDLE_HOST_SQL,
        rate_limit._SEED_SQL,
        rate_limit._CONSUME_SQL,
    ]
    assert conn.executed[3][1] == (
        float(rate_limit.HOST_ROW_IDLE_EVICT_S),
        rate_limit._EVICT_BATCH,
    )
    assert any("evicted 1 row(s)" in r.getMessage() for r in caplog.records)
    assert not any(r.levelno == logging.WARNING for r in caplog.records)


def test_seed_at_a_cap_full_of_live_rows_warns_once_and_fails_open(
    monkeypatch, rl_env, caplog
):
    """Nothing idle to evict: the host runs unpaced (fail-open, as before)
    but the cap-full state is now named in one WARNING per hour, not
    silent — and no DELETE is attempted on the live rows."""
    cap = rate_limit.MAX_SEEDED_HOST_ROWS
    conn = _FakeConn(
        consume=[None, None, None],
        state=[None, None, None],
        seed=[None, None],
        evict=[],
        occupancy=[(cap, 0), (cap, 0)],
    )
    monkeypatch.setattr(rate_limit, "_get_conn", lambda: conn)

    with caplog.at_level(logging.WARNING, logger=rate_limit.log.name):
        assert rate_limit.acquire("host:new.example", seed_row=(1, 1.0)) is True
        assert rate_limit.acquire("host:new.example", seed_row=(1, 1.0)) is True

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "host-row cap full" in warnings[0] and str(cap) in warnings[0]
    evicts = [s for s, _ in conn.executed if s == rate_limit._EVICT_IDLE_HOST_SQL]
    assert len(evicts) == 2  # tried, found nothing idle, deleted nothing
    assert rate_limit.HOST_ROW_IDLE_EVICT_S >= 3600  # a park (<=15 min) has lapsed


def test_acquire_missing_row_without_seed_fails_open_without_insert(
    monkeypatch, rl_env
):
    conn = _FakeConn(consume=[None], state=[None])
    monkeypatch.setattr(rate_limit, "_get_conn", lambda: conn)
    assert rate_limit.acquire("nope") is True
    assert rate_limit._SEED_SQL not in [s for s, _ in conn.executed]


def test_acquire_seeds_only_once_if_row_still_missing(monkeypatch, rl_env):
    conn = _FakeConn(consume=[None, None], state=[None, None])
    monkeypatch.setattr(rate_limit, "_get_conn", lambda: conn)
    assert rate_limit.acquire("ghost", seed_row=(1, 1.0)) is True
    seeds = [s for s, _ in conn.executed if s == rate_limit._SEED_SQL]
    assert len(seeds) == 1


def test_park_executes_update_with_seconds_and_provider(monkeypatch, rl_env):
    conn = _FakeConn()
    monkeypatch.setattr(rate_limit, "_get_conn", lambda: conn)
    rate_limit.park("s2", 30)
    assert conn.executed == [(rate_limit._PARK_SQL, (30.0, "s2"))]
    assert rate_limit._PARK_SQL.lstrip().startswith("UPDATE external_rate_limits")


def test_park_nonpositive_seconds_is_noop(monkeypatch, rl_env):
    conn = _FakeConn()
    monkeypatch.setattr(rate_limit, "_get_conn", lambda: conn)
    rate_limit.park("s2", 0)
    assert conn.executed == []


def test_fail_open_acquire_warns_once_across_two_calls(monkeypatch, rl_env, caplog):
    def boom() -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(rate_limit, "_get_conn", boom)
    with caplog.at_level(logging.DEBUG, logger=rate_limit.log.name):
        assert rate_limit.acquire("s2") is True
        assert rate_limit.acquire("s2") is True
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "s2" in warnings[0].getMessage()


def test_fail_open_park_does_not_raise(monkeypatch, rl_env, caplog):
    def boom() -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(rate_limit, "_get_conn", boom)
    with caplog.at_level(logging.DEBUG, logger=rate_limit.log.name):
        rate_limit.park("s2", 5)  # must not raise
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_warn_fail_open_is_per_provider(rl_env, caplog):
    with caplog.at_level(logging.WARNING, logger=rate_limit.log.name):
        rate_limit._warn_fail_open("a", "acquire")
        rate_limit._warn_fail_open("b", "acquire")
        rate_limit._warn_fail_open("a", "park")
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


def test_seed_sql_is_capped_and_park_sql_skips_zero_refill_rows():
    assert "refill_per_sec > 0" in rate_limit._PARK_SQL
    assert rate_limit.MAX_SEEDED_HOST_ROWS > 0
    assert "%s" in rate_limit._SEED_SQL


# ── follow-up: PoolTimeout, RateLimited chaining, fatal, breaker scope ─────


def test_retry_transient_does_not_retry_pool_timeout():
    sleeps: list[float] = []
    exc = httpx.PoolTimeout("pool")
    fn = _failing(exc)
    with pytest.raises(httpx.PoolTimeout) as ei:
        http.retry_transient(fn, sleep=sleeps.append)
    assert ei.value is exc
    assert fn.calls == 1
    assert sleeps == []


def test_non_idempotent_call_retries_pool_timeout(limiter):
    clock = Clock()
    results: list[Any] = [httpx.PoolTimeout("pool"), "ok"]

    def fn() -> str:
        item = results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    assert (
        call_with_backoff(
            fn,
            service="ni-pool",
            idempotent=False,
            sleep=clock.sleep,
            now=clock.now,
            uniform=_half,
        )
        == "ok"
    )


def test_with_backoff_first_attempt_rate_limited_propagates(limiter):
    limiter.refuse = True
    send = _scripted(_resp(200))
    with pytest.raises(rate_limit.RateLimited):
        with_backoff(
            send,
            service="rl-first",
            policy=Policy(provider="p", breaker_threshold=100),
            sleep=Clock().sleep,
        )
    assert send.calls == 0


def _refuse_from_second_require(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {"n": 0}

    def fake_require(provider: str, **_kw: Any) -> None:
        seen["n"] += 1
        if seen["n"] >= 2:
            raise rate_limit.RateLimited(provider)

    monkeypatch.setattr("precis.utils.rate_limit.require", fake_require)
    monkeypatch.setattr("precis.utils.rate_limit.park", lambda *a, **k: None)


def test_with_backoff_rate_limited_on_retry_is_vendor_unavailable_from_it(
    monkeypatch,
):
    _refuse_from_second_require(monkeypatch)
    clock = Clock()
    send = _scripted(_resp(429), _resp(200))
    with pytest.raises(VendorUnavailable) as ei:
        with_backoff(
            send,
            service="rl-retry",
            policy=Policy(provider="p", breaker_threshold=100),
            sleep=clock.sleep,
            now=clock.now,
            rng=StubRng(),
        )
    assert isinstance(ei.value.__cause__, rate_limit.RateLimited)
    assert send.calls == 1


def test_call_with_backoff_rate_limited_on_retry_reraises_original(monkeypatch):
    _refuse_from_second_require(monkeypatch)
    clock = Clock()
    exc = _status_error(429)
    fn = _failing(exc)
    with pytest.raises(httpx.HTTPStatusError) as ei:
        call_with_backoff(
            fn,
            service="rl-retry-call",
            policy=Policy(provider="p", breaker_threshold=100),
            sleep=clock.sleep,
            now=clock.now,
            uniform=_half,
        )
    assert ei.value is exc
    assert fn.calls == 1


@pytest.mark.parametrize("status", [401, 403])
def test_call_with_backoff_never_retries_fatal_even_if_retry_if_true(status, limiter):
    clock = Clock()
    exc = _status_error(status)
    fn = _failing(exc)
    with pytest.raises(httpx.HTTPStatusError) as ei:
        call_with_backoff(
            fn,
            service=f"fatal-call-{status}",
            retry_if=lambda e: True,
            sleep=clock.sleep,
            now=clock.now,
        )
    assert ei.value is exc
    assert fn.calls == 1
    assert clock.sleeps == []


def test_shims_leave_no_breaker_state_but_with_backoff_creates_one(limiter):
    reset_circuit()

    @http.external_retry(
        attempts=2, wait_min_s=0.0, wait_max_s=0.0, sleep=lambda s: None
    )
    def flaky() -> str:
        raise httpx.ConnectError("x")

    with pytest.raises(httpx.ConnectError):
        flaky()
    with pytest.raises(httpx.ConnectError):
        http.retry_transient(_failing(httpx.ConnectError("y")), sleep=lambda s: None)
    assert http._CIRCUITS == {}

    with_backoff(_scripted(_resp(200)), service="s2", sleep=Clock().sleep)
    assert "s2" in http._CIRCUITS
