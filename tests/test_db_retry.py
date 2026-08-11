"""Tests for :mod:`precis.utils.db_retry`.

No DB needed — ``retry_locked`` is pure control flow around a callable, so
these use a fake ``fn`` that raises canned psycopg errors on its own
schedule. ``time.sleep`` and ``random.uniform`` are monkeypatched so the
suite runs instantly and deterministically.
"""

from __future__ import annotations

from typing import Any

import pytest
from psycopg.errors import DeadlockDetected, LockNotAvailable

from precis.utils import db_retry
from precis.utils.db_retry import retry_locked


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this file is about retry *logic*, not real timing."""
    monkeypatch.setattr(db_retry.time, "sleep", lambda _s: None)
    # Deterministic, but not just "always 0" -- keep the jitter call
    # observable/countable without adding real delay.
    monkeypatch.setattr(db_retry.random, "uniform", lambda lo, hi: hi)


def test_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    sleeps: list[float] = []
    monkeypatch.setattr(db_retry.time, "sleep", lambda s: sleeps.append(s))

    def fn() -> str:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise LockNotAvailable("could not obtain lock")
        return "ok"

    result = retry_locked(fn, label="test")
    assert result == "ok"
    assert calls["n"] == 3  # two failures + the succeeding third call
    assert len(sleeps) == 2  # one sleep between each retry


def test_deadlock_detected_is_also_retried() -> None:
    calls = {"n": 0}

    def fn() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise DeadlockDetected("deadlock")
        return "ok"

    assert retry_locked(fn) == "ok"
    assert calls["n"] == 2


def test_exhaustion_reraises(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fn() -> None:
        calls["n"] += 1
        raise LockNotAvailable("still locked")

    with pytest.raises(LockNotAvailable):
        retry_locked(fn, attempts=3)
    assert calls["n"] == 3  # exactly `attempts` calls, no extra retry


def test_non_retriable_exception_propagates_immediately() -> None:
    calls = {"n": 0}

    def fn() -> None:
        calls["n"] += 1
        raise ValueError("not a lock problem")

    with pytest.raises(ValueError):
        retry_locked(fn)
    assert calls["n"] == 1  # no retry attempted at all


def test_success_on_first_call_never_sleeps(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(db_retry.time, "sleep", lambda s: sleeps.append(s))

    def fn() -> str:
        return "ok"

    assert retry_locked(fn) == "ok"
    assert sleeps == []


def test_backoff_grows_with_attempt_index(monkeypatch: pytest.MonkeyPatch) -> None:
    # random.uniform(lo, hi) called with the widening exponential ceiling --
    # capture the `hi` argument on each call to confirm it grows.
    highs: list[float] = []

    def fake_uniform(lo: float, hi: float) -> float:
        highs.append(hi)
        return 0.0

    monkeypatch.setattr(db_retry.random, "uniform", fake_uniform)

    calls = {"n": 0}

    def fn() -> Any:
        calls["n"] += 1
        raise LockNotAvailable("locked")

    with pytest.raises(LockNotAvailable):
        retry_locked(fn, attempts=4, base_s=0.2, max_s=5.0)
    assert highs == [0.2, 0.4, 0.8]  # 3 retries before the 4th (final) raise
