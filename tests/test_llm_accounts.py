"""Tests for the llm_account / pool ledger reader (migration 0280, S1a).

Covers: the five seeded accounts and six seeded pools read back correctly
(acceptance a); the reset-rule grammar round-trips and refuses garbage
(acceptance d); ``next_reset`` is correct for rolling and calendar rules,
including across a UTC/local day boundary (acceptance d). Real PG.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

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
)


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
