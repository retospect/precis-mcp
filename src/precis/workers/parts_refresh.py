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

A failed walk degrades rather than failing the job (gr481902): the
``getComponentInfos`` endpoint sat at HTTP 500 for weeks — an upstream outage
nobody here can fix — while the library top-up kept working, so a dead walk
still raises its alert but no longer marks the pass failed. The outage's
first-seen date and consecutive-run count are kept in ``app_state``
(:data:`OUTAGE_SETTING_KEY`) so the alert title can say *since when* instead
of reading identically on day 1 and day 30.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from precis.alert_ids import RULES_BY_ID
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
_ALERT_SOURCE = RULES_BY_ID["parts_refresh/jlcpcb-api"].rule
_ALERT_FINGERPRINT = "jlcpcb-api"


_LIBRARY_FINGERPRINT = "jlcpcb-library"


#: app_state record for a running upstream 5xx on the walk's endpoint —
#: ``{"first_seen": "YYYY-MM-DD", "runs": N, "status": <int>}``, bumped on
#: each outage-class failure and cleared (``""``, app_state has no delete —
#: same convention as :data:`CURSOR_SETTING_KEY`'s reset) on any other walk
#: outcome. Feeds the alert title's "since <date>, N consecutive runs"
#: (gr481902) so a multi-week upstream outage doesn't read like a fresh
#: failure every morning.
OUTAGE_SETTING_KEY = "pcb.parts_refresh.api_outage"


def _record_api_outage(store: Store, status: int | None) -> tuple[str, int]:
    """Bump the consecutive-5xx-run record; return ``(first_seen, runs)``."""
    today = datetime.now(UTC).date().isoformat()
    try:
        rec = json.loads(store.get_setting(OUTAGE_SETTING_KEY) or "")
    except ValueError:
        rec = {}
    first_seen = str(rec.get("first_seen") or today)
    runs = int(rec.get("runs") or 0) + 1
    store.set_setting(
        OUTAGE_SETTING_KEY,
        json.dumps({"first_seen": first_seen, "runs": runs, "status": status}),
    )
    return first_seen, runs


def _clear_api_outage(store: Store) -> None:
    """Any non-5xx walk outcome ends the outage streak."""
    if store.get_setting(OUTAGE_SETTING_KEY):
        store.set_setting(OUTAGE_SETTING_KEY, "")


def _sync_failure_alerts(
    store: Store,
    failures: dict[str, str],
    *,
    untested: tuple[str, ...] = (),
    titles: dict[str, str] | None = None,
) -> None:
    """Open an alert per failing step (``{fingerprint: error}``); resolve
    every other parts_refresh alert except ``untested`` steps (skipped this
    pass, so an open alert for them is neither confirmed nor cleared).
    ``titles`` overrides a step's default alert title this pass (the outage
    message names first-seen + run count, which vary per sighting)."""
    titles = titles or {}
    if _ALERT_FINGERPRINT in failures:
        raise_alert(
            store,
            source=_ALERT_SOURCE,
            fingerprint=_ALERT_FINGERPRINT,
            title=titles.get(
                _ALERT_FINGERPRINT, "[parts_refresh] JLCPCB catalog walk is failing"
            ),
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
    an ``"error"`` key when the walk itself failed — either
    :class:`~precis.pcb.jlc_api.JlcPermissionError` (403: the signature is
    valid but the app's Open API console hasn't been granted the Components
    scope) or a plainer :class:`~precis.pcb._http.VendorError`/
    :class:`~precis.pcb._http.VendorUnavailable` (outage, rate limiting, or
    the politeness circuit breaker open). A failed walk does NOT fail the
    job (gr481902 — the endpoint sat at upstream HTTP 500 for weeks while
    every other source kept working): ``failed`` counts the library top-up
    only, any rows the walk yielded before dying are still imported, and
    the failure surfaces via the ``jlcpcb-api`` alert — which for a 5xx
    reads "upstream HTTP <status> since <first-seen>, N consecutive runs"
    (tracked in :data:`OUTAGE_SETTING_KEY`) instead of the bare "walk is
    failing". ``"error"`` is still set verbatim so an operator-facing caller
    (the ``--from-api`` CLI verb; ``scheduler.py`` would otherwise only see
    a generic ``except Exception`` traceback) can show the real message.
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
    alert_titles: dict[str, str] = {}
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
        _clear_api_outage(store)
    except VendorError as exc:
        # An outage, rate limiting, or the politeness circuit breaker open
        # (precis.pcb._http) — not a bug here, just a failed cycle.
        log.warning("parts_refresh: %s", exc)
        error = str(exc)
        if exc.status is not None and exc.status >= 500:
            # A dead endpoint is an upstream condition, not a defect here —
            # name it (with first-seen and streak length) so the alert
            # doesn't read as a fresh failure each morning (gr481902).
            first_seen, runs = _record_api_outage(store, exc.status)
            alert_titles[_ALERT_FINGERPRINT] = (
                f"[parts_refresh] JLCPCB getComponentInfos upstream "
                f"HTTP {exc.status} since {first_seen}, {runs} consecutive runs"
            )
        else:
            _clear_api_outage(store)
    else:
        _clear_api_outage(store)
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
    _sync_failure_alerts(store, failures, titles=alert_titles)
    if error is not None:
        # Degrade, don't fail (gr481902): the alert above carries the walk
        # failure; the job's success is the other sources' upserts, and
        # rows the walk yielded before dying are still worth importing —
        # the checkpoint already moved past them, so dropping them here
        # would lose them permanently.
        counts = store.parts_import(rows) if rows else {"upserted": 0, "restocked": 0}
        if rows:
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
                "error": error,
            }
        )
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


__all__ = [
    "CURSOR_SETTING_KEY",
    "DEFAULT_ROW_BUDGET",
    "OUTAGE_SETTING_KEY",
    "run_parts_refresh_pass",
]
