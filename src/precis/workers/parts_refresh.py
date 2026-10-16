"""parts_refresh — the standing JLCPCB catalog ingest (gr264357).

Prod ``parts`` was EMPTY: :func:`precis.pcb.catalog.refresh_parts_from_sqlite`
existed but had zero callers, and there was no worker pass / CLI verb that
called it. See ``docs/backlog/pcb-guided-place-route.md`` "Footprint +
catalog reality" (the PREREQUISITE bullet) for the full context — everything
downstream (part selection, live-stock verification, the ``datasheet_url``
a later slice ingests from) is dead until this table is populated.

This module is the Flow B side of that fix: a bounded, resumable walk of the
JLCPCB Open API's ``lastKey`` cursor
(:meth:`precis.pcb.jlc_api.JlcApiClient.iter_components`) — the live,
incremental bulk pull, as opposed to the community jlcparts SQLite dump
(:mod:`precis.pcb.catalog`, Flow A), which stays the manual
``precis pcb refresh-parts --from-sqlite`` fallback for hosts without JLCPCB
API credentials.

The catalog is ~7M rows (spike-resolved 2026-08-27), so one tick imports at
most :data:`DEFAULT_ROW_BUDGET` rows and checkpoints the cursor in
``app_state`` (:data:`CURSOR_SETTING_KEY`) rather than memory — a killed
process, or the next day's ``parts_refresh`` scheduler-lease cadence
(``workers/scheduler.py`` CADENCES, daily), resumes the walk instead of
restarting it. Dark (no JLCPCB credentials configured) is a clean no-op, not
a raise — see :func:`run_parts_refresh_pass`.

The walk is API-only (gr464537). The jlcpcb.com website listing it briefly
leaned on while the Open API answered HTTP 500 is retired: that endpoint
sits under ``/api``, which jlcpcb.com's robots.txt disallows. The 500 was
our millisecond signing timestamp (now seconds,
:func:`precis.pcb.jlc_api.signing_timestamp`), not an upstream outage.
Pacing and backoff live in the client (``external_rate_limits`` row
``jlcpcb``, :func:`precis.pcb._http.with_backoff`). Basic/Preferred parts
arrive with the walk (``normalize_api_row`` maps ``componentLibraryType``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, Protocol

from precis.alert_ids import RULES_BY_ID
from precis.alerts import raise_alert, resolve_stale_alerts
from precis.pcb._http import VendorError
from precis.pcb.jlc_api import JlcApiClient, JlcPermissionError

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)


class ComponentSource(Protocol):
    """What this pass actually needs of a catalog client — a cursor walk and
    a readable checkpoint. Narrower than :class:`~precis.pcb.jlc_api.
    JlcApiClient` on purpose: the ``client=`` argument is a documented test
    seam, so it should type-check against a fake without the fake having to
    inherit a concrete class it shares no implementation with."""

    @property
    def available(self) -> bool: ...

    @property
    def last_key(self) -> str | None: ...

    def iter_components(
        self, *, since_key: str | None = ..., page_size: int = ...
    ) -> Iterator[dict[str, Any]]: ...


#: Durable resume point for the API ``lastKey`` cursor walk (``app_state``,
#: NOT in-memory) — shared by the standing worker pass and the
#: ``precis pcb refresh-parts --from-api`` CLI verb (both call
#: :func:`run_parts_refresh_pass`), so either one continues where the other
#: left off rather than each keeping its own progress.
CURSOR_SETTING_KEY = "pcb.parts_refresh.last_key"

#: Alert identity for a failing walk (gr464537: a daily 500 sat as a log
#: WARNING for a week while ``parts`` stayed empty).
_ALERT_SOURCE = RULES_BY_ID["parts_refresh/jlcpcb-api"].rule
_ALERT_FINGERPRINT = "jlcpcb-api"


def _sync_failure_alerts(store: Store, error: str | None) -> None:
    """Open the ``jlcpcb-api`` alert on a failed walk; resolve every
    parts_refresh alert otherwise (this also clears the retired
    ``jlcpcb-listing``/``jlcpcb-library`` rows)."""
    if error is not None:
        raise_alert(
            store,
            source=_ALERT_SOURCE,
            fingerprint=_ALERT_FINGERPRINT,
            title="[parts_refresh] JLCPCB catalog walk is failing",
            detail=f"{error}\n\nThe `parts` catalog stays stale/empty until "
            "this clears; fallback is `precis pcb refresh-parts --from-sqlite`.",
        )
    resolve_stale_alerts(
        store,
        source=_ALERT_SOURCE,
        live_fingerprints=(_ALERT_FINGERPRINT,) if error is not None else (),
    )


#: Rows imported per cycle — bounds one tick's transaction + HTTP time. The
#: standing pass fires daily (``workers/scheduler.py`` CADENCES), so the
#: ~7M-row catalog drains gradually over many days rather than in one tick.
DEFAULT_ROW_BUDGET = 2000


def run_parts_refresh_pass(
    store: Store,
    *,
    row_budget: int = DEFAULT_ROW_BUDGET,
    client: ComponentSource | None = None,
    page_size: int = 100,
) -> dict[str, Any]:
    """One bounded tick of the Open API ``lastKey`` walk.

    Dark (no credentials) is a clean no-op. A failed walk still imports the
    rows it yielded before dying, checkpoints the cursor it reached, raises
    the ``jlcpcb-api`` alert and reports the failure verbatim as ``"error"``
    (``failed`` 1; the ``--from-api`` CLI verb surfaces it). Success
    resolves the alert.

    Returns the ``BatchResult`` shape (``claimed``/``ok``/``failed``) plus
    ``"restocked"``.
    """
    client = client or JlcApiClient(store=store)
    if not client.available:
        log.info(
            "parts_refresh: no JLCPCB API credentials configured; skipping this cycle"
        )
        # Dark skips the walk: leave any open API alert as it was, but
        # still clear the retired listing/library rows.
        resolve_stale_alerts(
            store, source=_ALERT_SOURCE, live_fingerprints=(_ALERT_FINGERPRINT,)
        )
        return {"claimed": 0, "ok": 0, "failed": 0}

    since_key = store.get_setting(CURSOR_SETTING_KEY) or None
    rows: list[dict[str, Any]] = []
    error: str | None = None
    try:
        for norm in client.iter_components(since_key=since_key, page_size=page_size):
            rows.append(norm)
            if len(rows) >= row_budget:
                break
    except JlcPermissionError as exc:
        # The specific, actionable 403 message -- checked before the base
        # VendorError (JlcPermissionError is a subclass), so this one wins.
        log.warning("parts_refresh: %s", exc)
        error = str(exc)
    except VendorError as exc:
        # An outage, rate limiting, or the politeness circuit breaker open
        # (precis.pcb._http) -- not a bug here, just a failed cycle.
        log.warning("parts_refresh: %s", exc)
        error = str(exc)
    finally:
        # Checkpoint whatever the walk reached either way -- a partial page
        # still moved the cursor forward, and a walk that ran to
        # exhaustion resets `last_key` to None (stored as "") so the next
        # cycle starts a fresh full walk instead of replaying the stale
        # final cursor forever.
        if client.last_key != since_key:
            store.set_setting(CURSOR_SETTING_KEY, client.last_key or "")

    _sync_failure_alerts(store, error)
    counts = store.parts_import(rows) if rows else {"upserted": 0, "restocked": 0}
    if rows:
        log.info(
            "parts_refresh: source=jlcpcb-api rows=%d upserted=%d restocked=%d",
            len(rows),
            counts["upserted"],
            counts["restocked"],
        )
    return {
        "claimed": len(rows),
        "ok": counts["upserted"],
        "failed": 0 if error is None else 1,
        "restocked": counts["restocked"],
        **({"error": error} if error is not None else {}),
    }


__all__ = [
    "CURSOR_SETTING_KEY",
    "DEFAULT_ROW_BUDGET",
    "run_parts_refresh_pass",
]
