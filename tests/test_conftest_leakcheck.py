"""The ``store`` fixture's connection-leak check (gr476901).

Pins what counts as a leak: a client backend opened during the test and still
holding a transaction is caught; a backend that already existed at the
snapshot (an earlier test's connection turning active) is not blamed.
"""

from __future__ import annotations

import psycopg
from psycopg.conninfo import conninfo_to_dict

from tests.conftest import (
    _active_dsn,
    _describe_backends,
    _leaked_backends,
    _live_backends,
)


def _dbname() -> str:
    return str(conninfo_to_dict(_active_dsn()).get("dbname") or "")


def test_new_backend_in_transaction_is_a_leak(store):
    db = _dbname()
    before = _live_backends(db, any_state=True)
    with psycopg.connect(_active_dsn()) as c:
        c.execute("SELECT 1")  # leaves it idle in transaction
        pid = c.info.backend_pid
        assert pid in _leaked_backends(db, before)
        assert f"pid {pid} client backend" in _describe_backends(db, {pid})
        c.rollback()


def test_pre_existing_backend_turning_active_is_not_blamed(store):
    db = _dbname()
    with psycopg.connect(_active_dsn(), autocommit=True) as c:
        before = _live_backends(db, any_state=True)  # c is idle here
        assert c.info.backend_pid in before
        with c.transaction():
            c.execute("SELECT 1")
            assert c.info.backend_pid not in _leaked_backends(db, before)
