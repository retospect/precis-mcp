"""The shared MCP server's DB pool under a session storm.

``precis serve --transport streamable-http`` is one process holding ONE
:class:`~psycopg_pool.ConnectionPool`, and every attached agent session
draws from it (see ``tests/test_mcp_session_concurrency.py`` for the
semaphore half of that, and ``docs/backlog/session-mcp-http-server.md``
"Sizing for 12 concurrent sessions" for the numbers). The sizing rule the
deployment rests on is: **the tool semaphore is the binding constraint,
not the pool** — tool concurrency (12 in prod) sits under pool max (16),
so a storm of sessions queues in the server's round-robin semaphore and
never reaches the pool's wait queue, let alone its timeout.

This module pins both sides of that rule against a real Postgres (the
per-pytest-session ``precis_test_<uuid>`` clone), through the in-memory
multi-session harness in ``tests/_mcp_session.py`` and the production
``server._offload_sync`` wrapper with ``semaphore=None`` (the
module-singleton path):

* a storm with the pool wider than the semaphore never touches the
  pool's queue (the rule holds);
* a storm with the pool NARROWER than the semaphore — the configuration
  the rule forbids — waits on the pool but completes without a
  ``PoolTimeout`` or a deadlock (the pool is a correct backstop, not a
  trap);
* closing the pool leaves no backend behind.

Everything here is in-process against one Postgres. There is no
pgbouncer between the pool and the database, so none of it says anything
about pgbouncer's ``cl_waiting`` / transaction-pool behaviour — that
needs the real deployment.

The ``store`` fixture is used only for its skip-when-Postgres-is-down
gate and per-test TRUNCATE; each test builds its OWN pool with explicit
sizes (which override ``PRECIS_DB_POOL_*_SIZE``) over the active clone
DSN. Because that pool is closed inside the test, the fixture's
lock-holding-backend leak guard (``PRECIS_TEST_LEAKCHECK``) also covers
it at teardown.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg_pool import PoolTimeout

from precis import server
from precis.store import Store
from precis.store.pool import create_pool
from tests._mcp_session import build_test_mcp, connected_sessions, text_of
from tests.conftest import _active_dsn

pytestmark = pytest.mark.usefixtures("store")


@pytest.fixture
def _reset_tool_semaphore() -> Iterator[None]:
    """Clear ``server._tool_semaphore`` before and after the test.

    The semaphore is built lazily from ``PRECIS_MCP_TOOL_CONCURRENCY`` on
    first use, so a test that sets the env var must drop the cached
    instance to pick it up, and drop it again so no later test inherits
    this one's bound.
    """
    server._tool_semaphore = None
    try:
        yield
    finally:
        server._tool_semaphore = None


# ── helpers ──────────────────────────────────────────────────────────


def _backends_named(app: str) -> int:
    """Backends of ANY state whose ``application_name`` is ``app``.

    The conftest leak guard only counts ``active`` / ``idle in
    transaction`` backends (an ``idle`` one holds no lock); this counts
    every state, which is the stricter statement "the pool's connections
    are gone".
    """
    with psycopg.connect(_active_dsn(), autocommit=True) as c:
        row = c.execute(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE application_name = %s AND pid <> pg_backend_pid()",
            (app,),
        ).fetchone()
    assert row is not None
    return int(row[0])


@contextmanager
def _storm_pool(
    *, max_size: int, min_size: int = 1, **pool_kwargs: Any
) -> Iterator[tuple[Store, str]]:
    """A private Store over the active clone DSN, closed on exit.

    Yields ``(store, application_name)``; the name is unique per call so
    ``_backends_named`` sees only this pool's connections. Extra kwargs
    (``timeout=``) go through :func:`precis.store.pool.create_pool` to
    the ``ConnectionPool``.
    """
    app = f"pool-storm-{uuid.uuid4().hex[:10]}"
    dsn = make_conninfo(_active_dsn(), application_name=app)
    store = Store(
        create_pool(dsn, min_size=min_size, max_size=max_size, **pool_kwargs),
        dsn=dsn,
    )
    try:
        yield store, app
    finally:
        store.close()


class _Gauge:
    """Lock-guarded count of connections checked out right now, and its peak."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current = 0
        self.peak = 0

    @contextmanager
    def held(self) -> Iterator[None]:
        with self._lock:
            self.current += 1
            self.peak = max(self.peak, self.current)
        try:
            yield
        finally:
            with self._lock:
                self.current -= 1


def _storm(
    store: Store, gauge: _Gauge, *, sessions: int, calls: int
) -> tuple[list[str], float]:
    """Fire ``sessions`` × ``calls`` concurrent tool calls; return (texts, seconds).

    The tool borrows a pool connection, holds it across a 50 ms
    ``pg_sleep`` and a trivial read, and echoes its argument — so a
    dropped, duplicated or crossed reply shows up as a wrong string.
    """

    def probe(i: int) -> str:
        """Borrow a pooled connection and hold it for a beat."""
        with store.pool.connection() as conn, gauge.held():
            row = conn.execute("SELECT pg_sleep(0.05), %s::int", (i,)).fetchone()
        assert row is not None
        return f"row-{row[1]}"

    mcp = build_test_mcp([probe], semaphore=None)

    async def _run() -> list[str]:
        async with connected_sessions(mcp, sessions) as clients:
            results = await asyncio.gather(
                *(
                    client.call_tool("probe", {"i": s * calls + c})
                    for s, client in enumerate(clients)
                    for c in range(calls)
                )
            )
            return [text_of(r) for r in results]

    t0 = time.monotonic()
    texts = asyncio.run(_run())
    return texts, time.monotonic() - t0


def _warm(store: Store, size: int) -> None:
    """Grow the pool to ``size`` connections, then zero its counters.

    psycopg_pool counts a request as ``requests_queued`` whenever no idle
    connection is ready — including the moment a pool below ``max_size``
    is still opening a new one. Without this a ``min_size=1`` pool would
    report "queued" for its own lazy growth, which says nothing about
    contention. After warming, the connections stay idle in the pool
    (``max_idle`` is minutes), so what the storm then counts is real waiting.
    """
    held = [store.pool.getconn() for _ in range(size)]
    for conn in held:
        store.pool.putconn(conn)
    assert store.pool.get_stats()["pool_size"] == size
    store.pool.pop_stats()


# ── the sizing rule: the semaphore binds, the pool does not ──────────


def test_a_session_storm_queues_on_the_semaphore_not_the_pool(
    monkeypatch: pytest.MonkeyPatch, _reset_tool_semaphore: None
) -> None:
    """Tool concurrency 4 under pool max 6: the pool never has to wait.

    Four sessions × six concurrent calls = 24 calls against a semaphore of
    four. Proves the sizing rule: only four calls reach the pool at once,
    so at most four of its six connections are ever checked out and
    ``requests_queued`` stays 0 once the pool has grown (see
    :func:`_warm`), with no pool errors. Every call returns its own reply,
    and the wall time stays near the ideal 24/4 × 0.05 s rather than
    serialising.

    Does NOT prove anything about pgbouncer (none is in the path), nor
    about a storm wider than the pool — the next test.
    """
    monkeypatch.setenv(server._TOOL_CONCURRENCY_ENV, "4")
    gauge = _Gauge()
    with _storm_pool(max_size=6, min_size=1) as (store, _app):
        _warm(store, 6)
        texts, elapsed = _storm(store, gauge, sessions=4, calls=6)
        stats = store.pool.get_stats()

    print(
        f"\n[pool-storm test1] elapsed={elapsed:.2f}s peak={gauge.peak} "
        f"requests_queued={stats.get('requests_queued', 0)} "
        f"requests_errors={stats.get('requests_errors', 0)} "
        f"requests_num={stats.get('requests_num')} pool_size={stats['pool_size']}"
    )
    assert sorted(texts) == sorted(f"row-{i}" for i in range(24)), texts
    assert stats.get("requests_errors", 0) == 0, stats
    assert stats.get("requests_queued", 0) == 0, (
        "a request waited on the pool although tool concurrency (4) < "
        f"pool max (6): {stats}"
    )
    assert stats["requests_num"] >= 24, stats
    assert 1 < gauge.peak <= 4, f"peak checked-out connections: {gauge.peak}"
    assert elapsed < 10, f"storm took {elapsed:.1f}s (ideal ~0.3s)"


def test_a_storm_wider_than_the_pool_waits_but_does_not_time_out(
    monkeypatch: pytest.MonkeyPatch, _reset_tool_semaphore: None
) -> None:
    """Tool concurrency 8 over pool max 3 — the shape the rule forbids.

    Four sessions × four calls = 16 calls; up to eight pass the semaphore
    at once but only three connections exist, so the pool MUST queue
    (``requests_queued`` > 0). The pool's wait ``timeout`` is a generous
    10 s: every call completes, none raises :class:`PoolTimeout`, and
    never more than three connections are checked out. Documents that
    misconfiguration costs latency, not correctness or a deadlock — the
    pool is a backstop.

    Does NOT prove the default pool ``timeout`` (30 s) is long enough for
    a real storm of slow tools; only that waiting resolves.
    """
    monkeypatch.setenv(server._TOOL_CONCURRENCY_ENV, "8")
    gauge = _Gauge()
    with _storm_pool(max_size=3, min_size=1, timeout=10.0) as (store, _app):
        _warm(store, 3)
        try:
            texts, elapsed = _storm(store, gauge, sessions=4, calls=4)
        except PoolTimeout as exc:  # pragma: no cover - the failure being pinned
            pytest.fail(f"pool wait timed out under the storm: {exc!r}")
        stats = store.pool.get_stats()

    print(
        f"\n[pool-storm test2] elapsed={elapsed:.2f}s peak={gauge.peak} "
        f"requests_queued={stats.get('requests_queued', 0)} "
        f"requests_wait_ms={stats.get('requests_wait_ms', 0)} "
        f"requests_errors={stats.get('requests_errors', 0)} "
        f"pool_size={stats['pool_size']}"
    )
    assert sorted(texts) == sorted(f"row-{i}" for i in range(16)), texts
    assert stats.get("requests_errors", 0) == 0, stats
    assert stats.get("requests_queued", 0) > 0, (
        f"the pool never queued although 8 permits sat over 3 connections: {stats}"
    )
    assert gauge.peak <= 3, f"peak checked-out connections: {gauge.peak}"
    assert elapsed < 10, f"storm took {elapsed:.1f}s (ideal ~0.3s)"


# ── teardown ─────────────────────────────────────────────────────────


def test_closing_the_pool_after_a_storm_leaves_no_backends() -> None:
    """Every connection the pool opened is gone once it is closed.

    The ``store`` fixture's leak guard already fails a test that leaves
    ``active`` / ``idle in transaction`` backends, but it ignores plain
    ``idle`` ones — and an idle pooled connection is exactly what a
    forgotten ``pool.close()`` leaves. This counts backends in any state
    by a per-pool ``application_name``: seen while the pool is open
    (so the probe is known to work), zero shortly after close (Postgres
    reaps a closed client's backend asynchronously, hence the short
    settle poll rather than an instant check).

    Does NOT cover the fixture's own Store, nor the interpreter-exit
    finalizer.
    """
    with _storm_pool(max_size=4, min_size=4) as (store, app):
        gauge = _Gauge()
        # A tool-less burst: borrow and release concurrently from threads.
        threads = [
            threading.Thread(target=lambda: _hold(store, gauge)) for _ in range(12)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not any(t.is_alive() for t in threads)
        assert _backends_named(app) >= 1, "probe never saw the pool's backends"

    deadline = time.monotonic() + 6
    while (left := _backends_named(app)) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert left == 0, f"{left} backend(s) of the closed pool still alive"


def _hold(store: Store, gauge: _Gauge) -> None:
    """Borrow a connection, run one trivial query, return it."""
    with store.pool.connection() as conn, gauge.held():
        conn.execute("SELECT pg_sleep(0.02)")
