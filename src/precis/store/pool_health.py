"""Tell a pool that cannot connect from a pool that is full (gr663490).

``psycopg_pool.PoolTimeout`` means "no connection became available in
time" — it is the same exception whether every connection is checked out
(saturated: waiting helps) or the pool cannot open any (DB host
unreachable, Tailscale down: waiting cannot help). The pool's own stats
tell them apart: a pool below ``min_size`` with requests waiting, or whose
``connections_errors`` counter grew since the last look, is failing to
connect; a pool at ``max_size`` with nothing available is saturated.
"""

from __future__ import annotations

import threading
from typing import Any, Literal

PoolState = Literal["unreachable", "saturated", "unknown"]

#: ``id(pool)`` -> ``connections_errors`` seen at the previous diagnosis.
_last_errors: dict[int, int] = {}
_lock = threading.Lock()

UNREACHABLE_CAUSE = "cannot connect to the database"
UNREACHABLE_NEXT = (
    "waiting will not help — check Tailscale / the DB host is reachable, "
    "then retry the call"
)


def diagnose_pool(pool: Any) -> PoolState:
    """Classify ``pool`` from its stats; ``unknown`` when it cannot say."""
    try:
        stats = pool.get_stats()
    except Exception:
        return "unknown"
    size = int(stats.get("pool_size", 0))
    pool_min = int(stats.get("pool_min", 0))
    pool_max = int(stats.get("pool_max", 0))
    available = int(stats.get("pool_available", 0))
    errors = int(stats.get("connections_errors", 0))
    with _lock:
        grew = errors > _last_errors.get(id(pool), 0)
        _last_errors[id(pool)] = errors
    # A full pool is saturated even with an old connect error on record.
    if pool_max and size >= pool_max and available == 0:
        return "saturated"
    if size < pool_min or grew:
        return "unreachable"
    return "unknown"
