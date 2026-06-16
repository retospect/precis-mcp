"""Unit tests for the ``precis heartbeat`` reporter collection helpers.

No DB and no real sensors: each platform probe is monkeypatched so
the parsing / fallback logic is exercised deterministically.
"""

from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

from precis.cli import heartbeat

# ``os.getloadavg`` is Unix-only; the monkeypatch tests assume the
# attribute exists on the real module so it can be replaced. Windows
# never has it, so the tests can't be exercised there.
_NO_GETLOADAVG = not hasattr(os, "getloadavg")


def test_parse_first_float() -> None:
    assert heartbeat._parse_first_float("52.3") == 52.3
    assert heartbeat._parse_first_float("temp: 61.0C\n") == 61.0
    assert heartbeat._parse_first_float("-5") == -5.0
    assert heartbeat._parse_first_float("no number here") is None


def test_resolve_host_precedence(monkeypatch) -> None:
    monkeypatch.setenv("PRECIS_HOST_NAME", "envhost")
    assert heartbeat.resolve_host("flaghost") == "flaghost"  # flag wins
    assert heartbeat.resolve_host(None) == "envhost"  # env next
    monkeypatch.delenv("PRECIS_HOST_NAME", raising=False)
    assert heartbeat.resolve_host(None)  # hostname fallback, non-empty


@pytest.mark.skipif(_NO_GETLOADAVG, reason="os.getloadavg is Unix-only")
def test_collect_loads_normal(monkeypatch) -> None:
    monkeypatch.setattr(heartbeat.os, "getloadavg", lambda: (1.5, 1.2, 0.9))
    assert heartbeat.collect_loads() == (1.5, 1.2, 0.9)


@pytest.mark.skipif(_NO_GETLOADAVG, reason="os.getloadavg is Unix-only")
def test_collect_loads_unavailable(monkeypatch) -> None:
    def _boom() -> tuple[float, float, float]:
        raise OSError("no loadavg")

    monkeypatch.setattr(heartbeat.os, "getloadavg", _boom)
    assert heartbeat.collect_loads() == (None, None, None)


def test_read_temp_via_cmd(monkeypatch) -> None:
    monkeypatch.setenv("PRECIS_TEMP_CMD", "fake-sensor")

    def _fake_run(*_a, **_k):
        return SimpleNamespace(returncode=0, stdout="58.4\n", stderr="")

    monkeypatch.setattr(heartbeat.subprocess, "run", _fake_run)
    assert heartbeat.read_temp_c() == 58.4


def test_read_temp_cmd_failure_falls_through(monkeypatch) -> None:
    monkeypatch.setenv("PRECIS_TEMP_CMD", "fake-sensor")

    def _fake_run(*_a, **_k):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(heartbeat.subprocess, "run", _fake_run)
    # Non-Linux + failed cmd → None (no thermal zones to read).
    monkeypatch.setattr(heartbeat.platform, "system", lambda: "Darwin")
    assert heartbeat.read_temp_c() is None


def test_read_temp_cmd_timeout_is_swallowed(monkeypatch) -> None:
    monkeypatch.setenv("PRECIS_TEMP_CMD", "slow-sensor")

    def _boom(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="slow-sensor", timeout=10)

    monkeypatch.setattr(heartbeat.subprocess, "run", _boom)
    monkeypatch.setattr(heartbeat.platform, "system", lambda: "Darwin")
    assert heartbeat.read_temp_c() is None


def test_temp_from_linux_thermal(monkeypatch) -> None:
    monkeypatch.setattr(
        heartbeat.glob,
        "glob",
        lambda _pat: [
            "/sys/class/thermal/thermal_zone0/temp",
            "/sys/class/thermal/thermal_zone1/temp",
        ],
    )
    contents = {
        "/sys/class/thermal/thermal_zone0/temp": "45000\n",
        "/sys/class/thermal/thermal_zone1/temp": "62000\n",
    }

    import io

    def _fake_open(path, *_a, **_k):
        return io.StringIO(contents[path])

    monkeypatch.setattr("builtins.open", _fake_open)
    # Max across zones, millidegrees → °C.
    assert heartbeat._temp_from_linux_thermal() == 62.0


def test_read_temp_none_on_mac_without_cmd(monkeypatch) -> None:
    monkeypatch.delenv("PRECIS_TEMP_CMD", raising=False)
    monkeypatch.setattr(heartbeat.platform, "system", lambda: "Darwin")
    assert heartbeat.read_temp_c() is None
