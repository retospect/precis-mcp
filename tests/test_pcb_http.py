"""``precis.pcb._http.with_backoff`` — the shared politeness layer for the
two vendor integrations (JLCPCB, EasyEDA), exercised directly rather than
only indirectly through ``jlc_api``. Every test injects ``sleep``/``now``/
``rng`` so zero real time passes; nothing here hits the network.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

import httpx
import pytest

from precis.pcb._http import (
    BULK_POLICY,
    Policy,
    VendorError,
    VendorUnavailable,
    reset_circuit,
    with_backoff,
)


@pytest.fixture(autouse=True)
def _clean_circuits():
    """Breaker state is module-global keyed by service name — belt and
    braces on top of each test using its own unique service name."""
    reset_circuit()
    yield
    reset_circuit()


@dataclass
class FakeClock:
    """Injectable ``now``/``sleep`` pair: ``sleep`` advances the same clock
    ``now`` reads, so the code under test experiences a coherent (if
    instantaneous) timeline without any real waiting."""

    t: float = 0.0
    sleep_calls: list[float] = field(default_factory=list)

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.t += seconds


def _fixed_rng() -> random.Random:
    return random.Random(0)


def _resp(
    status: int,
    headers: dict[str, str] | None = None,
    *,
    text: str | None = None,
    url: str | None = None,
) -> httpx.Response:
    request = httpx.Request("GET", url) if url is not None else None
    return httpx.Response(status, headers=headers or {}, text=text, request=request)


# ── fatal statuses: never retried ────────────────────────────────────────


@pytest.mark.parametrize("status", [401, 403])
def test_fatal_status_raises_vendor_error_and_calls_send_once(status):
    clock = FakeClock()
    calls = []

    def send():
        calls.append(1)
        return _resp(status)

    with pytest.raises(VendorError) as excinfo:
        with_backoff(
            send,
            service=f"fatal-{status}",
            sleep=clock.sleep,
            now=clock.now,
            rng=_fixed_rng(),
        )
    assert excinfo.value.status == status
    assert len(calls) == 1
    assert clock.sleep_calls == []  # never slept — a fatal status is fast-fail


def test_fatal_status_is_not_vendor_unavailable():
    """401/403 are a VendorError, not the retries-exhausted subclass."""
    clock = FakeClock()

    def send():
        return _resp(401)

    with pytest.raises(VendorError) as excinfo:
        with_backoff(
            send,
            service="fatal-not-unavailable",
            sleep=clock.sleep,
            now=clock.now,
            rng=_fixed_rng(),
        )
    assert not isinstance(excinfo.value, VendorUnavailable)


# ── retryable statuses: retried up to policy.attempts, then unavailable ──


@pytest.mark.parametrize("status", [429, 503])
def test_retryable_status_retries_then_raises_unavailable(status):
    clock = FakeClock()
    calls = []
    policy = Policy(attempts=3, base=1.0, cap=5.0, breaker_threshold=100)

    def send():
        calls.append(1)
        return _resp(status)

    with pytest.raises(VendorUnavailable) as excinfo:
        with_backoff(
            send,
            service=f"retryable-{status}",
            policy=policy,
            sleep=clock.sleep,
            now=clock.now,
            rng=_fixed_rng(),
        )
    assert len(calls) == policy.attempts
    assert excinfo.value.status == status
    # Slept between attempts, but never blocked real time (fake clock only).
    assert len(clock.sleep_calls) == policy.attempts - 1


# ── Retry-After: honoured when sane, ignored when absurd/unparseable ─────


def test_retry_after_seconds_is_honoured():
    clock = FakeClock()
    calls = []
    policy = Policy(attempts=3, breaker_threshold=100)

    def send():
        calls.append(1)
        if len(calls) == 1:
            return _resp(429, {"Retry-After": "2"})
        return _resp(200)

    resp = with_backoff(
        send,
        service="retry-after-sane",
        policy=policy,
        sleep=clock.sleep,
        now=clock.now,
        rng=_fixed_rng(),
    )
    assert resp.status_code == 200
    assert len(calls) == 2
    assert clock.sleep_calls == [2.0]


def test_retry_after_absurd_value_is_ignored():
    clock = FakeClock()
    calls = []
    policy = Policy(attempts=3, base=1.0, cap=5.0, breaker_threshold=100)

    def send():
        calls.append(1)
        if len(calls) == 1:
            # Way outside the 0-900s sanity window — must not be honoured.
            return _resp(429, {"Retry-After": "999999"})
        return _resp(200)

    resp = with_backoff(
        send,
        service="retry-after-absurd",
        policy=policy,
        sleep=clock.sleep,
        now=clock.now,
        rng=_fixed_rng(),
    )
    assert resp.status_code == 200
    assert len(calls) == 2
    assert len(clock.sleep_calls) == 1
    # Fell back to the bounded jittered backoff, not the absurd header value.
    assert clock.sleep_calls[0] <= policy.cap
    assert clock.sleep_calls[0] != 999999.0


def test_retry_after_http_date_form_is_ignored():
    clock = FakeClock()
    calls = []
    policy = Policy(attempts=3, base=1.0, cap=5.0, breaker_threshold=100)

    def send():
        calls.append(1)
        if len(calls) == 1:
            return _resp(429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
        return _resp(200)

    resp = with_backoff(
        send,
        service="retry-after-http-date",
        policy=policy,
        sleep=clock.sleep,
        now=clock.now,
        rng=_fixed_rng(),
    )
    assert resp.status_code == 200
    assert len(calls) == 2
    assert len(clock.sleep_calls) == 1
    assert clock.sleep_calls[0] <= policy.cap


# ── circuit breaker: opens, stays open through cooldown, half-opens ──────


def test_breaker_opens_after_threshold_consecutive_failures_across_calls():
    clock = FakeClock()
    calls = []
    policy = Policy(
        attempts=1, base=0.0, cap=0.0, breaker_threshold=2, breaker_cooldown=300.0
    )
    service = "breaker-opens"

    def failing_send():
        calls.append(1)
        return _resp(500)

    # First call: one failure, breaker not open yet.
    with pytest.raises(VendorUnavailable):
        with_backoff(
            failing_send,
            service=service,
            policy=policy,
            sleep=clock.sleep,
            now=clock.now,
            rng=_fixed_rng(),
        )
    assert len(calls) == 1

    # Second call: second consecutive failure reaches the threshold — opens.
    with pytest.raises(VendorUnavailable):
        with_backoff(
            failing_send,
            service=service,
            policy=policy,
            sleep=clock.sleep,
            now=clock.now,
            rng=_fixed_rng(),
        )
    assert len(calls) == 2

    # Third call: breaker open, within cooldown — short-circuits, send NOT called.
    with pytest.raises(VendorUnavailable) as excinfo:
        with_backoff(
            failing_send,
            service=service,
            policy=policy,
            sleep=clock.sleep,
            now=clock.now,
            rng=_fixed_rng(),
        )
    assert len(calls) == 2  # unchanged — the thunk was never invoked
    assert "circuit open" in str(excinfo.value)


def test_breaker_half_opens_after_cooldown_and_closes_on_success():
    clock = FakeClock()
    calls = []
    policy = Policy(
        attempts=1, base=0.0, cap=0.0, breaker_threshold=1, breaker_cooldown=300.0
    )
    service = "breaker-half-open"

    def failing_send():
        calls.append(("fail", clock.t))
        return _resp(500)

    # One failure at threshold=1 opens the breaker immediately.
    with pytest.raises(VendorUnavailable):
        with_backoff(
            failing_send,
            service=service,
            policy=policy,
            sleep=clock.sleep,
            now=clock.now,
            rng=_fixed_rng(),
        )
    assert len(calls) == 1

    # Still within cooldown: short-circuits without calling send.
    with pytest.raises(VendorUnavailable):
        with_backoff(
            failing_send,
            service=service,
            policy=policy,
            sleep=clock.sleep,
            now=clock.now,
            rng=_fixed_rng(),
        )
    assert len(calls) == 1

    # Cooldown elapses.
    clock.t += policy.breaker_cooldown + 1.0

    def succeeding_send():
        calls.append(("ok", clock.t))
        return _resp(200)

    # Half-open probe: the call goes through and succeeds — breaker closes.
    resp = with_backoff(
        succeeding_send,
        service=service,
        policy=policy,
        sleep=clock.sleep,
        now=clock.now,
        rng=_fixed_rng(),
    )
    assert resp.status_code == 200
    assert len(calls) == 2

    # Breaker is closed again: a subsequent failure does not re-open it
    # immediately from stale state — it takes threshold(=1) fresh failures.
    with pytest.raises(VendorUnavailable):
        with_backoff(
            failing_send,
            service=service,
            policy=policy,
            sleep=clock.sleep,
            now=clock.now,
            rng=_fixed_rng(),
        )
    assert len(calls) == 3


# ── min_interval spacing (bulk-walk politeness) ───────────────────────────


def test_min_interval_spacing_sleeps_between_calls():
    clock = FakeClock(t=1000.0)  # nonzero start avoids a spurious first-call sleep
    calls = []
    service = "min-interval"

    def send():
        calls.append(1)
        return _resp(200)

    # First call: gap since (zero-valued) last_call_at is huge — no sleep.
    with_backoff(
        send,
        service=service,
        policy=BULK_POLICY,
        sleep=clock.sleep,
        now=clock.now,
        rng=_fixed_rng(),
    )
    assert clock.sleep_calls == []

    # Second call arrives inside the min_interval window.
    clock.t += 0.2
    with_backoff(
        send,
        service=service,
        policy=BULK_POLICY,
        sleep=clock.sleep,
        now=clock.now,
        rng=_fixed_rng(),
    )
    assert len(clock.sleep_calls) == 1
    assert clock.sleep_calls[0] == pytest.approx(BULK_POLICY.min_interval - 0.2)
    assert len(calls) == 2


# ── non-retryable, non-fatal status is returned, not raised ──────────────


def test_404_is_returned_not_raised():
    clock = FakeClock()
    calls = []

    def send():
        calls.append(1)
        return _resp(404)

    resp = with_backoff(
        send, service="not-found", sleep=clock.sleep, now=clock.now, rng=_fixed_rng()
    )
    assert resp.status_code == 404
    assert len(calls) == 1
    assert clock.sleep_calls == []


# ── reset_circuit clears state ────────────────────────────────────────────


def test_reset_circuit_clears_open_breaker():
    clock = FakeClock()
    calls = []
    policy = Policy(
        attempts=1, base=0.0, cap=0.0, breaker_threshold=1, breaker_cooldown=300.0
    )
    service = "reset-circuit"

    def failing_send():
        calls.append(1)
        return _resp(500)

    with pytest.raises(VendorUnavailable):
        with_backoff(
            failing_send,
            service=service,
            policy=policy,
            sleep=clock.sleep,
            now=clock.now,
            rng=_fixed_rng(),
        )
    # Breaker is open now — confirm the short-circuit before reset.
    with pytest.raises(VendorUnavailable) as excinfo:
        with_backoff(
            failing_send,
            service=service,
            policy=policy,
            sleep=clock.sleep,
            now=clock.now,
            rng=_fixed_rng(),
        )
    assert "circuit open" in str(excinfo.value)
    assert len(calls) == 1

    reset_circuit(service)

    # Freshly-reset circuit: the thunk is invoked again (no short-circuit),
    # even though we're still well inside what would have been the cooldown.
    with pytest.raises(VendorUnavailable):
        with_backoff(
            failing_send,
            service=service,
            policy=policy,
            sleep=clock.sleep,
            now=clock.now,
            rng=_fixed_rng(),
        )
    assert len(calls) == 2


# ── failure diagnosis: body prefix, URL, __cause__ (gr267454) ────────────


def test_exhausted_retries_carry_body_prefix_and_url():
    clock = FakeClock()
    policy = Policy(attempts=2, base=0.0, cap=0.0, breaker_threshold=100)

    def send():
        return _resp(
            500,
            text='{"error": "invalid signature"}',
            url="https://jlcpcb.example/openapi/parts",
        )

    with pytest.raises(VendorUnavailable) as excinfo:
        with_backoff(
            send,
            service="jlcpcb",
            policy=policy,
            sleep=clock.sleep,
            now=clock.now,
            rng=_fixed_rng(),
        )
    err = excinfo.value
    assert err.body is not None
    assert "invalid signature" in err.body
    assert err.url == "https://jlcpcb.example/openapi/parts"
    # Rendered in the message too, so callers see it without field access.
    message = str(err)
    assert "invalid signature" in message
    assert "https://jlcpcb.example/openapi/parts" in message


def test_body_prefix_is_truncated_at_bound():
    clock = FakeClock()
    policy = Policy(attempts=1, base=0.0, cap=0.0, breaker_threshold=100)
    huge = "x" * 5000

    def send():
        return _resp(500, text=huge, url="https://jlcpcb.example/openapi/parts")

    with pytest.raises(VendorUnavailable) as excinfo:
        with_backoff(
            send,
            service="jlcpcb-truncate",
            policy=policy,
            sleep=clock.sleep,
            now=clock.now,
            rng=_fixed_rng(),
        )
    assert excinfo.value.body is not None
    assert len(excinfo.value.body) == 500
    assert len(excinfo.value.body) < len(huge)


def test_circuit_opened_raise_carries_body_and_url():
    clock = FakeClock()
    policy = Policy(
        attempts=1, base=0.0, cap=0.0, breaker_threshold=1, breaker_cooldown=300.0
    )

    def send():
        return _resp(
            503, text="service unavailable", url="https://jlcpcb.example/openapi/foo"
        )

    with pytest.raises(VendorUnavailable) as excinfo:
        with_backoff(
            send,
            service="jlcpcb-circuit",
            policy=policy,
            sleep=clock.sleep,
            now=clock.now,
            rng=_fixed_rng(),
        )
    assert "circuit opened" in str(excinfo.value)
    assert excinfo.value.body is not None
    assert "service unavailable" in excinfo.value.body
    assert excinfo.value.url == "https://jlcpcb.example/openapi/foo"


def test_transport_error_chains_cause():
    clock = FakeClock()
    policy = Policy(attempts=1, base=0.0, cap=0.0, breaker_threshold=100)
    boom = httpx.ConnectError("connection refused")

    def send():
        raise boom

    with pytest.raises(VendorUnavailable) as excinfo:
        with_backoff(
            send,
            service="jlcpcb-transport",
            policy=policy,
            sleep=clock.sleep,
            now=clock.now,
            rng=_fixed_rng(),
        )
    assert excinfo.value.__cause__ is boom
    # No response was ever received — nothing to attribute a body/url to.
    assert excinfo.value.body is None
    assert excinfo.value.url is None


def test_status_only_failure_does_not_chain_a_stale_exception():
    """A transport error on an earlier attempt must not leak its __cause__
    onto a later, unrelated status-code failure."""
    clock = FakeClock()
    calls = []
    policy = Policy(attempts=3, base=0.0, cap=0.0, breaker_threshold=100)

    def send():
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("connection refused")
        return _resp(500, text="server error", url="https://jlcpcb.example/x")

    with pytest.raises(VendorUnavailable) as excinfo:
        with_backoff(
            send,
            service="jlcpcb-mixed",
            policy=policy,
            sleep=clock.sleep,
            now=clock.now,
            rng=_fixed_rng(),
        )
    assert excinfo.value.__cause__ is None
    assert excinfo.value.body is not None


def test_message_never_includes_response_headers():
    """Headers can carry auth material (session cookies, signed tokens) —
    they must never end up in the exception, even indirectly."""
    clock = FakeClock()
    policy = Policy(attempts=1, base=0.0, cap=0.0, breaker_threshold=100)

    def send():
        return _resp(
            500,
            headers={"Set-Cookie": "secret-session=abc123"},
            text="plain error body",
            url="https://jlcpcb.example/x",
        )

    with pytest.raises(VendorUnavailable) as excinfo:
        with_backoff(
            send,
            service="jlcpcb-headers",
            policy=policy,
            sleep=clock.sleep,
            now=clock.now,
            rng=_fixed_rng(),
        )
    assert "secret-session" not in str(excinfo.value)
    assert "abc123" not in str(excinfo.value)
