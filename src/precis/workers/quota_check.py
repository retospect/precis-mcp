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

from precis.store import Store
from precis.utils.claude_quota import (
    DEFAULT_SCOPE,
    REFRESH_INTERVAL_S,
    refresh_snapshot,
)
from precis.workers.runner import BatchResult

log = logging.getLogger(__name__)


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

    snapshot = refresh_snapshot(store)
    if snapshot is None:
        log.info("quota_check: refresh returned no snapshot")
        return BatchResult(handler="quota_check", claimed=1, ok=0, failed=1)
    log.info(
        "quota_check: snapshot updated (windows=%s)",
        sorted(snapshot.windows.keys()),
    )
    return BatchResult(handler="quota_check", claimed=1, ok=1, failed=0)


__all__ = ["run_quota_check_pass"]
