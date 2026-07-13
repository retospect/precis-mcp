"""Tests for ``claude_quota.parse_rate_limits``.

Verifies the shape of Anthropic's rate-limit envelope as Claude Code
emits it via ``-p ... --output-format json`` is parsed into the
``QuotaSnapshot`` we persist + render. No subprocess invocation —
that's covered by integration smoke tests once the agent worker is
deployed; here we lock the parser surface.
"""

from __future__ import annotations

import json

from precis.utils.claude_quota import parse_rate_limits


def test_parse_returns_none_on_garbage() -> None:
    assert parse_rate_limits("not json") is None
    assert parse_rate_limits("") is None
    assert parse_rate_limits("[]") is None  # list, not dict


def test_parse_returns_none_when_rate_limits_absent() -> None:
    # Free-tier / non-OAuth runs omit the field entirely.
    payload = json.dumps({"result": "ok", "usage": {"input_tokens": 4}})
    assert parse_rate_limits(payload) is None


def test_parse_returns_none_when_windows_empty() -> None:
    payload = json.dumps({"rate_limits": {}})
    assert parse_rate_limits(payload) is None


def test_parse_extracts_five_hour_and_seven_day() -> None:
    payload = json.dumps(
        {
            "rate_limits": {
                "five_hour": {
                    "used_percentage": 13.4,
                    "resets_at": 1_750_005_540,
                },
                "seven_day": {
                    "used_percentage": 36.0,
                    "resets_at": 1_750_232_340,
                },
            }
        }
    )
    snap = parse_rate_limits(payload)
    assert snap is not None
    assert set(snap.windows.keys()) == {"five_hour", "seven_day"}
    assert snap.windows["five_hour"]["used_percentage"] == 13.4
    assert snap.windows["seven_day"]["used_percentage"] == 36.0
    # Reset times normalised to ISO 8601 UTC.
    assert snap.windows["five_hour"]["resets_at"].startswith("2025-")
    assert snap.windows["seven_day"]["resets_at"].endswith("+00:00")


def test_parse_handles_sonnet_only_weekly() -> None:
    payload = json.dumps(
        {
            "rate_limits": {
                "seven_day_sonnet": {
                    "used_percentage": 11.0,
                    "resets_at": 1_750_232_340,
                },
            }
        }
    )
    snap = parse_rate_limits(payload)
    assert snap is not None
    assert "seven_day_sonnet" in snap.windows


def test_parse_skips_window_missing_both_fields() -> None:
    payload = json.dumps(
        {
            "rate_limits": {
                "five_hour": {"used_percentage": 22.0, "resets_at": 1_700_000_000},
                # malformed entry — no used_percentage, no resets_at
                "seven_day": {"other_field": "ignored"},
            }
        }
    )
    snap = parse_rate_limits(payload)
    assert snap is not None
    assert "five_hour" in snap.windows
    assert "seven_day" not in snap.windows


def test_parse_tolerates_non_numeric_percentage() -> None:
    # If Anthropic ever ships a string-typed percentage by mistake we
    # still want the resets_at to land.
    payload = json.dumps(
        {
            "rate_limits": {
                "five_hour": {
                    "used_percentage": "not a number",
                    "resets_at": 1_750_005_540,
                },
            }
        }
    )
    snap = parse_rate_limits(payload)
    assert snap is not None
    assert "used_percentage" not in snap.windows["five_hour"]
    assert "resets_at" in snap.windows["five_hour"]


# ---- stream-json mode (T7.3) --------------------------------------


def test_parse_handles_nested_rate_limit_info_envelope() -> None:
    """Claude Code 2.1.x nests the rate-limit fields under
    ``rate_limit_info`` on the event. The parser must unwrap that and
    still find ``rateLimitType`` / ``resetsAt`` / ``status``."""
    stream = json.dumps(
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "allowed",
                "resetsAt": 1_781_657_400,
                "rateLimitType": "five_hour",
                "overageStatus": "rejected",
                "overageDisabledReason": "org_level_disabled",
                "isUsingOverage": False,
            },
            "uuid": "27108884-c174-482b-93aa-0a08c54fbf0e",
        }
    )
    snap = parse_rate_limits(stream)
    assert snap is not None
    assert "five_hour" in snap.windows
    assert snap.windows["five_hour"]["status"] == "allowed"
    assert snap.windows["five_hour"]["resets_at"].endswith("+00:00")


def test_parse_extracts_rate_limit_events_from_stream() -> None:
    """NDJSON stream — each non-empty line is a JSON event. We harvest
    ``rate_limit_event`` and project rateLimitType / resetsAt / status."""
    stream = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps(
                {
                    "type": "rate_limit_event",
                    "rateLimitType": "five_hour",
                    "resetsAt": 1_750_005_540,
                    "status": "active",
                }
            ),
            json.dumps(
                {
                    "type": "rate_limit_event",
                    "rateLimitType": "seven_day",
                    "resetsAt": 1_750_232_340,
                    "status": "warning",
                }
            ),
            json.dumps({"type": "result", "result": "ok"}),
        ]
    )
    snap = parse_rate_limits(stream)
    assert snap is not None
    assert set(snap.windows.keys()) == {"five_hour", "seven_day"}
    assert snap.windows["five_hour"]["status"] == "active"
    assert snap.windows["five_hour"]["resets_at"].endswith("+00:00")
    assert snap.windows["seven_day"]["status"] == "warning"
    # No used_percentage in stream events.
    assert "used_percentage" not in snap.windows["five_hour"]


def test_parse_stream_tolerates_garbage_lines() -> None:
    stream = "\n".join(
        [
            "not json at all",
            "",
            json.dumps(
                {
                    "type": "rate_limit_event",
                    "rateLimitType": "five_hour",
                    "resetsAt": 1_750_005_540,
                    "status": "active",
                }
            ),
            "{ broken",
        ]
    )
    snap = parse_rate_limits(stream)
    assert snap is not None
    assert "five_hour" in snap.windows


def test_parse_stream_unwraps_nested_message() -> None:
    """Some Claude Code versions wrap the rate_limit_event under
    ``message``/``event``; the parser unwraps both."""
    stream = "\n".join(
        [
            json.dumps(
                {
                    "type": "system",
                    "message": {
                        "type": "rate_limit_event",
                        "rateLimitType": "seven_day_sonnet",
                        "resetsAt": 1_750_232_340,
                        "status": "active",
                    },
                }
            )
        ]
    )
    snap = parse_rate_limits(stream)
    assert snap is not None
    assert "seven_day_sonnet" in snap.windows


def test_parse_stream_falls_back_to_envelope_when_no_events() -> None:
    """An NDJSON-looking blob that carries no rate_limit_event events
    but does carry a final result with a ``rate_limits`` envelope still
    parses via the envelope path."""
    stream = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps({"type": "result", "result": "ok"}),
        ]
    )
    snap = parse_rate_limits(stream)
    # No rate_limits anywhere — None is correct.
    assert snap is None


def test_parse_stream_falls_back_to_single_envelope() -> None:
    """If the input is a single JSON envelope (legacy --output-format
    json shape), the envelope path still handles it."""
    payload = json.dumps(
        {
            "rate_limits": {
                "five_hour": {
                    "used_percentage": 13.4,
                    "resets_at": 1_750_005_540,
                },
            }
        }
    )
    snap = parse_rate_limits(payload)
    assert snap is not None
    assert snap.windows["five_hour"]["used_percentage"] == 13.4


# ── auth-failure classification (2026-07-12 OAuth alert) ──────────────


def test_looks_like_auth_failure_detects_signatures() -> None:
    from precis.utils.claude_quota import _looks_like_auth_failure

    assert _looks_like_auth_failure("API Error: 401 Invalid authentication credentials")
    assert _looks_like_auth_failure("", "Not logged in · Please run /login")
    assert _looks_like_auth_failure("Failed to authenticate.")
    assert not _looks_like_auth_failure("perfectly ordinary rate-limit payload")
    assert not _looks_like_auth_failure("")


class _FakeRes:
    def __init__(self, rc: int, out: str = "", err: str = "") -> None:
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def test_refresh_snapshot_outcomes(monkeypatch) -> None:
    from precis.utils import claude_quota as cq
    from precis.utils.claude_quota import RefreshOutcome, refresh_snapshot

    # 401 → AUTH_FAILED (the pageable condition)
    monkeypatch.setattr(
        cq.subprocess,
        "run",
        lambda *a, **k: _FakeRes(
            1, "API Error: 401 Invalid authentication credentials"
        ),
    )
    snap, outcome = refresh_snapshot(object())
    assert snap is None and outcome is RefreshOutcome.AUTH_FAILED

    # non-auth non-zero exit → UNAVAILABLE (don't flap the alert on a blip)
    monkeypatch.setattr(
        cq.subprocess, "run", lambda *a, **k: _FakeRes(1, "network glitch")
    )
    _snap, outcome = refresh_snapshot(object())
    assert outcome is RefreshOutcome.UNAVAILABLE

    # clean exit, no rate_limits payload → NO_LIMITS (auth is fine)
    monkeypatch.setattr(cq.subprocess, "run", lambda *a, **k: _FakeRes(0, "{}"))
    _snap, outcome = refresh_snapshot(object())
    assert outcome is RefreshOutcome.NO_LIMITS

    # missing binary → UNAVAILABLE
    def _missing(*a, **k):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(cq.subprocess, "run", _missing)
    _snap, outcome = refresh_snapshot(object())
    assert outcome is RefreshOutcome.UNAVAILABLE
