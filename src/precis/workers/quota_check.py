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

from precis.alert_ids import RULES_BY_ID
from precis.alerts import resolve_stale_alerts
from precis.store import Store
from precis.utils.claude_quota import (
    DEFAULT_SCOPE,
    REFRESH_INTERVAL_S,
    RefreshOutcome,
    raise_auth_alert,
    refresh_snapshot,
)
from precis.utils.db_log_handler import _resolve_host_name
from precis.workers.runner import BatchResult

log = logging.getLogger(__name__)

#: Alert source for the "``claude -p`` can't authenticate" condition. A
#: revoked/stale OAuth token 401s every agentic call cluster-wide
#: (plan_tick, reviewers, dream, figure) and was invisible for a whole day
#: behind an unrelated outage — this makes it page the moment it recurs.
#: The raise itself lives in ``utils/claude_quota.raise_auth_alert`` — the
#: router's tokenless-quota guard (gr635459) raises the SAME alert, one
#: fingerprint per host, so either detector pages once and either one's
#: healthy read resolves it.
_AUTH_ALERT_SOURCE = RULES_BY_ID["quota_check:auth/{host}:claude-oauth"].rule


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

    from precis.budget import quota as _quota

    existing = store.read_claude_quota(scope=DEFAULT_SCOPE)
    # A reactive stamp (budget/quota.stamp_exhausted) schedules exactly one
    # early probe at the window's reset: when it is due, refresh now even if
    # the snapshot is young — the refresh overwrites ``probe_due``, so the
    # probe fires once per exhausted window however many jobs parked.
    if existing is not None and not _quota.probe_due(store):
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
        raise_auth_alert(store, host=host)
    elif outcome in (RefreshOutcome.OK, RefreshOutcome.NO_LIMITS):
        # claude -p authenticated — resolve any open auth alert for this host.
        resolve_stale_alerts(store, source=_AUTH_ALERT_SOURCE, live_fingerprints=set())

    if snapshot is None:
        log.info("quota_check: refresh returned no snapshot (outcome=%s)", outcome)
        return BatchResult(handler="quota_check", claimed=1, ok=0, failed=1)
    # The pool ledger is a side channel: the snapshot above is already stored,
    # so a failed pool write (DB error, odd resets_at) must not fail the pass.
    try:
        from precis.utils.llm.accounts import record_pool_report

        for window, key in (
            ("five_hour", "llm:claude-sub:5h"),
            ("seven_day", "llm:claude-sub:7d"),
        ):
            bucket = snapshot.windows.get(window, {})
            used = bucket.get("used_percentage")
            if used is None:
                continue
            reset = bucket.get("resets_at")
            record_pool_report(
                store,
                key,
                used=used,
                remaining=100 - used,
                limit=100,
                source="provider",
                reported_at=snapshot.ts,
                resets_at=datetime.fromisoformat(reset) if reset else None,
            )
    except Exception:
        log.warning("quota_check: pool report failed", exc_info=True)
    log.info(
        "quota_check: snapshot updated (windows=%s)",
        sorted(snapshot.windows.keys()),
    )
    return BatchResult(handler="quota_check", claimed=1, ok=1, failed=0)


__all__ = ["run_quota_check_pass"]
