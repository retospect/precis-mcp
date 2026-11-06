"""Tests for the llm_account / pool ledger reader (migration 0280, S1a).

Covers: the five seeded accounts and six seeded pools read back correctly
(acceptance a); the reset-rule grammar round-trips and refuses garbage
(acceptance d); ``next_reset`` is correct for rolling and calendar rules,
including across a UTC/local day boundary (acceptance d). Real PG.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from psycopg import sql

from precis.utils.llm.accounts import (
    CalendarReset,
    ExpiresReset,
    RollingReset,
    format_reset_rule,
    get_account,
    list_accounts,
    list_pools,
    next_reset,
    parse_reset_rule,
    record_pool_failure,
    record_pool_report,
)


@pytest.fixture
def restore_seeded_pools(store: Any) -> Iterator[None]:
    """Preserved tables must be restored even when randomly ordered."""
    with store.pool.connection() as conn:
        cursor = conn.execute(
            "SELECT * FROM external_rate_limits WHERE provider = ANY(%s)",
            (
                [
                    "llm:claude-sub:5h",
                    "llm:claude-sub:7d",
                    "llm:codex:weekly",
                    "llm:kimi:monthly",
                    "llm:devin:sessions",
                    "llm:openrouter:credits",
                ],
            ),
        )
        rows = cursor.fetchall()
        assert cursor.description is not None
        columns = [column.name for column in cursor.description]
    try:
        yield
    finally:
        assignments = sql.SQL(", ").join(
            sql.SQL("{} = %s").format(sql.Identifier(column)) for column in columns
        )
        statement = sql.SQL(
            "UPDATE external_rate_limits SET {} WHERE provider = %s"
        ).format(assignments)
        key_index = columns.index("provider")
        with store.pool.connection() as conn:
            for row in rows:
                conn.execute(statement, (*row, row[key_index]))


@pytest.mark.usefixtures("restore_seeded_pools")
class TestPoolReport:
    def test_monotone_guard(self, store: Any) -> None:
        now = datetime.now(UTC)
        key = "llm:claude-sub:5h"
        assert record_pool_report(
            store,
            key,
            used=55,
            remaining=45,
            limit=100,
            source="provider",
            reported_at=now,
        )
        assert not record_pool_report(
            store,
            key,
            used=12,
            source="estimate",
            reported_at=now - timedelta(seconds=1),
        )
        pool = next(p for p in list_pools(store) if p.provider == key)
        assert pool.used_reported == 55
        assert pool.remaining_reported == 45
        assert pool.reported_at == now
        assert pool.source == "provider"

    def test_none_keeps_stored_values_and_advances_reported_at(
        self, store: Any
    ) -> None:
        """A report that does not carry used / remaining / resets_at must not
        null what an earlier full report stored; a later value still wins."""
        key = "llm:claude-sub:5h"
        first = datetime.now(UTC)
        reset = first + timedelta(hours=3)
        assert record_pool_report(
            store,
            key,
            used=55,
            remaining=45,
            limit=100,
            source="provider",
            reported_at=first,
            resets_at=reset,
        )
        second = first + timedelta(seconds=30)
        assert record_pool_report(
            store, key, used=None, source="provider", reported_at=second
        )
        pool = next(p for p in list_pools(store) if p.provider == key)
        assert pool.used_reported == 55
        assert pool.remaining_reported == 45
        assert pool.limit_reported == 100
        assert pool.resets_at == reset
        assert pool.reported_at == second
        third = second + timedelta(seconds=30)
        new_reset = reset + timedelta(hours=1)
        assert record_pool_report(
            store,
            key,
            used=60,
            remaining=40,
            source="provider",
            reported_at=third,
            resets_at=new_reset,
        )
        pool = next(p for p in list_pools(store) if p.provider == key)
        assert pool.used_reported == 60
        assert pool.remaining_reported == 40
        assert pool.resets_at == new_reset
        assert pool.reported_at == third

    def test_park_only_extends_and_tracks_provenance(self, store: Any) -> None:
        key = "llm:devin:sessions"
        until = datetime.now(UTC) + timedelta(days=1)
        assert record_pool_failure(
            store, key, reason_class="paused", retry_at=until, notice="provider"
        )
        assert record_pool_failure(
            store, key, reason_class="paused", retry_at=until - timedelta(hours=1)
        )
        pool = next(p for p in list_pools(store) if p.provider == key)
        assert pool.parked_until == until
        assert pool.source == "provider"
        later = until + timedelta(hours=1)
        assert record_pool_failure(store, key, reason_class="paused", retry_at=later)
        pool = next(p for p in list_pools(store) if p.provider == key)
        assert pool.parked_until == later
        assert pool.source == "estimate"

    @pytest.mark.parametrize(
        "key", ["llm:missing:weekly", "host:pool-writer-unknown.example"]
    )
    def test_unknown_key_refuses_without_inserting(
        self, store: Any, caplog: pytest.LogCaptureFixture, key: str
    ) -> None:
        now = datetime.now(UTC)
        assert not record_pool_report(store, key, source="provider", reported_at=now)
        assert not record_pool_failure(store, key, reason_class="quota", retry_at=now)
        assert "unknown pool key" in caplog.text
        with store.pool.connection() as conn:
            assert (
                conn.execute(
                    "SELECT 1 FROM external_rate_limits WHERE provider = %s", (key,)
                ).fetchone()
                is None
            )

    def test_limit_only_fills_null_and_decimal_readback(self, store: Any) -> None:
        now = datetime.now(UTC)
        key = "llm:codex:weekly"
        assert record_pool_report(
            store,
            key,
            used=Decimal("37.25"),
            remaining=Decimal("62.75"),
            limit=100,
            source="provider",
            reported_at=now,
        )
        assert record_pool_report(
            store,
            key,
            used=Decimal("37.25"),
            remaining=Decimal("62.75"),
            limit=200,
            source="provider",
            reported_at=now,
        )
        assert record_pool_report(
            store,
            "llm:devin:sessions",
            limit=100,
            source="provider",
            reported_at=now,
        )
        pools = {p.provider: p for p in list_pools(store)}
        assert pools[key].limit_reported == 100
        assert pools[key].used_reported == 37.25
        assert pools[key].remaining_reported == 62.75
        assert pools["llm:devin:sessions"].limit_reported == 1
        assert pools[key].unit == "percent"
        assert pools["llm:claude-sub:5h"].reset_rule == "rolling:18000"
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT used_reported FROM external_rate_limits WHERE provider = %s",
                (key,),
            ).fetchone()
        assert row is not None
        assert row[0] == Decimal("37.25")

    def test_stale_boundary(self, store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        from precis.utils.llm import accounts

        now = datetime(2026, 11, 6, tzinfo=UTC)

        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> FixedDatetime:
                return cls(2026, 11, 6, tzinfo=UTC)

        monkeypatch.setattr(accounts, "datetime", FixedDatetime)
        monkeypatch.setattr(accounts.settings, "get_int", lambda *args, **kwargs: 7200)
        keys = ("llm:claude-sub:5h", "llm:claude-sub:7d", "llm:codex:weekly")
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE external_rate_limits SET reported_at = NULL "
                "WHERE provider = ANY(%s)",
                (list(keys),),
            )
        record_pool_report(
            store,
            keys[0],
            source="provider",
            reported_at=now - timedelta(seconds=7200),
        )
        record_pool_report(
            store,
            keys[1],
            source="provider",
            reported_at=now - timedelta(seconds=7200, microseconds=1),
        )
        pools = {p.provider: p for p in list_pools(store)}
        assert pools[keys[0]].stale is False
        assert pools[keys[1]].stale is True
        assert pools[keys[2]].stale is True


class TestSeededAccounts:
    def test_five_accounts_seeded(self, store: Any) -> None:
        accounts = list_accounts(store)
        names = {a.name for a in accounts}
        assert names == {"claude-sub", "codex", "devin", "kimi", "metered"}

    def test_claude_sub_fields(self, store: Any) -> None:
        a = get_account(store, "claude-sub")
        assert a is not None
        assert a.vendor == "anthropic"
        assert a.plan == "subscription"
        assert a.adapter == "claude_p"
        assert a.concurrency == 2
        assert a.task_classes == ("*",)
        assert "claude-sonnet-5" in a.models
        assert a.default_model == "claude-sonnet-5"
        assert a.enabled is True
        assert a.api_key_ref is None
        assert set(a.pools) == {"llm:claude-sub:5h", "llm:claude-sub:7d"}

    def test_devin_fields_and_entitlement(self, store: Any) -> None:
        a = get_account(store, "devin")
        assert a is not None
        assert a.vendor == "cognition"
        assert a.plan == "free-promo"
        assert a.adapter == "devin_tree"
        assert a.enabled is False
        assert a.api_key_ref == "DEVIN_API_TOKEN"
        assert a.promo_expiry is not None and a.promo_expiry.isoformat() == "2026-11-30"
        assert "source" in a.entitlement
        assert a.pools == ("llm:devin:sessions",)

    def test_codex_has_empty_models_and_no_default(self, store: Any) -> None:
        a = get_account(store, "codex")
        assert a is not None
        assert a.models == ()
        assert a.default_model is None
        assert a.api_key_ref is None
        assert a.pools == ("llm:codex:weekly",)

    def test_kimi_and_metered_disabled_with_api_key_ref(self, store: Any) -> None:
        kimi = get_account(store, "kimi")
        assert kimi is not None
        assert kimi.enabled is False
        assert kimi.api_key_ref == "KIMI_CODE_API_KEY"
        assert kimi.pools == ("llm:kimi:monthly",)

        metered = get_account(store, "metered")
        assert metered is not None
        assert metered.enabled is False
        assert metered.api_key_ref is None
        assert metered.pools == ("llm:openrouter:credits",)

    def test_get_account_missing_is_none(self, store: Any) -> None:
        assert get_account(store, "no-such-account") is None


class TestSeededPools:
    def test_six_pools_seeded(self, store: Any) -> None:
        pools = {p.provider for p in list_pools(store)}
        assert pools == {
            "llm:claude-sub:5h",
            "llm:claude-sub:7d",
            "llm:codex:weekly",
            "llm:kimi:monthly",
            "llm:devin:sessions",
            "llm:openrouter:credits",
        }

    def test_carrier_shape_keeps_rate_limit_working(self, store: Any) -> None:
        pools = {p.provider: p for p in list_pools(store)}
        for p in pools.values():
            assert p.capacity == 1
            assert p.refill_per_sec == 1.0
            assert p.tokens == 1
            assert p.cls == "llm-api"
            assert p.daily_cap is None

    def test_reported_values_null_except_devin_sessions(self, store: Any) -> None:
        pools = {p.provider: p for p in list_pools(store)}
        assert pools["llm:devin:sessions"].limit_reported == 1
        assert pools["llm:devin:sessions"].unit == "sessions"
        for key in (
            "llm:claude-sub:5h",
            "llm:claude-sub:7d",
            "llm:codex:weekly",
            "llm:kimi:monthly",
            "llm:openrouter:credits",
        ):
            assert pools[key].limit_reported is None
            assert pools[key].used_reported is None
            assert pools[key].reported_at is None

    def test_reset_rules_seeded(self, store: Any) -> None:
        pools = {p.provider: p for p in list_pools(store)}
        assert pools["llm:claude-sub:5h"].reset_rule == "rolling:18000"
        assert pools["llm:claude-sub:7d"].reset_rule == "rolling:604800"
        assert pools["llm:codex:weekly"].reset_rule is None
        assert pools["llm:kimi:monthly"].reset_rule is None
        assert pools["llm:devin:sessions"].reset_rule is None

    def test_host_rows_excluded(self, store: Any) -> None:
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO external_rate_limits (provider, capacity, "
                "refill_per_sec, tokens) VALUES ('host:example.com', 1, 1.0, 1) "
                "ON CONFLICT (provider) DO NOTHING"
            )
        pools = {p.provider for p in list_pools(store)}
        assert "host:example.com" not in pools


class TestParseResetRule:
    def test_rolling_round_trips(self) -> None:
        rule = parse_reset_rule("rolling:18000")
        assert rule == RollingReset(18000)
        assert format_reset_rule(rule) == "rolling:18000"

    def test_calendar_daily_round_trips(self) -> None:
        text = "calendar:daily:00:00@UTC"
        rule = parse_reset_rule(text)
        assert isinstance(rule, CalendarReset)
        assert rule.period == "daily"
        assert rule.hour == 0 and rule.minute == 0
        assert rule.tz == "UTC"
        assert format_reset_rule(rule) == text

    def test_calendar_weekly_round_trips(self) -> None:
        text = "calendar:weekly:mon:09:30@America/New_York"
        rule = parse_reset_rule(text)
        assert isinstance(rule, CalendarReset)
        assert rule.weekday == 0
        assert format_reset_rule(rule) == text

    def test_calendar_monthly_round_trips(self) -> None:
        text = "calendar:monthly:1:00:00@UTC"
        rule = parse_reset_rule(text)
        assert isinstance(rule, CalendarReset)
        assert rule.day_of_month == 1
        assert format_reset_rule(rule) == text

    def test_expires_round_trips(self) -> None:
        text = "expires:2026-11-30T00:00:00Z"
        rule = parse_reset_rule(text)
        assert isinstance(rule, ExpiresReset)
        assert rule.at == datetime(2026, 11, 30, tzinfo=UTC)
        assert format_reset_rule(rule) == text

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "garbage",
            "rolling:",
            "rolling:0",
            "rolling:-5",
            "rolling:abc",
            "calendar:yearly:00:00@UTC",
            "calendar:daily:00:00",
            "calendar:daily:25:00@UTC",
            "calendar:daily:00:00@Not/A_Zone",
            "calendar:weekly:oops:00:00@UTC",
            "calendar:monthly:31:00:00@UTC",
            "expires:not-a-timestamp",
            "expires:2026-11-30T00:00:00",
        ],
    )
    def test_refuses_garbage(self, text: str) -> None:
        with pytest.raises(ValueError):
            parse_reset_rule(text)


class TestNextReset:
    def test_rolling_adds_seconds(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        rule = RollingReset(18000)
        assert next_reset(rule, now) == datetime(2026, 1, 1, 5, 0, 0, tzinfo=UTC)

    def test_expires_returns_fixed_instant(self) -> None:
        at = datetime(2026, 11, 30, tzinfo=UTC)
        rule = ExpiresReset(at)
        now = datetime(2026, 1, 1, tzinfo=UTC)
        assert next_reset(rule, now) == at

    def test_daily_calendar_across_tz_boundary(self) -> None:
        # 2026-01-01T03:00:00Z is 2025-12-31T22:00:00 in America/New_York
        # (EST, UTC-5) — the local calendar day is still Dec 31. The next
        # 00:00 America/New_York reset is Jan 1 00:00 local = 05:00Z.
        rule = parse_reset_rule("calendar:daily:00:00@America/New_York")
        now = datetime(2026, 1, 1, 3, 0, 0, tzinfo=UTC)
        got = next_reset(rule, now)
        assert got == datetime(2026, 1, 1, 5, 0, 0, tzinfo=UTC)

    def test_daily_calendar_same_utc_day_when_past_anchor(self) -> None:
        rule = parse_reset_rule("calendar:daily:00:00@UTC")
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        got = next_reset(rule, now)
        assert got == datetime(2026, 1, 2, 0, 0, 0, tzinfo=UTC)

    def test_weekly_calendar_next_occurrence(self) -> None:
        # 2026-01-01 is a Thursday; next Monday 00:00 UTC is 2026-01-05.
        rule = parse_reset_rule("calendar:weekly:mon:00:00@UTC")
        now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        got = next_reset(rule, now)
        assert got == datetime(2026, 1, 5, 0, 0, 0, tzinfo=UTC)

    def test_monthly_calendar_rolls_to_next_month(self) -> None:
        rule = parse_reset_rule("calendar:monthly:1:00:00@UTC")
        now = datetime(2026, 1, 15, 0, 0, 0, tzinfo=UTC)
        got = next_reset(rule, now)
        assert got == datetime(2026, 2, 1, 0, 0, 0, tzinfo=UTC)

    def test_monthly_calendar_rolls_year_boundary(self) -> None:
        rule = parse_reset_rule("calendar:monthly:1:00:00@UTC")
        now = datetime(2026, 12, 15, 0, 0, 0, tzinfo=UTC)
        got = next_reset(rule, now)
        assert got == datetime(2027, 1, 1, 0, 0, 0, tzinfo=UTC)

    def test_naive_now_rejected(self) -> None:
        rule = RollingReset(60)
        with pytest.raises(ValueError):
            next_reset(rule, datetime(2026, 1, 1))
