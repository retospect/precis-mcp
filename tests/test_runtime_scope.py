"""``runtime.scope.current_scope`` — the process-scope signal (gr512233,
``docs/backlog/read-scope-skill-fencing.md`` slice 1).

Each read source yields ``'read'``; the default is ``'write'``;
``write:scoped`` (only ``delete`` denied) stays ``'write'``. The
per-request read token is slice 4 — no request source exists yet.
"""

from __future__ import annotations

import pytest

import precis.runtime.scope as scope_mod
from precis.runtime.scope import current_scope, note_boot_read_only


@pytest.fixture(autouse=True)
def _clean_scope_signals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize every scope input so each test sets exactly one."""
    for name in (
        scope_mod.SERVE_READ_ONLY_ENV,
        "PRECIS_MCP_DENY_VERBS",
        "PRECIS_MCP_READ_ONLY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(scope_mod, "_boot_read_only", False)
    monkeypatch.setattr(scope_mod, "_serve_read_only", False)


def test_default_is_write() -> None:
    assert current_scope() == "write"


def test_deny_verbs_naming_put_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``write:none`` envelope exports all five write verbs."""
    monkeypatch.setenv("PRECIS_MCP_DENY_VERBS", "put,edit,delete,tag,link")
    assert current_scope() == "read"


def test_deny_verbs_delete_only_stays_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """``write:scoped`` denies ``delete`` only — still a writing run."""
    monkeypatch.setenv("PRECIS_MCP_DENY_VERBS", "delete")
    assert current_scope() == "write"


def test_deny_verbs_without_put_stays_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dream's deny list (edit,delete,link) leaves ``put`` open."""
    monkeypatch.setenv("PRECIS_MCP_DENY_VERBS", "edit,delete,link")
    assert current_scope() == "write"


def test_serve_read_only_env_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(scope_mod.SERVE_READ_ONLY_ENV, "1")
    assert current_scope() == "read"


def test_precis_mcp_read_only_env_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_MCP_READ_ONLY", "1")
    assert current_scope() == "read"


def test_boot_probe_verdict_is_read() -> None:
    note_boot_read_only(True)
    assert current_scope() == "read"


def test_second_boot_overwrites_stale_verdict() -> None:
    """``note_boot_read_only`` replaces, so a later writable boot in the
    same process (tests boot repeatedly) doesn't inherit a stale read."""
    note_boot_read_only(True)
    assert current_scope() == "read"
    note_boot_read_only(False)
    assert current_scope() == "write"


def test_put_denied_mirrors_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """``runtime.dispatch._put_denied`` is now the scope's read half —
    the env deny, serve --read-only and the boot probe all reach the
    gripe handler's capped channel through it (gr512233)."""
    from precis.runtime.dispatch import _put_denied

    assert _put_denied() is False
    monkeypatch.setenv("PRECIS_MCP_DENY_VERBS", "put,edit,delete,tag,link")
    assert _put_denied() is True


def test_put_denied_true_for_non_env_read_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cutover widens ``_put_denied`` beyond the env deny: an
    ``agent_ro``/read-only-DSN boot and a declared read-only process now
    take the same text-only gripe channel a write:none run gets."""
    from precis.runtime.dispatch import _put_denied

    monkeypatch.setenv("PRECIS_MCP_READ_ONLY", "1")
    assert _put_denied() is True


def test_serve_read_only_marks_scope() -> None:
    """``precis serve --read-only`` flags the scope inside
    ``_restrict_to_read_verbs`` — a module flag now, not the env var,
    so an in-process call no longer leaks ``read`` through
    ``os.environ`` (gr512233 round-2 review). The conftest reset
    clears it after the test."""
    from typing import cast

    from mcp.server.fastmcp import FastMCP

    from precis import server

    class _Tools:
        def list_tools(self) -> list:
            return []

    class _FakeFastMCP:
        _tool_manager = _Tools()

    server._restrict_to_read_verbs(cast(FastMCP, _FakeFastMCP()))
    assert current_scope() == "read"
