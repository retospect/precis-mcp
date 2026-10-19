"""The weekly credential check as a scheduled pass (gr496305 item 4).

``precis secret check`` (runbook ``docs/runbooks/secrets-check.md``) probes
every registry secret that has a cheap auth-only endpoint — but it only runs
when someone runs ``/whatneedsdoing``, so a revoked key otherwise surfaces
as a failing job. This pass runs :func:`precis.secret_status.weekly_report`
from the weekly, host-agnostic ``secret_check`` cadence and turns each
``failed`` row into one ``secret_check`` alert.

Dedup is the alerts producer's own: fingerprint ``failed:<name>`` keeps one
open alert per credential (a still-failing repeat sighting bumps
``seen_count``). Only a verified ``ok`` resolves it — ``unprobed`` (an
outage, a 5xx, a 429) and ``missing`` (the host-agnostic lease may land on
a host that simply lacks the key) keep the alert open, so a real "key
rejected" finding can't silently clear and re-raise as a fresh alert next
week. A credential dropped from the registry has no row and resolves.

Same security contract as the manual check: probes hit only the fixed
:data:`precis.secret_status._PROBES` endpoints, and a secret value is never
printed, logged, stored or put in alert text — the alert carries the
credential's name, the HTTP-status detail and the provider key URL.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from precis import secret_status
from precis.alert_ids import RULES_BY_ID
from precis.alerts import raise_alert, resolve_stale_alerts
from precis.workers.runner import BatchResult

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

ALERT_SOURCE = RULES_BY_ID["secret_check/failed:{secret}"].rule


def _fingerprint(name: str) -> str:
    return f"failed:{name}"


def run_secret_check_pass(store: Store) -> BatchResult:
    """One weekly check. ``claimed`` = registry secrets evaluated, ``ok`` =
    rows not ``failed``, ``failed`` = rows that raised/refreshed an alert."""
    rows = asyncio.run(secret_status.weekly_report(store))
    live = [_fingerprint(row.name) for row in rows if row.state != "ok"]
    n_failed = 0
    for row in rows:
        if row.state != "failed":
            continue
        n_failed += 1
        fingerprint = _fingerprint(row.name)
        # name, status detail and key URL only — never the value (see the
        # module docstring; raise_alert masks on top of that).
        raise_alert(
            store,
            source=ALERT_SOURCE,
            fingerprint=fingerprint,
            title=f"[secret_check] {row.name}: credential rejected",
            detail=(
                f"{row.detail}."
                + (f"\nKey URL: {row.get_url}" if row.get_url else "")
                + "\n\nRotate with `precis secret set`; the next weekly pass "
                "resolves this alert once the probe passes. Runbook: "
                "docs/runbooks/secrets-check.md"
            ),
        )
    resolved = resolve_stale_alerts(store, source=ALERT_SOURCE, live_fingerprints=live)
    if resolved:
        log.info("secret_check: resolved %d alert(s) no longer failing", resolved)
    return BatchResult(
        handler="secret_check",
        claimed=len(rows),
        ok=len(rows) - n_failed,
        failed=n_failed,
    )


__all__ = ["ALERT_SOURCE", "run_secret_check_pass"]
