"""Refresh + parse the Claude.ai OAuth quota snapshot.

Invokes ``claude -p "quota" --max-turns 0 --output-format json``, parses
``rate_limits.{five_hour,seven_day}.{used_percentage,resets_at}`` out of
the JSON, and persists to ``claude_quota_snapshot`` (scope='unified').

The 1-token call is cheap; the value is the headers Anthropic returns
in the response. Claude Code does the same trick under its ``/usage``
slash command (binary strings show a dedicated ``WkO`` function with
``source: "quota_check"``).

Failure modes are non-fatal: if the binary isn't installed, the OAuth
state is unreachable, or the JSON shape changed, the helper logs and
returns ``None`` so the cascade keeps running. The Status panel falls
back to "snapshot unavailable" when nothing's been written yet.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)


#: Scope key used by the singleton row. Future multi-OAuth deployments
#: would add scopes like ``hermes`` / ``deploy``.
DEFAULT_SCOPE = "unified"

#: Seconds between forced quota refreshes. The dispatcher's check
#: short-circuits when the snapshot's ``ts`` is younger than this.
REFRESH_INTERVAL_S = 600  # 10 min


@dataclass(frozen=True, slots=True)
class QuotaSnapshot:
    """Parsed view of the Anthropic rate-limit headers.

    ``windows`` keys: ``"five_hour"`` (always present when the account
    is on a Pro/Max plan), ``"seven_day"`` (weekly all-models),
    ``"seven_day_sonnet"`` (weekly Sonnet only — present only when
    that's the binding window), ``"overage"`` (pay-per-use overage —
    present only when enabled).

    Each window's value is ``{"used_percentage": float, "resets_at":
    iso8601_str}``. Reset times are normalised to UTC ISO 8601 here;
    rendering does the tz translation.
    """

    ts: datetime
    windows: dict[str, dict[str, Any]]
    representative_claim: str | None


def parse_rate_limits(stdout_json: str) -> QuotaSnapshot | None:
    """Extract rate_limits from a ``claude -p --output-format json`` stdout.

    Returns ``None`` when the JSON is unparseable, the ``rate_limits``
    key is absent (free-tier / non-OAuth runs), or every window is
    null. Callers can treat ``None`` as "no snapshot available right
    now"; the table isn't written.
    """
    try:
        envelope = json.loads(stdout_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(envelope, dict):
        return None
    rl = envelope.get("rate_limits")
    if not isinstance(rl, dict) or not rl:
        return None

    windows: dict[str, dict[str, Any]] = {}
    for key in (
        "five_hour",
        "seven_day",
        "seven_day_sonnet",
        "seven_day_opus",
        "overage",
    ):
        bucket = rl.get(key)
        if not isinstance(bucket, dict):
            continue
        used = bucket.get("used_percentage")
        resets = bucket.get("resets_at")
        if used is None and resets is None:
            continue
        entry: dict[str, Any] = {}
        if used is not None:
            try:
                entry["used_percentage"] = float(used)
            except (TypeError, ValueError):
                pass
        if resets is not None:
            try:
                # Anthropic emits unix epoch seconds; normalise to ISO.
                entry["resets_at"] = datetime.fromtimestamp(
                    float(resets), tz=UTC
                ).isoformat()
            except (TypeError, ValueError, OSError):
                pass
        if entry:
            windows[key] = entry

    if not windows:
        return None

    representative = None
    rep = envelope.get("rate_limit_status") or rl.get("representative_claim")
    if isinstance(rep, str):
        representative = rep

    return QuotaSnapshot(
        ts=datetime.now(UTC),
        windows=windows,
        representative_claim=representative,
    )


def refresh_snapshot(
    store: Any,
    *,
    binary: str | None = None,
    timeout_s: float = 30.0,
) -> QuotaSnapshot | None:
    """Shell out to ``claude -p "quota" ...`` and persist the snapshot.

    Cost: one 1-token completion. The binding utilisation figure is
    a side-effect of the response headers; the prompt content doesn't
    matter so long as the API actually returns headers (free-tier
    accounts return nothing — we no-op gracefully).
    """
    cmd_binary = binary or os.environ.get("PRECIS_CLAUDE_BIN", "claude")
    try:
        res = subprocess.run(
            [
                cmd_binary,
                "-p",
                "quota",
                "--max-turns",
                "0",
                "--output-format",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        log.warning("claude_quota: refresh failed (%s)", exc)
        return None

    if res.returncode != 0:
        log.warning(
            "claude_quota: claude -p exited %d (stderr=%s)",
            res.returncode,
            (res.stderr or "").strip()[:200],
        )
        return None

    snapshot = parse_rate_limits(res.stdout)
    if snapshot is None:
        log.info("claude_quota: response had no rate_limits payload")
        return None

    try:
        store.record_claude_quota(
            scope=DEFAULT_SCOPE,
            data={
                "windows": snapshot.windows,
                "representative_claim": snapshot.representative_claim,
            },
        )
    except Exception:
        log.exception("claude_quota: persist failed")
        return None

    return snapshot


__all__ = [
    "DEFAULT_SCOPE",
    "REFRESH_INTERVAL_S",
    "QuotaSnapshot",
    "parse_rate_limits",
    "refresh_snapshot",
]
