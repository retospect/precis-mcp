"""Pins for deploy/roles/hardening tasks that touch the Linux compute hosts.

- gr491172 (Reto ruling 2026-10-16): no raw nightly reboot cron. A weekly
  systemd timer (Tue 03:00Z) runs os-update-weekly, which upgrades and then
  reboots only with the reboot-required marker, an idle host (host-busy, the
  check scripts/host-ops/os-update also runs) and no service_config reserve.
- gr489603: the templated sshd_config carries an sftp Subsystem with the
  right sftp-server path per OS family, and both sshd tasks are tagged.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import jinja2
import pytest
import yaml

_REPO = Path(__file__).resolve().parent.parent
_ROLE = _REPO / "deploy" / "roles" / "hardening"
_FILES = _ROLE / "files"


def _tasks() -> list[dict[str, Any]]:
    tasks = yaml.safe_load((_ROLE / "tasks" / "main.yml").read_text(encoding="utf-8"))
    assert isinstance(tasks, list)
    return tasks


def _defaults() -> dict[str, Any]:
    defaults = yaml.safe_load(
        (_ROLE / "defaults" / "main.yml").read_text(encoding="utf-8")
    )
    assert isinstance(defaults, dict)
    return defaults


def _render_template(name: str, **context: Any) -> str:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(_ROLE / "templates"),
        undefined=jinja2.StrictUndefined,
    )
    return env.get_template(name).render(ansible_managed="managed", **context)


# ── gr491172: weekly guarded OS update ─────────────────────────────────────


def test_no_linux_task_installs_a_reboot_cron() -> None:
    for task in _tasks():
        cron = task.get("ansible.builtin.cron")
        if cron and "linux" in str(task.get("when", "")):
            assert cron.get("state") == "absent", task["name"]
            assert "job" not in cron, task["name"]


def test_weekly_timer_renders_tuesday_0300_utc_by_default() -> None:
    defaults = _defaults()
    assert defaults["hardening_os_update_weekly"] is True
    timer = _render_template("os-update-weekly.timer.j2", **defaults)
    assert "OnCalendar=Tue *-*-* 03:00:00 UTC" in timer.splitlines()
    assert "Persistent=false" in timer.splitlines()
    service = _render_template("os-update-weekly.service.j2", **defaults)
    assert "ExecStart=/usr/local/sbin/os-update-weekly" in service.splitlines()
    # The schedule is defined once: no literal calendar in the templates.
    assert "Tue" not in (_ROLE / "templates" / "os-update-weekly.timer.j2").read_text(
        encoding="utf-8"
    )


def test_os_update_tasks_are_tagged_and_linux_only() -> None:
    tasks = [t for t in _tasks() if "os-update" in t.get("tags", [])]
    assert len(tasks) == 4
    for task in tasks:
        assert task["when"] == 'os_family == "linux"', task["name"]
    copied = next(t for t in tasks if "ansible.builtin.copy" in t)
    assert copied["loop"] == ["host-busy", "os-update-weekly"]


def test_os_update_script_reuses_the_deployed_host_busy() -> None:
    text = (_REPO / "scripts" / "host-ops" / "os-update").read_text(encoding="utf-8")
    assert "deploy/roles/hardening/files/host-busy" in text
    assert "docker ps" not in text, "busy check copy-pasted back into os-update"


_FAKES = {
    "apt-get": 'echo "apt-get $*" >>"$FAKE_LOG"; exit "${FAKE_APT_RC:-0}"',
    "shutdown": 'echo "shutdown $*" >>"$FAKE_LOG"',
    "pgrep": 'echo "${FAKE_PGREP:-0}"; [ "${FAKE_PGREP:-0}" != 0 ]',
    "docker": '[ -z "${FAKE_DOCKER_FAIL:-}" ] || exit 1; printf "%b" "${FAKE_DOCKER:-}"',
    "systemctl": 'printf "%s" "${FAKE_WORKER_ENV:-}"',
    # sudo -u deploy -H <cmd...>: drop the identity switch, run the rest.
    "sudo": 'shift 3; exec "$@"',
    "precis": (
        'echo "precis host=${PRECIS_HOST_NAME:-} $*" >>"$FAKE_LOG"; '
        'case "${FAKE_RESERVE:-none}" in '
        'none) echo "reserve: none on castor" ;; '
        "crash) echo Traceback; exit 1 ;; "
        'odd) echo "service_config: ok" ;; '
        '*) echo "reserve: active on castor"; exit 3 ;; esac'
    ),
}


@pytest.fixture
def guard(tmp_path: Path) -> Any:
    if sys.platform == "win32":
        pytest.skip("POSIX-only shell scripts")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in _FAKES.items():
        fake = bin_dir / name
        fake.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
        fake.chmod(0o755)
    marker = tmp_path / "reboot-required"
    marker.touch()
    log = tmp_path / "calls.log"
    log.touch()

    def run(**fakes: str) -> tuple[subprocess.CompletedProcess[str], str]:
        env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FAKE_LOG": str(log),
            "FAKE_WORKER_ENV": "PRECIS_DATABASE_URL=postgresql://db/x PRECIS_HOST_NAME=castor",
            "OS_UPDATE_REBOOT_MARKER": str(marker),
            "OS_UPDATE_HOST_BUSY": str(_FILES / "host-busy"),
            "OS_UPDATE_PRECIS": str(bin_dir / "precis"),
            **fakes,
        }
        result = subprocess.run(
            [str(_FILES / "os-update-weekly")],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        return result, log.read_text(encoding="utf-8")

    run.marker = marker  # type: ignore[attr-defined]
    return run


def test_guard_reboots_only_when_marked_idle_and_unreserved(guard: Any) -> None:
    result, calls = guard(FAKE_DOCKER="vllm\\nn8n\\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "apt-get" in calls and "-y upgrade" in calls
    assert (
        "precis host=castor service reserved --database-url postgresql://db/x" in calls
    )
    assert "shutdown -r now" in calls


@pytest.mark.parametrize(
    ("fakes", "reason"),
    [
        ({"FAKE_PGREP": "1"}, "host busy (1 "),
        ({"FAKE_DOCKER": "vllm\\njob-7f3a\\n"}, "host busy (1 "),
        ({"FAKE_DOCKER_FAIL": "1"}, "host busy (unknown "),
        ({"FAKE_RESERVE": "active"}, "reserve set or unreadable"),
        ({"FAKE_RESERVE": "crash"}, "reserve set or unreadable: Traceback"),
        ({"FAKE_RESERVE": "odd"}, "reserve unreadable: service_config: ok"),
        ({"FAKE_WORKER_ENV": ""}, "reserve unreadable"),
        ({"OS_UPDATE_PRECIS": "/nonexistent/precis"}, "reserve unreadable"),
        ({"OS_UPDATE_HOST_BUSY": "/nonexistent/host-busy"}, "host busy (unknown "),
    ],
)
def test_guard_defers_reboot(guard: Any, fakes: dict[str, str], reason: str) -> None:
    result, calls = guard(**fakes)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reboot deferred" in result.stdout
    assert reason in result.stdout
    assert "-y upgrade" in calls
    assert "shutdown" not in calls
    assert guard.marker.exists()


def test_guard_without_marker_does_not_reboot(guard: Any) -> None:
    guard.marker.unlink()
    result, calls = guard()
    assert result.returncode == 0
    assert "no reboot required" in result.stdout
    assert "shutdown" not in calls
    assert "precis" not in calls


def test_guard_apt_failure_does_not_reboot(guard: Any) -> None:
    result, calls = guard(FAKE_APT_RC="100")
    assert result.returncode == 1
    assert "shutdown" not in calls


# ── gr489603: sshd sftp Subsystem ──────────────────────────────────────────


_SFTP_SERVER = {
    "linux": "/usr/lib/openssh/sftp-server",
    "darwin": "/usr/libexec/sftp-server",
}


def _render_sshd_config(os_family: str) -> str:
    return _render_template(
        "sshd_config.j2",
        ansible_host="tailnet-addr",
        lan_ip="lan-addr",
        sshd_permit_root_login="no",
        sshd_password_auth="no",
        sshd_allow_users="deploy",
        os_family=os_family,
    )


def test_sshd_config_renders_sftp_subsystem_per_os_family() -> None:
    for os_family, path in _SFTP_SERVER.items():
        lines = _render_sshd_config(os_family).splitlines()
        subsystems = [line for line in lines if line.startswith("Subsystem")]
        assert subsystems == [f"Subsystem sftp {path}"], (os_family, subsystems)


def test_sshd_tasks_are_tagged_sshd() -> None:
    sshd_tasks = [
        t
        for t in _tasks()
        if t.get("ansible.builtin.template", {}).get("src") == "sshd_config.j2"
    ]
    assert len(sshd_tasks) == 2
    for task in sshd_tasks:
        assert "sshd" in task["tags"], task["name"]


def test_role_shell_scripts_pass_shellcheck() -> None:
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed")
    for script in ("host-busy", "os-update-weekly"):
        result = subprocess.run(
            ["shellcheck", str(_FILES / script)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
