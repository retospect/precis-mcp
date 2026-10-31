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
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from precis import secrets as secrets_mod
from precis.store import Store
from precis.utils import rate_limit
from tests._timing import budget

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
                day_start if day_start is not None else datetime.now(UTC).date(),
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


def _refusal_cols(store: Store, provider: str) -> tuple[Any, ...] | None:
    """``(refused_today, last_refused_at, day_used, day_start)`` — the S2b
    durable refusal counter and the daily-rollover pair it shares."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT refused_today, last_refused_at, day_used, day_start "
            "FROM external_rate_limits WHERE provider = %s",
            (provider,),
        ).fetchone()
        return tuple(row) if row is not None else None


@pytest.fixture(autouse=True)
def _fresh_s1_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset ``_s1_columns``' process-global memo per test: a fake-conn
    test whose probe raises pins it False for ``_S1_REPROBE_S``, and a
    later real-DB test then skips the ``refused_today`` write (r100 CI)."""
    monkeypatch.setattr(rate_limit, "_has_s1_cols", None)
    monkeypatch.setattr(rate_limit, "_s1_cols_at", 0.0)


@pytest.fixture
def use_store_dsn(store: Store, monkeypatch: pytest.MonkeyPatch) -> Iterator[Store]:
    """Point ``rate_limit``'s own connection at the ``store`` fixture's DSN
    (via ``PRECIS_DATABASE_URL``, what ``load_config()`` reads) and reset
    its module-level cached connection afterwards."""
    assert store.dsn is not None
    monkeypatch.setenv("PRECIS_DATABASE_URL", store.dsn)
    yield store
    rate_limit.close()


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
            assert elapsed < budget(1.0)
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
            assert elapsed < budget(1.0)
        finally:
            _cleanup(use_store_dsn, provider)

    def test_day_start_rollover_resets_day_used(self, use_store_dsn: Store) -> None:
        provider = "test_rl_rollover"
        yesterday = datetime.now(UTC).date() - timedelta(days=1)
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
            assert day_start == datetime.now(UTC).date()
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
        monkeypatch.setattr(secrets_mod, "_ADOPTED_DSN", None)
        assert rate_limit.acquire("s2") is True

    def test_scrubbed_env_falls_back_to_adopted_dsn(
        self, use_store_dsn: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gr503435: ``adopt_process_store`` pops ``PRECIS_DATABASE_URL``; the
        limiter must still reach the DB through the adopted DSN and consume."""
        provider = "test_rl_adopted"
        _seed(use_store_dsn, provider, capacity=2, refill_per_sec=0.0001, tokens=2)
        try:
            monkeypatch.delenv("PRECIS_DATABASE_URL")
            monkeypatch.setattr(secrets_mod, "_ADOPTED_DSN", use_store_dsn.dsn)
            assert rate_limit.acquire(provider, max_wait_s=1.0) is True
            row = _row(use_store_dsn, provider)
            assert row is not None
            assert row[0] < 2  # a token was actually consumed, not failed open
        finally:
            _cleanup(use_store_dsn, provider)

    @pytest.mark.parametrize(
        ("setup", "reason"),
        [
            ("env", "disabled by PRECIS_RATE_LIMIT"),
            ("dsn", "no database_url"),
            ("row", "no external_rate_limits row"),
        ],
    )
    def test_silent_fail_open_warns_once_per_provider(
        self,
        use_store_dsn: Store,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        setup: str,
        reason: str,
    ) -> None:
        monkeypatch.setattr(rate_limit, "_silent_warned", {})
        if setup == "env":
            monkeypatch.setenv("PRECIS_RATE_LIMIT", "0")
        elif setup == "dsn":
            monkeypatch.delenv("PRECIS_DATABASE_URL")
            monkeypatch.setattr(secrets_mod, "_ADOPTED_DSN", None)
            monkeypatch.setattr(rate_limit, "_resolve_dsn", lambda: None)
        with caplog.at_level("WARNING", logger=rate_limit.log.name):
            assert rate_limit.acquire("test_rl_no_such_provider", max_wait_s=1.0)
            assert rate_limit.acquire("test_rl_no_such_provider", max_wait_s=1.0)
            assert rate_limit.acquire("test_rl_other_provider", max_wait_s=1.0)
        msgs = [r.getMessage() for r in caplog.records if reason in r.getMessage()]
        assert len(msgs) == 2  # once per provider, not per call
        assert "test_rl_no_such_provider" in msgs[0]
        assert "test_rl_other_provider" in msgs[1]


class TestLifecycle:
    def test_close_closes_and_clears_the_connection(self, use_store_dsn: Store) -> None:
        """After a real acquire opens the module connection, ``close()``
        closes it and resets the cached handle so the next call reopens."""
        assert rate_limit.acquire("test_rl_no_such_provider", max_wait_s=1.0) is True
        conn = rate_limit._conn
        assert conn is not None
        assert not conn.closed

        rate_limit.close()

        assert conn.closed
        assert rate_limit._conn is None
        assert rate_limit._conn_dsn is None

    def test_close_is_idempotent(self) -> None:
        """Calling ``close()`` with nothing open (or twice in a row) must
        not raise — it's registered with ``atexit`` and can't assume a
        connection exists."""
        rate_limit.close()
        rate_limit.close()
        assert rate_limit._conn is None

    def test_get_conn_reopens_after_close(self, use_store_dsn: Store) -> None:
        """``close()`` followed by another ``acquire`` transparently
        reopens the connection — ``close`` is a shutdown hook, not a
        permanent kill switch."""
        assert rate_limit.acquire("test_rl_no_such_provider", max_wait_s=1.0) is True
        rate_limit.close()
        assert rate_limit.acquire("test_rl_no_such_provider", max_wait_s=1.0) is True
        assert rate_limit._conn is not None
        assert not rate_limit._conn.closed


class TestS2Wiring:
    @patch("precis.utils.rate_limit.require")
    # citations builds its client via ingest.semantic_scholar._client
    @patch("precis.ingest.semantic_scholar.SemanticScholar")
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
            assert call.args == ("s2",)  # the core takes the s2 row per attempt


class TestRequire:
    def test_require_raises_on_refusal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rate_limit, "acquire", lambda *a, **k: False)
        with pytest.raises(rate_limit.RateLimited) as exc:
            rate_limit.require("openalex")
        assert exc.value.provider == "openalex"

    def test_require_returns_on_grant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rate_limit, "acquire", lambda *a, **k: True)
        rate_limit.require("openalex")

    def test_require_fails_open_when_flag_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PRECIS_RATE_LIMIT", "0")
        rate_limit.require("openalex")


class TestOpenAlexWiring:
    @patch("precis.ingest.openalex_meta.http_client")
    @patch("precis.utils.rate_limit.require")
    def test_fetch_openalex_work_refused_never_calls_out(
        self, mock_require: MagicMock, mock_client: MagicMock
    ) -> None:
        from precis.ingest.openalex_meta import fetch_openalex_work

        mock_require.side_effect = rate_limit.RateLimited("openalex")
        with pytest.raises(rate_limit.RateLimited):
            fetch_openalex_work("10.1/x")
        # The politeness core takes the openalex token (one call, no retry).
        mock_require.assert_called_once()
        assert mock_require.call_args.args == ("openalex",)
        mock_client.assert_not_called()

    @pytest.mark.parametrize(
        "helper",
        [
            "_query_openalex_pdf_urls",
            "_query_openalex_content_urls",
            "_query_openalex_arxiv_id",
        ],
    )
    def test_fetch_oa_helpers_require_openalex_before_http(
        self, helper: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.workers import fetch_oa

        mock_require = MagicMock(side_effect=rate_limit.RateLimited("openalex"))
        mock_client = MagicMock()
        monkeypatch.setattr(fetch_oa, "require_rate_limit", mock_require)
        monkeypatch.setattr(fetch_oa, "http_client", mock_client)
        with pytest.raises(rate_limit.RateLimited):
            getattr(fetch_oa, helper)("10.1/x", email="")
        mock_require.assert_called_once_with("openalex")
        mock_client.assert_not_called()

    def test_try_openalex_refusal_is_rate_limited_outcome(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        from precis.workers import fetch_oa

        def _refused(*a: Any, **k: Any) -> list[str]:
            raise rate_limit.RateLimited("openalex")

        monkeypatch.setattr(fetch_oa, "_query_openalex_pdf_urls", _refused)
        stub = fetch_oa.StubRef(
            ref_id=1, doi="10.1234/x", arxiv=None, s2_id=None, cite_key=None
        )
        out = fetch_oa._try_openalex(stub, inbox_dir=tmp_path, email="")
        assert out is not None
        assert out.event == "rate_limited"
        assert out.payload["limiter"] == "shared"


class TestBucketRate:
    """gr507340: a fan-out caller reads the bucket's refill rate to size its
    in-flight work; every fail-open case of ``acquire`` reads as ``None``."""

    def test_reads_the_rows_refill_rate(self, use_store_dsn: Store) -> None:
        provider = "test_rl_rate"
        _seed(use_store_dsn, provider, capacity=4, refill_per_sec=2.5)
        try:
            assert rate_limit.bucket_rate(provider) == 2.5
        finally:
            _cleanup(use_store_dsn, provider)

    def test_missing_row_disabled_limiter_and_dead_store_read_none(
        self, use_store_dsn: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert rate_limit.bucket_rate("test_rl_no_such_row") is None
        monkeypatch.setenv("PRECIS_RATE_LIMIT", "0")
        assert rate_limit.bucket_rate("s2") is None
        monkeypatch.delenv("PRECIS_RATE_LIMIT")

        def boom() -> None:
            raise RuntimeError("db down")

        monkeypatch.setattr(rate_limit, "_get_conn", boom)
        assert rate_limit.bucket_rate("s2") is None


class TestHostRowEviction:
    """gr507345: at the seeding cap, ``host:`` rows idle past
    ``HOST_ROW_IDLE_EVICT_S`` are retired (oldest first), live ``host:``
    rows and every named provider row are left alone."""

    def _backdate(self, store: Store, provider: str, hours: float) -> None:
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE external_rate_limits SET last_refill = now() - "
                "make_interval(secs => %s) WHERE provider = %s",
                (hours * 3600.0, provider),
            )
            conn.commit()

    def test_evicts_only_idle_host_rows(self, use_store_dsn: Store) -> None:
        store = use_store_dsn
        stale = ["host:test-rl-stale-a.example", "host:test-rl-stale-b.example"]
        fresh = "host:test-rl-fresh.example"
        for p in [*stale, fresh]:
            _seed(store, p, capacity=1, refill_per_sec=1.0)
        self._backdate(store, stale[0], hours=72)
        self._backdate(store, stale[1], hours=25)
        self._backdate(store, fresh, hours=1)
        try:
            with store.pool.connection() as conn:
                gone = rate_limit._evict_idle_host_rows(conn)
                conn.commit()
            assert gone >= 2
            with store.pool.connection() as conn:
                left = {
                    r[0]
                    for r in conn.execute(
                        "SELECT provider FROM external_rate_limits "
                        "WHERE provider = ANY(%s)",
                        ([*stale, fresh],),
                    ).fetchall()
                }
            assert left == {fresh}
            # Named providers (migration-seeded, however idle) are never evicted.
            assert _crossref_present(store)
        finally:
            for p in [*stale, fresh]:
                _cleanup(store, p)

    def test_occupancy_sql_counts_rows_and_idle(self, use_store_dsn: Store) -> None:
        store = use_store_dsn
        idle = "host:test-rl-occ-idle.example"
        live = "host:test-rl-occ-live.example"
        for p in (idle, live):
            _seed(store, p, capacity=1, refill_per_sec=1.0)
        self._backdate(store, idle, hours=30)
        try:
            with store.pool.connection() as conn:
                row = conn.execute(
                    rate_limit.HOST_ROW_OCCUPANCY_SQL,
                    {"idle_s": float(rate_limit.HOST_ROW_IDLE_EVICT_S)},
                ).fetchone()
            assert row is not None
            n, n_idle = int(row[0]), int(row[1])
            assert n >= 2 and n_idle >= 1 and n_idle < n
        finally:
            for p in (idle, live):
                _cleanup(store, p)


def _crossref_present(store: Store) -> bool:
    with store.pool.connection() as conn:
        return (
            conn.execute(
                "SELECT 1 FROM external_rate_limits WHERE provider = 'crossref'"
            ).fetchone()
            is not None
        )


class TestCrossrefSeed:
    """Ruling 1 (Reto 2026-10-16): crossref is 1 request/s. Found by name,
    not number, so a landing-order renumber only renames the file."""

    (_MIGRATION,) = (
        Path(rate_limit.__file__).resolve().parents[1] / "migrations"
    ).glob("[0-9][0-9][0-9][0-9]_crossref_rate_1rps.sql")

    def _crossref(self, store: Store) -> tuple[Any, ...] | None:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT capacity, refill_per_sec, tokens FROM external_rate_limits "
                "WHERE provider = 'crossref'"
            ).fetchone()
            return tuple(row) if row is not None else None

    def test_migrated_db_caps_crossref_at_one_per_second(self, store: Store) -> None:
        assert self._crossref(store) == (1, 1.0, 1)

    def test_migration_lowers_a_20rps_row_and_is_idempotent(self, store: Store) -> None:
        sql = self._MIGRATION.read_text(encoding="utf-8")
        _seed(store, "crossref", capacity=20, refill_per_sec=20.0, tokens=20)
        try:
            for _ in range(2):
                with store.pool.connection() as conn:
                    conn.execute(sql)
                assert self._crossref(store) == (1, 1.0, 1)
        finally:
            _seed(store, "crossref", capacity=1, refill_per_sec=1.0, tokens=1)


class _FetchOne:
    """A ``conn.execute`` result carrying one ``fetchone`` row."""

    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row


class _StarvedConn:
    """Never grants; reports a live, rate-starved provider row — the state
    that makes ``acquire`` poll until its wait budget runs out (the same
    fake shape tests/test_drain_sleep.py uses)."""

    closed = False

    def execute(self, sql: str, params: Any) -> _FetchOne:
        if "UPDATE external_rate_limits" in sql:
            return _FetchOne(None)
        # _SELECT_STATE_SQL: capacity, refill_per_sec, daily_cap, avail,
        # used_today — a live row, starved, no quota lane.
        return _FetchOne((10, 1.0, None, 0.0, 0))


class _GrantConn(_StarvedConn):
    def execute(self, sql: str, params: Any) -> _FetchOne:
        if "UPDATE external_rate_limits" in sql:
            return _FetchOne((9.0,))  # granted
        return _FetchOne((10, 1.0, None, 0.0, 0))


class _QuotaConn(_StarvedConn):
    def execute(self, sql: str, params: Any) -> _FetchOne:
        if "UPDATE external_rate_limits" in sql:
            return _FetchOne(None)
        return _FetchOne((10, 100.0, 5, 10.0, 5))  # daily_cap spent


class TestDeadlineBound:
    """outbound-policy deadline merge (gr506756's limiter half): a limiter wait bounds
    itself to the caller's deadline — the ``deadline=`` argument or the
    ambient deadline ``call_with_deadline`` publishes on the bounded call's
    worker thread — so it can never eat the budget of the pass that owns
    it. Fake conns keep the starved-row waits millisecond-fast; one real-DB
    test proves the wired path end to end."""

    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rate_limit, "_refusals", {})
        monkeypatch.setattr(rate_limit, "_refusal_warned", {})

    @staticmethod
    def _use_conn(monkeypatch: pytest.MonkeyPatch, conn: Any) -> None:
        monkeypatch.setattr(rate_limit, "_rate_limit_enabled", lambda: True)
        monkeypatch.setattr(rate_limit, "_get_conn", lambda: conn)

    def test_ambient_deadline_bounds_the_wait(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The core gr506756 shape: a pass with fractions of a second left
        never waits the configured 300 s on a starved bucket — it is refused
        at its own deadline, and the refusal is counted and logged."""
        from precis.utils.deadline import deadline_scope

        self._use_conn(monkeypatch, _StarvedConn())
        t0 = time.monotonic()
        with caplog.at_level("WARNING", logger=rate_limit.log.name):
            with deadline_scope(t0 + 0.4):
                granted = rate_limit.acquire("test_rl_dl_ambient", max_wait_s=300.0)
        elapsed = time.monotonic() - t0
        assert granted is False
        assert elapsed < budget(5.0), f"waited past the deadline ({elapsed:.1f}s)"
        assert rate_limit.refusal_counts()["test_rl_dl_ambient"] == 1
        msgs = [
            r.getMessage()
            for r in caplog.records
            if "test_rl_dl_ambient" in r.getMessage()
        ]
        assert len(msgs) == 1
        assert "refused by the shared limiter" in msgs[0]
        assert "caller deadline" in msgs[0]

    def test_explicit_deadline_kwarg_bounds_the_wait(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._use_conn(monkeypatch, _StarvedConn())
        t0 = time.monotonic()
        granted = rate_limit.acquire(
            "test_rl_dl_kwarg", max_wait_s=300.0, deadline=t0 + 0.3
        )
        assert granted is False
        assert time.monotonic() - t0 < budget(5.0)
        assert rate_limit.refusal_counts()["test_rl_dl_kwarg"] == 1

    def test_past_deadline_refuses_without_waiting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._use_conn(monkeypatch, _StarvedConn())
        t0 = time.monotonic()
        granted = rate_limit.acquire(
            "test_rl_dl_past", max_wait_s=300.0, deadline=t0 - 1.0
        )
        assert granted is False
        assert time.monotonic() - t0 < budget(1.0)

    def test_no_deadline_still_waits_max_wait_s(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression pin: unbounded callers keep today's behaviour — the
        wait runs out ``max_wait_s``, not the deadline machinery."""
        self._use_conn(monkeypatch, _StarvedConn())
        t0 = time.monotonic()
        granted = rate_limit.acquire("test_rl_dl_plain", max_wait_s=0.3)
        elapsed = time.monotonic() - t0
        assert granted is False
        assert elapsed >= 0.25 and elapsed < budget(5.0)

    def test_call_with_deadline_publishes_the_ambient_bound(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The wired path: ``call_with_deadline`` publishes its bound as the
        ambient deadline on the worker thread. With a looser bound than the
        wait's own budget, the configured ``max_wait_s`` still fires — the
        ambient bound joins a ``min()``, it never widens."""
        from precis.utils.deadline import call_with_deadline, deadline_at

        self._use_conn(monkeypatch, _StarvedConn())
        granted, ambient = call_with_deadline(
            lambda: (
                rate_limit.acquire("test_rl_dl_cwd", max_wait_s=0.3),
                deadline_at(),
            ),
            deadline_s=30.0,
        )
        assert granted is False
        assert ambient is not None  # the bound was ambient inside the call
        assert rate_limit.refusal_counts()["test_rl_dl_cwd"] == 1

    def test_inner_deadline_scope_tightens_a_bounded_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ambient-bound composition end to end: a call bounded to 5 s
        whose inner scope leaves the limiter 0.3 s — the wait refuses at
        the tighter bound and the call returns normally (a wait consuming
        literally the whole outer bound races the outer abandonment by
        construction; the deterministic shape tests the tighter bound)."""
        from precis.utils.deadline import call_with_deadline, deadline_scope

        self._use_conn(monkeypatch, _StarvedConn())

        def _work() -> bool:
            with deadline_scope(time.monotonic() + 0.3):
                return rate_limit.acquire("test_rl_dl_cwd2", max_wait_s=300.0)

        t0 = time.monotonic()
        assert call_with_deadline(_work, deadline_s=5.0) is False
        assert time.monotonic() - t0 < budget(5.0)
        assert rate_limit.refusal_counts()["test_rl_dl_cwd2"] == 1

    def test_grant_under_deadline_still_grants(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A deadline must never refuse a grant that's already there — the
        bound only cuts the WAIT."""
        from precis.utils.deadline import deadline_scope

        self._use_conn(monkeypatch, _GrantConn())
        with deadline_scope(time.monotonic() + 0.1):
            assert rate_limit.acquire("test_rl_dl_grant", max_wait_s=300.0)
        assert "test_rl_dl_grant" not in rate_limit.refusal_counts()

    def test_require_raises_the_deadline_refusal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.utils.deadline import deadline_scope

        self._use_conn(monkeypatch, _StarvedConn())
        with deadline_scope(time.monotonic() + 0.2):
            with pytest.raises(rate_limit.RateLimited) as exc:
                rate_limit.require("test_rl_dl_req", max_wait_s=300.0)
        assert exc.value.provider == "test_rl_dl_req"

    def test_quota_refusal_is_counted_and_logged(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Refusal accounting is not deadline-only: the quota lane's
        instant refusal lands in the same counter and log."""
        self._use_conn(monkeypatch, _QuotaConn())
        with caplog.at_level("WARNING", logger=rate_limit.log.name):
            granted = rate_limit.acquire("test_rl_dl_quota", max_wait_s=5.0)
        assert granted is False
        assert rate_limit.refusal_counts()["test_rl_dl_quota"] == 1
        msgs = [
            r.getMessage()
            for r in caplog.records
            if "test_rl_dl_quota" in r.getMessage()
        ]
        assert len(msgs) == 1 and "daily quota exhausted" in msgs[0]

    def test_refusal_warning_is_rate_limited_per_provider(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A starved row refuses every call; the WARNING fires once per
        provider per window and carries the running count."""
        self._use_conn(monkeypatch, _StarvedConn())
        with caplog.at_level("WARNING", logger=rate_limit.log.name):
            for _ in range(3):
                assert rate_limit.acquire("test_rl_dl_spam", max_wait_s=0.05) is False
        # every refusal counted…
        assert rate_limit.refusal_counts()["test_rl_dl_spam"] == 3
        # …but logged once, with the running count at fire time
        msgs = [
            r.getMessage()
            for r in caplog.records
            if "test_rl_dl_spam" in r.getMessage()
        ]
        assert len(msgs) == 1
        assert "1 refusal(s)" in msgs[0]

    def test_observe_timing_records_a_deadline_cut_wait(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The 5165ecf80 instrumentation still reports the DB and sleep
        phases for a wait the deadline cuts — the measurement layer is
        orthogonal to admission."""
        from precis.utils.deadline import deadline_scope

        self._use_conn(monkeypatch, _StarvedConn())
        phases: list[str] = []

        def _record(provider: str, phase: str, elapsed_ms: float) -> None:
            phases.append(phase)

        with deadline_scope(time.monotonic() + 0.3):
            with rate_limit.observe_timing(_record):
                assert (
                    rate_limit.acquire("test_rl_dl_timing", max_wait_s=300.0) is False
                )
        assert "db" in phases and "sleep" in phases

    def test_deadline_scope_restores_and_isolation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ambient deadline is scoped: it does not leak out of the
        block, and a nested scope restores the outer value."""
        from precis.utils.deadline import deadline_at, deadline_scope

        assert deadline_at() is None
        outer = time.monotonic() + 60.0
        inner = time.monotonic() + 5.0
        with deadline_scope(outer):
            assert deadline_at() == outer
            with deadline_scope(inner):
                assert deadline_at() == inner
            assert deadline_at() == outer
        assert deadline_at() is None

    def test_real_row_refusal_under_deadline(
        self, use_store_dsn: Store, caplog: pytest.LogCaptureFixture
    ) -> None:
        """End to end on a real row: a drained bucket + an ambient deadline
        → the acquire returns ``False`` at the deadline, not at
        ``max_wait_s``, and the refusal is logged."""
        from precis.utils.deadline import deadline_scope

        provider = "test_rl_dl_real"
        _seed(use_store_dsn, provider, capacity=1, refill_per_sec=0.0001, tokens=0.0)
        try:
            t0 = time.monotonic()
            with caplog.at_level("WARNING", logger=rate_limit.log.name):
                with deadline_scope(t0 + 0.4):
                    granted = rate_limit.acquire(provider, max_wait_s=300.0)
            elapsed = time.monotonic() - t0
            assert granted is False
            assert elapsed < budget(5.0)
            assert rate_limit.refusal_counts()[provider] == 1
            assert any(
                provider in r.getMessage()
                for r in caplog.records
                if r.levelname == "WARNING"
            )
        finally:
            _cleanup(use_store_dsn, provider)


class TestRefusalCounter:
    """Refusal accounting: every counted refusal lands on the row's
    ``refused_today``/``last_refused_at`` (the durable half of
    :func:`rate_limit._record_refusal`), and the ``day_start`` rollover
    clears it daily — whether the day's first touch is a grant or another
    refusal."""

    def test_starved_refusal_writes_the_counter(self, use_store_dsn: Store) -> None:
        provider = "test_rl_rc_starved"
        _seed(use_store_dsn, provider, capacity=1, refill_per_sec=0.0001, tokens=0.0)
        try:
            assert rate_limit.acquire(provider, max_wait_s=0.1) is False
            row = _refusal_cols(use_store_dsn, provider)
            assert row is not None
            refused_today, last_refused_at, _used, _start = row
            assert int(refused_today) == 1
            assert last_refused_at is not None
            if last_refused_at.tzinfo is None:
                last_refused_at = last_refused_at.replace(tzinfo=UTC)
            assert (datetime.now(UTC) - last_refused_at).total_seconds() < 60
            assert rate_limit.refusal_counts()[provider] == 1
        finally:
            _cleanup(use_store_dsn, provider)

    def test_quota_refusal_writes_the_counter(self, use_store_dsn: Store) -> None:
        provider = "test_rl_rc_quota"
        _seed(
            use_store_dsn,
            provider,
            capacity=10,
            refill_per_sec=100.0,
            daily_cap=5,
            day_used=5,
        )
        try:
            assert rate_limit.acquire(provider, max_wait_s=0.1) is False
            row = _refusal_cols(use_store_dsn, provider)
            assert row is not None and int(row[0]) == 1 and row[1] is not None
        finally:
            _cleanup(use_store_dsn, provider)

    def test_refusal_on_a_stale_day_rolls_both_counters(
        self, use_store_dsn: Store
    ) -> None:
        """A refusal can be the row's first touch of a new day: the write
        must roll ``day_used`` over too, or yesterday's spend would count
        against today's cap — and ``refused_today`` restarts at 1 (this
        refusal), not yesterday's leftover."""
        provider = "test_rl_rc_rollover"
        _seed(use_store_dsn, provider, capacity=1, refill_per_sec=0.0001, tokens=0.0)
        yesterday = datetime.now(UTC).date() - timedelta(days=1)
        with use_store_dsn.pool.connection() as conn:
            conn.execute(
                "UPDATE external_rate_limits SET day_start = %s, day_used = 9, "
                "refused_today = 7 WHERE provider = %s",
                (yesterday, provider),
            )
            conn.commit()
        try:
            assert rate_limit.acquire(provider, max_wait_s=0.1) is False
            row = _refusal_cols(use_store_dsn, provider)
            assert row is not None
            refused_today, _last, day_used, day_start = row
            assert int(refused_today) == 1  # rolled over, then this refusal
            assert int(day_used) == 0  # yesterday's spend cleared
            assert day_start == datetime.now(UTC).date()
        finally:
            _cleanup(use_store_dsn, provider)

    def test_grant_on_a_stale_day_clears_refused_today(
        self, use_store_dsn: Store
    ) -> None:
        """The consume path's rollover: a grant on a row whose day turned
        writes ``refused_today`` back to 0 — the counter is demand TODAY,
        not a lifetime tally."""
        provider = "test_rl_rc_grantroll"
        _seed(use_store_dsn, provider, capacity=2, refill_per_sec=1.0, tokens=2)
        yesterday = datetime.now(UTC).date() - timedelta(days=1)
        with use_store_dsn.pool.connection() as conn:
            conn.execute(
                "UPDATE external_rate_limits SET day_start = %s, "
                "refused_today = 7 WHERE provider = %s",
                (yesterday, provider),
            )
            conn.commit()
        try:
            assert rate_limit.acquire(provider, max_wait_s=1.0) is True
            row = _refusal_cols(use_store_dsn, provider)
            assert row is not None
            assert int(row[0]) == 0
            assert row[1] is None  # no refusal today: timestamp stays unset
        finally:
            _cleanup(use_store_dsn, provider)
