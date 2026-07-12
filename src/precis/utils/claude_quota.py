"""Refresh + parse the Claude.ai OAuth quota snapshot.

Invokes ``claude -p "quota" --max-turns 0 --output-format stream-json
--verbose`` and harvests the ``rate_limit_event`` events the CLI emits
during the call (one per rate-limit window the headers carry). Each
event has the shape::

    {"type": "rate_limit_event",
     "rateLimitType": "five_hour" | "seven_day" | ...,
     "resetsAt": 1750005540,
     "status": "active" | "warning" | "exceeded"}

The events carry **no** ``used_percentage`` — that field is only
returned to ``--output-format json`` consumers and that mode is what
the original implementation used. As of 2026-06 Claude Code emits the
percentage only via the stream-json variant for the per-window status;
the JSON variant returns a final envelope with no ``rate_limits`` for
many account configurations. The parser therefore accepts **either**
input shape and falls back gracefully when the shape changes again.

Persists to ``claude_quota_snapshot`` (scope='unified'). The 1-token
call is cheap; the value is the headers Anthropic returns. Claude Code
does the same trick under its ``/usage`` slash command (binary strings
show a dedicated ``WkO`` function with ``source: "quota_check"``).

Failure modes are non-fatal: if the binary isn't installed, the OAuth
state is unreachable, or the shape changed, the helper logs and
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

from precis.utils.claude_oauth import ensure_oauth_token

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


#: Window keys we render in fixed order; everything else lands at the
#: bottom alphabetically. Status panel reuses this for sort + label.
_KNOWN_WINDOWS: tuple[str, ...] = (
    "five_hour",
    "seven_day",
    "seven_day_sonnet",
    "seven_day_opus",
    "overage",
)


def _normalise_resets(value: Any) -> str | None:
    """Coerce a unix-epoch (int|float|str) into UTC ISO 8601."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _parse_envelope(envelope: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Pull rate_limits out of an ``--output-format json`` envelope.

    Returns an empty dict when the envelope shape is unfamiliar. Used
    as a fallback when the input wasn't NDJSON (legacy single-blob
    JSON output, or older Claude Code versions).
    """
    rl = envelope.get("rate_limits")
    if not isinstance(rl, dict) or not rl:
        return {}
    windows: dict[str, dict[str, Any]] = {}
    for key in _KNOWN_WINDOWS:
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
        iso = _normalise_resets(resets)
        if iso is not None:
            entry["resets_at"] = iso
        if entry:
            windows[key] = entry
    return windows


def _parse_stream_events(text: str) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Walk NDJSON Claude Code stream-json output.

    Yields one dict per event; we keep ``rate_limit_event`` entries and
    fold them into a windows dict. Also returns the binding-window
    name when a ``rate_limit_status`` field surfaces on the trailing
    result envelope (some Claude Code versions tuck it there).
    """
    windows: dict[str, dict[str, Any]] = {}
    representative: str | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        # The CLI nests the actual Anthropic event under "message" for
        # some types; we tolerate either nesting.
        candidate = ev
        if ev.get("type") in {"system", "result", "assistant"}:
            inner = ev.get("message") or ev.get("event")
            if isinstance(inner, dict):
                candidate = inner
        if candidate.get("type") == "rate_limit_event":
            # As of Claude Code 2.1.x the per-window fields are nested
            # under ``rate_limit_info`` on the event:
            #   {"type":"rate_limit_event",
            #    "rate_limit_info":{
            #       "status":"allowed", "resetsAt":..., "rateLimitType":"...",
            #       "overageStatus":..., ...}}
            # Older versions had the fields flat on the event. Look in
            # the nested envelope first; fall back to flat for forward
            # compat with both shapes.
            info = candidate.get("rate_limit_info")
            if not isinstance(info, dict):
                info = candidate
            key = info.get("rateLimitType") or info.get("rate_limit_type")
            if not isinstance(key, str):
                continue
            entry: dict[str, Any] = windows.setdefault(key, {})
            iso = _normalise_resets(info.get("resetsAt") or info.get("resets_at"))
            if iso is not None:
                entry["resets_at"] = iso
            status = info.get("status")
            if isinstance(status, str):
                entry["status"] = status
            used = info.get("used_percentage")
            if used is not None:
                try:
                    entry["used_percentage"] = float(used)
                except (TypeError, ValueError):
                    pass
        if isinstance(ev.get("rate_limit_status"), str):
            representative = ev["rate_limit_status"]
    return windows, representative


def parse_rate_limits(stdout: str) -> QuotaSnapshot | None:
    """Extract rate-limit windows from ``claude -p`` stdout.

    Accepts two shapes:

    * ``--output-format stream-json --verbose`` — NDJSON, one event per
      line. The parser walks ``rate_limit_event`` events.
    * ``--output-format json`` — single JSON object with an optional
      ``rate_limits`` map (legacy shape).

    Returns ``None`` when the input is unparseable, both shapes turn
    up empty, or every window is null.
    """
    if not stdout or not stdout.strip():
        return None

    # Always try the NDJSON walk first — bad lines are skipped silently,
    # so this is safe to run on single-envelope JSON too (it just won't
    # find any rate_limit_event entries and we fall back below).
    stripped = stdout.strip()
    windows, representative = _parse_stream_events(stdout)

    if not windows:
        # No stream events found. Try the single-envelope shape — the
        # legacy --output-format json output, or some future shape that
        # tucks rate_limits onto the trailing result envelope.
        try:
            envelope = json.loads(stripped)
        except (json.JSONDecodeError, TypeError):
            envelope = None
        if isinstance(envelope, dict):
            windows = _parse_envelope(envelope)
            rep = envelope.get("rate_limit_status")
            if isinstance(rep, str):
                representative = rep
            elif isinstance(envelope.get("rate_limits"), dict):
                inner_rep = envelope["rate_limits"].get("representative_claim")
                if isinstance(inner_rep, str):
                    representative = inner_rep

    if not windows:
        return None

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
    # Bootstrap the OAuth token from ~/.claude_oauth_token so a launchd
    # daemon's ``claude -p`` doesn't fall back to stale keychain creds and
    # 401 (2026-07-12 incident — same fix as plan_tick / claude_agent).
    quota_env = dict(os.environ)
    ensure_oauth_token(quota_env)
    try:
        res = subprocess.run(
            [
                cmd_binary,
                "-p",
                "quota",
                "--max-turns",
                "0",
                "--output-format",
                "stream-json",
                "--verbose",
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=quota_env,
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
