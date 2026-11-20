"""Rolling spend meter — the tote, a query over the existing ledger.

Sums *actual* recorded cost over a time window from the two authoritative
ledgers, with no double-count (see `me690560` open
question #1):

* ``llm_call_log.cost_usd`` — every router dispatch (claude reports true
  dollars; OSS/local is priced via :mod:`precis.budget.pricing`).
* ``cache_state.cost_usd`` — paid fetches (perplexity, …); ``fetched_at`` is
  the last-fetch time, so a window sum approximates spend in that window.

``ref_events`` is deliberately **excluded**: an agentic call already logs to
``llm_call_log`` via ``router.route``, so summing ``ref_events`` too would
double-count it.

**Dark by construction.** :func:`bind_store` wires the process store at worker
/ runtime boot (mirroring :mod:`precis.route_log`); with none bound,
:func:`current_status` returns ``None`` and the breaker never trips.

**Shared snapshot + pending, not a per-process TTL** (gr732477). The steady
15s per-process recompute fully enumerated ``cache_state`` ~12k times a day
across the meter-bound processes, and at that table size the planner rightly
seq-scans — so the fix is the caller's frequency, not an index.
:func:`current_status` now reads two ``app_state`` point rows —
``budget.meter:snapshot`` (the last full-ledger sums) and
``budget.meter:pending`` (spend committed since) — and a single process
re-baselines the snapshot under a ``pg_try_advisory_xact_lock`` only when it
is older than the horizon :func:`_ttl_for` gives the *effective* status: 300s
comfortably under both caps, 15s near or over one. Ledger writers fold each
call's *actual* committed cost into ``pending`` inside the ledger
transaction itself (:func:`note_spend` — never a pre-call estimate, so a
failed or mis-estimated call bills the meter nothing), which makes new spend
— and therefore a cap trip — visible to every process at its next gate read
rather than one baseline later. The horizon then governs only the *decay*
side: a stale baseline keeps expired spend counted until the next refresh,
so the read can over-count but never under-count — a false trip self-corrects
within the tight cadence, and a real trip can't hide behind a quiet horizon.
Spend written outside the two ledger paths (ad-hoc SQL, backfills) enters at
the next baseline — at most one horizon late.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import psycopg

    from precis.store import Store

log = logging.getLogger(__name__)

#: Process-bound store the meter queries. ``bind_store`` sets it at boot;
#: unbound → :func:`current_status` returns ``None`` (breaker stays dark).
_STORE: Store | None = None

#: Default caps (USD). Placeholders — tune from a week of observed spend on
#: the read-only meter before the breaker goes live. Env-overridable.
DEFAULT_HOURLY_USD = 5.0
DEFAULT_DAILY_USD = 20.0

#: Transports whose ``llm_call_log.cost_usd`` is **notional**, not real money.
#: The ``claude -p`` OAuth path reports an API-list-price-equivalent dollar
#: figure, but that spend draws down the account's rate-limit *quota*, not a
#: metered balance — so it is excluded from the dollar meter (which must
#: reflect real money) and gated on the quota snapshot instead
#: (:mod:`precis.budget.quota`). Keep in sync with the ``claude`` transports in
#: :class:`precis.utils.llm.router.Transport`.
OAUTH_TRANSPORTS: tuple[str, ...] = ("claude_agent", "claude_p")

#: Re-baseline horizon for a shared snapshot that is near or over a cap:
#: :data:`_QUIET_FRACTION` of either window or more. Trips — and their
#: clearing — must be seen promptly, so this stays tight.
_CACHE_TTL_S = 15.0

#: Re-baseline horizon when the effective status was comfortably under both
#: caps. The db-thrash review (gr732477) logged ~35k full ``cache_state``
#: seq scans in 2.9 days from this meter's steady recompute across the
#: meter-bound processes (~12k/day ≈ one scan per 15s TTL tick per process);
#: the table is small enough that a seq scan is the planner's right choice
#: per call, so the fix is the caller's frequency, not an index. This is
#: 20× the tight horizon and, being shared, runs at most once per interval
#: *fleet-wide* — an order of magnitude fewer scans in the common under-cap
#: state. Accrued spend is carried by the ``pending`` counter between
#: baselines, so the long horizon defers only the decay of expired spend:
#: an approaching burn shows up in ``pending`` immediately and is then
#: re-read every :data:`_CACHE_TTL_S` before it can trip.
_CACHE_TTL_QUIET_S = 300.0

#: Under-cap fraction below which a status counts as quiet — spend strictly
#: below this fraction of BOTH caps. At or above it the tight horizon
#: applies, so the window from "half spent" to "tripped" is polled at the
#: 15s cadence.
_QUIET_FRACTION = 0.5

_HOUR_S = 3600
_DAY_S = 86400

#: ``app_state`` keys for the shared meter state (gr732477). The snapshot is
#: a JSON ``{"at": <db-epoch>, "hourly": …, "daily": …}`` written by
#: :func:`_rebaseline`; the pending counter is a float-as-text total of
#: :func:`note_spend` calls committed since that baseline.
_SNAPSHOT_KEY = "budget.meter:snapshot"
_PENDING_KEY = "budget.meter:pending"

#: ``pg_try_advisory_xact_lock`` key serialising baseline refreshes across
#: every meter-bound process — one refresher at a time, losers keep the
#: stale snapshot + pending rather than pile on ("bmtr" bytes).
_REFRESH_LOCK = 0x626D7472

_lock = threading.Lock()
#: Fallback memo for the shared path's failure mode only — when the
#: ``app_state`` read itself is broken (fakes, a missing/corrupt row) the
#: meter degrades to the old shape: a live full-ledger compute reused for
#: the tight horizon, so a broken shared row can't resurrect the
#: scan-per-call storm it replaced.
_cached: tuple[float, BudgetStatus] | None = None


def bind_store(store: Store | None) -> None:
    """Bind (or clear) the process store the meter queries. Also drops any
    cached status so a rebind takes effect immediately."""
    global _STORE, _cached
    _STORE = store
    _cached = None


def active_store() -> Store | None:
    """The process-bound store, if any — the breaker's fallback when a caller
    doesn't pass one explicitly."""
    return _STORE


@dataclass(frozen=True, slots=True)
class BudgetStatus:
    """Rolling spend vs caps for the hourly + 24h windows."""

    hourly_spent: float
    daily_spent: float
    hourly_cap: float
    daily_cap: float

    @property
    def tripped(self) -> bool:
        """True when either window is at or over its cap."""
        return (
            self.hourly_spent >= self.hourly_cap or self.daily_spent >= self.daily_cap
        )

    @property
    def tripped_window(self) -> str | None:
        """``'hourly'`` / ``'daily'`` (hourly wins ties) — the tripped window,
        or ``None`` when under both caps."""
        if self.hourly_spent >= self.hourly_cap:
            return "hourly"
        if self.daily_spent >= self.daily_cap:
            return "daily"
        return None


def note_spend(conn: psycopg.Connection, usd: float | None) -> None:
    """Fold one committed spend event's *actual* cost into the shared
    ``budget.meter:pending`` counter — inside the caller's transaction, so
    the note commits or rolls back with the ledger row it rides on.

    Callers are the two spend chokepoints' *write* sides:
    :func:`precis.route_log._write` for ``llm_call_log`` (non-OAuth
    transports only — matching the meter's filter) and the
    ``cache_state`` upserts in :mod:`precis.store._cache_ops` for paid
    fetches. Recording the actual committed cost — never a pre-call
    estimate — means a failed provider call bills the meter nothing and a
    provider-reported cost different from the estimate bills correctly.

    Best-effort under a savepoint: a metering note must never break the
    write it rides on. A missed increment is not lost forever — the next
    baseline re-sums the ledger itself (gr732477).
    """
    if usd is None or usd <= 0.0:
        return
    try:
        conn.execute("SAVEPOINT precis_meter_pending")
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET "
            "value = (app_state.value::float + %s)::text, updated_at = now()",
            (_PENDING_KEY, repr(float(usd)), usd),
        )
    except Exception:
        try:
            conn.execute("ROLLBACK TO SAVEPOINT precis_meter_pending")
        except Exception:
            pass
        log.debug("budget meter: pending-spend note failed", exc_info=True)


def spent_usd(store: Store, *, since_seconds: int) -> float:
    """Total recorded **real-money** USD spend over the trailing window.

    Union of ``llm_call_log`` (router LLMs) and ``cache_state`` (paid fetches);
    ``ref_events`` is excluded to avoid double-counting agentic calls, and the
    notional :data:`OAUTH_TRANSPORTS` rows (claude subscription, priced but not
    billed) are excluded so the dollar cap reflects money actually spent. A
    ``NULL``-transport row is kept (unknown → counted, conservative for a
    catastrophe rail).
    """
    interval = f"{int(since_seconds)} seconds"
    with store.pool.connection() as conn:
        llm = conn.execute(
            "SELECT COALESCE(sum(cost_usd), 0)::float FROM llm_call_log "
            "WHERE cost_usd IS NOT NULL AND ts > now() - %s::interval "
            "AND (transport IS NULL OR transport <> ALL(%s))",
            (interval, list(OAUTH_TRANSPORTS)),
        ).fetchone()
        fetch = conn.execute(
            "SELECT COALESCE(sum(cost_usd), 0)::float FROM cache_state "
            "WHERE cost_usd IS NOT NULL AND cost_usd > 0 "
            "AND fetched_at > now() - %s::interval",
            (interval,),
        ).fetchone()
    return float(llm[0] if llm else 0.0) + float(fetch[0] if fetch else 0.0)


def _spent_usd_windows(store: Store) -> tuple[float, float]:
    """``(hourly, daily)`` real-money spend in one scan per table.

    :func:`_compute_status` used to call :func:`spent_usd` twice — four
    scans of ``llm_call_log`` + ``cache_state`` per compute, the steady
    seq-scan load the db-thrash review logged on ``cache_state``
    (gr575088). One FILTER'd aggregate per table answers both windows in
    a single pass; the predicates mirror ``spent_usd`` — and the
    :func:`_rebaseline` CTE, which must stay byte-equivalent — exactly.
    """
    with store.pool.connection() as conn:
        llm = conn.execute(
            "SELECT COALESCE(sum(cost_usd) FILTER "
            "(WHERE ts > now() - %s::interval), 0)::float, "
            "COALESCE(sum(cost_usd), 0)::float "
            "FROM llm_call_log "
            "WHERE cost_usd IS NOT NULL AND ts > now() - %s::interval "
            "AND (transport IS NULL OR transport <> ALL(%s))",
            (f"{_HOUR_S} seconds", f"{_DAY_S} seconds", list(OAUTH_TRANSPORTS)),
        ).fetchone()
        fetch = conn.execute(
            "SELECT COALESCE(sum(cost_usd) FILTER "
            "(WHERE fetched_at > now() - %s::interval), 0)::float, "
            "COALESCE(sum(cost_usd), 0)::float "
            "FROM cache_state "
            "WHERE cost_usd IS NOT NULL AND cost_usd > 0 "
            "AND fetched_at > now() - %s::interval",
            (f"{_HOUR_S} seconds", f"{_DAY_S} seconds"),
        ).fetchone()
    assert llm is not None and fetch is not None
    return float(llm[0]) + float(fetch[0]), float(llm[1]) + float(fetch[1])


def _resolve_caps(store: Store) -> tuple[float, float]:
    """``(hourly_cap, daily_cap)`` — DB → env → compiled-default resolution
    lives in the registry (:mod:`precis.settings`); the registry's env var /
    default already match DEFAULT_HOURLY_USD / DEFAULT_DAILY_USD. ``or``
    would be wrong here — it'd also catch a legitimate 0.0."""
    from precis import settings as _settings

    hourly_cap = _settings.get_float("budget.hourly_usd", store=store)
    daily_cap = _settings.get_float("budget.daily_usd", store=store)
    return (
        hourly_cap if hourly_cap is not None else DEFAULT_HOURLY_USD,
        daily_cap if daily_cap is not None else DEFAULT_DAILY_USD,
    )


def _compute_status(store: Store) -> BudgetStatus:
    hourly_cap, daily_cap = _resolve_caps(store)
    hourly_spent, daily_spent = _spent_usd_windows(store)
    return BudgetStatus(
        hourly_spent=hourly_spent,
        daily_spent=daily_spent,
        hourly_cap=hourly_cap,
        daily_cap=daily_cap,
    )


def _ttl_for(status: BudgetStatus) -> float:
    """Re-baseline horizon for a shared snapshot — quiet far under both
    caps, tight at or above :data:`_QUIET_FRACTION` of either (gr732477).
    Applies to the *effective* status (snapshot + pending), so accrued
    spend that pushes the read over the fraction also shortens the
    horizon."""
    quiet = (
        status.hourly_spent < _QUIET_FRACTION * status.hourly_cap
        and status.daily_spent < _QUIET_FRACTION * status.daily_cap
    )
    return _CACHE_TTL_QUIET_S if quiet else _CACHE_TTL_S


def _shared_read(
    store: Store,
) -> tuple[float | None, float, float, float, float]:
    """One point read of the shared state.

    Returns ``(snapshot_epoch | None, snapshot_hourly, snapshot_daily,
    pending, db_now_epoch)`` — a missing snapshot reads as ``None`` with
    zeroed sums. Runs in one statement so the ``now()`` the age check uses
    is the DB's clock, consistent with the epoch the refresher stamps."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT (SELECT value FROM app_state WHERE key = %s), "
            "(SELECT value FROM app_state WHERE key = %s), "
            "extract(epoch from now())",
            (_SNAPSHOT_KEY, _PENDING_KEY),
        ).fetchone()
    if row is None:
        raise RuntimeError("budget meter: shared-state read returned no row")
    snap_at: float | None = None
    snap_h = snap_d = 0.0
    if row[0]:
        doc = json.loads(row[0])
        snap_at = float(doc["at"])
        snap_h = float(doc["hourly"])
        snap_d = float(doc["daily"])
    pending = float(row[1]) if row[1] else 0.0
    return snap_at, snap_h, snap_d, pending, float(row[2])


def _rebaseline(store: Store) -> tuple[float, float, float] | None:
    """Recompute the window sums and fold ``pending`` into a fresh shared
    snapshot. ``None`` when another process holds the refresh lock.

    The pending figure ``p0`` and the ledger sums share one statement
    snapshot, so subtracting ``p0`` removes exactly the accrual the new
    baseline already counts. The subtract UPDATE re-reads the latest row
    at *its* instant: a :func:`note_spend` committed between the CTE read
    and the UPDATE is neither subtracted (it isn't in ``p0``) nor lost —
    it stays in the counter, and the RETURNING value carries it into this
    call's result (review td733299: clearing the whole counter would
    discard spend recorded in that window).
    """
    with store.pool.connection() as conn:
        got = conn.execute(
            "SELECT pg_try_advisory_xact_lock(%s)", (_REFRESH_LOCK,)
        ).fetchone()
        if not got or not got[0]:
            return None
        row = conn.execute(
            "WITH p AS ("
            " SELECT value FROM app_state WHERE key = %s"
            "), l AS ("
            " SELECT COALESCE(sum(cost_usd) FILTER "
            "  (WHERE ts > now() - %s::interval), 0)::float AS h, "
            "  COALESCE(sum(cost_usd), 0)::float AS d "
            "  FROM llm_call_log "
            " WHERE cost_usd IS NOT NULL AND ts > now() - %s::interval "
            "  AND (transport IS NULL OR transport <> ALL(%s))"
            "), c AS ("
            " SELECT COALESCE(sum(cost_usd) FILTER "
            "  (WHERE fetched_at > now() - %s::interval), 0)::float AS h, "
            "  COALESCE(sum(cost_usd), 0)::float AS d "
            "  FROM cache_state "
            " WHERE cost_usd IS NOT NULL AND cost_usd > 0 "
            "  AND fetched_at > now() - %s::interval"
            ") SELECT (SELECT value FROM p), extract(epoch from now()), "
            "l.h, l.d, c.h, c.d FROM l, c",
            (
                _PENDING_KEY,
                f"{_HOUR_S} seconds",
                f"{_DAY_S} seconds",
                list(OAUTH_TRANSPORTS),
                f"{_HOUR_S} seconds",
                f"{_DAY_S} seconds",
            ),
        ).fetchone()
        assert row is not None
        pending_at_read = float(row[0]) if row[0] else 0.0
        at = float(row[1])
        hourly = float(row[2]) + float(row[4])
        daily = float(row[3]) + float(row[5])
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET "
            "value = EXCLUDED.value, updated_at = now()",
            (
                _SNAPSHOT_KEY,
                json.dumps({"at": at, "hourly": hourly, "daily": daily}),
            ),
        )
        left = conn.execute(
            "UPDATE app_state SET "
            "value = GREATEST(0.0, value::float - %s)::text, "
            "updated_at = now() WHERE key = %s RETURNING value",
            (pending_at_read, _PENDING_KEY),
        ).fetchone()
        conn.commit()
    pending_left = float(left[0]) if left and left[0] else 0.0
    return hourly, daily, pending_left


def _shared_status(store: Store) -> BudgetStatus:
    """Effective status off the shared snapshot + pending; re-baselines
    when the snapshot outlives the horizon the effective status implies.
    Raises (→ the caller's live-compute fallback) when the shared path is
    unavailable or a missing baseline can't be refreshed because another
    process holds the lock."""
    snap_at, snap_h, snap_d, pending, now = _shared_read(store)
    hourly_cap, daily_cap = _resolve_caps(store)
    status = BudgetStatus(
        hourly_spent=snap_h + pending,
        daily_spent=snap_d + pending,
        hourly_cap=hourly_cap,
        daily_cap=daily_cap,
    )
    if snap_at is None or (now - snap_at) > _ttl_for(status):
        refreshed = _rebaseline(store)
        if refreshed is not None:
            hourly, daily, pending_left = refreshed
            status = BudgetStatus(
                hourly_spent=hourly + pending_left,
                daily_spent=daily + pending_left,
                hourly_cap=hourly_cap,
                daily_cap=daily_cap,
            )
        elif snap_at is None:
            raise RuntimeError(
                "budget meter: no baseline yet and the refresh lock is held"
            )
        # Lock busy with a usable snapshot: the stale baseline + pending is
        # still conservative (pending carries every accrual); the other
        # refresher's write lands for the next read.
    return status


def current_status(
    store: Store | None = None, *, use_cache: bool = True
) -> BudgetStatus | None:
    """The current rolling status, or ``None`` when no store is available.

    ``store`` defaults to the process-bound one. With ``use_cache`` the
    read rides the shared ``app_state`` snapshot + pending counter —
    point reads on every call, with the full-ledger re-baseline shared
    fleet-wide on the horizon :func:`_ttl_for` gives the effective status
    (15s near or over a cap, 300s comfortably under both). A broken or
    absent shared state degrades to the old shape: a live compute reused
    for the tight horizon only. ``use_cache=False`` is always a live
    full-ledger read — the web tote wants a live read. Any query failure
    degrades to ``None`` so a metering problem can never break dispatch.
    """
    st = store if store is not None else _STORE
    if st is None:
        return None
    global _cached
    if use_cache:
        try:
            return _shared_status(st)
        except Exception:
            with _lock:
                if _cached is not None and time.monotonic() - _cached[0] < _CACHE_TTL_S:
                    return _cached[1]
    try:
        status = _compute_status(st)
    except Exception:
        return None
    if use_cache:
        with _lock:
            _cached = (time.monotonic(), status)
    return status


__all__ = [
    "DEFAULT_DAILY_USD",
    "DEFAULT_HOURLY_USD",
    "OAUTH_TRANSPORTS",
    "BudgetStatus",
    "bind_store",
    "current_status",
    "note_spend",
    "spent_usd",
]
