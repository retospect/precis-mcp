"""Pool pacing and pool-aware gates (S3, td568206; memory me568188).

The pure cases pin :func:`precis.utils.llm.accounts.pacing` at its
boundaries. The DB cases (real PG; the six seeded pools are restored and
every inserted ``llm:<profile>:calls|tokens`` row is deleted) pin the
breaker's background pause on an ahead Claude pool and the subscription
window cap kept in pool rows, including across two processes.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from precis.budget import breaker
from precis.budget.breaker import GateRefusal
from precis.utils.llm import router
from precis.utils.llm.accounts import (
    DRAIN_WINDOW,
    PACE_TARGET,
    Pool,
    add_pool_usage,
    consume_cap,
    list_pools,
    pacing,
    record_pool_report,
)
from precis.utils.llm.router import LlmRequest, LlmResult, Tier
from tests.test_llm_accounts import restore_seeded_pools  # noqa: F401  fixture

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def _pool(**over: Any) -> Pool:
    """A ``percent`` pool resetting three days after :data:`NOW`."""
    fields: dict[str, Any] = {
        "provider": "llm:example:7d",
        "capacity": 1,
        "refill_per_sec": 1.0,
        "tokens": 1.0,
        "daily_cap": None,
        "cls": "llm-api",
        "unit": "percent",
        "limit_reported": 100.0,
        "used_reported": None,
        "remaining_reported": None,
        "reported_at": NOW,
        "source": "provider",
        "confidence": None,
        "reset_rule": "rolling:604800",
        "resets_at": NOW + timedelta(days=3),
        "expires_at": None,
        "parked_until": None,
        "stale": False,
    }
    fields.update(over)
    return Pool(**fields)


# ── pacing(), pure ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("used", "resets_in", "state"),
    [
        (50.0, timedelta(days=3), "behind"),
        (94.5, timedelta(days=3), "on-pace"),
        (96.0, timedelta(days=3), "ahead"),
        (96.0, timedelta(hours=5), "behind"),
        (99.5, timedelta(hours=5), "on-pace"),
        (100.0, timedelta(days=3), "exhausted"),
    ],
    ids=[
        "half-behind",
        "0.945-on-pace",
        "0.96-ahead",
        "0.96-in-drain-behind",
        "0.995-in-drain-on-pace",
        "full-exhausted",
    ],
)
def test_pacing_states(used: float, resets_in: timedelta, state: str) -> None:
    pool = _pool(used_reported=used, resets_at=NOW + resets_in)
    assert pacing(pool, NOW).state == state


def test_ahead_retries_when_the_drain_opens() -> None:
    reset = NOW + timedelta(days=3)
    pace = pacing(_pool(used_reported=96.0, resets_at=reset), NOW)
    assert pace.state == "ahead"
    assert pace.target == PACE_TARGET
    assert pace.horizon == reset
    assert pace.retry_at == reset - DRAIN_WINDOW


def test_drain_targets_the_whole_pool() -> None:
    pace = pacing(_pool(used_reported=99.5, resets_at=NOW + timedelta(hours=5)), NOW)
    assert (pace.state, pace.target, pace.retry_at) == ("on-pace", 1.0, None)


def test_exhausted_retries_at_reset_or_park_end() -> None:
    reset = NOW + timedelta(days=3)
    full = pacing(_pool(used_reported=100.0, resets_at=reset), NOW)
    assert (full.state, full.retry_at) == ("exhausted", reset)
    park = NOW + timedelta(minutes=30)
    parked = pacing(_pool(used_reported=50.0, parked_until=park), NOW)
    assert (parked.state, parked.retry_at) == ("exhausted", park)
    # A park that already ended leaves the reading alone.
    ended = pacing(_pool(used_reported=50.0, parked_until=NOW), NOW)
    assert ended.state == "behind"


@pytest.mark.parametrize(
    "over",
    [
        {"used_reported": 96.0, "resets_at": NOW - timedelta(minutes=1)},
        {"used_reported": None, "remaining_reported": None},
        {"used_reported": 9.6, "unit": "usd", "limit_reported": None},
    ],
    ids=["past-reset", "null-used", "no-limit"],
)
def test_unknown_without_horizon_or_fraction(over: dict[str, Any]) -> None:
    pace = pacing(_pool(**over), NOW)
    assert (pace.state, pace.retry_at) == ("unknown", None)


def test_fraction_from_remaining_and_percent_default_limit() -> None:
    assert pacing(_pool(remaining_reported=4.0), NOW).fraction == pytest.approx(0.96)
    no_limit = pacing(_pool(used_reported=96.0, limit_reported=None), NOW)
    assert no_limit.state == "ahead"
    assert no_limit.fraction == pytest.approx(0.96)


def test_expiring_credit_paces_to_its_expiry() -> None:
    def credit(expires_in: timedelta) -> str:
        pool = _pool(
            unit="usd",
            limit_reported=10.0,
            used_reported=9.6,
            reset_rule=None,
            resets_at=None,
            expires_at=NOW + expires_in,
        )
        return pacing(pool, NOW).state

    assert credit(timedelta(days=3)) == "ahead"
    assert credit(timedelta(hours=2)) == "behind"


def test_earlier_of_reset_and_expiry_wins() -> None:
    soon, later = NOW + timedelta(hours=2), NOW + timedelta(days=3)
    expiring = pacing(_pool(used_reported=96.0, resets_at=later, expires_at=soon), NOW)
    assert (expiring.horizon, expiring.state) == (soon, "behind")
    resetting = pacing(_pool(used_reported=96.0, resets_at=soon, expires_at=later), NOW)
    assert (resetting.horizon, resetting.state) == (soon, "behind")
    both_far = pacing(
        _pool(
            used_reported=96.0, resets_at=later, expires_at=later + timedelta(days=1)
        ),
        NOW,
    )
    assert (both_far.horizon, both_far.retry_at) == (later, later - DRAIN_WINDOW)


def test_gate_fails_open_on_a_store_without_pool(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A fake store with no ``.pool`` (quota snapshot only) cannot read
    the ledger: the pool gate admits quietly rather than failing the call
    or warning on every gate."""
    fake: Any = SimpleNamespace(read_claude_quota=lambda scope="unified": None)
    with caplog.at_level("DEBUG", logger="precis.budget.breaker"):
        assert breaker.gate_tier(Tier.BIG, transport="claude_p", store=fake) is None
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("no pool ledger" in r.getMessage() for r in caplog.records)


# ── gate and cap on the pool ledger (real PG) ───────────────────────────


@pytest.fixture
def drop_cap_pools(store: Any) -> Iterator[None]:
    """Cap rows are inserted, never seeded, into a table the suite never
    truncates: delete every ``llm:<profile>:calls|tokens`` row around the
    test so the seeded six stay the only pools."""

    def drop() -> None:
        with store.pool.connection() as conn:
            conn.execute(
                "DELETE FROM external_rate_limits "
                "WHERE provider LIKE %s OR provider LIKE %s",
                ("llm:%:calls", "llm:%:tokens"),
            )

    drop()
    yield
    drop()


def _report_7d(store: Any, *, used: float, resets_at: datetime) -> None:
    assert record_pool_report(
        store,
        "llm:claude-sub:7d",
        used=used,
        remaining=100 - used,
        limit=100,
        source="provider",
        reported_at=datetime.now(UTC),
        resets_at=resets_at,
    )


def _pools(store: Any) -> dict[str, Pool]:
    return {pool.provider: pool for pool in list_pools(store)}


#: A subscription profile with a one-call window cap (example endpoint).
_SUB_PROFILES = {
    "sub": {
        "base_url": "https://sub.example.invalid/v1",
        "api_key_ref": "SUB_LLM_KEY",
        "kind": "subscription",
        "cap": {"calls": 1, "window_s": 3600},
    }
}


@pytest.mark.usefixtures("restore_seeded_pools", "drop_cap_pools")
class TestPoolGates:
    def test_ahead_claude_pool_pauses_background_only(self, store: Any) -> None:
        reset = datetime.now(UTC) + timedelta(days=3)
        _report_7d(store, used=96, resets_at=reset)

        verdict = breaker.gate_tier(Tier.BIG, transport="claude_p", store=store)

        assert isinstance(verdict, GateRefusal)
        assert verdict.startswith("budget: claude pool llm:claude-sub:7d is ahead")
        assert verdict.retry_at == reset - DRAIN_WINDOW
        for work_class in ("deliverable", "interactive"):
            assert (
                breaker.gate_tier(
                    Tier.BIG, transport="claude_p", store=store, work_class=work_class
                )
                is None
            ), work_class

    def test_drain_window_admits_background(self, store: Any) -> None:
        _report_7d(store, used=96, resets_at=datetime.now(UTC) + timedelta(hours=2))
        assert breaker.gate_tier(Tier.BIG, transport="claude_p", store=store) is None

    def test_resume_override_admits_background(self, store: Any) -> None:
        from precis.budget import settings as budget_settings

        _report_7d(store, used=96, resets_at=datetime.now(UTC) + timedelta(days=3))
        until = datetime.now(UTC) + timedelta(hours=1)
        budget_settings.set_setting(
            store, budget_settings.RESUME_UNTIL_KEY, until.isoformat()
        )
        assert breaker.gate_tier(Tier.BIG, transport="claude_p", store=store) is None

    def test_consume_cap_upserts_then_restarts_after_reset(self, store: Any) -> None:
        now = datetime.now(UTC)
        window = 3600.5

        def spend(amount: int, limit: int, at: datetime) -> bool:
            return consume_cap(
                store,
                "example-sub",
                axis="tokens",
                amount=amount,
                limit=limit,
                window_s=window,
                now=at,
            )

        assert spend(40, 100, now)
        assert spend(25, 120, now + timedelta(minutes=1))
        row = _pools(store)["llm:example-sub:tokens"]
        assert (row.used_reported, row.limit_reported) == (65, 120)
        assert (row.unit, row.reset_rule, row.source) == (
            "tokens",
            "rolling:3601",
            "estimate",
        )
        assert row.resets_at == now + timedelta(seconds=window)
        assert (row.capacity, row.refill_per_sec, row.tokens) == (1, 1.0, 1)
        assert (row.daily_cap, row.cls) == (None, "llm-api")

        later = now + timedelta(seconds=window + 1)
        assert spend(5, 120, later)
        row = _pools(store)["llm:example-sub:tokens"]
        assert row.used_reported == 5
        assert row.resets_at == later + timedelta(seconds=window)

    def test_consume_cap_refuses_other_keys(
        self, store: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        now = datetime.now(UTC)
        with caplog.at_level("WARNING", logger="precis.utils.llm.accounts"):
            for profile, axis in (("example-sub", "minutes"), ("Example Sub", "calls")):
                assert not consume_cap(
                    store,
                    profile,
                    axis=axis,
                    amount=1,
                    limit=1,
                    window_s=60,
                    now=now,
                )
        assert sum("unknown pool key" in r.message for r in caplog.records) == 2
        pools = _pools(store)
        assert "llm:example-sub:minutes" not in pools
        assert "llm:Example Sub:calls" not in pools

    def test_cap_ahead_of_pace_refuses_background_only(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A week-long cap at 96 of 100 calls, three days before its reset,
        reads ahead: background calls skip until the drain window opens,
        interactive calls run, and a raised cap applies at once."""
        week = 7 * 86400
        profiles = {
            "example-sub": {
                **_SUB_PROFILES["sub"],
                "cap": {"calls": 100, "window_s": week},
            }
        }
        monkeypatch.setenv("PRECIS_LLM_PROFILES", json.dumps(profiles))
        monkeypatch.setattr("precis.budget.meter.active_store", lambda: store)
        opened = datetime.now(UTC) - timedelta(days=4)
        assert consume_cap(
            store,
            "example-sub",
            axis="calls",
            amount=96,
            limit=100,
            window_s=week,
            now=opened,
        )
        profile = router.resolve_profile("example-sub")
        assert profile is not None
        req = LlmRequest(tier=Tier.MEDIUM, prompt="x")

        refusal = router._subscription_refusal(profile, req, model="m")

        assert refusal is not None
        assert (refusal.reason_class, refusal.paused) == ("subscription_cap", True)
        assert "window cap" in (refusal.error or "")
        assert refusal.retry_at == opened + timedelta(seconds=week) - DRAIN_WINDOW
        interactive = LlmRequest(tier=Tier.MEDIUM, prompt="x", work_class="interactive")
        assert router._subscription_refusal(profile, interactive, model="m") is None
        profiles["example-sub"]["cap"] = {"calls": 1000, "window_s": week}
        monkeypatch.setenv("PRECIS_LLM_PROFILES", json.dumps(profiles))
        raised = router.resolve_profile("example-sub")
        assert raised is not None
        assert router._subscription_refusal(raised, req, model="m") is None

    def test_caps_hold_across_two_processes(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The parent spends profile ``sub``'s one-call cap and reports the
        7-day Claude pool ahead; a separate process bound to the same DB
        sees both — the cap refuses and the background gate refuses. The
        child runs as a subprocess on purpose: the old per-process ledger
        passed any single-process version of this test."""
        from tests.conftest import _active_dsn

        monkeypatch.setenv("PRECIS_LLM_PROFILES", json.dumps(_SUB_PROFILES))
        monkeypatch.setattr("precis.budget.meter.active_store", lambda: store)
        profile = router.resolve_profile("sub")
        assert profile is not None
        router._subscription_tally(
            profile,
            LlmResult(
                text="ok",
                cost_usd=None,
                turns_used=None,
                model="m",
                tier=Tier.MEDIUM,
                total_tokens=5,
            ),
        )
        assert _pools(store)["llm:sub:calls"].used_reported == 1
        _report_7d(store, used=96, resets_at=datetime.now(UTC) + timedelta(days=3))

        child = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from precis.budget import breaker, meter\n"
                    "from precis.store import Store\n"
                    "from precis.utils.llm import router\n"
                    "from precis.utils.llm.router import LlmRequest, Tier\n"
                    f"store = Store.connect({_active_dsn()!r})\n"
                    "meter.bind_store(store)\n"
                    "try:\n"
                    "    profile = router.resolve_profile('sub')\n"
                    "    req = LlmRequest(tier=Tier.MEDIUM, prompt='x')\n"
                    "    refusal = router._subscription_refusal(profile, req, "
                    "model='m')\n"
                    "    print(refusal.reason_class if refusal else 'admitted')\n"
                    "    verdict = breaker.gate_tier(Tier.BIG, transport='claude_p')\n"
                    "    print('refused' if verdict is not None else 'admitted')\n"
                    "finally:\n"
                    "    store.close()\n"
                ),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=120,
        )

        assert child.returncode == 0, child.stderr
        assert child.stdout.split()[-2:] == ["subscription_cap", "refused"], (
            f"stdout={child.stdout!r} stderr={child.stderr!r}"
        )


@pytest.mark.usefixtures("restore_seeded_pools")
class TestAddPoolUsage:
    """add_pool_usage (td572038): caller-side metering into a seeded pool's
    ``used_reported``, with the row's reset rule deciding when a new
    window opens. Real PG; the seeded pools are restored per test."""

    def test_add_pool_usage_accumulates_within_window(self, store: Any) -> None:
        key = "llm:devin:acu"
        now = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)  # a Friday
        assert add_pool_usage(store, key, 2.0, now=now)
        first = _pools(store)[key]
        assert first.used_reported == 2.0
        # resets_at seeded NULL → the first write opens the window at the
        # rule's next instant: Sunday 08:00 Etc/UTC = 2026-11-08T08:00Z.
        assert first.resets_at == datetime(2026, 11, 8, 8, 0, tzinfo=UTC)
        later = now + timedelta(minutes=5)
        assert add_pool_usage(store, key, 2.5, now=later)
        row = _pools(store)[key]
        assert row.used_reported == 4.5
        assert row.resets_at == first.resets_at
        assert row.reported_at == later
        assert row.source == "estimate"

    def test_add_pool_usage_restarts_after_reset_and_stamps_weekly_resets_at(
        self, store: Any
    ) -> None:
        key = "llm:devin:acu"
        now = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE external_rate_limits SET used_reported = 9, "
                "resets_at = %s WHERE provider = %s",
                (now - timedelta(hours=1), key),
            )
        assert add_pool_usage(store, key, 1.25, now=now)
        row = _pools(store)[key]
        # The past resets_at ends the window: used restarts at the new
        # amount (never 10.25) and resets_at moves to the next weekly
        # anchor after now.
        assert row.used_reported == 1.25
        assert row.resets_at == datetime(2026, 11, 8, 8, 0, tzinfo=UTC)

    def test_add_pool_usage_no_rule_accumulates_forever(self, store: Any) -> None:
        key = "llm:openrouter:credits"  # seeded with reset_rule NULL
        now = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
        assert add_pool_usage(store, key, 0.5, now=now)
        assert add_pool_usage(store, key, Decimal("1.25"), now=now + timedelta(days=30))
        row = _pools(store)[key]
        assert row.used_reported == 1.75
        assert row.resets_at is None

    def test_add_pool_usage_refuses_unknown_key_and_nonpositive(
        self, store: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        now = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
        with caplog.at_level("WARNING", logger="precis.utils.llm.accounts"):
            assert not add_pool_usage(store, "llm:missing:weekly", 1, now=now)
            assert not add_pool_usage(
                store, "host:pool-usage-unknown.example", 1, now=now
            )
            for bad in (0, -3, math.nan, math.inf):
                assert not add_pool_usage(store, "llm:devin:acu", bad, now=now)
        assert "unknown pool key" in caplog.text
        # Nothing landed: the acu row keeps its seeded NULLs, and no
        # carrier row was invented for either refused key.
        assert _pools(store)["llm:devin:acu"].used_reported is None
        with store.pool.connection() as conn:
            for key in ("llm:missing:weekly", "host:pool-usage-unknown.example"):
                assert (
                    conn.execute(
                        "SELECT 1 FROM external_rate_limits WHERE provider = %s",
                        (key,),
                    ).fetchone()
                    is None
                )
