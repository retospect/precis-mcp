"""Drain-aware retry/backoff sleeps (gr204611).

A worker's SIGTERM drain gives it ~60s to stop before SIGKILL. A single
``wait_exponential(min=1, max=60)`` backoff sleep can alone eat that whole
budget, so every retry/backoff sleep in the outbound-HTTP paths must wake
early on drain and stop retrying once draining. Pure unit tests — no DB;
``tests/conftest.py``'s autouse ``_reset_drain_flag`` fixture clears
``precis.liveness._DRAIN`` after every test in this module, so a
``request_drain()`` here can't leak into a later test.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

import precis.utils.rate_limit as rate_limit_mod
import precis.workers.bib_parse as bib_parse_mod
from precis.liveness import drain_sleep, request_drain
from precis.utils import http

# ── liveness.drain_sleep ────────────────────────────────────────────────


class TestDrainSleep:
    def test_full_sleep_returns_false(self) -> None:
        start = time.monotonic()
        assert drain_sleep(0.05) is False
        assert time.monotonic() - start >= 0.05

    def test_wakes_early_on_drain(self) -> None:
        request_drain()
        start = time.monotonic()
        assert drain_sleep(10.0) is True
        assert time.monotonic() - start < 0.5


# ── http.external_retry ─────────────────────────────────────────────────


class TestExternalRetry:
    def test_drain_before_call_stops_after_one_attempt(self) -> None:
        request_drain()
        calls = {"n": 0}

        @http.external_retry(attempts=5, wait_min_s=0.001, wait_max_s=0.001)
        def flaky() -> None:
            calls["n"] += 1
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            flaky()
        assert calls["n"] == 1

    def test_without_drain_retries_up_to_attempts(self) -> None:
        calls = {"n": 0}

        @http.external_retry(attempts=3, wait_min_s=0.001, wait_max_s=0.001)
        def flaky() -> None:
            calls["n"] += 1
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            flaky()
        assert calls["n"] == 3

    def test_drain_mid_flight_stops_next_attempt(self) -> None:
        calls = {"n": 0}

        @http.external_retry(attempts=5, wait_min_s=0.001, wait_max_s=0.001)
        def flaky() -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                # Drain arrives mid-flight, from inside the failing call —
                # the retry predicate must see it before deciding on a
                # second attempt.
                request_drain()
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            flaky()
        assert calls["n"] == 1


# ── bib_parse._crossref_query ───────────────────────────────────────────


class TestCrossrefQueryDrain:
    def test_drain_returns_none_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        request_drain()

        def _boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("network down")

        monkeypatch.setattr("precis.utils.safe_fetch.safe_get", _boom)

        start = time.monotonic()
        result = bib_parse_mod._crossref_query("some raw citation text")
        elapsed = time.monotonic() - start

        assert result is None
        # Undrained, the two backoffs alone (1s + 2s) would dominate this.
        assert elapsed < 1.0


# ── rate_limit.acquire ──────────────────────────────────────────────────


class TestRateLimitAcquireDrain:
    def test_drain_during_poll_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Faking the DB conn well enough to reach the poll-sleep branch is
        contorted (per the design note) — assert the drain branch directly:
        monkeypatch ``rate_limit.drain_sleep`` to report draining and check
        ``acquire`` reads that as "stop waiting for tokens"."""

        class _FetchOne:
            def __init__(self, row: tuple[Any, ...] | None) -> None:
                self._row = row

            def fetchone(self) -> tuple[Any, ...] | None:
                return self._row

        class _FakeConn:
            closed = False

            def execute(self, sql: str, params: Any) -> _FetchOne:
                if "UPDATE external_rate_limits" in sql:
                    return _FetchOne(None)  # never granted -> falls to poll
                # _SELECT_STATE_SQL: capacity, refill_per_sec, daily_cap,
                # avail, used_today -- rate-starved, no quota lane.
                return _FetchOne((10, 1.0, None, 0.0, 0))

        monkeypatch.setattr(rate_limit_mod, "_rate_limit_enabled", lambda: True)
        monkeypatch.setattr(rate_limit_mod, "_get_conn", lambda: _FakeConn())

        drain_calls: list[float] = []

        def _fake_drain_sleep(seconds: float) -> bool:
            drain_calls.append(seconds)
            return True

        monkeypatch.setattr(rate_limit_mod, "drain_sleep", _fake_drain_sleep)

        assert rate_limit_mod.acquire("s2", max_wait_s=30.0) is False
        assert drain_calls
