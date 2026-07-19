"""Unit tests for the ``precis heartbeat`` temperature cascade.

Pure-logic (no DB): monkeypatch the platform + per-source probes and
assert the ``read_temp_c`` priority order and the guards around it.
"""

from __future__ import annotations

from precis.cli import heartbeat


def test_read_temp_prefers_env_cmd(monkeypatch) -> None:
    monkeypatch.setenv("PRECIS_TEMP_CMD", "echo 42.0")
    # env-cmd wins even on a Darwin host with a working sensor.
    monkeypatch.setattr(heartbeat.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(heartbeat, "_temp_from_macos_iokit", lambda: 99.0)
    assert heartbeat.read_temp_c() == 42.0


def test_read_temp_macos_uses_iokit_first(monkeypatch) -> None:
    monkeypatch.delenv("PRECIS_TEMP_CMD", raising=False)
    monkeypatch.setattr(heartbeat.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(heartbeat, "_temp_from_macos_iokit", lambda: 77.6)
    # smc must not even be consulted when IOKit yields a reading.
    monkeypatch.setattr(
        heartbeat,
        "_temp_from_macos_smc",
        lambda: (_ for _ in ()).throw(AssertionError("smc should not run")),
    )
    assert heartbeat.read_temp_c() == 77.6


def test_read_temp_macos_falls_back_to_smc(monkeypatch) -> None:
    monkeypatch.delenv("PRECIS_TEMP_CMD", raising=False)
    monkeypatch.setattr(heartbeat.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(heartbeat, "_temp_from_macos_iokit", lambda: None)
    monkeypatch.setattr(heartbeat, "_temp_from_macos_smc", lambda: 55.0)
    assert heartbeat.read_temp_c() == 55.0


def test_read_temp_macos_none_when_no_source(monkeypatch) -> None:
    monkeypatch.delenv("PRECIS_TEMP_CMD", raising=False)
    monkeypatch.setattr(heartbeat.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(heartbeat, "_temp_from_macos_iokit", lambda: None)
    monkeypatch.setattr(heartbeat, "_temp_from_macos_smc", lambda: None)
    assert heartbeat.read_temp_c() is None


def test_smc_zero_reading_suppressed(monkeypatch) -> None:
    # osx-cpu-temp prints "0.0°C" on Apple Silicon (Intel-only SMC keys);
    # that must be treated as "no reading", not a bogus 0.0.
    class _Res:
        returncode = 0
        stdout = "0.0°C\n"

    monkeypatch.setattr(heartbeat.subprocess, "run", lambda *a, **k: _Res())
    assert heartbeat._temp_from_macos_smc() is None
