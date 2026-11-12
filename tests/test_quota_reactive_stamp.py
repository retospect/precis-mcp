"""Reactive claude quota snapshot (docs/backlog/llm-quota-failure-classification.md).

A live ``quota``-class 429 stamps ``claude_quota_snapshot`` at once; the
gate pauses the next claude call without a subprocess; one probe is
scheduled per exhausted window; a reset in the past clears the pause.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import Mock

import pytest

from precis.budget import meter, quota
from precis.store._claude_quota_ops import ClaudeQuotaRow
from precis.utils.llm import router
from precis.utils.llm.router import LlmRequest, LlmResult, Tier, Transport, route
from tests.test_budget import SqlStore

NOW = datetime.now(UTC).replace(microsecond=0)
# Import-time constants can be hours stale under xdist; keep RESET far enough
# ahead that real-clock checks (evaluate/route) still see an open window.
RESET = NOW + timedelta(days=1)
NOTICE = "You've hit your session limit · resets 9pm (UTC)"


class SnapshotStore(SqlStore):
    """``SqlStore`` plus a writable snapshot — what the reactive stamp needs."""

    def __init__(self, windows: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.data: dict[str, Any] | None = {"windows": windows} if windows else None
        self.writes = 0
        # Snapshot age is measured against the real clock, and NOW is fixed
        # at import: under xdist a test can run long after import, so stamp
        # the snapshot when the store is built.
        self.ts = datetime.now(UTC).replace(microsecond=0)

    def read_claude_quota(self, scope: str = "unified") -> object:
        if self.data is None:
            return None
        return ClaudeQuotaRow(scope=scope, ts=self.ts, data=dict(self.data))

    def record_claude_quota(
        self, *, scope: str, data: dict[str, Any], conn=None
    ) -> None:
        self.writes += 1
        self.data = dict(data)


def test_stamp_writes_exceeded_window_and_one_probe() -> None:
    store = SnapshotStore({"seven_day": {"status": "allowed"}})
    assert quota.stamp_exhausted(
        cast("Any", store), resets_at=RESET, notice=NOTICE, now=NOW
    )
    assert store.data is not None
    win = store.data["windows"]
    assert win["seven_day"] == {"status": "allowed"}  # untouched
    assert win["five_hour"]["status"] == "exceeded"
    assert win["five_hour"]["resets_at"] == RESET.isoformat()
    due = datetime.fromisoformat(store.data["probe_due"])
    assert RESET < due <= RESET + timedelta(seconds=180)
    # The gate now pauses on the stamp alone.
    pause = quota.evaluate(cast("Any", store))
    assert pause is not None and pause.window == "five_hour"
    # A second notice for the same window + reset (another parked job) is a no-op.
    assert not quota.stamp_exhausted(
        cast("Any", store), resets_at=RESET, notice=NOTICE, now=NOW
    )
    assert store.writes == 1
    assert not quota.probe_due(cast("Any", store), now=NOW)
    assert quota.probe_due(cast("Any", store), now=RESET + timedelta(minutes=5))


def test_stamp_maps_notice_wording_to_window() -> None:
    assert (
        quota.window_for_notice("You've hit your weekly limit · resets 9am")
        == "seven_day"
    )
    assert (
        quota.window_for_notice("You're out of extra usage · resets 3pm") == "overage"
    )
    assert quota.window_for_notice("Claude AI usage limit reached|123") == "five_hour"
    assert quota.window_for_notice(None) == "five_hour"


def test_stamp_is_dark_without_store_and_defaults_the_horizon() -> None:
    assert not quota.stamp_exhausted(None, resets_at=None)
    store = SnapshotStore()
    assert quota.stamp_exhausted(cast("Any", store), resets_at=None, now=NOW)
    assert store.data is not None
    assert (
        store.data["windows"]["five_hour"]["resets_at"]
        == (NOW + timedelta(hours=2)).isoformat()
    )


def test_gate_ignores_a_window_whose_reset_has_passed() -> None:
    store = SqlStore(
        windows={
            "five_hour": {
                "status": "exceeded",
                "resets_at": (NOW - timedelta(minutes=1)).isoformat(),
            }
        }
    )
    assert quota.evaluate(cast("Any", store)) is None
    store = SqlStore(
        windows={"five_hour": {"status": "exceeded", "resets_at": RESET.isoformat()}}
    )
    assert quota.evaluate(cast("Any", store)) is not None


class _QuotaProv:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, req: LlmRequest, *, model: str) -> LlmResult:
        self.calls += 1
        return LlmResult(
            text=NOTICE,
            cost_usd=0.0,
            turns_used=None,
            model=model,
            tier=req.tier,
            error=f"account quota exhausted: {NOTICE}",
            paused=True,
            quota_exhausted=True,
            reason_class="quota",
            retry_at=RESET,
        )


def test_route_quota_result_stamps_snapshot_and_next_call_pauses_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SnapshotStore()
    meter.bind_store(cast("Any", store))
    try:
        prov = _QuotaProv()
        monkeypatch.setitem(router._PROVIDERS, Transport.CLAUDE_P, prov)
        monkeypatch.delenv("PRECIS_LLM_BACKEND", raising=False)
        monkeypatch.delenv("PRECIS_LLM_FAILOVER", raising=False)
        first = route(LlmRequest(tier=Tier.BIG, prompt="x"))
        assert first.reason_class == "quota" and prov.calls == 1
        assert store.data is not None and store.writes == 1
        assert store.data["windows"]["five_hour"]["status"] == "exceeded"

        second = route(LlmRequest(tier=Tier.BIG, prompt="y"))
        assert second.paused and second.reason_class == "quota"
        assert "quota" in (second.error or "")
        assert prov.calls == 1  # the gate answered; no provider run
    finally:
        meter.bind_store(None)


def test_bare_rung_quota_text_does_not_stamp(monkeypatch: pytest.MonkeyPatch) -> None:
    store = SnapshotStore()
    calls: list[Any] = []
    monkeypatch.setattr(quota, "stamp_exhausted", lambda *a, **k: calls.append(k))
    res = _QuotaProv().run(LlmRequest(tier=Tier.BIG, prompt="x"), model="m")
    router._react_to_quota(res, transport=Transport.CLAUDE_P, bare=True)
    router._react_to_quota(res, transport=Transport.OPENAI_COMPAT, bare=False)
    assert calls == [] and store.writes == 0
    meter.bind_store(cast("Any", store))
    try:
        router._react_to_quota(res, transport=Transport.CLAUDE_AGENT, bare=False)
    finally:
        meter.bind_store(None)
    assert len(calls) == 1 and calls[0]["resets_at"] == RESET


def test_quota_check_refreshes_early_when_probe_is_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.utils.claude_quota import RefreshOutcome
    from precis.workers import quota_check as qc

    store = SnapshotStore({"five_hour": {"status": "allowed"}})
    probes: list[int] = []

    def _refresh(s: Any) -> tuple[None, RefreshOutcome]:
        probes.append(1)
        return None, RefreshOutcome.UNAVAILABLE

    monkeypatch.setattr(qc, "refresh_snapshot", _refresh)
    monkeypatch.setattr(qc, "_resolve_host_name", lambda: "t")
    # Fresh snapshot (ts = build time), no stamp → cadence short-circuits.
    qc.run_quota_check_pass(cast("Any", store))
    assert probes == []
    # A stamp whose probe instant has passed forces the refresh.
    assert store.data is not None
    store.data["probe_due"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    qc.run_quota_check_pass(cast("Any", store))
    assert probes == [1]


# ── gr635459: a quota-worded refusal with no resolvable OAuth token is a ──
# ── key-management failure — alert as auth, never stamp the snapshot. ─────


def test_react_to_quota_without_a_token_alerts_auth_not_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 2026-11-11 incident: workers spawned claude without the vault
    token, the CLI answered off fallback credentials with quota wording,
    and the reactive stamp paged "[budget] claude subscription quota
    reached (seven_day)" — a phantom window. Tokenless, the refusal must
    NOT stamp (the breaker pages off that stamp); the claude-auth alert
    fires instead."""
    store = SnapshotStore()
    stamps: list[Any] = []
    alerts: list[tuple[Any, str]] = []
    monkeypatch.setattr(quota, "stamp_exhausted", lambda *a, **k: stamps.append(k))
    monkeypatch.setattr("precis.utils.claude_oauth.token_resolvable", lambda: False)
    monkeypatch.setattr(
        "precis.utils.claude_quota.raise_auth_alert",
        lambda s, *, host: alerts.append((s, host)),
    )
    monkeypatch.setattr(
        "precis.utils.db_log_handler._resolve_host_name", lambda: "testhost"
    )
    meter.bind_store(cast("Any", store))
    try:
        router._react_to_quota(
            _QuotaProv().run(LlmRequest(tier=Tier.BIG, prompt="x"), model="m"),
            transport=Transport.CLAUDE_AGENT,
            bare=False,
        )
    finally:
        meter.bind_store(None)
    assert stamps == []
    assert store.writes == 0  # no snapshot row for the gate to pause on
    assert [(s is store, host) for s, host in alerts] == [(True, "testhost")]


def test_react_to_quota_with_a_token_stamps_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard's other side: a token on the host means the quota wording
    IS the subscription's window — the stamp lands and no auth alert
    fires."""
    store = SnapshotStore()
    stamps: list[Any] = []
    alerts: list[Any] = []
    monkeypatch.setattr(quota, "stamp_exhausted", lambda *a, **k: stamps.append(k))
    monkeypatch.setattr("precis.utils.claude_oauth.token_resolvable", lambda: True)
    monkeypatch.setattr(
        "precis.utils.claude_quota.raise_auth_alert",
        lambda *a, **k: alerts.append(k),
    )
    meter.bind_store(cast("Any", store))
    try:
        router._react_to_quota(
            _QuotaProv().run(LlmRequest(tier=Tier.BIG, prompt="x"), model="m"),
            transport=Transport.CLAUDE_AGENT,
            bare=False,
        )
    finally:
        meter.bind_store(None)
    assert len(stamps) == 1 and stamps[0]["resets_at"] == RESET
    assert alerts == []


def test_pool_park_skipped_when_no_token_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pool ledger reads the same guard: a stream-less quota refusal
    parks the claude-sub pool its wording names — unless no OAuth token
    resolves here, in which case the wording is a fallback credential's
    window, not the pool's."""
    from precis.utils.llm import accounts

    parks: list[Any] = []
    store = Mock()
    monkeypatch.setattr("precis.budget.meter.active_store", lambda: store)
    monkeypatch.setattr(
        accounts, "record_pool_failure", lambda *a, **k: parks.append(k)
    )
    monkeypatch.setattr("precis.utils.claude_oauth.token_resolvable", lambda: False)
    refusal = LlmResult(
        text="",
        cost_usd=None,
        turns_used=None,
        model="m",
        tier=Tier.BIG,
        error="account quota exhausted: You've hit your weekly limit · resets 9am",
        paused=True,
        quota_exhausted=True,
        reason_class="quota",
        retry_at=RESET,
    )
    router._harvest_pool_signals(refusal, transport=Transport.CLAUDE_P, bare=False)
    assert parks == []


def test_pool_stream_writes_skipped_when_no_token_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same guard covers the stream path: a ``claude_agent`` refusal's
    stream reports windows, and the harvest used to write them
    (``record_pool_report``) and park the stream-marked pool
    (``record_pool_failure``) even when no OAuth token could have produced
    them. Tokenless, the stream is the fallback credential's account — no
    report, no park."""
    from precis.utils.llm import accounts

    reports: list[Any] = []
    parks: list[Any] = []
    store = Mock()
    monkeypatch.setattr("precis.budget.meter.active_store", lambda: store)
    monkeypatch.setattr(
        accounts, "record_pool_report", lambda *a, **k: reports.append(k)
    )
    monkeypatch.setattr(
        accounts, "record_pool_failure", lambda *a, **k: parks.append(k)
    )
    monkeypatch.setattr("precis.utils.claude_oauth.token_resolvable", lambda: False)
    stream = json.dumps(
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "rejected",
                "resetsAt": 1791915000,
                "rateLimitType": "five_hour",
                "unifiedWindows": {
                    "five_hour": {"utilization": 0.55, "resetsAt": 1791915000},
                    "seven_day": {"utilization": 0.5, "resetsAt": 1792126800},
                },
            },
        }
    )
    refusal = LlmResult(
        text="",
        raw_text=stream,
        cost_usd=None,
        turns_used=None,
        model="m",
        tier=Tier.BIG,
        error="account quota exhausted: You've hit your limit",
        reason_class="quota",
        retry_at=RESET,
    )
    router._harvest_pool_signals(refusal, transport=Transport.CLAUDE_AGENT, bare=False)
    assert reports == [] and parks == []
