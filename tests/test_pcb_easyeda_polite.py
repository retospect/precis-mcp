"""EasyEDA politeness (gr492999): >=2 s spacing floor on every path, an
immediate 30-minute breaker on a 401/403, and refusal diagnostics. The
injected ``now``/``sleep``/``wall`` mean no real time passes."""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise

import httpx
import pytest

from precis.pcb import easyeda
from precis.pcb._http import (
    EASYEDA_POLICY,
    VendorError,
    VendorUnavailable,
    reset_circuit,
    with_backoff,
)


@pytest.fixture(autouse=True)
def _clean_circuits():
    reset_circuit()
    yield
    reset_circuit()


@dataclass
class FakeClock:
    t: float = 1000.0
    sleeps: list[float] = field(default_factory=list)

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def _resp(status, *, headers=None, text=None):
    return httpx.Response(
        status,
        headers=headers or {},
        text=text,
        request=httpx.Request("GET", "https://easyeda.test/api/x"),
    )


def _call(send, clock, wall=None):
    kwargs = {"wall": wall} if wall else {}
    return with_backoff(
        send,
        service="easyeda",
        policy=EASYEDA_POLICY,
        sleep=clock.sleep,
        now=clock.now,
        rng=random.Random(0),
        **kwargs,
    )


def test_spacing_floor_between_consecutive_calls():
    clock = FakeClock()
    stamps: list[float] = []

    def send():
        stamps.append(clock.t)
        return _resp(200)

    for _ in range(4):
        _call(send, clock)
    gaps = [b - a for a, b in pairwise(stamps)]
    assert len(gaps) == 3
    assert all(2.0 <= g <= 2.0 + EASYEDA_POLICY.spacing_jitter + 1e-9 for g in gaps)


@pytest.mark.parametrize("injected", [False, True])
def test_single_and_injected_client_paths_both_space_calls(monkeypatch, injected):
    """fetch_component with and without a client goes through the same
    policy object, so back-to-back fetches are spaced on both paths."""
    clock = FakeClock()
    stamps: list[float] = []
    real = easyeda.with_backoff

    def fake_get(client, url, headers=None):
        stamps.append(clock.t)
        return _resp(404)

    monkeypatch.setattr("precis.utils.safe_fetch.safe_get", fake_get)
    monkeypatch.setattr(
        easyeda,
        "with_backoff",
        lambda send, **kw: real(send, sleep=clock.sleep, now=clock.now, **kw),
    )
    client = httpx.Client() if injected else None
    try:
        for part in ("C1", "C2", "C3"):
            easyeda.fetch_component(part, client=client)
    finally:
        if client:
            client.close()
    gaps = [b - a for a, b in pairwise(stamps)]
    assert len(gaps) == 2
    assert all(g >= 2.0 for g in gaps)


def test_403_opens_breaker_at_once_fails_fast_then_closes():
    clock = FakeClock()
    sent: list[int] = []
    wall = lambda: datetime(2026, 10, 16, 19, 0, tzinfo=UTC)

    def refuse():
        sent.append(1)
        return _resp(403)

    with pytest.raises(VendorError) as first:
        _call(refuse, clock, wall)
    assert first.value.status == 403
    assert len(sent) == 1

    clock.t += 60
    with pytest.raises(VendorUnavailable) as later:
        _call(refuse, clock, wall)
    assert "EasyEDA refused, cooling down until 19:30Z" in str(later.value)
    assert len(sent) == 1  # no request went out

    clock.t += EASYEDA_POLICY.fatal_cooldown or 0.0

    def ok():
        sent.append(1)
        return _resp(200)

    assert _call(ok, clock, wall).status_code == 200
    assert _call(ok, clock, wall).status_code == 200
    assert len(sent) == 3


def test_non_fatal_failures_keep_the_threshold():
    clock = FakeClock()
    # One exhausted call = `attempts` failures, still under the threshold.
    assert EASYEDA_POLICY.attempts < EASYEDA_POLICY.breaker_threshold
    with pytest.raises(VendorUnavailable, match="attempts exhausted"):
        _call(lambda: _resp(500), clock)
    sent: list[int] = []

    def fail():
        sent.append(1)
        return _resp(500)

    with pytest.raises(VendorUnavailable):
        _call(fail, clock)
    assert len(sent) == EASYEDA_POLICY.breaker_threshold - EASYEDA_POLICY.attempts


def test_refusal_log_carries_body_prefix_and_cf_id(caplog):
    clock = FakeClock()
    refusal = _resp(
        403,
        headers={"x-amz-cf-id": "cf-abc123", "set-cookie": "secret=1"},
        text="A" * 1000,
    )
    with caplog.at_level(logging.WARNING, logger="precis.pcb._http"):
        with pytest.raises(VendorError):
            _call(lambda: refusal, clock)
    assert "cf-abc123" in caplog.text
    assert "A" * 300 in caplog.text
    assert "A" * 301 not in caplog.text
    assert "secret=1" not in caplog.text
