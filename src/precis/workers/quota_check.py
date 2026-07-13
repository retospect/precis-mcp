"""Quota-check pass — refresh the Claude.ai OAuth utilisation snapshot.

Runs on the **agent** profile (where hermes's OAuth state lives) every
worker cycle, but only fires the actual ``claude -p`` call when the
last persisted snapshot's ``ts`` is older than
:data:`precis.utils.claude_quota.REFRESH_INTERVAL_S` (default 600s).
That keeps the per-cycle cost near zero — most ticks short-circuit
on the SQL freshness check; the every-N-minute fire spends one input
token + one output token.

Failure modes are non-fatal: if the binary is missing or the response
has no ``rate_limits`` (free-tier accounts), the helper logs and the
pass reports ``failed=0`` so the cascade keeps running.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from precis.alerts import notify_critical_alert, raise_alert, resolve_stale_alerts
from precis.store import Store
from precis.utils.claude_quota import (
    DEFAULT_SCOPE,
    REFRESH_INTERVAL_S,
    RefreshOutcome,
    refresh_snapshot,
)
from precis.utils.db_log_handler import _resolve_host_name
from precis.workers.runner import BatchResult

log = logging.getLogger(__name__)

#: Alert source for the "``claude -p`` can't authenticate" condition. A
#: revoked/stale OAuth token 401s every agentic call cluster-wide
#: (plan_tick, reviewers, dream, figure) and was invisible for a whole day
#: behind an unrelated outage — this makes it page the moment it recurs.
_AUTH_ALERT_SOURCE = "quota_check:auth"


def _raise_auth_alert(store: Store, host: str) -> None:
    """Raise (once) the critical claude-auth alert and page on first sight."""
    fingerprint = f"{host}:claude-oauth"
    title = f"[claude-auth] claude -p can't authenticate on {host} (401)"
    detail = (
        "The Claude OAuth token is stale/revoked — plan_tick, reviewers, "
        "dream, and the /figure editor all 401. Re-drop the token into "
        "~hermes/.claude_oauth_token (+ ~deploy for precis-web) and restart "
        "the agent worker (launchctl kickstart -k system/com.precis.worker-agent)."
    )
    _ref, is_new = raise_alert(
        store,
        source=_AUTH_ALERT_SOURCE,
        fingerprint=fingerprint,
        title=title,
        detail=detail,
        severity="critical",
    )
    # Page exactly once per condition (mirrors the nursery worker-health
    # detectors). Keep only THIS host's fingerprint live so a stale alert
    # from a recovered host resolves; single-runner today, but correct if
    # quota_check ever runs on more than one agent node.
    resolve_stale_alerts(
        store, source=_AUTH_ALERT_SOURCE, live_fingerprints={fingerprint}
    )
    if is_new:
        notify_critical_alert(store, title, detail, fingerprint=fingerprint)


def run_quota_check_pass(store: Store, *, limit: int = 1) -> BatchResult:
    """Refresh the unified-scope snapshot if it's stale; otherwise no-op.

    Counters:

    * ``claimed`` = 1 when the freshness check fired (regardless of
      outcome) so the operator sees the pass is alive.
    * ``ok`` = 1 when a fresh snapshot landed.
    * ``failed`` = 1 when the refresh attempt failed (binary missing,
      no rate_limits in the response, persist error).
    """
    # ``limit`` is part of the BatchResult contract but a quota refresh
    # is fundamentally singleton — there's nothing to batch.
    del limit

    existing = store.read_claude_quota(scope=DEFAULT_SCOPE)
    if existing is not None:
        existing_ts = existing.ts
        if existing_ts.tzinfo is None:
            existing_ts = existing_ts.replace(tzinfo=UTC)
        age = datetime.now(UTC) - existing_ts
        if age < timedelta(seconds=REFRESH_INTERVAL_S):
            return BatchResult(handler="quota_check", claimed=0, ok=0, failed=0)

    snapshot, outcome = refresh_snapshot(store)

    # Auth-alert side channel: raise on a genuine auth failure, clear when
    # auth is healthy again, leave untouched on a transient/unknown blip.
    host = _resolve_host_name()
    if outcome is RefreshOutcome.AUTH_FAILED:
        _raise_auth_alert(store, host)
    elif outcome in (RefreshOutcome.OK, RefreshOutcome.NO_LIMITS):
        # claude -p authenticated — resolve any open auth alert for this host.
        resolve_stale_alerts(store, source=_AUTH_ALERT_SOURCE, live_fingerprints=set())

    if snapshot is None:
        log.info("quota_check: refresh returned no snapshot (outcome=%s)", outcome)
        return BatchResult(handler="quota_check", claimed=1, ok=0, failed=1)
    log.info(
        "quota_check: snapshot updated (windows=%s)",
        sorted(snapshot.windows.keys()),
    )
    return BatchResult(handler="quota_check", claimed=1, ok=1, failed=0)


__all__ = ["run_quota_check_pass"]
