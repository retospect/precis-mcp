"""Unit tests for the dream cadence knob (``dream.min_interval_minutes``) and
its self-throttle no-op logic — Wave-0 lane §G.

Store-free by design (a tiny KV-backed fake store), mirroring
``tests/test_budget.py``'s settings tests, plus one live-PG round-trip via
the ``store`` fixture.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest

from precis.store import Store
from precis.workers import dream_throttle

# ── fake KV store (mirrors test_budget.py's KVStore) ─────────────────────


class _Rows:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None


class _KVConn:
    def __init__(self, data: dict[str, str]) -> None:
        self._data = data

    def __enter__(self) -> _KVConn:
        return self

    def __exit__(self, *a: object) -> Literal[False]:
        return False

    def execute(self, sql: str, params: tuple = ()) -> _Rows:
        s = sql.upper()
        if s.startswith("SELECT VALUE"):
            val = self._data.get(params[0])
            return _Rows([(val,)] if val is not None else [])
        if "INSERT INTO APP_SETTINGS" in s:
            self._data[params[0]] = params[1]
            return _Rows([])
        if s.startswith("DELETE"):
            self._data.pop(params[0], None)
            return _Rows([])
        return _Rows([])


class _KVPool:
    def __init__(self, data: dict[str, str]) -> None:
        self._data = data

    def connection(self) -> _KVConn:
        return _KVConn(self._data)


class KVStore:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.pool = _KVPool(self.data)


def _fake(**overrides: str) -> Store:
    kv = KVStore()
    kv.data.update(overrides)
    from typing import cast

    return cast(Store, kv)


# ── resolution order: DB > env > compiled default ────────────────────────


def test_resolve_default_is_15_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_DREAM_MIN_INTERVAL_MINUTES", raising=False)
    assert dream_throttle.resolve_min_interval_minutes(_fake()) == 15.0
    assert dream_throttle.DEFAULT_MIN_INTERVAL_MINUTES == 15.0


def test_resolve_env_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_DREAM_MIN_INTERVAL_MINUTES", "30")
    assert dream_throttle.resolve_min_interval_minutes(_fake()) == 30.0


def test_resolve_db_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_DREAM_MIN_INTERVAL_MINUTES", "30")
    store = _fake(**{dream_throttle.MIN_INTERVAL_KEY: "60"})
    assert dream_throttle.resolve_min_interval_minutes(store) == 60.0


def test_resolve_bad_env_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_DREAM_MIN_INTERVAL_MINUTES", "not-a-number")
    assert dream_throttle.resolve_min_interval_minutes(_fake()) == 15.0


def test_resolve_nonpositive_db_value_falls_through_to_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A non-positive DB value is treated as unset (get_float's own contract).
    monkeypatch.setenv("PRECIS_DREAM_MIN_INTERVAL_MINUTES", "45")
    store = _fake(**{dream_throttle.MIN_INTERVAL_KEY: "0"})
    assert dream_throttle.resolve_min_interval_minutes(store) == 45.0


# ── last_real_run_at / mark_real_run round-trip ───────────────────────────


def test_last_real_run_at_none_when_unset() -> None:
    assert dream_throttle.last_real_run_at(_fake()) is None


def test_mark_real_run_roundtrips() -> None:
    store = _fake()
    when = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)
    dream_throttle.mark_real_run(store, when=when)
    assert dream_throttle.last_real_run_at(store) == when


def test_last_real_run_at_bad_value_is_none() -> None:
    store = _fake(**{dream_throttle.LAST_REAL_RUN_KEY: "not-a-timestamp"})
    assert dream_throttle.last_real_run_at(store) is None


# ── skip_if_too_soon: the no-op gate ──────────────────────────────────────


def test_skip_false_when_never_run_before(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_DREAM_MIN_INTERVAL_MINUTES", raising=False)
    assert dream_throttle.skip_if_too_soon(_fake()) is False


def test_default_interval_never_skips_a_15min_launchd_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance: unset = byte-identical to today. A last real run 15
    minutes ago (launchd's own cadence, allowing for its scheduling jitter)
    must never be throttled at the compiled default."""
    monkeypatch.delenv("PRECIS_DREAM_MIN_INTERVAL_MINUTES", raising=False)
    store = _fake()
    now = datetime(2026, 8, 2, 12, 15, 0, tzinfo=UTC)
    last = now - timedelta(minutes=15)
    dream_throttle.mark_real_run(store, when=last)
    assert dream_throttle.skip_if_too_soon(store, now=now) is False


def test_default_interval_jitter_guard_covers_early_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fire landing up to ~60s early on the 15-min mark still runs — the
    jitter guard's whole point."""
    monkeypatch.delenv("PRECIS_DREAM_MIN_INTERVAL_MINUTES", raising=False)
    store = _fake()
    now = datetime(2026, 8, 2, 12, 14, 5, tzinfo=UTC)  # 55s early
    last = now - timedelta(minutes=14, seconds=5)  # == 14m55s elapsed
    dream_throttle.mark_real_run(store, when=last)
    assert dream_throttle.skip_if_too_soon(store, now=now) is False


def test_skip_true_well_before_default_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PRECIS_DREAM_MIN_INTERVAL_MINUTES", raising=False)
    store = _fake()
    now = datetime(2026, 8, 2, 12, 5, 0, tzinfo=UTC)
    last = now - timedelta(minutes=5)  # well short of 15 - jitter
    dream_throttle.mark_real_run(store, when=last)
    assert dream_throttle.skip_if_too_soon(store, now=now) is True


def test_skip_true_within_a_60min_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Acceptance: dream.min_interval_minutes=60 -> an in-interval (e.g.
    15-min-later) pass no-ops."""
    monkeypatch.delenv("PRECIS_DREAM_MIN_INTERVAL_MINUTES", raising=False)
    store = _fake(**{dream_throttle.MIN_INTERVAL_KEY: "60"})
    now = datetime(2026, 8, 2, 12, 15, 0, tzinfo=UTC)
    last = now - timedelta(minutes=15)
    dream_throttle.mark_real_run(store, when=last)
    assert dream_throttle.skip_if_too_soon(store, now=now) is True


def test_skip_false_once_60min_cap_elapses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_DREAM_MIN_INTERVAL_MINUTES", raising=False)
    store = _fake(**{dream_throttle.MIN_INTERVAL_KEY: "60"})
    now = datetime(2026, 8, 2, 13, 0, 0, tzinfo=UTC)
    last = now - timedelta(minutes=60)
    dream_throttle.mark_real_run(store, when=last)
    assert dream_throttle.skip_if_too_soon(store, now=now) is False


# ── live-PG round-trip (app_settings is real here) ────────────────────────


def test_settings_roundtrip_pg(store: Store) -> None:
    assert dream_throttle.resolve_min_interval_minutes(store) == 15.0
    from precis.budget import settings as app_settings

    app_settings.set_float(store, dream_throttle.MIN_INTERVAL_KEY, 60.0)
    assert dream_throttle.resolve_min_interval_minutes(store) == 60.0
    app_settings.clear_setting(store, dream_throttle.MIN_INTERVAL_KEY)
    assert dream_throttle.resolve_min_interval_minutes(store) == 15.0


def test_mark_and_read_real_run_pg(store: Store) -> None:
    assert dream_throttle.last_real_run_at(store) is None
    when = datetime.now(UTC).replace(microsecond=0)
    dream_throttle.mark_real_run(store, when=when)
    assert dream_throttle.last_real_run_at(store) == when
