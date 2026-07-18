"""Tier-2 write-isolation: the connection-pool ``SET ROLE`` consumer (§13).

The per-todo envelope's ``write`` axis resolves to a DB role
(``agent_ro``/``agent_rw``) and exports it as ``PRECIS_MCP_DB_ROLE``. This is
the consumer that finally makes it bite: a session assumes that role so a write
is refused by Postgres, not merely by a missing tool. Proven against real PG
using the built-in ``pg_monitor`` predefined role as a harmless SET-ROLE target
(a superuser test session can assume any role; no role creation / side effects).

Dark by default: the consumer keys on a SEPARATE ``PRECIS_MCP_DB_ROLE_ENFORCE``
flag, because ``PRECIS_MCP_DB_ROLE`` is already exported to every agentic
subprocess today — so its mere presence must NOT turn isolation on.
"""

from __future__ import annotations

import pytest

from precis.store import pool as poolmod


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("PRECIS_MCP_DB_ROLE", "PRECIS_MCP_DB_ROLE_ENFORCE"):
        monkeypatch.delenv(k, raising=False)


def _current_role(conn) -> str:
    row = conn.execute("SELECT current_role").fetchone()
    return str(row[0])


def test_dark_by_default_no_set_role(store, monkeypatch) -> None:
    """Role var set but enforce flag OFF → no SET ROLE (byte-identical today)."""
    monkeypatch.setenv("PRECIS_MCP_DB_ROLE", "pg_monitor")
    with store.pool.connection() as conn:
        before = _current_role(conn)
        poolmod._apply_db_role(conn)
        assert _current_role(conn) == before  # unchanged


def test_set_role_issues_safely_quoted_statement(store, monkeypatch) -> None:
    """Enforce ON + valid role → exactly one ``SET ROLE`` with the identifier
    safely quoted (``sql.Identifier``). Asserted on the statement rather than a
    live role switch, so it doesn't depend on the test session's grants — the
    actual privilege check (fail-closed on missing membership) is Postgres's own
    and is the documented deploy prerequisite."""
    monkeypatch.setenv("PRECIS_MCP_DB_ROLE_ENFORCE", "1")
    monkeypatch.setenv("PRECIS_MCP_DB_ROLE", "agent_ro")

    captured: list[object] = []

    class _Capture:
        def execute(self, query, *a, **k):
            captured.append(query)

    poolmod._apply_db_role(_Capture())  # type: ignore[arg-type]
    assert len(captured) == 1
    with store.pool.connection() as conn:
        rendered = captured[0].as_string(conn)  # type: ignore[attr-defined]
    assert rendered == 'SET ROLE "agent_ro"'


def test_enforced_but_no_role_is_noop(store, monkeypatch) -> None:
    """Enforce ON but the role var is unset → no-op (nothing to assume)."""
    monkeypatch.setenv("PRECIS_MCP_DB_ROLE_ENFORCE", "1")
    with store.pool.connection() as conn:
        before = _current_role(conn)
        poolmod._apply_db_role(conn)
        assert _current_role(conn) == before


@pytest.mark.parametrize("bad", ["bad; DROP TABLE refs", "has space", "1starts", ""])
def test_invalid_role_name_rejected(monkeypatch, bad) -> None:
    """A malformed / injected role name is refused before any SQL runs."""
    monkeypatch.setenv("PRECIS_MCP_DB_ROLE_ENFORCE", "1")
    monkeypatch.setenv("PRECIS_MCP_DB_ROLE", bad)

    class _NoExec:
        def execute(self, *a, **k):
            raise AssertionError("must not execute SQL for an invalid role")

    if bad == "":
        # empty is a clean no-op (nothing to assume), not an error.
        poolmod._apply_db_role(_NoExec())  # type: ignore[arg-type]
    else:
        with pytest.raises(ValueError):
            poolmod._apply_db_role(_NoExec())  # type: ignore[arg-type]


def test_enforce_flag_parsing(monkeypatch) -> None:
    for on in ("1", "true", "yes", "TRUE", "Yes"):
        monkeypatch.setenv("PRECIS_MCP_DB_ROLE_ENFORCE", on)
        assert poolmod._db_role_enforced() is True
    for off in ("0", "", "no", "off"):
        monkeypatch.setenv("PRECIS_MCP_DB_ROLE_ENFORCE", off)
        assert poolmod._db_role_enforced() is False
