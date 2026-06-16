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
