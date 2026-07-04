"""Unit tests for the fixer persistent per-tick log line (ADR 0048).

``format_log_line`` is the pure floor of the durable record — a single
greppable line per tick appended to ``<work_dir>/fixer.log``. The richer
``agentlog`` record stays deferred; this is the cheap file-log floor.
"""

from __future__ import annotations

from precis.fixer.tick import format_log_line


def test_format_log_line_carries_fields() -> None:
    line = format_log_line(
        "2026-07-04T12:00:00Z", "ok", "do-the-thing", "fix/do-the-thing", "all green"
    )
    assert "do-the-thing" in line
    assert "ok" in line
    assert "fix/do-the-thing" in line
    assert "2026-07-04T12:00:00Z" in line


def test_format_log_line_is_single_line_for_multiline_detail() -> None:
    detail = "gate failed: ruff\nline two\nline three"
    line = format_log_line(
        "2026-07-04T12:00:00Z", "needs_you", "slug", "fix/slug", detail
    )
    assert "\n" not in line
    # Only the first line of detail survives.
    assert "gate failed: ruff" in line
    assert "line two" not in line
