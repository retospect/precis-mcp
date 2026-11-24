"""Worker ceiling from the container memory limit (gr788515)."""

from __future__ import annotations

import pytest

from tests import _worker_budget as wb

GIB = 1024**3


def test_24g_gate_fits_four_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(wb.WORKER_BUDGET_ENV, raising=False)
    assert wb.max_workers(24 * GIB) == 4


def test_no_limit_means_no_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(wb.WORKER_BUDGET_ENV, raising=False)
    assert wb.max_workers(None) is None


def test_tiny_limit_keeps_one_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(wb.WORKER_BUDGET_ENV, raising=False)
    assert wb.max_workers(GIB) == 1


def test_budget_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(wb.WORKER_BUDGET_ENV, "4096")
    assert wb.max_workers(24 * GIB) == 6


def test_alarm_is_throttled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(wb.RSS_ALARM_ENV, "100")
    monkeypatch.setattr(wb, "rss_mb", lambda: 512.0)
    assert wb.rss_alarm(None) == 512.0
    assert wb.rss_alarm(512.0) is None
    monkeypatch.setattr(wb, "rss_mb", lambda: 1536.0)
    assert wb.rss_alarm(512.0) == 1536.0
    monkeypatch.setenv(wb.RSS_ALARM_ENV, "0")
    assert wb.rss_alarm(None) is None
