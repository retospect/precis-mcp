"""Tests for :mod:`precis.serve_ledger` — session-keyed serve dedup state
(docs/backlog/skill-graph.md slice 1).

Pure module-level behavior — no handler, no MCP, no DB.
"""

from __future__ import annotations

import gc

from precis import serve_ledger


class _FakeSession:
    """Weakly-referenceable stand-in for a real MCP session object —
    a bare ``object()`` isn't weakly referenceable."""


def test_lookup_none_when_no_session_bound() -> None:
    assert serve_ledger.lookup("a") is None
    assert serve_ledger.was_served("a") is False


def test_record_is_a_noop_without_a_bound_session() -> None:
    serve_ledger.record("a", "deadbeef")  # no-op: nothing bound
    assert serve_ledger.lookup("a") is None


def test_bind_unbind_round_trip() -> None:
    session = _FakeSession()
    token = serve_ledger.bind(session)
    try:
        serve_ledger.record("a", "sha1")
        entry = serve_ledger.lookup("a")
        assert entry is not None
        assert entry[0] == "sha1"
    finally:
        serve_ledger.unbind(token)
    # Unbound again: the session is no longer current, so lookups miss.
    assert serve_ledger.lookup("a") is None


def test_session_scope_context_manager() -> None:
    session = _FakeSession()
    with serve_ledger.session_scope(session):
        serve_ledger.record("a", "sha1")
        assert serve_ledger.was_served("a") is True
    assert serve_ledger.lookup("a") is None


def test_session_scope_resets_even_on_exception() -> None:
    session = _FakeSession()
    try:
        with serve_ledger.session_scope(session):
            raise ValueError("boom")
    except ValueError:
        pass
    assert serve_ledger.lookup("a") is None


def test_two_sessions_never_share_ledger_state() -> None:
    s1 = _FakeSession()
    s2 = _FakeSession()

    with serve_ledger.session_scope(s1):
        serve_ledger.record("a", "sha1")

    with serve_ledger.session_scope(s2):
        assert serve_ledger.lookup("a") is None
        serve_ledger.record("a", "sha2")
        entry = serve_ledger.lookup("a")
        assert entry is not None
        assert entry[0] == "sha2"

    with serve_ledger.session_scope(s1):
        entry = serve_ledger.lookup("a")
        assert entry is not None
        assert entry[0] == "sha1"


def test_re_recording_updates_the_sha() -> None:
    session = _FakeSession()
    with serve_ledger.session_scope(session):
        serve_ledger.record("a", "sha1")
        serve_ledger.record("a", "sha2")
        entry = serve_ledger.lookup("a")
        assert entry is not None
        assert entry[0] == "sha2"


def test_closed_session_ledger_is_reclaimed() -> None:
    """A session that's garbage-collected drops its ledger entry with
    it — no explicit teardown hook, per the module docstring."""
    session = _FakeSession()
    token = serve_ledger.bind(session)
    serve_ledger.record("a", "sha1")
    serve_ledger.unbind(token)

    assert session in serve_ledger._SESSION_LEDGERS
    del session
    gc.collect()
    assert len(serve_ledger._SESSION_LEDGERS) == 0
