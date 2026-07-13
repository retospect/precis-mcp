"""Tests for the quota_check pass's claude-auth alert side channel.

The numbers-parsing lives in ``test_claude_quota``; here we pin the
2026-07-12 addition: a genuine ``claude -p`` auth failure raises a
critical ``quota_check:auth`` alert (so a revoked OAuth token pages
instead of failing silently), and auth recovering resolves it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from precis.alerts import list_open_alerts
from precis.store import Store
from precis.utils.claude_quota import QuotaSnapshot, RefreshOutcome
from precis.workers import quota_check as qc


def _auth_alerts(store: Store) -> list[dict]:
    return [a for a in list_open_alerts(store) if a["source"] == "quota_check:auth"]


def test_auth_failure_raises_then_resolves(store: Store, monkeypatch) -> None:
    monkeypatch.setenv("PRECIS_HOST_NAME", "testhost")

    # 1) claude -p 401s → a critical auth alert appears, named for the host.
    monkeypatch.setattr(
        qc, "refresh_snapshot", lambda s: (None, RefreshOutcome.AUTH_FAILED)
    )
    qc.run_quota_check_pass(store)
    open_now = _auth_alerts(store)
    assert len(open_now) == 1
    assert open_now[0]["severity"] == "critical"
    assert "testhost" in open_now[0]["title"]

    # A second failing pass dedups (still exactly one open alert).
    qc.run_quota_check_pass(store)
    assert len(_auth_alerts(store)) == 1

    # 2) auth recovers → the alert resolves (clears from the open list).
    snap = QuotaSnapshot(
        ts=datetime.now(UTC),
        windows={"five_hour": {"used_percentage": 1.0}},
        representative_claim=None,
    )
    monkeypatch.setattr(qc, "refresh_snapshot", lambda s: (snap, RefreshOutcome.OK))
    qc.run_quota_check_pass(store)
    assert _auth_alerts(store) == []


def test_unavailable_does_not_touch_alert(store: Store, monkeypatch) -> None:
    # A transient/unknown blip (binary missing, timeout) must neither raise
    # nor clear — it's not evidence either way about auth.
    monkeypatch.setenv("PRECIS_HOST_NAME", "testhost")
    monkeypatch.setattr(
        qc, "refresh_snapshot", lambda s: (None, RefreshOutcome.AUTH_FAILED)
    )
    qc.run_quota_check_pass(store)
    assert len(_auth_alerts(store)) == 1

    # Now a blip — the standing auth alert must survive it.
    monkeypatch.setattr(
        qc, "refresh_snapshot", lambda s: (None, RefreshOutcome.UNAVAILABLE)
    )
    qc.run_quota_check_pass(store)
    assert len(_auth_alerts(store)) == 1
