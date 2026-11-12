"""Hourly ``app_state`` cache for the corpus-wide backlog counters (gr573440).

The queue depths behind the ``watchdog:discovery/*`` checks, the
``/status`` backlog panel and the materializer's mint gate are anti-joins
or ``count(*) FILTER`` scans over every ``chunks`` row (5.8M on prod,
5-25 s each). They ran on every 300 s ``materialize`` tick, at the tail of
every ``embed_batch`` job and on every panel load: about 1,000 minutes of
DB time since 2026-10-22. No index gives the same numbers.
``chunks_keywords_pending_idx`` covers only ``keywords IS NULL``, but the
keywords backlog also counts version/``content_sha``-stale rows and splits
on the embed gate. The other counters have no partial index at all.

:func:`cached_count` runs such a count at most once per :data:`TTL_S`
across the fleet and parks the result in ``app_state`` under
``backlog_count:<name>``. Readers get the parked value with its
``counted_at`` (``app_state.updated_at``, written and compared on the DB
clock, so host clock skew cannot age it) and show that age wherever they
render the number.

Trade-off: a reader can see a number up to one TTL old. Thresholds still
compare against the same quantity, so an alert fires at most one TTL late:
the digest that evaluates ``discovery/embed`` (2 h budget) and
``discovery/chunk_keywords`` (6 h) runs hourly anyway. The materializer can
mint ``embed_batch`` jobs that find nothing for up to an hour after a
drain, and a backlog that appears right after a count waits up to an hour
for its mint. Hub and memory writes do not wait on this path: they queue
their own nudge job (``workers/embed.py`` fresh-hub nudge).

Transactions: the caller's transaction is never committed or rolled back
here. A fresh hit is one ``SELECT`` on the caller's connection (or a pooled
one when it passes none). A stale hit recounts and writes on a separate
short connection from ``pool``, in its own transaction, so a caller with
writes in flight keeps full control of them. The recount runs there too,
so it sees committed rows only, which is what a fleet-wide count should see.

Concurrency: the side connection takes a transaction advisory lock on the
key before it recounts; the lock is released when that transaction ends.
A stale reader that cannot take the lock keeps the stale value rather than
running a second scan.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

#: Longest a parked count is served before a reader recounts it.
TTL_S = 3600

_KEY_PREFIX = "backlog_count:"


@dataclass(frozen=True, slots=True)
class CachedCount:
    """A parked counter value and when it was counted (DB clock)."""

    value: Any
    counted_at: datetime


def _read(conn: Any, key: str, ttl_s: float) -> tuple[CachedCount | None, bool]:
    row = conn.execute(
        "SELECT value, updated_at, "
        "updated_at > clock_timestamp() - make_interval(secs => %s) "
        "FROM app_state WHERE key = %s",
        (ttl_s, key),
    ).fetchone()
    if row is None:
        return None, False
    try:
        value = json.loads(row[0])
    except ValueError:
        return None, False
    return CachedCount(value, row[1]), bool(row[2])


def cached_count(
    pool: Any,
    name: str,
    compute: Callable[[Any], Any],
    *,
    conn: Any | None = None,
    ttl_s: float = TTL_S,
) -> CachedCount:
    """``compute(side_conn)``'s JSON-able result, recomputed at most once
    per ``ttl_s`` fleet-wide (see the module docstring). ``conn`` serves the
    fresh-hit read and is otherwise left alone; the recount and the write
    use their own connection from ``pool``. ``compute`` raising propagates
    and parks nothing."""
    key = _KEY_PREFIX + name
    if conn is not None:
        hit, fresh = _read(conn, key, ttl_s)
        if hit is not None and fresh:
            return hit
    with pool.connection() as side:
        if conn is None:
            hit, fresh = _read(side, key, ttl_s)
            if hit is not None and fresh:
                return hit
        locked = side.execute(
            "SELECT pg_try_advisory_xact_lock(hashtext(%s))", (key,)
        ).fetchone()
        if locked and locked[0]:
            # Another session may have parked a fresh value meanwhile.
            hit, fresh = _read(side, key, ttl_s)
            if hit is not None and fresh:
                return hit
        elif hit is not None:
            return hit
        value = compute(side)
        row = side.execute(
            "INSERT INTO app_state (key, value, updated_at) "
            "VALUES (%s, %s, clock_timestamp()) "
            "ON CONFLICT (key) DO UPDATE SET "
            "value = EXCLUDED.value, updated_at = EXCLUDED.updated_at "
            "RETURNING updated_at",
            (key, json.dumps(value)),
        ).fetchone()
    return CachedCount(value, row[0])


def invalidate(pool: Any, name: str | None = None) -> None:
    """Drop one parked count (or all of them) so the next read recounts."""
    with pool.connection() as side:
        if name is None:
            side.execute(
                "DELETE FROM app_state WHERE starts_with(key, %s)", (_KEY_PREFIX,)
            )
        else:
            side.execute("DELETE FROM app_state WHERE key = %s", (_KEY_PREFIX + name,))


__all__ = ["TTL_S", "CachedCount", "cached_count", "invalidate"]
