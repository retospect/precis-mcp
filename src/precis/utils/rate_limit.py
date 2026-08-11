"""External-API rate limiter — general, DB-backed, cross-host.

Coordinates outbound calls to external HTTP APIs (Semantic Scholar,
OpenAlex, Unpaywall, arXiv, Crossref, ...) across every host in the
cluster through **one row per provider** in ``external_rate_limits``
(migration 0121). The single-row atomic ``UPDATE`` in
:func:`_try_consume` is the cross-host coordination point — analogous to
``resource_slots`` for LLM backends, but rate/quota-shaped for external
HTTP instead of concurrency-shaped for LLM serving.

Two independent lanes per provider row, both gated by the same atomic
statement:

* **Rate lane** — a token bucket (``capacity``/``refill_per_sec``/
  ``tokens``/``last_refill``). Every :func:`acquire` call refills lazily,
  computed in SQL from elapsed wall-clock time, before checking/consuming
  tokens — no background refill job needed.
* **Quota lane** — a daily counter (``daily_cap``/``day_used``/
  ``day_start``) for providers with a hard per-day ceiling (OpenAlex,
  Unpaywall: 100k/day). ``daily_cap IS NULL`` means the lane is inert. A
  row whose ``day_start`` has rolled past ``CURRENT_DATE`` resets
  ``day_used`` to 0 on the next acquire (read inline, in the same
  statement — no separate midnight-rollover job).

**Fail-open, always.** This module must never wedge a worker or break
ingest: no database configured, no row for the provider, ``PRECIS_
RATE_LIMIT=0``, or any DB error at all → :func:`acquire` returns ``True``
immediately and the caller degrades to whatever uncoordinated behaviour
it had before this module existed (its own tenacity backoff). The only
way :func:`acquire` returns ``False`` is a real, live "you are rate/
quota limited" answer read from the shared row.

**Store-free by design.** The S2 fetch functions this gates
(``precis.ingest.citations``, ``precis.ingest.semantic_scholar``) don't
carry a ``Store``/pool — they're plain functions called from workers and
handlers alike. So this module owns a small, lazily-opened, module-level
psycopg connection of its own (mirrors :mod:`precis.utils.db_log_handler`'s
"dedicated connection, not the shared pool" pattern) rather than taking one
as an argument. At the ~1 rps this coordinates, a single connection is
plenty — no pooling needed.

v1 wires only ``s2`` (rate lane) — see ``precis.ingest.citations``,
``precis.ingest.semantic_scholar``, and the straggler call site in
``precis.workers.fetch_oa``. The other seeded providers (``openalex``,
``unpaywall``, ``arxiv``, ``crossref``) are dormant config rows; nothing
calls :func:`acquire` for them yet.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import psycopg

from precis.config import load_config

log = logging.getLogger(__name__)

#: Module-level dedicated connection. Opened lazily on first :func:`acquire`
#: call; reopened if closed or the configured DSN changes (the latter
#: matters for tests, which point different sessions at different clone
#: DBs — see the module docstring's "store-free by design" note).
_conn: psycopg.Connection[Any] | None = None
_conn_dsn: str | None = None

#: The atomic consume: refill (computed from elapsed time since
#: ``last_refill``) then, in the same statement, consume ``n`` tokens and
#: ``n`` quota-units IF both lanes have room. A returned row means granted;
#: no row means either lane refused (or the provider doesn't exist — the
#: caller runs :data:`_SELECT_STATE_SQL` to tell those apart).
_CONSUME_SQL = """
WITH cur AS (
  SELECT provider, capacity, refill_per_sec, daily_cap,
         LEAST(capacity, tokens + refill_per_sec * EXTRACT(EPOCH FROM (now() - last_refill))) AS avail,
         CASE WHEN day_start < CURRENT_DATE THEN 0 ELSE day_used END AS used_today
    FROM external_rate_limits WHERE provider = %s FOR UPDATE
)
UPDATE external_rate_limits e
   SET tokens = cur.avail - %s,
       last_refill = now(),
       day_used = cur.used_today + %s,
       day_start = CURRENT_DATE
  FROM cur
 WHERE e.provider = cur.provider
   AND cur.avail >= %s
   AND (cur.daily_cap IS NULL OR cur.used_today + %s <= cur.daily_cap)
RETURNING e.tokens
"""

#: A cheap read-only probe run only when :data:`_CONSUME_SQL` didn't grant,
#: to tell "no row for this provider" (fail-open) apart from "rate-starved"
#: (retry) apart from "quota exhausted" (refuse now, don't spin).
_SELECT_STATE_SQL = """
SELECT capacity, refill_per_sec, daily_cap,
       LEAST(capacity, tokens + refill_per_sec * EXTRACT(EPOCH FROM (now() - last_refill))) AS avail,
       CASE WHEN day_start < CURRENT_DATE THEN 0 ELSE day_used END AS used_today
  FROM external_rate_limits WHERE provider = %s
"""

#: Never sleep longer than this in one poll iteration, even when the
#: token-bucket math or a generous ``max_wait_s`` would suggest a longer
#: nap — keeps the loop responsive to the ``max_wait_s`` deadline.
_MAX_POLL_INTERVAL_S = 5.0

#: Floor on a single poll sleep so we don't spin the DB in a tight loop
#: when ``avail`` is already very close to ``n``.
_MIN_POLL_INTERVAL_S = 0.05


#: Env values (case-insensitive) that turn the limiter OFF. Anything else —
#: including an empty string or unrecognised junk — leaves it ON, because the
#: safe default under a misconfig is to coordinate, and because this parse must
#: NEVER raise: it runs before :func:`acquire`'s broad fail-open ``try``, and an
#: uncaught ``ValueError`` here would propagate through the tenacity-retried S2
#: call sites (``reraise=True``) and break ingest — the one thing this module
#: promises it can't do. (A bare ``int("false")`` would do exactly that.)
_DISABLE_VALUES = frozenset({"0", "false", "no", "off"})


def _rate_limit_enabled() -> bool:
    """The limiter is on by default; ``PRECIS_RATE_LIMIT`` in
    :data:`_DISABLE_VALUES` (``0``/``false``/``no``/``off``, case-insensitive)
    disables it. Deliberately tolerant of non-numeric values so it can never
    raise — see :data:`_DISABLE_VALUES`."""
    return (
        os.environ.get("PRECIS_RATE_LIMIT", "1").strip().lower() not in _DISABLE_VALUES
    )


def _jitter_frac() -> float:
    """A small, deterministic per-process jitter fraction in ``[0, 0.25)``.

    Derived from ``os.getpid()`` rather than a PRNG so concurrent workers
    across hosts don't converge on the same poll cadence and hammer the row
    in lockstep, without needing a seeded random source."""
    return (os.getpid() % 100) / 400.0


def _get_conn() -> psycopg.Connection[Any] | None:
    """The lazily-opened module-level connection, or ``None`` when no
    ``database_url`` is configured. Raises on a genuine connect failure —
    the caller wraps every DB touch in a single broad ``try/except`` that
    fails open, so this doesn't need its own."""
    global _conn, _conn_dsn
    dsn = load_config().database_url
    if not dsn:
        return None
    if _conn is not None and dsn != _conn_dsn:
        try:
            _conn.close()
        except Exception:
            pass
        _conn = None
    if _conn is None or _conn.closed:
        _conn = psycopg.connect(dsn, autocommit=True)
        _conn_dsn = dsn
    return _conn


def _next_wait(n: int, avail: float, refill_per_sec: float, remaining: float) -> float:
    """Bounded, jittered sleep before the next poll: roughly the time for
    ``avail`` to reach ``n`` at ``refill_per_sec``, floored, jittered, and
    capped so it never overshoots ``remaining`` (the time left under
    ``max_wait_s``) or :data:`_MAX_POLL_INTERVAL_S`."""
    if refill_per_sec > 0:
        base = max(_MIN_POLL_INTERVAL_S, (n - avail) / refill_per_sec)
    else:
        base = _MAX_POLL_INTERVAL_S
    jittered = base * (1.0 + _jitter_frac())
    return max(0.0, min(jittered, remaining, _MAX_POLL_INTERVAL_S))


def acquire(provider: str, *, n: int = 1, max_wait_s: float = 30.0) -> bool:
    """Block (bounded by ``max_wait_s``) until ``n`` tokens for ``provider``
    are granted under the shared cross-host limiter, then return ``True``.

    Returns ``True`` immediately (fail-**open**) when: ``PRECIS_RATE_LIMIT=0``,
    ``database_url`` is unset, the provider has no row, or the table/DB is
    unreachable — the limiter must NEVER wedge a worker or break ingest;
    worst case we degrade to the pre-limiter uncoordinated tenacity
    behaviour. Returns ``False`` only when ``max_wait_s`` elapses while
    rate-starved, or the daily quota is exhausted (the caller proceeds and
    relies on its existing tenacity backoff either way).
    """
    if not _rate_limit_enabled():
        return True
    try:
        deadline = time.monotonic() + max_wait_s
        while True:
            conn = _get_conn()
            if conn is None:
                return True
            granted = conn.execute(_CONSUME_SQL, (provider, n, n, n, n)).fetchone()
            if granted is not None:
                return True
            state = conn.execute(_SELECT_STATE_SQL, (provider,)).fetchone()
            if state is None:
                return True  # unknown provider -> fail open
            _capacity, refill_per_sec, daily_cap, avail, used_today = state
            if daily_cap is not None and used_today + n > daily_cap:
                return False  # quota exhausted -> don't spin, refuse now
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False  # rate-starved past the wait budget
            time.sleep(_next_wait(n, float(avail), float(refill_per_sec), remaining))
    except Exception:
        log.debug(
            "rate_limit: acquire(%r, n=%d) failed; failing open",
            provider,
            n,
            exc_info=True,
        )
        return True


__all__ = ["acquire"]
