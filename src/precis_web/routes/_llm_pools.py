"""Read-only allowance rows, sharing the scheduler's pacing calculation.

Only rolling rules supply a known window length. Expiry alone cannot tell
us when an entitlement began, so its remaining window share stays unknown.
"""

from datetime import UTC, datetime
from typing import Any

from precis.store import Store
from precis.utils.llm.accounts import RollingReset, list_pools, pacing, parse_reset_rule


def _timestamp(value: datetime | None) -> str | None:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%MZ") if value else None


def llm_pools_view(store: Store, now: datetime) -> list[dict[str, Any]]:
    """Group the stored pools by account without refreshing or spending them."""
    rows: list[dict[str, Any]] = []
    for pool in sorted(list_pools(store), key=lambda p: p.provider):
        state = pacing(pool, now)
        window_left = None
        if pool.reset_rule and pool.resets_at:
            try:
                rule = parse_reset_rule(pool.reset_rule)
            except ValueError:
                rule = None
            if isinstance(rule, RollingReset) and rule.seconds > 0:
                window_left = max(
                    0.0,
                    min(
                        100.0,
                        (pool.resets_at - now).total_seconds() / rule.seconds * 100,
                    ),
                )
        reasons = {
            "unknown": "Usage or a future reset/expiry is unknown.",
            "behind": "Usage is below the scheduler's pacing target; calls may run.",
            "on-pace": "Usage is within the scheduler's pacing band; calls may run.",
            "ahead": "Usage reached the pacing target; waiting for the drain window.",
            "exhausted": "Pool is parked, exhausted, or conservatively held on a stale provider report.",
        }
        relative = None
        if state.horizon:
            minutes = max(0, int((state.horizon - now).total_seconds() // 60))
            days, minutes = divmod(minutes, 1440)
            hours, minutes = divmod(minutes, 60)
            verb = "expires" if state.horizon == pool.expires_at else "resets"
            relative = f"{verb} in {days}d {hours}h {minutes}m"
        rows.append(
            {
                "account": pool.provider.split(":", 2)[1],
                "pool_key": pool.provider,
                "credit_left_pct": None
                if state.fraction is None
                else max(0.0, min(100.0, (1 - state.fraction) * 100)),
                "window_left_pct": window_left,
                "resets_at": _timestamp(pool.resets_at),
                "expires_at": _timestamp(pool.expires_at),
                "relative": relative,
                "stale": pool.stale,
                "pacing_state": state.state,
                "why": reasons[state.state],
                "retry_at": _timestamp(state.retry_at),
            }
        )
    return rows
