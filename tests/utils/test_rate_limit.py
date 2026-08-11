"""Tests for the external-API rate limiter (``precis.utils.rate_limit``).

Uses the real ``external_rate_limits`` table (migration 0121) against the
container test DB via the ``store`` fixture. The table is in the fixture's
preserve-set (it holds migration-seeded provider config, like
``providers``/``news_sources``), so the ~5 seeded rows persist; each test
instead seeds its own throwaway ``test_rl_*`` provider row (``_seed``
deletes-then-inserts) and cleans it up in a ``finally``, so tests never
collide with the seeds or with each other.

``rate_limit`` reads its DSN from ``load_config().database_url`` (own
connection, not the ``store`` pool — see the module docstring), so tests
that exercise the real DB path point ``PRECIS_DATABASE_URL`` at the same
DSN the ``store`` fixture is using.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from precis.store import Store
from precis.utils import rate_limit

pytestmark = pytest.mark.usefixtures("store")


def _seed(
    store: Store,
    provider: str,
    *,
    capacity: int,
    refill_per_sec: float,
    tokens: float | None = None,
    daily_cap: int | None = None,
    day_used: int = 0,
    day_start: date | None = None,
) -> None:
    """Insert (replacing any existing row) one ``external_rate_limits`` row."""
    with store.pool.connection() as conn:
        conn.execute(
            "DELETE FROM external_rate_limits WHERE provider = %s", (provider,)
        )
        conn.execute(
            """
            INSERT INTO external_rate_limits
                (provider, capacity, refill_per_sec, tokens, daily_cap,
                 day_used, day_start)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                provider,
                capacity,
                refill_per_sec,
                tokens if tokens is not None else capacity,
                daily_cap,
                day_used,
                day_start if day_start is not None else date.today(),
            ),
        )
        conn.commit()


def _row(store: Store, provider: str) -> tuple[Any, ...] | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT tokens, day_used, day_start FROM external_rate_limits "
            "WHERE provider = %s",
            (provider,),
        ).fetchone()
        return tuple(row) if row is not None else None


def _cleanup(store: Store, provider: str) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "DELETE FROM external_rate_limits WHERE provider = %s", (provider,)
        )
        conn.commit()


@pytest.fixture
def use_store_dsn(store: Store, monkeypatch: pytest.MonkeyPatch) -> Iterator[Store]:
    """Point ``rate_limit``'s own connection at the ``store`` fixture's DSN
    (via ``PRECIS_DATABASE_URL``, what ``load_config()`` reads) and reset
    its module-level cached connection afterwards."""
    assert store.dsn is not None
    monkeypatch.setenv("PRECIS_DATABASE_URL", store.dsn)
    yield store
    rate_limit._conn = None
    rate_limit._conn_dsn = None


class TestRateLane:
    def test_drains_capacity_then_starves(self, use_store_dsn: Store) -> None:
        provider = "test_rl_drain"
        # No meaningful refill within the test's lifetime.
        _seed(use_store_dsn, provider, capacity=2, refill_per_sec=0.0001, tokens=2)
        try:
            assert rate_limit.acquire(provider, max_wait_s=1.0) is True
            assert rate_limit.acquire(provider, max_wait_s=1.0) is True
            # Bucket is drained; a tiny max_wait_s must not block long.
            t0 = time.monotonic()
            granted = rate_limit.acquire(provider, max_wait_s=0.2)
            elapsed = time.monotonic() - t0
            assert granted is False
            assert elapsed < 1.0
        finally:
            _cleanup(use_store_dsn, provider)

    def test_refill_restores_grants(self, use_store_dsn: Store) -> None:
        provider = "test_rl_refill"
        # Fast refill: 50 tokens/sec, starts drained.
        _seed(use_store_dsn, provider, capacity=2, refill_per_sec=50.0, tokens=0.0)
        try:
            granted = rate_limit.acquire(provider, max_wait_s=2.0)
            assert granted is True
        finally:
            _cleanup(use_store_dsn, provider)

    def test_atomic_drain_never_over_issues(self, use_store_dsn: Store) -> None:
        """N rapid-fire acquires against a no-refill bucket of capacity C
        must grant exactly C and never drive ``tokens`` negative."""
        provider = "test_rl_atomic"
        capacity = 5
        _seed(
            use_store_dsn,
            provider,
            capacity=capacity,
            refill_per_sec=0.0,
            tokens=capacity,
        )
        try:
            grants = sum(
                1
                for _ in range(capacity + 5)
                if rate_limit.acquire(provider, max_wait_s=0.05)
            )
            assert grants == capacity
            row = _row(use_store_dsn, provider)
            assert row is not None
            tokens = row[0]
            assert tokens is not None
            assert float(tokens) >= 0.0
        finally:
            _cleanup(use_store_dsn, provider)


class TestQuotaLane:
    def test_quota_exhausted_returns_false_immediately(
        self, use_store_dsn: Store
    ) -> None:
        provider = "test_rl_quota"
        # Rate lane wide open; quota lane one acquire away from the cap.
        _seed(
            use_store_dsn,
            provider,
            capacity=1000,
            refill_per_sec=1000.0,
            tokens=1000.0,
            daily_cap=5,
            day_used=5,
        )
        try:
            t0 = time.monotonic()
            granted = rate_limit.acquire(provider, max_wait_s=5.0)
            elapsed = time.monotonic() - t0
            assert granted is False
            # Quota exhaustion must not spin/wait — near-instant refusal.
            assert elapsed < 1.0
        finally:
            _cleanup(use_store_dsn, provider)

    def test_day_start_rollover_resets_day_used(self, use_store_dsn: Store) -> None:
        provider = "test_rl_rollover"
        yesterday = date.today() - timedelta(days=1)
        _seed(
            use_store_dsn,
            provider,
            capacity=1000,
            refill_per_sec=1000.0,
            tokens=1000.0,
            daily_cap=5,
            day_used=5,  # at the (stale) cap
            day_start=yesterday,
        )
        try:
            granted = rate_limit.acquire(provider, max_wait_s=1.0)
            assert granted is True
            row = _row(use_store_dsn, provider)
            assert row is not None
            _tokens, day_used, day_start = row
            assert day_used == 1  # reset to 0, then this acquire consumed 1
            assert day_start == date.today()
        finally:
            _cleanup(use_store_dsn, provider)


class TestFailOpen:
    def test_flag_off_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PRECIS_RATE_LIMIT", "0")
        assert rate_limit.acquire("any-provider-whatsoever") is True

    @pytest.mark.parametrize(
        ("value", "enabled"),
        [
            ("0", False),
            ("false", False),
            ("FALSE", False),
            ("off", False),
            ("no", False),
            (" false ", False),
            ("1", True),
            ("true", True),
            ("", True),
            ("garbage", True),
        ],
    )
    def test_flag_parse_never_raises(
        self, monkeypatch: pytest.MonkeyPatch, value: str, enabled: bool
    ) -> None:
        """The enable check runs *before* acquire's fail-open ``try`` and must
        never raise on a non-numeric value — a bare ``int("false")`` would, and
        would propagate through the tenacity-retried S2 call sites and break
        ingest. A disabling value short-circuits acquire to True without any DB
        touch (asserted here with no DSN configured)."""
        monkeypatch.setenv("PRECIS_RATE_LIMIT", value)
        assert rate_limit._rate_limit_enabled() is enabled
        if not enabled:
            assert rate_limit.acquire("any-provider-whatsoever") is True

    def test_unknown_provider_returns_true(self, use_store_dsn: Store) -> None:
        assert rate_limit.acquire("test_rl_no_such_provider", max_wait_s=1.0) is True

    def test_no_database_url_returns_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _FakeConfig:
            database_url = None

        monkeypatch.setattr(rate_limit, "load_config", lambda: _FakeConfig())
        assert rate_limit.acquire("s2") is True


class TestS2Wiring:
    @patch("precis.ingest.citations.acquire_rate_limit")
    @patch("precis.ingest.citations.SemanticScholar")
    def test_citations_acquires_s2_before_fetch(
        self, mock_cls: MagicMock, mock_acquire: MagicMock
    ) -> None:
        from precis.ingest.citations import citations

        mock_sch = MagicMock()
        mock_cls.return_value = mock_sch
        mock_sch.get_paper.return_value = None

        citations("doi:10.1/x", api_key="test-key")

        assert mock_acquire.call_count >= 2  # references + citations
        for call in mock_acquire.call_args_list:
            assert call.args == ("s2",)
