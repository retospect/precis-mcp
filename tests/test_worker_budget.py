"""Worker ceiling from the container memory limit (gr788515)."""

from __future__ import annotations

import os
from pathlib import Path

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


def test_rss_ignores_builtin_open_mock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    statm = tmp_path / "statm"
    statm.write_text("1024 512 0 0 0 0 0\n", encoding="ascii")
    monkeypatch.setattr(wb, "_PROC_STATM", statm)

    def fake_open(path: str, *_args: object, **_kwargs: object) -> None:
        raise KeyError(path)

    with monkeypatch.context() as patch:
        patch.setattr("builtins.open", fake_open)
        assert wb.rss_mb() == 512 * os.sysconf("SC_PAGE_SIZE") / 1024**2


def test_alarm_is_throttled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(wb.RSS_ALARM_ENV, "100")
    monkeypatch.setattr(wb, "rss_mb", lambda: 512.0)
    assert wb.rss_alarm(None) == 512.0
    assert wb.rss_alarm(512.0) is None
    monkeypatch.setattr(wb, "rss_mb", lambda: 1536.0)
    assert wb.rss_alarm(512.0) == 1536.0
    monkeypatch.setenv(wb.RSS_ALARM_ENV, "0")
    assert wb.rss_alarm(None) is None
