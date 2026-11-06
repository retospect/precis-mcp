"""Periodic pool signal copying, dark registration and missing-signal safety."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from precis.dispatch import boot
from precis.store import Store
from precis.utils.llm.accounts import list_pools
from precis.workers.pool_refresh import run_pool_refresh_pass
from precis.workers.service_config import set_service_prio


@pytest.fixture(autouse=True)
def _handler_kinds(store: Store) -> None:
    """Booting the hub registers the handler kinds; the fleet host rows
    below need ``kind='fleet'`` in the kinds table (tests/test_fleet.py
    gets it the same way through its handler fixture)."""
    boot(store=store)


@pytest.fixture
def restore_pools(store: Store) -> Iterator[None]:
    """The six shared seeds survive the suite's normal table truncation."""
    with store.pool.connection() as conn:
        cursor = conn.execute(
            "SELECT * FROM external_rate_limits WHERE provider LIKE 'llm:%%'"
        )
        assert cursor.description is not None
        columns = [column.name for column in cursor.description]
        rows = cursor.fetchall()
    yield
    with store.pool.connection() as conn:
        assignments = ", ".join(f'"{column}" = %s' for column in columns)
        for row in rows:
            conn.execute(
                f"UPDATE external_rate_limits SET {assignments} WHERE provider = %s",
                (*row, row[columns.index("provider")]),
            )


def _host(store: Store, name: str, at: str, quota: dict[str, Any]) -> None:
    store.insert_ref(
        kind="fleet",
        slug=None,
        title=name,
        meta={"type": "host", "reported_at": at, "quota": quota},
    )


def _light(store: Store) -> None:
    set_service_prio(store, "*", "pool_refresh", 1)


@pytest.mark.usefixtures("restore_pools")
def test_codex_weekly_newest_valid_host(store: Store) -> None:
    _light(store)
    _host(
        store,
        "build-a.example.com",
        "2026-11-06T10:00:00Z",
        {"codex": {"secondary": {"window_minutes": 10080, "used_percent": 12}}},
    )
    _host(
        store,
        "build-b.example.com",
        "2026-11-06T11:00:00Z",
        {
            "codex": {
                "primary": {"window_minutes": 300, "used_percent": 99},
                "secondary": {"window_minutes": 10080, "used_percent": 37},
            }
        },
    )
    _host(store, "idle.example.com", "2026-11-06T12:00:00Z", {})
    result = run_pool_refresh_pass(store)
    assert (result.claimed, result.ok, result.failed) == (1, 1, 0)
    pools = {pool.provider: pool for pool in list_pools(store)}
    weekly = pools["llm:codex:weekly"]
    assert weekly.used_reported == 37
    assert weekly.remaining_reported == 63
    assert weekly.limit_reported == 100
    assert weekly.source == "provider"
    assert weekly.reported_at == datetime(2026, 11, 6, 11, tzinfo=UTC)
    assert weekly.resets_at is None
    run_pool_refresh_pass(store)
    assert list_pools(store) == list(pools.values())


@pytest.mark.usefixtures("restore_pools")
def test_empty_quota_leaves_pool_alone(store: Store) -> None:
    _light(store)
    before = list_pools(store)
    _host(store, "idle.example.com", "2026-11-06T12:00:00Z", {})
    result = run_pool_refresh_pass(store)
    assert (result.claimed, result.ok, result.failed) == (0, 0, 0)
    assert list_pools(store) == before


@pytest.mark.usefixtures("restore_pools")
def test_devin_pause_copied_idempotently(store: Store) -> None:
    _light(store)
    # Relative to now: a fixed date would lapse and be (rightly) skipped.
    until = (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0)
    store.set_setting("devin_exec:paused_until", until.isoformat())
    result = run_pool_refresh_pass(store)
    assert (result.claimed, result.ok, result.failed) == (1, 1, 0)
    pools = list_pools(store)
    devin = next(pool for pool in pools if pool.provider == "llm:devin:sessions")
    assert devin.parked_until == until
    assert devin.source == "estimate"
    assert devin.limit_reported == 1
    run_pool_refresh_pass(store)
    assert list_pools(store) == pools


@pytest.mark.usefixtures("restore_pools")
@pytest.mark.parametrize(
    ("offset", "copied"),
    [
        (timedelta(hours=-1), False),
        (timedelta(0), False),
        (timedelta(seconds=1), True),
    ],
    ids=["lapsed", "exactly-now", "just-ahead"],
)
def test_devin_pause_copied_only_while_in_force(
    store: Store, monkeypatch: pytest.MonkeyPatch, offset: timedelta, copied: bool
) -> None:
    """devin_api never clears ``devin_exec:paused_until``, so a lapsed value
    stays stored; copying it would pin a past retry_at on the pool and report
    a write every cycle. A pause at or before now is no signal at all."""
    now = datetime(2026, 11, 6, 12, tzinfo=UTC)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> FrozenDatetime:
            return cls(2026, 11, 6, 12, tzinfo=UTC)

    monkeypatch.setattr("precis.workers.pool_refresh.datetime", FrozenDatetime)
    _light(store)
    until = now + offset
    store.set_setting("devin_exec:paused_until", until.isoformat())
    before = list_pools(store)
    result = run_pool_refresh_pass(store)
    pools = list_pools(store)
    devin = next(pool for pool in pools if pool.provider == "llm:devin:sessions")
    if copied:
        assert (result.claimed, result.ok, result.failed) == (1, 1, 0)
        assert devin.parked_until == until
    else:
        assert (result.claimed, result.ok, result.failed) == (0, 0, 0)
        assert devin.parked_until is None
        assert pools == before
    # Idempotent either way: a second cycle reports the same and changes nothing.
    again = run_pool_refresh_pass(store)
    assert (again.claimed, again.ok) == (result.claimed, result.ok)
    assert list_pools(store) == pools


@pytest.mark.usefixtures("restore_pools")
def test_dark_until_lit(store: Store) -> None:
    # In force, so only the dark switch can be what keeps the pass from claiming.
    until = datetime.now(UTC) + timedelta(days=1)
    store.set_setting("devin_exec:paused_until", until.isoformat())
    before = list_pools(store)
    assert run_pool_refresh_pass(store).claimed == 0
    assert list_pools(store) == before


def test_registration() -> None:
    from precis.workers.registry import SERVICES_BY_NAME
    from precis.workers.scheduler import CADENCES

    cadence = next(item for item in CADENCES if item.name == "pool_refresh")
    assert cadence.interval_s == 600
    assert cadence.service is None and cadence.spends is False
    service = SERVICES_BY_NAME["pool_refresh"]
    assert not service.default_profiles
    assert not service.ref_pass and not service.uses_model
