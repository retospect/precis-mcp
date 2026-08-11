"""Fleet lock-contention retry helper.

Background: prod roles run ``lock_timeout='5s'`` (set via ``ALTER ROLE`` in
ansible — a deliberate fail-fast policy, not something this module changes
or should change). Any write that can contend for the same row/table
*across the fleet* (many hosts racing the same paper, the same chunk's
batch, the same alert fingerprint, ...) can trip
``psycopg.errors.LockNotAvailable`` (SQLSTATE 55P03) well inside that 5s
budget when a sibling host happens to hold the lock a moment too long. The
fix at each call site is one of two things:

1. Keep the contended write's own transaction *short* — the primary
   defense, since a short transaction is rarely the one still holding the
   lock when a sibling arrives (``runner.py``'s claim-commit-first
   discipline, :func:`precis.workers.runner.run_handler_once`, is the
   canonical example).
2. When contention is still expected despite (1) — because the write is
   naturally retriable and the alternative (failing the caller's whole
   unit of work) is worse — wrap the write in :func:`retry_locked` here.

This is the ``src/`` counterpart of ``tests/conftest.py::_run_with_lock_retry``
(this repo's only other lock-retry helper, there because the test fixtures
themselves fight over table locks under xdist) — same idea, exponential
backoff with full jitter instead of that helper's linear one, since a
fleet-wide retry storm benefits more from hosts *not* converging on the
same retry cadence than a single-process test fixture does.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable

from psycopg.errors import DeadlockDetected, LockNotAvailable

log = logging.getLogger(__name__)

#: The two lock-contention error classes this helper retries on. Anything
#: else (a constraint violation, a syntax error, a connection drop) is a
#: real bug or a real outage — it propagates immediately, uncaught here.
_RETRIABLE = (LockNotAvailable, DeadlockDetected)


def retry_locked[T](
    fn: Callable[[], T],
    *,
    attempts: int = 4,
    base_s: float = 0.2,
    max_s: float = 5.0,
    label: str = "",
) -> T:
    """Call ``fn()``, retrying on lock contention (55P03 / deadlock).

    ``fn`` MUST open and commit its own fresh transaction — e.g. ``with
    store.pool.connection() as conn: ...; conn.commit()``, or ``with
    store.tx() as conn: ...`` (which commits on clean exit). This is a hard
    contract, not a style preference: once a statement inside a transaction
    raises ``LockNotAvailable``/``DeadlockDetected``, Postgres marks that
    transaction aborted — re-running a statement against the *same* still-open
    transaction just raises ``InFailedSqlTransaction`` instead of retrying
    anything. ``retry_locked`` re-invokes ``fn`` in full on each attempt
    specifically so it opens a brand new connection/transaction that hasn't
    been poisoned by the previous attempt's failure.

    Backoff is exponential with FULL jitter (``random.uniform(0, min(max_s,
    base_s * 2**attempt))``), not the linear backoff of
    ``tests/conftest.py::_run_with_lock_retry`` — full jitter means many
    hosts colliding on the same row don't converge on the same retry
    cadence and re-collide in lockstep on the next attempt.

    Logs a ``log.warning`` on every retry (including ``label``, so a grep
    over worker logs can tell which call site is contending) and reraises
    the triggering exception once ``attempts`` is exhausted — what an
    exhausted retry *means* (fail the batch outright, defer the item to the
    next pass, ...) is the caller's call, not this helper's.
    """
    if attempts <= 0:
        raise ValueError("attempts must be positive")
    for attempt in range(attempts):
        try:
            return fn()
        except _RETRIABLE as exc:
            if attempt + 1 >= attempts:
                raise
            delay = random.uniform(0, min(max_s, base_s * (2**attempt)))
            log.warning(
                "retry_locked%s: lock contention on attempt %d/%d, "
                "retrying in %.2fs: %r",
                f" ({label})" if label else "",
                attempt + 1,
                attempts,
                delay,
                exc,
            )
            time.sleep(delay)
    # Unreachable: the loop above either returns or raises on its last
    # iteration.
    raise AssertionError("retry_locked: exhausted attempts without raising")


__all__ = ["retry_locked"]
