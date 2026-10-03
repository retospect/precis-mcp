"""Tests for :mod:`precis.store.advisory` (gr463966).

Transaction-scoped advisory locks on a dedicated connection: mutual
exclusion, release on exit (normal or exception), and no session-state
leak from the ``SET LOCAL`` that lifts the idle-in-transaction timeout.

Two connections in one process may be multiplexed onto one backend by the
dev container's network path (advisory locks are re-entrant within a
backend), which would make the exclusion tests vacuous — those skip
rather than pass on a same-backend pair.
"""

from __future__ import annotations

import secrets
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from precis.store.advisory import try_xact_advisory_lock
from tests.workers._helpers import skip_if_backends_multiplexed


def _dsn() -> str:
    from tests.conftest import _active_dsn

    return _active_dsn()


def _fresh_key() -> int:
    """A random signed-bigint key, so sibling test runs never collide."""
    return secrets.randbits(63) - 2**62


@pytest.fixture
def dsn(store: Any) -> str:
    """The per-session test DB (``store`` skips when Postgres is down)."""
    d = _dsn()
    skip_if_backends_multiplexed(d)
    return d


def test_second_holder_misses_until_first_exits(dsn: str) -> None:
    key = _fresh_key()
    with try_xact_advisory_lock(dsn, key) as first:
        assert first is True
        with try_xact_advisory_lock(dsn, key) as second:
            assert second is False
    with try_xact_advisory_lock(dsn, key) as third:
        assert third is True


def test_distinct_keys_do_not_collide(dsn: str) -> None:
    with try_xact_advisory_lock(dsn, _fresh_key()) as a:
        with try_xact_advisory_lock(dsn, _fresh_key()) as b:
            assert a is True and b is True


def test_exception_in_block_releases(dsn: str) -> None:
    key = _fresh_key()
    with pytest.raises(RuntimeError):
        with try_xact_advisory_lock(dsn, key) as got:
            assert got is True
            raise RuntimeError("forced")
    with try_xact_advisory_lock(dsn, key) as after:
        assert after is True


def test_two_key_form_is_per_text_key(dsn: str) -> None:
    ns = secrets.randbits(30)
    login = f"u-{secrets.token_hex(4)}"
    with try_xact_advisory_lock(dsn, ns, text_key=login) as a:
        assert a is True
        with try_xact_advisory_lock(dsn, ns, text_key=login) as same:
            assert same is False
        with try_xact_advisory_lock(dsn, ns, text_key=login + "-other") as other:
            assert other is True
    with try_xact_advisory_lock(dsn, ns, text_key=login) as after:
        assert after is True


def test_idle_timeout_is_lifted_in_txn_only(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``SET LOCAL``: the lock txn sees ``0``; nothing leaks to the session."""
    # A nonzero per-connection baseline (as prod's ALTER ROLE gives), so a
    # "0" can only have come from the helper.
    dsn300 = make_conninfo(dsn, options="-c idle_in_transaction_session_timeout=300s")
    held: list[psycopg.Connection[Any]] = []
    real_connect = psycopg.connect

    def _capture(*a: Any, **kw: Any) -> psycopg.Connection[Any]:
        conn = real_connect(*a, **kw)
        held.append(conn)
        return conn

    def _show(conn: psycopg.Connection[Any]) -> str:
        row = conn.execute("SHOW idle_in_transaction_session_timeout").fetchone()
        assert row is not None
        return str(row[0])

    with real_connect(dsn300, autocommit=True) as before:
        assert _show(before) == "5min"

    monkeypatch.setattr(psycopg, "connect", _capture)
    with try_xact_advisory_lock(dsn300, _fresh_key()) as got:
        assert got is True
        (conn,) = held
        assert _show(conn) == "0"
    monkeypatch.undo()
    assert conn.closed

    with real_connect(dsn300, autocommit=True) as after:
        assert _show(after) == "5min"


def test_claim_uses_the_xact_lock(dsn: str) -> None:
    """:class:`Claim` (ingest) holds/refuses/releases via the helper."""
    from precis.ingest.claim import Claim

    sha = secrets.token_hex(32)
    with Claim(dsn, sha) as first:
        assert first.acquired is True
        with Claim(dsn, sha) as second:
            assert second.acquired is False
    with pytest.raises(RuntimeError):
        with Claim(dsn, sha) as again:
            assert again.acquired is True
            raise RuntimeError("forced")
    with Claim(dsn, sha) as after:
        assert after.acquired is True
