"""Connection plumbing shared by the design-core data modules.

The renter's persist discipline, transferred: reach the database over the
store's **public** connection surface only (``store.tx()`` /
``store.pool.connection()``), and let every write join an outer transaction
when the caller passes one — a design save that mints a revision and
records a branch must be one unit, not three.

``store`` is typed ``Any`` on purpose, as in ``precis_se.persist``: these
helpers want a connection pool, not the whole ``Store`` protocol, and the
plugin's tests routinely pass a thin fake.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from psycopg import Connection


@contextmanager
def read_conn(store: Any, conn: Connection | None = None) -> Iterator[Connection]:
    """A connection for reads — the caller's if given, else a pooled one."""
    if conn is not None:
        yield conn
        return
    with store.pool.connection() as own:
        yield own


@contextmanager
def write_conn(store: Any, conn: Connection | None = None) -> Iterator[Connection]:
    """A connection for writes — the caller's if given (so the write lands
    in their transaction), else a fresh one inside ``store.tx()``."""
    if conn is not None:
        yield conn
        return
    with store.tx() as own:
        yield own
