"""Host-side systemd worker-unit snapshots published by heartbeat."""

from __future__ import annotations

import subprocess
from datetime import datetime
from types import SimpleNamespace
from typing import cast

import pytest

from precis.store import Store
from precis.workers import heartbeat


def _result(stdout: str = "", returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, returncode=returncode)


@pytest.mark.parametrize(
    ("active", "enabled", "expected"),
    [
        (
            "inactive",
            "disabled",
            {"active_state": "inactive", "unit_file_state": "disabled"},
        ),
        ("failed", "enabled", {"active_state": "failed", "unit_file_state": "enabled"}),
        (
            "active",
            "disabled",
            {"active_state": "active", "unit_file_state": "disabled"},
        ),
    ],
)
def test_collect_worker_units_records_raw_states(
    monkeypatch, active, enabled, expected
) -> None:
    monkeypatch.setattr(heartbeat.platform, "system", lambda: "Linux")
    monkeypatch.setattr(heartbeat, "_in_container", lambda: False)
    monkeypatch.setattr(heartbeat.shutil, "which", lambda name: "/usr/bin/systemctl")
    calls = iter(
        [
            _result("precis-worker.service enabled enabled\n"),
            _result(
                f"Id=precis-worker.service\nActiveState={active}\nUnitFileState={enabled}\n"
            ),
        ]
    )
    monkeypatch.setattr(heartbeat.subprocess, "run", lambda *a, **k: next(calls))

    snapshot = heartbeat.collect_worker_units()

    datetime.fromisoformat(snapshot["observed_at"])
    assert snapshot["units"] == {"precis-worker": expected}


def test_collect_worker_units_rejects_bad_listing_without_show(monkeypatch) -> None:
    monkeypatch.setattr(heartbeat.platform, "system", lambda: "Linux")
    monkeypatch.setattr(heartbeat, "_in_container", lambda: False)
    monkeypatch.setattr(heartbeat.shutil, "which", lambda name: "/usr/bin/systemctl")
    calls: list[list[str]] = []

    def run(args, **kwargs):
        calls.append(args)
        return _result("precis-workers.service enabled enabled\n")

    monkeypatch.setattr(heartbeat.subprocess, "run", run)
    assert heartbeat.collect_worker_units() == {}
    assert len(calls) == 1


def test_collect_worker_units_rejects_failed_listing(monkeypatch) -> None:
    monkeypatch.setattr(heartbeat.platform, "system", lambda: "Linux")
    monkeypatch.setattr(heartbeat, "_in_container", lambda: False)
    monkeypatch.setattr(heartbeat.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(
        heartbeat.subprocess, "run", lambda *a, **k: _result(returncode=1)
    )
    assert heartbeat.collect_worker_units() == {}


def test_collect_worker_units_parses_multiple_valid_units_and_bounds_calls(
    monkeypatch,
) -> None:
    monkeypatch.setattr(heartbeat.platform, "system", lambda: "Linux")
    monkeypatch.setattr(heartbeat, "_in_container", lambda: False)
    monkeypatch.setattr(heartbeat.shutil, "which", lambda name: "/usr/bin/systemctl")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if args[1] == "list-unit-files":
            return _result(
                "precis-worker.service enabled enabled\n"
                "precis-worker-compute.service disabled disabled\n"
                "precis-worker-sandbox.service enabled enabled\n"
            )
        return _result(
            "Id=precis-worker.service\nActiveState=active\nUnitFileState=enabled\n\n"
            "Id=precis-worker-compute.service\nActiveState=inactive\nUnitFileState=disabled\n\n"
            "Id=precis-worker-sandbox.service\nActiveState=failed\nUnitFileState=enabled\n"
        )

    monkeypatch.setattr(heartbeat.subprocess, "run", run)
    snapshot = heartbeat.collect_worker_units()

    assert set(snapshot["units"]) == {
        "precis-worker",
        "precis-worker-compute",
        "precis-worker-sandbox",
    }
    assert snapshot["units"]["precis-worker-compute"] == {
        "active_state": "inactive",
        "unit_file_state": "disabled",
    }
    assert len(calls) == 2
    for args, kwargs in calls:
        assert kwargs["timeout"] == 5
        assert kwargs["text"] is True
        assert kwargs["encoding"] == "utf-8"
        assert "shell" not in kwargs
        assert args[-1] == "--no-pager"
    assert calls[1][0][2:5] == [
        "precis-worker-compute.service",
        "precis-worker-sandbox.service",
        "precis-worker.service",
    ]


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError("systemctl"),
        subprocess.TimeoutExpired("systemctl", 5),
        UnicodeError("bad output"),
    ],
)
def test_collect_worker_units_swallows_collection_failures(
    monkeypatch, failure
) -> None:
    monkeypatch.setattr(heartbeat.platform, "system", lambda: "Linux")
    monkeypatch.setattr(heartbeat, "_in_container", lambda: False)
    monkeypatch.setattr(heartbeat.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(
        heartbeat.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(failure)
    )
    assert heartbeat.collect_worker_units() == {}


@pytest.mark.parametrize(
    ("system", "in_container", "systemctl"),
    [
        ("Darwin", False, "/usr/bin/systemctl"),
        ("Linux", True, "/usr/bin/systemctl"),
        ("Linux", False, None),
    ],
)
def test_collect_worker_units_skips_unsupported_hosts(
    monkeypatch, system, in_container, systemctl
) -> None:
    monkeypatch.setattr(heartbeat.platform, "system", lambda: system)
    monkeypatch.setattr(heartbeat, "_in_container", lambda: in_container)
    monkeypatch.setattr(heartbeat.shutil, "which", lambda name: systemctl)
    monkeypatch.setattr(
        heartbeat.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("systemctl should not run")
        ),
    )
    assert heartbeat.collect_worker_units() == {}


def test_collect_worker_units_clears_on_missing_show_record(monkeypatch) -> None:
    monkeypatch.setattr(heartbeat.platform, "system", lambda: "Linux")
    monkeypatch.setattr(heartbeat, "_in_container", lambda: False)
    monkeypatch.setattr(heartbeat.shutil, "which", lambda name: "/usr/bin/systemctl")
    calls = iter(
        [
            _result("precis-worker.service enabled enabled\n"),
            _result("Id=precis-worker.service\nActiveState=inactive\n"),
        ]
    )
    monkeypatch.setattr(heartbeat.subprocess, "run", lambda *a, **k: next(calls))
    assert heartbeat.collect_worker_units() == {}


def test_collect_worker_units_clears_on_failed_show(monkeypatch) -> None:
    monkeypatch.setattr(heartbeat.platform, "system", lambda: "Linux")
    monkeypatch.setattr(heartbeat, "_in_container", lambda: False)
    monkeypatch.setattr(heartbeat.shutil, "which", lambda name: "/usr/bin/systemctl")
    calls = iter(
        [_result("precis-worker.service enabled enabled\n"), _result(returncode=1)]
    )
    monkeypatch.setattr(heartbeat.subprocess, "run", lambda *a, **k: next(calls))
    assert heartbeat.collect_worker_units() == {}


def test_collect_and_upsert_publishes_even_empty_worker_unit_snapshot(
    monkeypatch,
) -> None:
    seen: dict[str, object] = {}

    class _Store:
        def record_heartbeat(self, host, **kwargs):
            seen.update(kwargs["meta"])

        def record_heartbeat_history(self, *args, **kwargs):
            pass

    monkeypatch.setattr(heartbeat, "collect_loads", lambda: (1.0, 1.0, 1.0))
    monkeypatch.setattr(heartbeat, "read_temp_c", lambda: None)
    monkeypatch.setattr(heartbeat, "collect_top_cpu", lambda: [])
    monkeypatch.setattr(heartbeat, "collect_worker_units", lambda: {})
    monkeypatch.setattr(heartbeat, "_probe_nas", lambda: {})
    monkeypatch.setattr(heartbeat, "nas_attestation_entry", lambda nas: None)
    monkeypatch.setattr(heartbeat, "_own_boot_ids_meta", lambda: {})
    monkeypatch.setattr(heartbeat.activity, "snapshot", lambda: {})
    monkeypatch.setattr(heartbeat, "_resolve_process_name_for_boot", lambda: None)
    monkeypatch.setattr(heartbeat, "_report_resource_slots", lambda *args: "n/a")
    monkeypatch.setattr("precis.settings.advertised_env_presence", lambda: {})

    heartbeat._collect_and_upsert(cast(Store, _Store()), "host")

    assert seen["worker_units"] == {}
