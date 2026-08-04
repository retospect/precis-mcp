"""Tests for the disk_check pass (gripe 191008).

Pins: a healthy disk is a no-op; crossing warn/crit raises the matching
severity of ``kind='alert'``; a fresh critical (and a warn->crit
escalation, which bumps the same open row rather than inserting a fresh
one) pages via ``notify_critical_alert`` exactly once per condition;
and a recovered reading resolves the standing alert.
``shutil.disk_usage`` and the host resolver are stubbed so the tests
are deterministic and host-independent.
"""

from __future__ import annotations

from collections import namedtuple

from precis.alerts import list_open_alerts
from precis.store import Store
from precis.workers import disk_check as dc

_Usage = namedtuple("_Usage", ["total", "used", "free"])

_TOTAL = 100 * 1024**3  # 100 GiB


def _usage_for_pct(pct: float) -> _Usage:
    used = int(_TOTAL * pct / 100.0)
    return _Usage(total=_TOTAL, used=used, free=_TOTAL - used)


def _disk_alerts(store: Store) -> list[dict]:
    return [a for a in list_open_alerts(store) if a["source"] == "disk_check"]


def _stub_host(monkeypatch, host: str = "testhost") -> None:
    monkeypatch.setattr(dc, "_resolve_host_name", lambda: host)


def test_healthy_disk_raises_no_alert(store: Store, monkeypatch) -> None:
    _stub_host(monkeypatch)
    monkeypatch.setattr(dc.shutil, "disk_usage", lambda path: _usage_for_pct(10.0))

    result = dc.run_disk_check_pass(store)

    assert result.handler == "disk_check"
    assert result.claimed == 1
    assert result.ok == 1
    assert result.failed == 0
    assert _disk_alerts(store) == []


def test_warn_threshold_opens_warn_alert(store: Store, monkeypatch) -> None:
    _stub_host(monkeypatch)
    monkeypatch.setattr(dc.shutil, "disk_usage", lambda path: _usage_for_pct(88.0))

    dc.run_disk_check_pass(store)

    alerts = _disk_alerts(store)
    assert len(alerts) == 1
    assert alerts[0]["severity"] == "warn"
    assert alerts[0]["source"] == "disk_check"
    assert "testhost:/" in alerts[0]["title"]


def test_crit_threshold_opens_critical_and_pages_once(
    store: Store, monkeypatch
) -> None:
    _stub_host(monkeypatch)
    monkeypatch.setattr(dc.shutil, "disk_usage", lambda path: _usage_for_pct(95.0))

    calls: list[tuple] = []

    def _fake_notify(store_, title, detail="", *, fingerprint="") -> bool:
        calls.append((title, fingerprint))
        return True

    monkeypatch.setattr(dc, "notify_critical_alert", _fake_notify)

    dc.run_disk_check_pass(store)

    alerts = _disk_alerts(store)
    assert len(alerts) == 1
    assert alerts[0]["severity"] == "critical"
    assert len(calls) == 1
    assert calls[0][1] == "testhost:/"

    # A second critical pass dedups the open alert and does NOT page again.
    dc.run_disk_check_pass(store)
    assert len(_disk_alerts(store)) == 1
    assert len(calls) == 1


def test_warn_then_crit_escalation_pages_exactly_once(
    store: Store, monkeypatch
) -> None:
    """The gripe-191008 regression: a gradual fill opens `warn` first, so
    the later `warn` -> `crit` cycle BUMPS that same open row instead of
    inserting a fresh one (`raise_alert`'s `is_new` is False on that
    call). The page must still fire on the escalation — and then stay
    silent on a further still-critical cycle."""
    _stub_host(monkeypatch)

    calls: list[tuple] = []

    def _fake_notify(store_, title, detail="", *, fingerprint="") -> bool:
        calls.append((title, fingerprint))
        return True

    monkeypatch.setattr(dc, "notify_critical_alert", _fake_notify)

    # 1) warn cycle — opens a warn alert, no page.
    monkeypatch.setattr(dc.shutil, "disk_usage", lambda path: _usage_for_pct(88.0))
    dc.run_disk_check_pass(store)
    alerts = _disk_alerts(store)
    assert len(alerts) == 1
    assert alerts[0]["severity"] == "warn"
    assert calls == []

    # 2) crit cycle — same fingerprint, bumps the existing row to
    # critical (is_new is False) but must still page exactly once.
    monkeypatch.setattr(dc.shutil, "disk_usage", lambda path: _usage_for_pct(95.0))
    dc.run_disk_check_pass(store)
    alerts = _disk_alerts(store)
    assert len(alerts) == 1
    assert alerts[0]["severity"] == "critical"
    assert len(calls) == 1
    assert calls[0][1] == "testhost:/"

    # 3) still crit — no further page.
    dc.run_disk_check_pass(store)
    assert len(_disk_alerts(store)) == 1
    assert len(calls) == 1


def test_recovery_resolves_open_alert(store: Store, monkeypatch) -> None:
    _stub_host(monkeypatch)
    monkeypatch.setattr(dc.shutil, "disk_usage", lambda path: _usage_for_pct(96.0))
    dc.run_disk_check_pass(store)
    assert len(_disk_alerts(store)) == 1

    monkeypatch.setattr(dc.shutil, "disk_usage", lambda path: _usage_for_pct(5.0))
    dc.run_disk_check_pass(store)

    assert _disk_alerts(store) == []
