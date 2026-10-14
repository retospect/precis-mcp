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

Since 2026-10-14 the Open API walk is an OPPORTUNISTIC leg, not a dependency
(gr481902): ``getComponentInfos`` has answered HTTP 500 to every call since
at least 2026-10-01, and the unfiltered listing enumerates the same ~7.27M-row
assemblable catalog — so the pass runs a bounded, page-checkpointed
**listing walk** (:func:`precis.pcb.jlc_library.walk_listing`,
:data:`LIST_PAGE_SETTING_KEY`) as its daily catalog coverage and the Open API
leg only confirms the endpoint is still dead. During a recorded 5xx outage
the leg is skipped outright (one WARN line, no daily alert — the listing
carries the catalog), re-probed every :data:`_OUTAGE_PROBE_EVERY`-th outage
day so a recovery is noticed within the week; a probe day's 500 or any
non-5xx failure still surfaces the same way it used to (degrade the job,
alert for the non-5xx cases). The outage record in ``app_state``
(:data:`OUTAGE_SETTING_KEY`) tracks first-seen + consecutive outage days,
and the pass result carries ``api_walk``/``coverage_lost`` so the run log
says plainly what the dead endpoint cost.
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
from precis.pcb.jlc_library import PostFn, refresh_library_parts, walk_listing
from precis.utils.coerce import to_int

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
_LISTING_FINGERPRINT = "jlcpcb-listing"

#: app_state checkpoint for the unfiltered listing walk — the listing has
#: no ``lastKey`` cursor, so ``currentPage`` is the resume token; ""
#: means "start at page 1", which is also where the walk resets after it
#: reaches the end (a fresh rescan of the ascending-C-number order).
LIST_PAGE_SETTING_KEY = "pcb.parts_refresh.list_page"

#: During a recorded 5xx outage the Open API leg is skipped and only
#: re-probed every Nth outage day — dead-endpoint traffic daily is still
#: a dependency, and a week-old recovery signal is good enough when the
#: listing walk covers the catalog.
_OUTAGE_PROBE_EVERY = 7


#: app_state record for a running upstream 5xx on the walk's endpoint —
#: ``{"first_seen": "YYYY-MM-DD", "runs": N, "status": <int>}``, bumped on
#: each outage-class failure and cleared (``""``, app_state has no delete —
#: same convention as :data:`CURSOR_SETTING_KEY`'s reset) on any other walk
#: outcome. Feeds the alert title's "since <date>, N consecutive runs"
#: (gr481902) so a multi-week upstream outage doesn't read like a fresh
#: failure every morning.
OUTAGE_SETTING_KEY = "pcb.parts_refresh.api_outage"


def _api_outage(store: Store) -> dict[str, Any] | None:
    """The recorded 5xx outage, or ``None``. ``{"first_seen", "runs",
    "status"}`` — ``runs`` counts consecutive outage *days* (skipped days
    bump it too: the outage is what persists, not the attempts)."""
    try:
        rec = json.loads(store.get_setting(OUTAGE_SETTING_KEY) or "")
    except ValueError:
        return None
    return rec if isinstance(rec, dict) and rec.get("status") else None


def _record_api_outage(store: Store, status: int | None) -> tuple[str, int]:
    """Bump the consecutive-5xx-day record; return ``(first_seen, runs)``."""
    today = datetime.now(UTC).date().isoformat()
    rec = _api_outage(store) or {}
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
    if _LISTING_FINGERPRINT in failures:
        raise_alert(
            store,
            source=_ALERT_SOURCE,
            fingerprint=_LISTING_FINGERPRINT,
            title="[parts_refresh] JLCPCB listing walk is failing",
            detail=f"{failures[_LISTING_FINGERPRINT]}\n\nThe listing walk is "
            "the pass's catalog coverage now that the Open API leg is "
            "opportunistic — `parts` gets nothing new until this clears.",
        )
    resolve_stale_alerts(
        store, source=_ALERT_SOURCE, live_fingerprints=(*failures, *untested)
    )


#: Rows imported per cycle — bounds one tick's transaction + HTTP time. The
#: standing pass fires daily (``workers/scheduler.py`` CADENCES), so the
#: ~7M-row catalog drains gradually over many days rather than in one tick.
DEFAULT_ROW_BUDGET = 2000


def _listing_walk(
    store: Store,
    *,
    row_budget: int,
    page_size: int,
    post: PostFn | None = None,
) -> dict[str, Any]:
    """One bounded tick of the unfiltered listing walk, checkpointed by
    page in :data:`LIST_PAGE_SETTING_KEY` (wraps to "" at the end)."""
    start = to_int(store.get_setting(LIST_PAGE_SETTING_KEY)) or 1
    out = walk_listing(
        store,
        row_budget=row_budget,
        start_page=start,
        page_size=page_size,
        post=post,
    )
    store.set_setting(
        LIST_PAGE_SETTING_KEY,
        "" if out["next_page"] is None else str(out["next_page"]),
    )
    return out


def _coverage_note(listing_error: str | None) -> str:
    """What the pass lost while the Open API leg didn't run — recorded so
    the job log/result says plainly what the dead endpoint costs."""
    if listing_error is not None:
        return (
            "BOTH live catalog sources failed this pass — `parts` only has "
            "the jlcparts dump plus whatever on-demand live lookups landed"
        )
    return (
        "none — the jlcpcb.com listing walk enumerates the same ~7.27M-row "
        "assemblable catalog the Open API walk covered (verified 2026-10-14); "
        "what's lost is only the credentialed channel's stability — the "
        "listing is an undocumented website endpoint"
    )


def run_parts_refresh_pass(
    store: Store,
    *,
    row_budget: int = DEFAULT_ROW_BUDGET,
    client: ComponentSource | None = None,
    page_size: int = 100,
    library: Callable[[Store], dict[str, int]] | None = None,
    list_post: PostFn | None = None,
    force_walk: bool = False,
) -> dict[str, Any]:
    """One parts-library top-up, one listing-walk tick, one opportunistic
    Open API leg.

    The library top-up (``library``, default
    :func:`~precis.pcb.jlc_library.refresh_library_parts`) runs first; its
    failure is reported as ``"library_error"`` (``failed`` +1) and the
    ``jlcpcb-library`` alert.

    The **listing walk** (:func:`walk_listing` over the unfiltered,
    credential-free listing — the same ~7.27M-row catalog the Open API
    enumerated) is the pass's real daily catalog coverage since gr481902:
    bounded to ``row_budget`` rows per tick, page-checkpointed in
    :data:`LIST_PAGE_SETTING_KEY`. Its failure is ``"listing_error"``
    (``failed`` +1) + the ``jlcpcb-listing`` alert. ``list_post`` is the
    HTTP seam for tests (``body -> decoded JSON``).

    The Open API leg is now opportunistic: dark is a clean no-op; a
    recorded 5xx outage (:data:`OUTAGE_SETTING_KEY`) skips it outright —
    one WARN line, no daily alert — except every
    :data:`_OUTAGE_PROBE_EVERY`-th outage day, which attempts it so a
    recovery is noticed. ``force_walk`` overrides the skip (the
    ``--from-api`` CLI verb: an operator asking for the API walk gets an
    attempt). A failed attempt never fails the job — rows it yielded
    before dying are still imported — but only non-5xx failures raise the
    ``jlcpcb-api`` alert; a 5xx just re-records the outage (the endpoint
    has been dead upstream since 2026-10-01; an alert that can't be
    acted on daily is noise).

    Returns the ``BatchResult`` shape (``claimed``/``ok``/``failed``) plus
    ``"library"``/``"listing"`` upsert counts, ``"api_walk"`` (``ok`` /
    ``dark`` / ``skipped:…`` / the failure detail), ``"coverage_lost"`` —
    what the pass forfeited while the Open API leg didn't contribute —
    and ``"error"`` verbatim when the walk was attempted and failed (the
    ``--from-api`` verb surfaces it).
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

    listing_upserted = 0
    listing_error: str | None = None
    try:
        listing = _listing_walk(
            store, row_budget=row_budget, page_size=page_size, post=list_post
        )
        listing_upserted = listing["upserted"]
        log.info(
            "parts_refresh: source=jlcpcb-listing rows=%d upserted=%d "
            "restocked=%d next_page=%s",
            listing["rows"],
            listing_upserted,
            listing["restocked"],
            listing["next_page"],
        )
    except VendorError as exc:
        log.warning("parts_refresh: listing walk failed: %s", exc)
        listing_error = str(exc)
        failures[_LISTING_FINGERPRINT] = listing_error

    def _result(base: dict[str, Any]) -> dict[str, Any]:
        base["library"] = library_upserted
        base["listing"] = listing_upserted
        if library_error is not None:
            base["failed"] += 1
            base["library_error"] = library_error
        if listing_error is not None:
            base["failed"] += 1
            base["listing_error"] = listing_error
        return base

    client = client or JlcApiClient(store=store)
    if not client.available:
        log.info(
            "parts_refresh: no JLCPCB API credentials configured; skipping this cycle"
        )
        # Dark skips the walk: leave any open API alert as it was.
        _sync_failure_alerts(store, failures, untested=(_ALERT_FINGERPRINT,))
        return _result(
            {
                "claimed": 0,
                "ok": 0,
                "failed": 0,
                "api_walk": "dark",
                "coverage_lost": _coverage_note(listing_error),
            }
        )

    # Skip the leg during a recorded 5xx outage except on probe days —
    # hammering a dead endpoint daily is still depending on it.
    outage = _api_outage(store)
    runs_next = int(outage["runs"]) + 1 if outage else 0
    if outage is not None and not force_walk and runs_next % _OUTAGE_PROBE_EVERY:
        first_seen, runs = _record_api_outage(store, outage["status"])
        api_walk = (
            f"skipped: upstream HTTP {outage['status']} since {first_seen}, "
            f"{runs} consecutive outage days (re-probes every "
            f"{_OUTAGE_PROBE_EVERY})"
        )
        log.warning("parts_refresh: jlcpcb-api walk %s", api_walk)
        _sync_failure_alerts(store, failures)
        return _result(
            {
                "claimed": 0,
                "ok": 0,
                "failed": 0,
                "api_walk": api_walk,
                "coverage_lost": _coverage_note(listing_error),
            }
        )

    since_key = store.get_setting(CURSOR_SETTING_KEY) or None
    rows: list[dict[str, Any]] = []
    error: str | None = None
    outage_failure = False  # this pass's walk died on an upstream 5xx
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
        error = str(exc)
        if exc.status is not None and exc.status >= 500:
            # A dead endpoint is an upstream condition, not a defect — one
            # WARN line with since/streak, no daily alert (gr481902): the
            # outage record already tracks it, and the listing walk carries
            # the catalog.
            outage_failure = True
            first_seen, runs = _record_api_outage(store, exc.status)
            log.warning(
                "parts_refresh: jlcpcb-api walk failed: upstream HTTP %d "
                "since %s, %d consecutive outage days — the listing walk "
                "carries the catalog (next probe run %d)",
                exc.status,
                first_seen,
                runs,
                ((runs // _OUTAGE_PROBE_EVERY) + 1) * _OUTAGE_PROBE_EVERY
                if runs % _OUTAGE_PROBE_EVERY
                else runs + _OUTAGE_PROBE_EVERY,
            )
        else:
            log.warning("parts_refresh: %s", exc)
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

    # A 5xx failure goes un-alerted (the outage record + WARN line carry
    # it); every other failure keeps the jlcpcb-api alert.
    if error is not None and not outage_failure:
        failures[_ALERT_FINGERPRINT] = error
    _sync_failure_alerts(store, failures)
    counts = store.parts_import(rows) if rows else {"upserted": 0, "restocked": 0}
    if rows:
        log.info(
            "parts_refresh: source=jlcpcb-api rows=%d upserted=%d restocked=%d",
            len(rows),
            counts["upserted"],
            counts["restocked"],
        )
    # Degrade, don't fail (gr481902): the job's success is the listing +
    # library upserts; `error`/`api_walk` carry the walk failure verbatim.
    return _result(
        {
            "claimed": len(rows),
            "ok": counts["upserted"],
            "failed": 0,
            "restocked": counts["restocked"],
            "api_walk": "ok" if error is None else error,
            "coverage_lost": (None if error is None else _coverage_note(listing_error)),
            **({"error": error} if error is not None else {}),
        }
    )


__all__ = [
    "CURSOR_SETTING_KEY",
    "DEFAULT_ROW_BUDGET",
    "LIST_PAGE_SETTING_KEY",
    "OUTAGE_SETTING_KEY",
    "run_parts_refresh_pass",
]
