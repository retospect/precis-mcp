"""Resolver-layer tests for :mod:`precis.settings` — no DB required.

Mirrors ``tests/test_secrets_resolver.py``'s shape: fake stores exercise the
DB → env → compiled-default order, the cache, and the best-effort/warn-once
guards without Postgres. The registered keys under test are the four budget
caps seeded in :data:`precis.settings.REGISTRY` (Slice 1).
"""

from __future__ import annotations

import logging
from typing import Any, Literal, cast

import psycopg
import pytest

from precis import settings as psettings


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    psettings.bind_store(None)
    psettings.invalidate()
    psettings._warned.clear()
    yield
    psettings.bind_store(None)
    psettings.invalidate()
    psettings._warned.clear()


# ── fakes ──────────────────────────────────────────────────────────────────


class _Cursor:
    def __init__(self, row: tuple | None) -> None:
        self._row = row

    def fetchone(self) -> tuple | None:
        return self._row


class _FakeConn:
    """A single-row ``app_settings`` fake: one key's value, recording the SQL
    it's handed. ``updated_by_fails`` raises ``UndefinedColumn`` on the
    4-column (``updated_by``-carrying) insert, so the pre-0125 fallback path
    is exercised the same way ``test_secrets_access_audit.py`` exercises
    ``_reveal``'s pre-0111 fallback."""

    def __init__(
        self, *, value: str | None = None, updated_by_fails: bool = False
    ) -> None:
        self.value = value
        self.updated_by_fails = updated_by_fails
        self.sql: list[str] = []
        self.last_params: tuple | None = None
        self.rolled_back = False
        self.committed = False

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *a: object) -> Literal[False]:
        return False

    def execute(self, sql: str, params: tuple = ()) -> _Cursor:
        self.sql.append(sql)
        self.last_params = params
        s = sql.upper()
        if s.startswith("SELECT VALUE"):
            return _Cursor((self.value,) if self.value is not None else None)
        if "INSERT INTO APP_SETTINGS" in s:
            if sql.count("%s") == 3:  # key, value, updated_by
                if self.updated_by_fails:
                    raise psycopg.errors.UndefinedColumn("no updated_by column")
                self.value = str(params[1])
            else:  # pre-0125 fallback: key, value only
                self.value = str(params[1])
            return _Cursor(None)
        if s.startswith("DELETE"):
            self.value = None
            return _Cursor(None)
        return _Cursor(None)

    def rollback(self) -> None:
        self.rolled_back = True

    def commit(self) -> None:
        self.committed = True


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def connection(self) -> _FakeConn:
        return self._conn


class _FakeStore:
    def __init__(self, conn: _FakeConn) -> None:
        self.pool = _FakePool(conn)


class _BoomPool:
    """A pool whose ``connection()`` blows up — proves a code path never
    touches the DB at all (not just that it degrades on a DB error)."""

    def connection(self) -> Any:
        raise AssertionError("DB tier must not be touched here")


class _BoomStore:
    pool = _BoomPool()


class _ExplodingPool:
    def connection(self) -> Any:
        raise RuntimeError("no schema")


class _ExplodingStore:
    pool = _ExplodingPool()


# ── DB → env → default precedence ───────────────────────────────────────────


def test_db_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_BUDGET_HOURLY_USD", "9")
    store: Any = _FakeStore(_FakeConn(value="3.0"))
    assert psettings.get_float("budget.hourly_usd", store=store) == 3.0


def test_env_used_when_no_db_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_BUDGET_HOURLY_USD", "9")
    store: Any = _FakeStore(_FakeConn(value=None))
    assert psettings.get_float("budget.hourly_usd", store=store) == 9.0


def test_compiled_default_when_neither(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_BUDGET_HOURLY_USD", raising=False)
    assert psettings.get_float("budget.hourly_usd", store=None) == 5.0


def test_store_none_falls_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_BUDGET_DAILY_USD", "42")
    assert psettings.get_float("budget.daily_usd", store=None) == 42.0


def test_db_error_degrades_to_env_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_BUDGET_HOURLY_USD", "11")
    store = cast("Any", _ExplodingStore())
    assert psettings.get_float("budget.hourly_usd", store=store) == 11.0


def test_no_store_bound_and_none_passed_degrades_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PRECIS_QUOTA_CEILING_PCT", raising=False)
    assert psettings.get_float("budget.quota_ceiling_pct") == 100.0


# ── cache ────────────────────────────────────────────────────────────────


def test_write_invalidates_cache_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_BUDGET_DAILY_USD", raising=False)
    conn = _FakeConn(value="1.0")
    store = cast("Any", _FakeStore(conn))
    assert psettings.get_float("budget.daily_usd", store=store) == 1.0
    psettings.set_setting("budget.daily_usd", 2.0, store=store)
    assert psettings.get_float("budget.daily_usd", store=store) == 2.0


def test_clear_setting_invalidates_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_BUDGET_DAILY_USD", raising=False)
    conn = _FakeConn(value="1.0")
    store = cast("Any", _FakeStore(conn))
    assert psettings.get_float("budget.daily_usd", store=store) == 1.0
    psettings.clear_setting("budget.daily_usd", store=store)
    # Reverts to the compiled default (env unset).
    assert psettings.get_float("budget.daily_usd", store=store) == 20.0


# ── unregistered keys ────────────────────────────────────────────────────


def test_unregistered_key_warns_once_and_resolves_env(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("MY_UNKNOWN_SETTING", "hello")
    store = cast("Any", _BoomStore())  # DB tier must never be touched
    with caplog.at_level(logging.WARNING):
        assert psettings.get_str("MY_UNKNOWN_SETTING", store=store) == "hello"
        assert psettings.get_str("MY_UNKNOWN_SETTING", store=store) == "hello"
    warnings = [r for r in caplog.records if "unregistered" in r.message]
    assert len(warnings) == 1  # warn-once, not per-call


def test_unregistered_key_falls_to_default_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SOME_OTHER_UNKNOWN_SETTING", raising=False)
    assert (
        psettings.get_str("SOME_OTHER_UNKNOWN_SETTING", store=None, default="fallback")
        == "fallback"
    )


# ── updated_by / 0125 rolling-deploy fallback ────────────────────────────


def test_updated_by_recorded_on_set() -> None:
    conn = _FakeConn(value=None)
    store = cast("Any", _FakeStore(conn))
    psettings.set_setting(
        "budget.resume_until", "2999-01-01T00:00:00+00:00", store=store
    )
    assert conn.value == "2999-01-01T00:00:00+00:00"
    insert_sql = [s for s in conn.sql if "INSERT" in s.upper()][0]
    assert insert_sql.count("%s") == 3  # key, value, updated_by
    assert conn.last_params is not None
    ident = cast("tuple", conn.last_params)[2]
    assert isinstance(ident, str) and ident  # a non-empty identity summary
    assert conn.committed is True


def test_set_setting_pre_migration_fallback() -> None:
    """A DB that hasn't taken 0125 yet still accepts the write (rolling
    deploy) — rollback then retry without ``updated_by``, mirroring
    ``precis.secrets._reveal``'s fallback to the 1-arg ``vault.reveal``."""
    conn = _FakeConn(value=None, updated_by_fails=True)
    store = cast("Any", _FakeStore(conn))
    psettings.set_setting("budget.hourly_usd", 7.5, store=store)
    assert conn.value == "7.5"
    assert conn.rolled_back is True
    inserts = [s for s in conn.sql if "INSERT" in s.upper()]
    assert inserts[-1].count("%s") == 2  # key, value only


# ── resolve / get / is_available / list_settings ─────────────────────────


def test_resolve_reports_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_BUDGET_HOURLY_USD", raising=False)
    store: Any = _FakeStore(_FakeConn(value="3.0"))
    value, layer = psettings.resolve("budget.hourly_usd", store=store)
    assert value == 3.0
    assert layer == "db"

    monkeypatch.setenv("PRECIS_BUDGET_HOURLY_USD", "9")
    value, layer = psettings.resolve("budget.hourly_usd", store=None)
    assert value == 9.0
    assert layer == "env"

    monkeypatch.delenv("PRECIS_BUDGET_HOURLY_USD", raising=False)
    value, layer = psettings.resolve("budget.hourly_usd", store=None)
    assert value == 5.0
    assert layer == "default"


def test_get_dispatches_by_registered_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_BUDGET_HOURLY_USD", raising=False)
    assert psettings.get("budget.hourly_usd", store=None) == 5.0
    assert isinstance(psettings.get("budget.hourly_usd", store=None), float)


def test_is_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_BUDGET_HOURLY_USD", raising=False)
    assert psettings.is_available("budget.hourly_usd", store=None) is True

    monkeypatch.delenv("PRECIS_QUOTA_CEILING_PCT", raising=False)
    store: Any = _FakeStore(_FakeConn(value=None))
    # budget.resume_until has no env fallback and no DB row → unavailable.
    assert psettings.is_available("budget.resume_until", store=store) is False


def test_list_settings_covers_the_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_BUDGET_HOURLY_USD", raising=False)
    monkeypatch.delenv("PRECIS_BUDGET_DAILY_USD", raising=False)
    monkeypatch.delenv("PRECIS_QUOTA_CEILING_PCT", raising=False)
    rows = psettings.list_settings(store=None)
    keys = {r["key"] for r in rows}
    assert keys == set(psettings.REGISTRY)
    hourly = next(r for r in rows if r["key"] == "budget.hourly_usd")
    assert hourly["value"] == 5.0
    assert hourly["layer"] == "default"


# ── type coercion ─────────────────────────────────────────────────────────


def test_get_bool_coercion() -> None:
    assert psettings._coerce_bool("true", "k") is True
    assert psettings._coerce_bool("0", "k") is False
    assert psettings._coerce_bool("not-a-bool", "k") is None


def test_get_int_coercion() -> None:
    assert psettings._coerce_int("7", "k") == 7
    assert psettings._coerce_int("not-an-int", "k") is None


# ── advertised_env_presence (heartbeat self-report, slice 4) ─────────────


def test_advertised_env_presence_lists_locally_set_registered_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for spec in psettings.REGISTRY.values():
        if spec.env_var:
            monkeypatch.delenv(spec.env_var, raising=False)
    monkeypatch.setenv("PRECIS_UNPAYWALL_EMAIL", "ops@example.org")
    assert psettings.advertised_env_presence() == ["contact.polite_email"]


def test_advertised_env_presence_empty_when_nothing_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for spec in psettings.REGISTRY.values():
        if spec.env_var:
            monkeypatch.delenv(spec.env_var, raising=False)
    assert psettings.advertised_env_presence() == []
