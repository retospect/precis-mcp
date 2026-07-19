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


def _fake_ps(stdout: str, returncode: int = 0):
    def _run(*args, **kwargs):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    return _run


def test_collect_top_cpu_sorts_limits_and_basenames(monkeypatch) -> None:
    # Unsorted `ps -Ao pcpu=,comm=` output with an absolute path, a zero-CPU
    # process, and a blank line — sorted desc, cpu>0 kept, comm basenamed, top-n.
    out = (
        " 7.3 /System/Library/.../WindowServer\n"
        "100.0 /opt/homebrew/opt/postgresql/bin/postgres\n"
        " 0.0 idled\n"
        "\n"
        " 99.8 /opt/homebrew/opt/postgresql/bin/postgres\n"
    )
    monkeypatch.setattr(heartbeat.subprocess, "run", _fake_ps(out))
    top = heartbeat.collect_top_cpu(n=2)
    assert top == [
        {"cpu": 100.0, "cmd": "postgres"},
        {"cpu": 99.8, "cmd": "postgres"},
    ]


def test_collect_top_cpu_degrades_on_failure(monkeypatch) -> None:
    def _boom(*args, **kwargs):
        raise OSError("no ps")

    monkeypatch.setattr(heartbeat.subprocess, "run", _boom)
    assert heartbeat.collect_top_cpu() == []
    # Non-zero exit → empty, not a crash.
    monkeypatch.setattr(heartbeat.subprocess, "run", _fake_ps("", returncode=1))
    assert heartbeat.collect_top_cpu() == []


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
    """Mac without ``osx-cpu-temp`` installed and without
    PRECIS_TEMP_CMD → None. Stub the macOS SMC probe to None so we
    don't accidentally pick up a real brew install in CI."""
    monkeypatch.delenv("PRECIS_TEMP_CMD", raising=False)
    monkeypatch.setattr(heartbeat.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(heartbeat, "_temp_from_macos_smc", lambda: None)
    assert heartbeat.read_temp_c() is None


def test_read_temp_uses_macos_smc_when_available(monkeypatch) -> None:
    """When ``osx-cpu-temp`` returns "47.5°C" we lift that float into
    the heartbeat reading."""
    monkeypatch.delenv("PRECIS_TEMP_CMD", raising=False)
    monkeypatch.setattr(heartbeat.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(heartbeat, "_temp_from_macos_smc", lambda: 47.5)
    assert heartbeat.read_temp_c() == 47.5


def test_temp_from_macos_smc_parses_brew_binary_output(monkeypatch) -> None:
    """The brew binary outputs "47.5°C\\n"; parse the first float."""
    import subprocess as _sp

    def _fake_run(cmd, **kw):
        # Match either Apple Silicon or Intel install path.
        if cmd[0] in (
            "/opt/homebrew/bin/osx-cpu-temp",
            "/usr/local/bin/osx-cpu-temp",
        ):

            class _R:
                returncode = 0
                stdout = "47.5°C\n"
                stderr = ""

            return _R()
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(_sp, "run", _fake_run)
    monkeypatch.setattr(heartbeat.subprocess, "run", _fake_run)
    assert heartbeat._temp_from_macos_smc() == 47.5


def test_temp_from_macos_smc_returns_none_when_binary_missing(monkeypatch) -> None:
    """When neither install path exists, the probe returns None
    (every Mac without the brew install just reports no temp)."""

    def _raise_missing(cmd, **kw):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(heartbeat.subprocess, "run", _raise_missing)
    assert heartbeat._temp_from_macos_smc() is None


# ── slice 6b: the resource-slot self-probe wiring ────────────────────────


class _RecordingStore:
    def __init__(self, boom: bool = False) -> None:
        self.boom = boom
        self.synced: tuple | None = None
        self.soft: list[tuple] = []
        self.deleted: list[tuple] = []

    def sync_host_resource_slots(self, host, slots, *, kinds=None) -> None:
        if self.boom:
            raise RuntimeError("db down")
        self.synced = (host, slots, kinds)

    def sync_soft_signal(self, host, resource, free, capacity, *, conn=None) -> None:
        if self.boom:
            raise RuntimeError("db down")
        self.soft.append((host, resource, free, capacity))

    def delete_soft_signal(self, host, resource, *, conn=None) -> None:
        if self.boom:
            raise RuntimeError("db down")
        self.deleted.append((host, resource))


def test_report_resource_slots_syncs_and_summarises(monkeypatch) -> None:
    from precis.workers import capability_probe

    monkeypatch.setattr(
        capability_probe,
        "probe_host_resources",
        lambda: {"gpu": 1, "podman": 0, "tts": None},
    )
    # Deterministic soft signal (6d-deferred) so the test doesn't read real RAM.
    monkeypatch.setattr(capability_probe, "probe_soft_signals", lambda: {"mem": 0})
    store = _RecordingStore()
    summary = heartbeat._report_resource_slots(store, "melchior")
    # Only present (>0) capabilities land in the CLI summary.
    assert summary == "gpu=1"
    # The full verdict (including the 0 and the None) is handed to the store.
    assert store.synced is not None
    host, slots, kinds = store.synced
    assert host == "melchior"
    assert slots == {"gpu": 1, "podman": 0, "tts": None}
    assert kinds == {"gpu": "hard", "podman": "hard", "tts": "hard"}
    # The soft memory gauge is written free-first with the nominal capacity.
    assert store.soft == [("melchior", "mem", 0, capability_probe.mem_capacity())]


def test_report_resource_slots_threads_per_resource_soft_capacity(monkeypatch) -> None:
    """Each soft gauge is written with ITS OWN capacity, not one stamp for all.

    Regression for the pre-fix bug where the heartbeat passed ``mem_capacity()``
    for every soft signal — a ``container_agent`` 0/1 flag would have been
    advertised with mem's capacity of 2, mis-rendering the console."""
    from precis.workers import capability_probe

    monkeypatch.setattr(capability_probe, "probe_host_resources", lambda: {})
    monkeypatch.setattr(
        capability_probe,
        "probe_soft_signals",
        lambda: {"mem": 1, "container_agent": 0},
    )
    store = _RecordingStore()
    heartbeat._report_resource_slots(store, "melchior")
    assert ("melchior", "mem", 1, capability_probe.soft_capacity("mem")) in store.soft
    assert (
        "melchior",
        "container_agent",
        0,
        capability_probe.soft_capacity("container_agent"),
    ) in store.soft
    # The two capacities genuinely differ (the point of the fix).
    assert capability_probe.soft_capacity("mem") != capability_probe.soft_capacity(
        "container_agent"
    )


def test_report_resource_slots_retracts_dropped_soft_gauge(monkeypatch) -> None:
    """A retractable soft gauge absent from the probe (container_agent once a
    host opts out) is DELETEd, so the console stops showing a stale chip. mem,
    always present, is never retracted."""
    from precis.workers import capability_probe

    monkeypatch.setattr(capability_probe, "probe_host_resources", lambda: {})
    # container_agent has dropped out (host opted back out); only mem remains.
    monkeypatch.setattr(capability_probe, "probe_soft_signals", lambda: {"mem": 2})
    store = _RecordingStore()
    heartbeat._report_resource_slots(store, "melchior")
    assert store.deleted == [("melchior", "container_agent")]
    # mem was synced, not deleted.
    assert ("melchior", "mem") not in store.deleted


def test_report_resource_slots_no_retract_when_gauge_present(monkeypatch) -> None:
    """When container_agent IS reported (host still opted in), nothing is
    retracted — the row is synced, not deleted."""
    from precis.workers import capability_probe

    monkeypatch.setattr(capability_probe, "probe_host_resources", lambda: {})
    monkeypatch.setattr(
        capability_probe,
        "probe_soft_signals",
        lambda: {"mem": 2, "container_agent": 1},
    )
    store = _RecordingStore()
    heartbeat._report_resource_slots(store, "melchior")
    assert store.deleted == []


def test_report_resource_slots_swallows_failure(monkeypatch) -> None:
    """A probe/sync failure must not fail the (liveness-critical) heartbeat."""
    from precis.workers import capability_probe

    monkeypatch.setattr(capability_probe, "probe_host_resources", lambda: {"gpu": 1})
    store = _RecordingStore(boom=True)
    assert heartbeat._report_resource_slots(store, "melchior") == "n/a"


def test_report_resource_slots_none_when_nothing_present(monkeypatch) -> None:
    from precis.workers import capability_probe

    monkeypatch.setattr(
        capability_probe, "probe_host_resources", lambda: {"gpu": 0, "tts": 0}
    )
    monkeypatch.setattr(capability_probe, "probe_soft_signals", lambda: {"mem": None})
    store = _RecordingStore()
    assert heartbeat._report_resource_slots(store, "spark") == "none"
