"""psycopg 3 connection pool factory.

Per-connection setup registers pgvector codecs lazily — only when the
`vector` extension exists in the target database — so the pool also
works against a freshly-created DB before migrations have applied the
extension.

JSONB round-trip happens via the `Jsonb()` adapter wrapper at call
sites; we don't register a global dict-as-jsonb adapter because that
breaks ordinary JSON columns and is too implicit.
"""

from __future__ import annotations

import os
import re
from typing import Any

from psycopg import Connection, sql
from psycopg_pool import ConnectionPool

#: A syntactically valid Postgres role name (lower-snake, ≤63 chars). Guards the
#: ``SET ROLE`` below against a malformed / injected ``PRECIS_MCP_DB_ROLE`` even
#: though ``sql.Identifier`` already quotes it — a privilege boundary refuses
#: anything that isn't an obvious role name rather than quoting garbage.
_ROLE_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _db_role_enforced() -> bool:
    """Whether a session should ``SET ROLE`` to ``PRECIS_MCP_DB_ROLE`` (tier-2 of
    the per-todo envelope, §13). DARK default: OFF.

    Reads a SEPARATE flag from the role var itself: ``PRECIS_MCP_DB_ROLE`` is
    already exported to every agentic ``claude -p`` subprocess today (harmless,
    unconsumed), so keying enforcement on its mere presence would silently turn
    write-isolation on across the fleet at deploy. ``PRECIS_MCP_DB_ROLE_ENFORCE``
    is the explicit opt-in flipped in the Phase-2 window — and ONLY where
    ``precis serve`` connects DIRECT to Postgres (session pooling): under a
    pgbouncer *transaction* pool a session ``SET ROLE`` both fails to persist and
    leaks across tenants, so it must not be enabled on a pgbouncer DSN.
    """
    return os.environ.get("PRECIS_MCP_DB_ROLE_ENFORCE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _apply_db_role(conn: Connection) -> None:
    """Issue ``SET ROLE <PRECIS_MCP_DB_ROLE>`` when enforcement is on (tier-2).

    Fails CLOSED: if the flag is on and the connecting role can't assume the
    target (the ``agent_rw`` → ``agent_ro`` membership isn't granted), the
    ``SET ROLE`` raises and the connection is refused — a write-isolation
    boundary must break loudly, never silently run over-privileged. No-op (and
    byte-identical to today) whenever the flag is off or the var is unset.

    PREREQUISITE for turning this on: the connecting role must be a member of the
    target role (``GRANT agent_ro TO agent_rw``), provisioned out-of-tree in the
    prod DB before the window flips the flag.
    """
    if not _db_role_enforced():
        return
    role = (os.environ.get("PRECIS_MCP_DB_ROLE") or "").strip()
    if not role:
        return
    if not _ROLE_NAME_RE.match(role):
        raise ValueError(f"PRECIS_MCP_DB_ROLE {role!r} is not a valid role name")
    conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))


def _configure_connection(conn: Connection) -> None:
    """Per-connection setup. Called by the pool's `configure=` hook.

    Must leave the connection in IDLE state — psycopg pool rejects
    connections still inside a transaction. We commit after the
    introspection SELECT to be safe.
    """
    try:
        # Pin every session to UTC, regardless of the server's default
        # ``TimeZone``. Homebrew initdb bakes the host-detected zone
        # (e.g. ``GB`` on the cluster Macs) into the base config; that
        # both renders timestamps in local/DST time and makes psycopg
        # emit "unknown PostgreSQL timezone: 'GB'; will use UTC" because
        # it can't map the alias to a Python zoneinfo. Precis is UTC
        # throughout, so make the session say so explicitly — this holds
        # even against a dev/test/freshly-initdb'd DB whose server
        # default has not (yet) been overridden to UTC.
        conn.execute("SET TIME ZONE 'UTC'")
        has_vector = conn.execute(
            "SELECT 1 FROM pg_type WHERE typname = 'vector' LIMIT 1"
        ).fetchone()
        if has_vector is not None:
            from pgvector.psycopg import register_vector

            register_vector(conn)
        # Tier-2 write-isolation (§13): assume the envelope's DB role for the
        # session's life. Session-level (survives the commit below), so a
        # dedicated per-call ``precis serve`` binds e.g. ``agent_ro`` for every
        # query. Dark unless PRECIS_MCP_DB_ROLE_ENFORCE is set. Runs LAST so the
        # introspection above executes as the full connecting role.
        _apply_db_role(conn)
    finally:
        conn.commit()


#: Project-wide default pool size. Imported by ``Store.connect`` so
#: both entry points agree on the same cap — previously they differed
#: (``Store.connect`` defaulted to 8, ``create_pool`` to 10), and the
#: lower number silently won for any caller routing through
#: ``Store.connect``. One source of truth.
DEFAULT_POOL_MIN_SIZE: int = 2
DEFAULT_POOL_MAX_SIZE: int = 10

#: Recycle idle connections after this many seconds. Kept strictly
#: BELOW pgbouncer's ``server_idle_timeout`` (default 600s) so precis
#: retires a pooled connection before pgbouncer tears down the server
#: link underneath it. When the two clocks are equal there is a race
#: window where pgbouncer has already recycled the backend but the
#: pool still hands the client connection out, and the next query
#: dies with ``OperationalError`` — the intermittent, recovers-on-
#: retry failures we saw on the MCP write path. 300s leaves a 2x
#: margin under pgbouncer.
DEFAULT_POOL_MAX_IDLE_SECONDS: float = 300.0

#: Force-recycle even active connections after this many seconds.
#: Kept under pgbouncer's ``server_lifetime`` (default 3600s) for the
#: same race-avoidance reason as ``max_idle`` above. Without this a
#: long-running worker can hold one connection for days; if Postgres
#: (or pgbouncer) recycles it the next query crashes.
DEFAULT_POOL_MAX_LIFETIME_SECONDS: float = 1800.0


def create_pool(
    dsn: str,
    *,
    min_size: int = DEFAULT_POOL_MIN_SIZE,
    max_size: int = DEFAULT_POOL_MAX_SIZE,
    open_timeout: float = 10.0,
    max_idle: float = DEFAULT_POOL_MAX_IDLE_SECONDS,
    max_lifetime: float = DEFAULT_POOL_MAX_LIFETIME_SECONDS,
    **kwargs: Any,
) -> ConnectionPool:
    """Create a psycopg connection pool with pgvector codec registered.

    The pool is opened immediately so callers can use it without a
    separate ``open()`` call. Caller owns the pool — close with
    ``pool.close()`` on shutdown.

    Fail-fast: ``open_timeout`` bounds how long ``pool.open(wait=True)``
    will block waiting for ``min_size`` healthy connections. If the DB
    is unreachable we raise ``PoolTimeout`` instead of hanging forever.

    ``max_idle`` and ``max_lifetime`` recycle connections on a clock
    so a NAT / firewall idle-timeout or a Postgres restart bounds the
    failure mode to one request rather than wedging the worker until
    manual intervention.
    """
    pool = ConnectionPool(
        conninfo=dsn,
        min_size=min_size,
        max_size=max_size,
        max_idle=max_idle,
        max_lifetime=max_lifetime,
        configure=_configure_connection,
        # Validate liveness on checkout. pgbouncer (transaction mode)
        # recycles server connections on its own clock, so a pooled
        # client connection can outlive the backend it routes to. Without
        # a check, the pool hands out that dead connection and the request
        # fails with OperationalError; ``check_connection`` runs a cheap
        # ``SELECT 1`` and transparently discards+replaces a dead one
        # instead. This is the primary fix for the intermittent MCP
        # write failures; the sub-pgbouncer ``max_idle``/``max_lifetime``
        # above narrow the race, ``check`` closes it.
        check=ConnectionPool.check_connection,
        open=False,
        **kwargs,
    )
    pool.open(wait=True, timeout=open_timeout)
    return pool
