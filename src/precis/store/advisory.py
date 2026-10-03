"""Transaction-scoped advisory locks that survive pgbouncer pooling (gr463966).

Prod fronts Postgres with pgbouncer in ``pool_mode = transaction``. There
every autocommit statement is its own transaction and may land on a
different server backend, so a session-scoped ``pg_try_advisory_lock`` and
its ``pg_advisory_unlock`` hit different backends: no mutual exclusion,
and the lock leaks on whichever pooled backend took it. A planned
``server_reset_query = DISCARD ALL`` would additionally drop any session
lock on release. Nothing may therefore depend on session state surviving
between transactions.

:func:`try_xact_advisory_lock` is the one sanctioned holder for passes
that are too long (or too cross-host) for a pooled connection:

* It opens a DEDICATED ``psycopg.connect`` (never the pool) with
  ``autocommit=False`` and takes ``pg_try_advisory_xact_lock`` as the
  first statement of the transaction. An open explicit transaction pins
  ONE pgbouncer server connection for its whole duration, so lock and
  release are on the same backend.
* The lock ends with the transaction (``rollback`` in the ``finally``),
  so ``DISCARD ALL`` and backend reuse cannot leak it.
* ``SET LOCAL idle_in_transaction_session_timeout = 0`` lifts prod's
  ``300s`` idle-in-transaction kill for THIS transaction only (the lock
  connection sits idle by design while the holder works on pool
  connections). It is ``SET LOCAL``, never a session ``SET``: a session
  ``SET`` through pgbouncer would stay on the pooled backend and poison
  unrelated clients.
* If the process dies, pgbouncer closes the server connection whose
  client vanished mid-transaction, which releases the lock.

Cost: one server connection is held per running holder for the pass. The
lock connection ONLY holds the lock; real work keeps using the store pool.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg


@contextmanager
def try_xact_advisory_lock(
    dsn: str, key: int, *, text_key: str | None = None
) -> Iterator[bool]:
    """Try a transaction-scoped advisory lock; yield whether it was won.

    ``key`` alone uses the one-bigint form
    (``pg_try_advisory_xact_lock(bigint)``). With ``text_key`` the two-int4
    form is used and the second key is ``hashtext(text_key)`` computed in
    SQL, so ``key`` must fit int4 (anki's per-user lock: namespace +
    ``hashtext(login)``).

    Yields ``False`` on a miss (another holder owns it; nothing is held).
    On ``True`` the lock is held until the ``with`` block exits, normally or
    by exception; exit rolls back and closes the dedicated connection.
    """
    conn = psycopg.connect(dsn)
    try:
        conn.execute("SET LOCAL idle_in_transaction_session_timeout = 0")
        if text_key is None:
            row = conn.execute(
                "SELECT pg_try_advisory_xact_lock(%s)", (key,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT pg_try_advisory_xact_lock(%s, hashtext(%s))",
                (key, text_key),
            ).fetchone()
        yield bool(row and row[0])
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


__all__ = ["try_xact_advisory_lock"]
