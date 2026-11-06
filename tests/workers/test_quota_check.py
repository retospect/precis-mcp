"""Tests for the quota_check pass's claude-auth alert side channel.

The numbers-parsing lives in ``test_claude_quota``; here we pin the
2026-07-12 addition: a genuine ``claude -p`` auth failure raises a
critical ``quota_check:auth`` alert (so a revoked OAuth token pages
instead of failing silently), and auth recovering resolves it.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from precis.alerts import list_open_alerts
from precis.store import Store
from precis.utils.claude_quota import DEFAULT_SCOPE, QuotaSnapshot, RefreshOutcome
from precis.workers import quota_check as qc
from tests.test_llm_accounts import restore_seeded_pools  # noqa: F401  fixture


def _auth_alerts(store: Store) -> list[dict]:
    return [a for a in list_open_alerts(store) if a["source"] == "quota_check:auth"]


@pytest.mark.usefixtures("restore_seeded_pools")
def test_auth_failure_raises_then_resolves(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_unavailable_does_not_touch_alert(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
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


@pytest.mark.usefixtures("restore_seeded_pools")
def test_probe_reports_both_pools(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.utils.llm.accounts import list_pools

    snap = QuotaSnapshot(
        ts=datetime.now(UTC),
        windows={
            "five_hour": {"used_percentage": 55.0, "resets_at": "2026-11-07T00:00:00Z"},
            "seven_day": {"used_percentage": 50.0, "resets_at": "2026-11-10T00:00:00Z"},
            "seven_day_sonnet": {"used_percentage": 12.0},
        },
        representative_claim=None,
    )

    def refresh(target: Store) -> tuple[QuotaSnapshot, RefreshOutcome]:
        return snap, RefreshOutcome.OK

    monkeypatch.setattr(qc, "refresh_snapshot", refresh)
    result = qc.run_quota_check_pass(store)
    assert result.ok == 1
    pools = {pool.provider: pool for pool in list_pools(store)}
    assert pools["llm:claude-sub:5h"].used_reported == 55.0
    assert pools["llm:claude-sub:7d"].used_reported == 50.0
    assert pools["llm:claude-sub:5h"].remaining_reported == 45.0
    assert pools["llm:claude-sub:7d"].reported_at == snap.ts
    assert pools["llm:claude-sub:7d"].source == "provider"


@pytest.mark.usefixtures("restore_seeded_pools")
def test_pool_report_failure_does_not_fail_the_pass(
    store: Store, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The pool ledger is a side channel: ``refresh_snapshot`` has already
    stored the snapshot when the pool leg runs, so a failing pool write must
    neither raise out of the pass nor cost the pass its ok."""
    from precis.utils import claude_quota as cq
    from precis.utils.llm import accounts

    stream = json.dumps(
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "allowed",
                "resetsAt": 1791915000,
                "rateLimitType": "five_hour",
                "unifiedWindows": {
                    "five_hour": {"utilization": 0.55, "resetsAt": 1791915000},
                    "seven_day": {"utilization": 0.5, "resetsAt": 1792126800},
                },
            },
        }
    )
    monkeypatch.setattr(
        cq,
        "run_claude",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=stream, stderr=""),
    )

    def boom(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("pool ledger down")

    monkeypatch.setattr(accounts, "record_pool_report", boom)
    with caplog.at_level(logging.WARNING, logger=qc.log.name):
        result = qc.run_quota_check_pass(store)

    assert (result.claimed, result.ok, result.failed) == (1, 1, 0)
    row = store.read_claude_quota(scope=DEFAULT_SCOPE)
    assert row is not None
    assert row.data["windows"]["five_hour"]["used_percentage"] == 55.0
    assert row.data["windows"]["seven_day"]["used_percentage"] == 50.0
    assert "quota_check: pool report failed" in caplog.text
