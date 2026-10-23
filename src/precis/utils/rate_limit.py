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

**Deadline-aware waits.** ``acquire`` waits at most ``max_wait_s``, but
never past the caller's own deadline — the ``deadline=`` argument or the
ambient deadline :func:`precis.utils.deadline.call_with_deadline`
publishes on its worker thread, whichever bound lands first (gr506756: a
30 s limiter wait used to eat a ref pass's last seconds, keeping the
pass's per-cycle summary row from ever logging). A refusal is counted
per provider in this process (:func:`refusal_counts`) and logged; the
durable ``limiter_refusals`` row counter is the deferred half of S2.

**Store-free by design.** The S2 fetch functions this gates
(``precis.ingest.citations``, ``precis.ingest.semantic_scholar``,
``precis.workers.fetch_oa``) don't carry a ``Store``/pool — they're plain
functions called from workers and handlers alike, several layers below any
place a pool would be in scope, so threading one down would mean a wider
signature change across all three call sites for no benefit. So this module
owns a small, lazily-opened, module-level psycopg connection of its own
(mirrors :mod:`precis.utils.db_log_handler`'s "dedicated connection, not the
shared pool" pattern) rather than taking one as an argument. At the ~1 rps
this coordinates, a single connection is plenty — no pooling needed. A
:class:`threading.Lock` guards open/reopen/close so concurrent callers in
one process can't race on the module-level handle, and :func:`close`
(atexit-registered) gives it an explicit shutdown hook instead of leaking
the connection until process exit.

Wired providers: ``s2`` (rate lane) — ``precis.ingest.citations``,
``precis.ingest.semantic_scholar`` and the straggler call site in
``precis.workers.fetch_oa`` call :func:`acquire` and proceed either way,
because for a pure rate lane the bounded wait *is* the coordination.
``openalex`` (rate + 100k/day quota lane) — ``precis.ingest.openalex_meta``
and the three OpenAlex helpers in ``precis.workers.fetch_oa`` call
:func:`require`, which raises :class:`RateLimited` on a refusal so the
request is skipped instead of fired; a caller that ignored ``False`` would
defeat the daily cap. ``unpaywall`` (100k/day quota lane) —
``precis.workers.fetch_oa._query_unpaywall`` calls :func:`require` the same
way. ``arxiv`` (rate lane, ~1 request per 3s) — the arXiv PDF leg
(``fetch_oa._try_arxiv``) and the arXiv HTML/source markup legs call
:func:`require`; a refusal records ``rate_limited`` and skips the download.
``crossref`` (rate lane) — the three worker-pass chokepoints call
:func:`require`: ``precis.ingest.paper_meta_enrich.enrich_paper`` (defers
its batch unstamped), ``precis.workers.bib_parse._crossref_query`` (defers
the paper unstamped and stops the batch) and
``precis.workers.fetch_oa._query_crossref_pdf_links``. ``core`` (rate lane)
is consumed by ``precis.workers.fetch_oa._query_core_fulltext_urls`` before
its API lookup; a refusal is recorded as ``rate_limited``. The on-demand and
ingest-time lookups call it too: ``precis.ingest.crossref.lookup_crossref``
(``doi_backfill``/``metadata_resolve``), ``precis.ingest.provenance``,
``doi_registry``, ``crossref_citation`` and ``si_discovery``; each maps a
refusal onto its existing transport-failure outcome, never a "not found".
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import psycopg

from precis.config import load_config
from precis.liveness import drain_sleep
from precis.utils.deadline import deadline_at

log = logging.getLogger(__name__)

_TimingObserver = Callable[[str, str, float], None]
_timing_observer: ContextVar[_TimingObserver | None] = ContextVar(
    "rate_limit_timing_observer", default=None
)


@contextmanager
def observe_timing(observer: _TimingObserver) -> Iterator[None]:
    """Observe this context's limiter DB and sleep time in milliseconds.

    Opt-in tracing for a caller that needs to separate shared-limiter wait
    from its own network call. The callback is deliberately isolated from
    limiter behavior: an instrumentation error never changes admission.
    """
    token = _timing_observer.set(observer)
    try:
        yield
    finally:
        _timing_observer.reset(token)


def _report_timing(
    observer: _TimingObserver | None,
    provider: str,
    phase: str,
    started: float | None,
) -> None:
    if observer is None or started is None:
        return
    try:
        observer(provider, phase, (time.perf_counter() - started) * 1000)
    except Exception:
        log.debug("rate_limit: timing observer failed", exc_info=True)


#: Module-level dedicated connection. Opened lazily on first :func:`acquire`
#: call; reopened if closed or the configured DSN changes (the latter
#: matters for tests, which point different sessions at different clone
#: DBs — see the module docstring's "store-free by design" note). All
#: access goes through :data:`_lock` so open/reopen/close can't race across
#: threads in one process.
_conn: psycopg.Connection[Any] | None = None
_conn_dsn: str | None = None
_lock = threading.Lock()

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


def _resolve_dsn() -> str | None:
    """The DSN to coordinate through: the configured ``database_url``, else
    the one a long-lived process adopted.

    ``precis worker`` / ``precis serve`` call ``secrets.adopt_process_store``,
    which scrubs ``PRECIS_DATABASE_URL`` from ``os.environ`` after connecting.
    ``load_config()`` then sees no DSN, and without this fallback every
    :func:`acquire` in those processes failed open silently (gr503435: the
    unpaywall/openalex/arxiv rows went untouched from 2026-08-11)."""
    dsn = load_config().database_url
    if dsn:
        return dsn
    from precis import secrets

    return secrets.get_adopted_dsn()


def _get_conn() -> psycopg.Connection[Any] | None:
    """The lazily-opened module-level connection, or ``None`` when no
    ``database_url`` is configured. Raises on a genuine connect failure —
    the caller wraps every DB touch in a single broad ``try/except`` that
    fails open, so this doesn't need its own.

    Guarded by :data:`_lock` so concurrent callers in one process open/
    reopen the shared connection exactly once instead of racing.
    """
    global _conn, _conn_dsn
    dsn = _resolve_dsn()
    if not dsn:
        return None
    with _lock:
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


def close() -> None:
    """Explicit shutdown hook: close the module-level connection, if open.

    Registered with :mod:`atexit` so a clean process exit doesn't just
    let the connection leak until the OS reaps the socket. Safe to call
    when nothing is open (idempotent) and safe to call again after —
    :func:`_get_conn` reopens lazily on the next :func:`acquire`.
    """
    global _conn, _conn_dsn
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None
            _conn_dsn = None


atexit.register(close)


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


#: Most ``host:<netloc>`` rows :data:`_SEED_SQL` will create; past it a
#: new host runs unpaced (fail-open) instead of growing the table without
#: bound from agent-supplied URLs. At the cap, :func:`_evict_idle_host_rows`
#: first retires rows idle past :data:`HOST_ROW_IDLE_EVICT_S`; only a cap
#: full of *live* rows leaves the new host unpaced (gr507345 — prod filled
#: 500 rows in ~36 h from the OA fetcher's long tail of one-shot publisher
#: hosts, silently).
MAX_SEEDED_HOST_ROWS = 500

#: A ``host:`` row untouched for this long carries no state worth keeping:
#: its bucket refilled within a second of its last grant and any
#: ``Retry-After`` park has long lapsed (:data:`~precis.utils.http.
#: RETRY_AFTER_MAX_S` is 15 min), so evicting it loses nothing — the host is
#: re-seeded at its policy's default on its next request. ``last_refill``
#: is the last *granted* consume, i.e. last use.
HOST_ROW_IDLE_EVICT_S = 24 * 3600

#: Idle rows retired per eviction, so the cap is hit (and the delete run)
#: once per batch of new hosts rather than once per new host.
_EVICT_BATCH = 50

#: Seed a missing row (the ``host:<netloc>`` default from
#: :mod:`precis.utils.http`). ``DO NOTHING`` so a migration-seeded or
#: hand-tuned row always wins; ``RETURNING`` tells "inserted" from "cap
#: hit / already there" apart.
_SEED_SQL = """
INSERT INTO external_rate_limits (provider, capacity, refill_per_sec, tokens, daily_cap)
SELECT %s, %s, %s, %s, NULL
 WHERE (SELECT count(*) FROM external_rate_limits WHERE provider LIKE 'host:%%') < %s
ON CONFLICT (provider) DO NOTHING
RETURNING provider
"""

#: Retire the longest-idle ``host:`` rows past the idle threshold — the
#: eviction that makes room under :data:`MAX_SEEDED_HOST_ROWS`.
_EVICT_IDLE_HOST_SQL = """
DELETE FROM external_rate_limits
 WHERE provider IN (
       SELECT provider FROM external_rate_limits
        WHERE provider LIKE 'host:%%'
          AND last_refill < now() - make_interval(secs => %s)
        ORDER BY last_refill
        LIMIT %s)
RETURNING provider
"""

#: Host-row occupancy: ``(count, idle_count)`` — how full the cap is and how
#: many rows an eviction could retire right now. Read by the
#: ``limiter-host-cap`` condition (:mod:`precis.workers.conditions`).
HOST_ROW_OCCUPANCY_SQL = """
SELECT count(*) AS n,
       count(*) FILTER (WHERE last_refill < now() - make_interval(secs => %(idle_s)s)) AS idle
  FROM external_rate_limits
 WHERE provider LIKE 'host:%%'
"""


def _evict_idle_host_rows(conn: Any) -> int:
    """Retire up to :data:`_EVICT_BATCH` ``host:`` rows idle past
    :data:`HOST_ROW_IDLE_EVICT_S`; returns how many went. Logged at INFO
    so a churning cap is visible without flooding."""
    gone = conn.execute(
        _EVICT_IDLE_HOST_SQL, (float(HOST_ROW_IDLE_EVICT_S), _EVICT_BATCH)
    ).fetchall()
    if gone:
        log.info(
            "rate_limit: host-row cap (%d) reached — evicted %d row(s) idle "
            "over %dh to seed a new host",
            MAX_SEEDED_HOST_ROWS,
            len(gone),
            HOST_ROW_IDLE_EVICT_S // 3600,
        )
    return len(gone)


def _seed_host_row(conn: Any, provider: str, seed_row: tuple[int, float]) -> bool:
    """Insert ``provider``'s row; at the cap, evict idle rows and retry
    once. ``False`` means the cap is full of live rows (or the row appeared
    concurrently — the caller's next consume tells those apart) and the
    cap-full state is warned about once per :data:`_SILENT_WARN_EVERY_S`."""
    capacity, refill = seed_row
    params = (provider, capacity, refill, capacity, MAX_SEEDED_HOST_ROWS)
    if conn.execute(_SEED_SQL, params).fetchone() is not None:
        return True
    if _evict_idle_host_rows(conn) and conn.execute(_SEED_SQL, params).fetchone():
        return True
    occupancy = conn.execute(
        HOST_ROW_OCCUPANCY_SQL, {"idle_s": float(HOST_ROW_IDLE_EVICT_S)}
    ).fetchone()
    if occupancy is not None and int(occupancy[0]) >= MAX_SEEDED_HOST_ROWS:
        _warn_unpaced(
            provider,
            f"host-row cap full ({MAX_SEEDED_HOST_ROWS} rows, none idle over "
            f"{HOST_ROW_IDLE_EVICT_S // 3600}h to evict) — raise the cap or "
            "shorten the idle threshold",
        )
    return False


#: Park a row for a server's Retry-After: drive its tokens negative so the
#: refill takes ``seconds`` to bring them back to zero, for every host.
#: ``LEAST`` keeps an already-longer park.
_PARK_SQL = """
UPDATE external_rate_limits
   SET tokens = LEAST(
           LEAST(capacity, tokens + refill_per_sec * EXTRACT(EPOCH FROM (now() - last_refill))),
           -refill_per_sec * %s),
       last_refill = now()
 WHERE provider = %s AND refill_per_sec > 0
"""

#: Seconds between two fail-open warnings for one provider, so a dead
#: limiter store is visible in the logs without flooding them.
_FAIL_OPEN_WARN_EVERY_S = 300.0
_fail_open_warned: dict[str, float] = {}


def _warn_fail_open(provider: str, what: str) -> None:
    """Log that the limiter store failed and ``provider`` ran unpaced:
    WARNING at most once per :data:`_FAIL_OPEN_WARN_EVERY_S` per provider,
    DEBUG (with the traceback) every time."""
    log.debug("rate_limit: %s(%r) failed; failing open", what, provider, exc_info=True)
    now = time.monotonic()
    last = _fail_open_warned.get(provider)
    if last is not None and now - last < _FAIL_OPEN_WARN_EVERY_S:
        return
    _fail_open_warned[provider] = now
    log.warning(
        "rate_limit: limiter store unavailable for %r (%s); failing open, "
        "calls are unpaced across hosts",
        provider,
        what,
    )


#: Seconds between two warnings for one provider about a *silent* fail-open
#: (limiter off by env, no DSN, no row): these are configuration states, not
#: transient faults, so once an hour per process is enough to be found.
_SILENT_WARN_EVERY_S = 3600.0
_silent_warned: dict[str, float] = {}


def _warn_unpaced(provider: str, reason: str) -> None:
    """WARNING naming ``provider`` and why it runs unpaced, at most once per
    :data:`_SILENT_WARN_EVERY_S` per provider per process."""
    now = time.monotonic()
    last = _silent_warned.get(provider)
    if last is not None and now - last < _SILENT_WARN_EVERY_S:
        return
    _silent_warned[provider] = now
    log.warning(
        "rate_limit: %r runs unpaced, failing open (%s)",
        provider,
        reason,
    )


#: Per-process refusal tally, provider → count. The durable
#: ``limiter_refusals`` column the spec wants on the row is the deferred
#: half of outbound-policy S2 (its owner is undecided) — until then this
#: counter plus the rate-limited WARNING below is the demand signal
#: (log/metric only, no schema change).
_refusals: dict[str, int] = {}

#: Seconds between two refusal warnings for one provider — a starved 1/s
#: row can refuse every call, so the WARNING carries the running count and
#: fires at most once per this interval per provider per process.
_REFUSAL_WARN_EVERY_S = 300.0
_refusal_warned: dict[str, float] = {}


def _record_refusal(provider: str, reason: str) -> None:
    """Count a genuine limiter refusal and log it, at most once per
    :data:`_REFUSAL_WARN_EVERY_S` per provider — the warning's running count
    carries the suppressed ones. Called only on a real "you are limited"
    answer: never on fail-open, and a drain stop is shutdown, not demand."""
    _refusals[provider] = _refusals.get(provider, 0) + 1
    now = time.monotonic()
    last = _refusal_warned.get(provider)
    if last is not None and now - last < _REFUSAL_WARN_EVERY_S:
        return
    _refusal_warned[provider] = now
    log.warning(
        "rate_limit: %r refused by the shared limiter (%s; %d refusal(s) "
        "in this process)",
        provider,
        reason,
        _refusals[provider],
    )


def refusal_counts() -> dict[str, int]:
    """This process's limiter-refusal tally, provider → count. Process-local:
    the durable per-provider row counter is the deferred half of S2. For
    tests and the ``/limits`` view."""
    return dict(_refusals)


def acquire(
    provider: str,
    *,
    n: int = 1,
    max_wait_s: float = 30.0,
    seed_row: tuple[int, float] | None = None,
    deadline: float | None = None,
) -> bool:
    """Block (bounded by ``max_wait_s``) until ``n`` tokens for ``provider``
    are granted under the shared cross-host limiter, then return ``True``.

    Returns ``True`` immediately (fail-**open**) when: ``PRECIS_RATE_LIMIT=0``,
    ``database_url`` is unset, the provider has no row, or the table/DB is
    unreachable — the limiter must NEVER wedge a worker or break ingest;
    worst case we degrade to the pre-limiter uncoordinated tenacity
    behaviour. Returns ``False`` when ``max_wait_s`` elapses while
    rate-starved, the daily quota is exhausted, or the process starts
    draining mid-poll (gr204611 — a token-bucket wait must not outlive
    the SIGTERM drain budget) — the caller proceeds and relies on its
    existing tenacity backoff either way.

    ``deadline`` bounds the wait by the caller's own budget: an absolute
    ``time.monotonic()`` point past which the wait must not run (a ref
    pass's remaining seconds, outbound-policy S2 / gr506756). The ambient
    deadline :func:`precis.utils.deadline.call_with_deadline` publishes on
    its worker thread bounds the wait the same way — the effective budget
    is the EARLIEST of ``max_wait_s``, ``deadline`` and the ambient bound,
    so a limiter wait can never eat the budget of the call that owns it.
    A grant still wins on the instant it lands, even at the bound; a
    refusal past the bound is counted (:func:`refusal_counts`) and logged.

    ``seed_row=(capacity, refill_per_sec)`` inserts the provider's row on
    first use instead of failing open for a missing row.
    """
    if not _rate_limit_enabled():
        _warn_unpaced(provider, "disabled by PRECIS_RATE_LIMIT")
        return True
    try:
        now = time.monotonic()
        wait_until = now + max_wait_s
        ambient = deadline_at()
        if ambient is not None:
            wait_until = min(wait_until, ambient)
        if deadline is not None:
            wait_until = min(wait_until, deadline)
        deadline_bound = wait_until < now + max_wait_s
        wait_budget = wait_until - now
        observer = _timing_observer.get()
        while True:
            started = time.perf_counter() if observer is not None else None
            try:
                conn = _get_conn()
            finally:
                _report_timing(observer, provider, "db", started)
            if conn is None:
                _warn_unpaced(provider, "no database_url configured")
                return True
            started = time.perf_counter() if observer is not None else None
            try:
                granted = conn.execute(_CONSUME_SQL, (provider, n, n, n, n)).fetchone()
            finally:
                _report_timing(observer, provider, "db", started)
            if granted is not None:
                return True
            started = time.perf_counter() if observer is not None else None
            try:
                state = conn.execute(_SELECT_STATE_SQL, (provider,)).fetchone()
            finally:
                _report_timing(observer, provider, "db", started)
            if state is None:
                if seed_row is None:
                    _warn_unpaced(provider, "no external_rate_limits row")
                    return True  # unknown provider -> fail open
                started = time.perf_counter() if observer is not None else None
                try:
                    _seed_host_row(conn, provider, seed_row)
                finally:
                    _report_timing(observer, provider, "db", started)
                seed_row = None  # seed once; a second miss fails open
                continue
            _capacity, refill_per_sec, daily_cap, avail, used_today = state
            if daily_cap is not None and used_today + n > daily_cap:
                _record_refusal(provider, "daily quota exhausted")
                return False  # quota exhausted -> don't spin, refuse now
            remaining = wait_until - time.monotonic()
            if remaining <= 0:
                reason = f"rate-starved past its {max(0.0, wait_budget):g}s wait budget"
                if deadline_bound:
                    reason += " (caller deadline)"
                _record_refusal(provider, reason)
                return False  # rate-starved past the wait budget
            wait = _next_wait(n, float(avail), float(refill_per_sec), remaining)
            started = time.perf_counter() if observer is not None else None
            try:
                draining = drain_sleep(wait)
            finally:
                _report_timing(observer, provider, "sleep", started)
            if draining:
                # Draining — stop waiting for tokens; the caller's retry
                # path is itself drain-gated (precis.utils.http.external_retry).
                return False
    except Exception:
        _warn_fail_open(provider, "acquire")
        return True


_RATE_SQL = "SELECT refill_per_sec FROM external_rate_limits WHERE provider = %s"


def bucket_rate(provider: str) -> float | None:
    """``provider``'s refill rate in tokens per second, or ``None`` when
    there is no shared bucket to read (limiter off, no DSN, no row, or the
    store unreachable — the same fail-open cases :func:`acquire` grants).

    For a caller that fans out concurrent requests against one bucket
    (``classify``'s thread pool, gr507340): the rate bounds how many of its
    requests can be granted per second, so in-flight work beyond it only
    queues on the row and gets refused at ``max_wait_s``.
    """
    if not _rate_limit_enabled():
        return None
    try:
        conn = _get_conn()
        if conn is None:
            return None
        row = conn.execute(_RATE_SQL, (provider,)).fetchone()
        return float(row[0]) if row is not None else None
    except Exception:
        _warn_fail_open(provider, "bucket_rate")
        return None


class RateLimited(Exception):
    """The shared limiter refused ``provider``: its daily quota is spent, the
    rate wait ran past ``max_wait_s`` (or the caller's deadline, whichever
    came first), or the process is draining. Raised by :func:`require`;
    callers skip or defer the request rather than fire it."""

    def __init__(self, provider: str) -> None:
        super().__init__(f"rate limit: {provider} refused by the shared limiter")
        self.provider = provider


def require(
    provider: str,
    *,
    n: int = 1,
    max_wait_s: float = 30.0,
    seed_row: tuple[int, float] | None = None,
    deadline: float | None = None,
) -> None:
    """:func:`acquire`, raising :class:`RateLimited` instead of returning
    ``False``. For quota-lane providers, where firing the request anyway
    would breach the hard daily cap. Fails open exactly like :func:`acquire`.
    """
    if not acquire(
        provider, n=n, max_wait_s=max_wait_s, seed_row=seed_row, deadline=deadline
    ):
        raise RateLimited(provider)


def park(provider: str, seconds: float) -> None:
    """Hold ``provider``'s row for ``seconds`` (a server's Retry-After), so
    every host waits, not just the one that got the 429. No-op for a
    missing row; fails open (logged) like :func:`acquire`."""
    if not _rate_limit_enabled() or seconds <= 0:
        return
    try:
        conn = _get_conn()
        if conn is None:
            return
        conn.execute(_PARK_SQL, (float(seconds), provider))
    except Exception:
        _warn_fail_open(provider, "park")


__all__ = [
    "RateLimited",
    "acquire",
    "bucket_rate",
    "close",
    "observe_timing",
    "park",
    "refusal_counts",
    "require",
]
