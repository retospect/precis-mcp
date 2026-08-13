"""Slice-2 contact-identity keys — ``contact.crossref_mailto`` and
``contact.edgar_user_agent`` — resolving DB → env → compiled default.

Mirrors ``tests/test_settings.py``'s fake-store shape (no DB required); this
file is a sibling, not an extension, so the two coders touching each don't
collide on the same file.
"""

from __future__ import annotations

from typing import Any, Literal

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


# ── fakes (copied from tests/test_settings.py's pattern) ───────────────────


class _Cursor:
    def __init__(self, row: tuple | None) -> None:
        self._row = row

    def fetchone(self) -> tuple | None:
        return self._row


class _FakeConn:
    def __init__(self, *, value: str | None = None) -> None:
        self.value = value

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *a: object) -> Literal[False]:
        return False

    def execute(self, sql: str, params: tuple = ()) -> _Cursor:
        s = sql.upper()
        if s.startswith("SELECT VALUE"):
            return _Cursor((self.value,) if self.value is not None else None)
        return _Cursor(None)

    def rollback(self) -> None:
        pass

    def commit(self) -> None:
        pass


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def connection(self) -> _FakeConn:
        return self._conn


class _FakeStore:
    def __init__(self, conn: _FakeConn) -> None:
        self.pool = _FakePool(conn)


# ── registry shape ──────────────────────────────────────────────────────────


def test_four_contact_keys_registered() -> None:
    for key in (
        "contact.crossref_mailto",
        "contact.polite_email",
        "contact.edgar_user_agent",
        "contact.wikipedia_ua",
    ):
        assert key in psettings.REGISTRY


# ── crossref_mailto: env-only, DB-wins, nothing-set ─────────────────────────


def test_crossref_mailto_env_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_CROSSREF_MAILTO", "ops@example.org")
    assert psettings.get_str("contact.crossref_mailto", store=None) == "ops@example.org"


def test_crossref_mailto_db_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_CROSSREF_MAILTO", "stale@example.org")
    store: Any = _FakeStore(_FakeConn(value="fresh@example.org"))
    assert (
        psettings.get_str("contact.crossref_mailto", store=store) == "fresh@example.org"
    )


def test_crossref_mailto_nothing_set_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_CROSSREF_MAILTO", raising=False)
    assert psettings.get_str("contact.crossref_mailto", store=None) is None


# ── edgar_user_agent: env-only, DB-wins, nothing-set ────────────────────────

_EDGAR_DEFAULT = "precis-mcp (+https://github.com/retospect/precis-mcp)"


def test_edgar_user_agent_env_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_EDGAR_USER_AGENT", "myco (contact@example.org)")
    assert (
        psettings.get_str("contact.edgar_user_agent", store=None)
        == "myco (contact@example.org)"
    )


def test_edgar_user_agent_db_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_EDGAR_USER_AGENT", "stale-ua")
    store: Any = _FakeStore(_FakeConn(value="fresh-ua (contact@example.org)"))
    assert (
        psettings.get_str("contact.edgar_user_agent", store=store)
        == "fresh-ua (contact@example.org)"
    )


def test_edgar_user_agent_nothing_set_is_compiled_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PRECIS_EDGAR_USER_AGENT", raising=False)
    assert psettings.get_str("contact.edgar_user_agent", store=None) == _EDGAR_DEFAULT


def test_config_edgar_user_agent_delegates_to_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``precis.config.edgar_user_agent`` is the kept call-site API; it must
    resolve through the registered setting, not read the env var raw."""
    from precis import config as _config

    monkeypatch.delenv("PRECIS_EDGAR_USER_AGENT", raising=False)
    assert _config.edgar_user_agent() == _config.DEFAULT_EDGAR_USER_AGENT

    monkeypatch.setenv("PRECIS_EDGAR_USER_AGENT", "env-ua (a@b.org)")
    assert _config.edgar_user_agent() == "env-ua (a@b.org)"
