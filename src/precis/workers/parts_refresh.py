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
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, Protocol

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
    """One JLCPCB catalog Open API cursor-walk tick.

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
    client = client or JlcApiClient(store=store)
    if not client.available:
        log.info(
            "parts_refresh: no JLCPCB API credentials configured; skipping this cycle"
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
        return {"claimed": len(rows), "ok": 0, "failed": 1, "error": error}
    if not rows:
        return {"claimed": 0, "ok": 0, "failed": 0}

    counts = store.parts_import(rows)
    log.info(
        "parts_refresh: source=jlcpcb-api rows=%d upserted=%d restocked=%d",
        len(rows),
        counts["upserted"],
        counts["restocked"],
    )
    return {
        "claimed": len(rows),
        "ok": counts["upserted"],
        "failed": 0,
        "restocked": counts["restocked"],
    }


__all__ = ["CURSOR_SETTING_KEY", "DEFAULT_ROW_BUDGET", "run_parts_refresh_pass"]
