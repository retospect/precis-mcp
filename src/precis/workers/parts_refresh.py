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

Each pass first tops up the JLCPCB Basic/Preferred *library* from the public
website listing (:mod:`precis.pcb.jlc_library`) — credential-free, so it runs
even when the API walk is dark. The dump lacks the low C-number range and the
Open API has answered HTTP 500 (gr464537).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any, Protocol

from precis.alerts import raise_alert, resolve_stale_alerts
from precis.pcb._http import VendorError
from precis.pcb.jlc_api import JlcApiClient, JlcPermissionError
from precis.pcb.jlc_library import refresh_library_parts

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
_ALERT_SOURCE = "parts_refresh"
_ALERT_FINGERPRINT = "jlcpcb-api"


_LIBRARY_FINGERPRINT = "jlcpcb-library"


def _sync_failure_alerts(
    store: Store, failures: dict[str, str], *, untested: tuple[str, ...] = ()
) -> None:
    """Open an alert per failing step (``{fingerprint: error}``); resolve
    every other parts_refresh alert except ``untested`` steps (skipped this
    pass, so an open alert for them is neither confirmed nor cleared)."""
    if _ALERT_FINGERPRINT in failures:
        raise_alert(
            store,
            source=_ALERT_SOURCE,
            fingerprint=_ALERT_FINGERPRINT,
            title="[parts_refresh] JLCPCB catalog walk is failing",
            detail=f"{failures[_ALERT_FINGERPRINT]}\n\nThe `parts` catalog "
            "stays stale/empty until this clears; fallback is "
            "`precis pcb refresh-parts --from-sqlite`.",
        )
    if _LIBRARY_FINGERPRINT in failures:
        raise_alert(
            store,
            source=_ALERT_SOURCE,
            fingerprint=_LIBRARY_FINGERPRINT,
            title="[parts_refresh] JLCPCB parts-library top-up is failing",
            detail=f"{failures[_LIBRARY_FINGERPRINT]}\n\nBasic/Preferred parts "
            "may be missing from `parts` until this clears; manual retry is "
            "`precis pcb refresh-parts --library`.",
        )
    resolve_stale_alerts(
        store, source=_ALERT_SOURCE, live_fingerprints=(*failures, *untested)
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
    library: Callable[[Store], dict[str, int]] | None = None,
) -> dict[str, Any]:
    """One parts-library top-up plus one JLCPCB catalog Open API cursor-walk tick.

    The library top-up (``library``, default
    :func:`~precis.pcb.jlc_library.refresh_library_parts`) runs first, before
    the credential check; its failure is reported as ``"library_error"``
    (``failed`` +1) and the ``jlcpcb-library`` alert, and never stops the
    API walk. ``"library"`` is the upserted count (0 on failure).

    Resumes from :data:`CURSOR_SETTING_KEY`'s checkpoint, imports up to
    ``row_budget`` normalized rows via the existing per-row
    :meth:`~precis.store._pcb_ops.PcbMixin.parts_import` upsert (right-sized
    for an incremental page; the full-dump ``--from-sqlite`` bulk reload
    uses the staging + atomic swap path instead — see
    :meth:`~precis.store._pcb_ops.PcbMixin.parts_bulk_replace`), then
    re-checkpoints the cursor — even a zero-row or fully-exhausted walk, so
    the *next* tick starts a fresh walk from the top rather than replaying a
    stale cursor forever.

    ``client`` is injectable (tests; also the CLI verb's ``--from-api``
    reuses this function directly rather than duplicating the walk). Dark
    (no credentials configured) is a clean no-op, not a raise — the
    community dump stays the manual fallback.

    Returns the ``BatchResult`` shape (``claimed``/``ok``/``failed``), plus
    an ``"error"`` key when ``failed`` — the walk hit either
    :class:`~precis.pcb.jlc_api.JlcPermissionError` (403: the signature is
    valid but the app's Open API console hasn't been granted the Components
    scope) or a plainer :class:`~precis.pcb._http.VendorError`/
    :class:`~precis.pcb._http.VendorUnavailable` (outage, rate limiting, or
    the politeness circuit breaker open) — either way the message is worth
    surfacing verbatim to an operator (this module's caller,
    ``scheduler.py``, would otherwise only see a generic ``except
    Exception`` traceback) rather than just logging it.
    """
    failures: dict[str, str] = {}
    library_upserted = 0
    library_error: str | None = None
    try:
        counts = (library or refresh_library_parts)(store)
        library_upserted = counts.get("upserted", 0)
        log.info(
            "parts_refresh: source=jlcpcb-library rows=%d upserted=%d restocked=%d",
            counts.get("rows", 0),
            library_upserted,
            counts.get("restocked", 0),
        )
    except VendorError as exc:
        log.warning("parts_refresh: library top-up failed: %s", exc)
        library_error = str(exc)
        failures[_LIBRARY_FINGERPRINT] = library_error

    def _result(base: dict[str, Any]) -> dict[str, Any]:
        base["library"] = library_upserted
        if library_error is not None:
            base["failed"] += 1
            base["library_error"] = library_error
        return base

    client = client or JlcApiClient(store=store)
    if not client.available:
        log.info(
            "parts_refresh: no JLCPCB API credentials configured; skipping this cycle"
        )
        # Dark skips the walk: leave any open API alert as it was.
        _sync_failure_alerts(store, failures, untested=(_ALERT_FINGERPRINT,))
        return _result({"claimed": 0, "ok": 0, "failed": 0})

    since_key = store.get_setting(CURSOR_SETTING_KEY) or None
    rows: list[dict[str, Any]] = []
    error: str | None = None
    try:
        for norm in client.iter_components(since_key=since_key, page_size=page_size):
            rows.append(norm)
            if len(rows) >= row_budget:
                break
    except JlcPermissionError as exc:
        # The specific, actionable 403 message — checked before the base
        # VendorError below (JlcPermissionError is a VendorError subclass),
        # so this one wins.
        log.warning("parts_refresh: %s", exc)
        error = str(exc)
    except VendorError as exc:
        # An outage, rate limiting, or the politeness circuit breaker open
        # (precis.pcb._http) — not a bug here, just a failed cycle.
        log.warning("parts_refresh: %s", exc)
        error = str(exc)
    finally:
        # Checkpoint whatever the walk reached either way — a partial page
        # still moved the cursor forward, and a walk that ran to
        # exhaustion resets `last_key` to None (stored as "") so the next
        # cycle starts a fresh full walk instead of replaying the stale
        # final cursor forever.
        if client.last_key != since_key:
            store.set_setting(CURSOR_SETTING_KEY, client.last_key or "")

    if error is not None:
        failures[_ALERT_FINGERPRINT] = error
    _sync_failure_alerts(store, failures)
    if error is not None:
        return _result({"claimed": len(rows), "ok": 0, "failed": 1, "error": error})
    if not rows:
        return _result({"claimed": 0, "ok": 0, "failed": 0})

    counts = store.parts_import(rows)
    log.info(
        "parts_refresh: source=jlcpcb-api rows=%d upserted=%d restocked=%d",
        len(rows),
        counts["upserted"],
        counts["restocked"],
    )
    return _result(
        {
            "claimed": len(rows),
            "ok": counts["upserted"],
            "failed": 0,
            "restocked": counts["restocked"],
        }
    )


__all__ = ["CURSOR_SETTING_KEY", "DEFAULT_ROW_BUDGET", "run_parts_refresh_pass"]
