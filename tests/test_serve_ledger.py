"""Tests for :mod:`precis.serve_ledger` — the per-tool-call MCP session
binding (the skill dedup ledger it once held was replaced by the
client-held ``skill rev`` tag, gr477798).

Pure module-level behavior — no handler, no MCP, no DB.
"""

from __future__ import annotations

from precis import serve_ledger


class _FakeSession:
    """Stand-in for a real MCP session object."""


def test_unbound_by_default() -> None:
    assert serve_ledger.current_session() is None


def test_bind_unbind_round_trip() -> None:
    session = _FakeSession()
    token = serve_ledger.bind(session)
    try:
        assert serve_ledger.current_session() is session
    finally:
        serve_ledger.unbind(token)
    assert serve_ledger.current_session() is None


def test_session_scope_context_manager() -> None:
    session = _FakeSession()
    with serve_ledger.session_scope(session):
        assert serve_ledger.current_session() is session
    assert serve_ledger.current_session() is None


def test_session_scope_resets_even_on_exception() -> None:
    try:
        with serve_ledger.session_scope(_FakeSession()):
            raise ValueError("boom")
    except ValueError:
        pass
    assert serve_ledger.current_session() is None
