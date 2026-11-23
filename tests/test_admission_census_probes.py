"""Production census probes must distinguish services from inherited groups."""

import subprocess
from pathlib import Path

import pytest

from precis.workers import heartbeat as hb


def test_leaderless_cpu_group_uses_surviving_age() -> None:
    rows = [
        dict(pid=102, pgid=100, ppid=1, rss_mb=10, cpu=60, age_s=120, comm="job"),
        dict(pid=103, pgid=100, ppid=102, rss_mb=10, cpu=0, age_s=5, comm="job"),
    ]
    (group,) = hb._process_groups(rows)
    assert group["age_s"] == 120
    assert group["leader_pid"] == 0
    rows[0]["age_s"] = 5
    assert hb._process_groups(rows) == []


@pytest.mark.parametrize("main_pid, expected", [(900, True), (800, False), (0, False)])
@pytest.mark.parametrize("user", [False, True])
@pytest.mark.parametrize("hierarchy", ["0::", "1:name=systemd:"])
def test_linux_service_requires_main_pid(
    monkeypatch: pytest.MonkeyPatch,
    main_pid: int,
    expected: bool,
    user: bool,
    hierarchy: str,
) -> None:
    monkeypatch.setattr(hb.platform, "system", lambda: "Linux")
    prefix = (
        "/user.slice/user-1000.slice/user@1000.service/app.slice"
        if user
        else "/system.slice"
    )
    monkeypatch.setattr(
        Path, "read_text", lambda *a, **kw: f"{hierarchy}{prefix}/worker.service\n"
    )

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert argv == [
            "systemctl",
            *(["--user"] if user else []),
            "show",
            "--property=MainPID",
            "--value",
            "--",
            "worker.service",
        ]
        return subprocess.CompletedProcess(argv, 0, str(main_pid))

    monkeypatch.setattr(hb.subprocess, "run", run)
    assert hb._init_service(900, None) is expected
    (group,) = hb._process_groups(
        [dict(pid=900, pgid=900, ppid=1, rss_mb=900, cpu=0, age_s=120, comm="job")]
    )
    assert hb._declared_service_group(group, None) is expected
    group["leader_pid"] = 0
    group["leader_ppid"] = 0
    assert hb._declared_service_group(group, None) is expected


@pytest.mark.parametrize("output, code", [("", 1), ("invalid", 0)])
def test_linux_service_probe_failure_is_unknown(
    monkeypatch: pytest.MonkeyPatch, output: str, code: int
) -> None:
    monkeypatch.setattr(hb.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        Path, "read_text", lambda *a, **kw: "0::/system.slice/worker.service\n"
    )
    monkeypatch.setattr(
        hb.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, code, output),
    )
    assert hb._init_service(900, None) is None
