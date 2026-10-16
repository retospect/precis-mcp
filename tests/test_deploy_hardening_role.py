"""Pins for deploy/roles/hardening tasks that touch the Linux compute hosts.

- gr491172 (Reto ruling 2026-10-16): no raw nightly reboot cron. A weekly
  systemd timer (Tue 03:00Z) runs os-update-weekly, which upgrades and then
  reboots only with the reboot-required marker, an idle host (host-busy, the
  check scripts/host-ops/os-update also runs) and no service_config reserve.
- gr489603: the templated sshd_config carries an sftp Subsystem with the
  right sftp-server path per OS family, and both sshd tasks are tagged.
- gr489996: dgx-dashboard-admin.service is stopped, disabled and masked,
  tagged dgx-dashboard and only where the unit exists.
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


_FLEET = [
    "node-a",
    "node-b",
    "node-c",
    "node-d",
    "node-e",
    "node-f",
    "node-g",
    "node-h",
]


def _resolved_defaults(host: str, group: list[str]) -> dict[str, Any]:
    """Resolve the string defaults through jinja2 the way ansible would."""
    raw = _defaults()
    values = dict(raw)
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    ctx = {"inventory_hostname": host, "groups": {"linux": group}}
    for _ in range(10):
        ctx_all = {**values, **ctx}
        rendered = {
            k: env.from_string(v).render(**ctx_all).strip() if isinstance(v, str) else v
            for k, v in raw.items()
        }
        if rendered == values:
            return values
        values = rendered
    raise AssertionError("defaults did not converge")


def _start_and_timeout(host: str, group: list[str]) -> tuple[str, int]:
    d = _resolved_defaults(host, group)
    cal = d["hardening_os_update_oncalendar"]
    day, _, hhmm, tz = cal.split()
    assert (day, tz) == ("Tue", "UTC"), cal
    assert hhmm.endswith(":00") and len(hhmm) == 8, cal
    timeout = d["hardening_os_update_timeout"]
    assert timeout.endswith("min")
    return hhmm[:5], int(timeout.removesuffix("min"))


@pytest.mark.parametrize(
    ("host", "start", "timeout"),
    [
        ("node-a", "03:00", 75),
        ("node-b", "03:20", 55),
        ("node-c", "03:40", 35),
        ("node-d", "04:00", 15),
        ("node-h", "04:00", 15),  # index 7: clamped at max offset
        ("node-z", "03:00", 75),  # not in the group
    ],
)
def test_weekly_schedule_is_staggered_per_host(
    host: str, start: str, timeout: int
) -> None:
    # Group order is shuffled: the index comes from the sorted names.
    assert _start_and_timeout(host, list(reversed(_FLEET))) == (start, timeout)


def test_weekly_schedule_stays_inside_the_no_deploy_window() -> None:
    for host in [*_FLEET, "node-z"]:
        start, timeout = _start_and_timeout(host, _FLEET)
        minutes = int(start[:2]) * 60 + int(start[3:])
        assert 3 * 60 <= minutes <= 4 * 60, (host, start)
        assert minutes + timeout <= 4 * 60 + 20, (host, start, timeout)


def test_weekly_timer_and_service_render_the_defaults() -> None:
    defaults = _resolved_defaults("node-b", _FLEET)
    assert defaults["hardening_os_update_weekly"] is True
    timer = _render_template("os-update-weekly.timer.j2", **defaults)
    assert "OnCalendar=Tue *-*-* 03:20:00 UTC" in timer.splitlines()
    assert "Persistent=false" in timer.splitlines()
    service = _render_template("os-update-weekly.service.j2", **defaults)
    assert "ExecStart=/usr/local/sbin/os-update-weekly" in service.splitlines()
    assert "TimeoutStartSec=55min" in service.splitlines()
    # The schedule is defined once: no literal calendar in the templates.
    assert "Tue" not in (_ROLE / "templates" / "os-update-weekly.timer.j2").read_text(
        encoding="utf-8"
    )


def test_os_update_tasks_are_tagged_and_linux_only() -> None:
    tasks = [t for t in _tasks() if "os-update-weekly" in t.get("tags", [])]
    # 4 weekly-update tasks + 4 that retire the old nightly-reboot timer.
    assert len(tasks) == 8
    for task in tasks:
        when = task["when"] if isinstance(task["when"], list) else [task["when"]]
        assert when[0] == 'os_family == "linux"', task["name"]
    copied = next(t for t in tasks if "ansible.builtin.copy" in t)
    assert copied["loop"] == ["host-busy", "os-update-weekly"]


def test_nightly_reboot_is_retired_into_the_weekly_path() -> None:
    assert not (_REPO / "deploy" / "roles" / "nightly_reboot").exists()
    assert not (_REPO / "deploy" / "playbooks" / "42-nightly-reboot.yml").exists()
    site = (_REPO / "deploy" / "site.yml").read_text(encoding="utf-8")
    assert "42-nightly-reboot" not in site
    assert "nightly_reboot" not in site
    retire = [
        t
        for t in _tasks()
        if "nightly-reboot" in str(t) and "retir" in t["name"].lower()
    ]
    assert len(retire) == 4
    assert any(
        t.get("ansible.builtin.systemd", {}).get("name") == "nightly-reboot.timer"
        for t in retire
    )
    for task in retire:
        assert "os-update-weekly" in task["tags"], task["name"]
        assert "linux" in str(task["when"]), task["name"]


def test_os_update_script_reuses_the_deployed_host_busy() -> None:
    text = (_REPO / "scripts" / "host-ops" / "os-update").read_text(encoding="utf-8")
    assert "deploy/roles/hardening/files/host-busy" in text
    assert "docker ps" not in text, "busy check copy-pasted back into os-update"


_FAKES = {
    "apt-get": 'echo "apt-get $*" >>"$FAKE_LOG"; exit "${FAKE_APT_RC:-0}"',
    "shutdown": 'echo "shutdown $*" >>"$FAKE_LOG"; '
    '[ -z "${FAKE_SHUTDOWN_KILL:-}" ] || kill -TERM $PPID; exit "${FAKE_SHUTDOWN_RC:-0}"',
    "pgrep": 'echo "${FAKE_PGREP:-0}"; [ "${FAKE_PGREP:-0}" != 0 ]',
    "docker": '[ -z "${FAKE_DOCKER_FAIL:-}" ] || exit 1; printf "%b" "${FAKE_DOCKER:-}"',
    # `show` answers the worker env; the serving-engine calls log to FAKE_LOG
    # and answer from FAKE_ACTIVE / FAKE_ENABLED (space-separated unit names).
    # A successful `stop` marks the unit inactive (FAKE_STOP_FAIL=1 fails it).
    "systemctl": """
cmd=$1; shift
case "$cmd" in
  show) printf "%s" "${FAKE_WORKER_ENV:-}" ;;
  is-active|is-enabled)
    unit=${@: -1}
    echo "systemctl $cmd $unit" >>"$FAKE_LOG"
    if [ "$cmd" = is-active ]; then
      [ ! -e "$FAKE_LOG.stopped.$unit" ] || exit 1
      list=${FAKE_ACTIVE:-}
    else
      list=${FAKE_ENABLED:-}
    fi
    for u in $list; do [ "$u" = "$unit" ] && exit 0; done
    exit 1 ;;
  stop)
    echo "systemctl stop $*" >>"$FAKE_LOG"
    [ -z "${FAKE_STOP_FAIL:-}" ] || exit 1
    touch "$FAKE_LOG.stopped.$1" ;;
  start)
    echo "systemctl start $*" >>"$FAKE_LOG"
    rm -f "$FAKE_LOG.stopped.$1" ;;
esac
""",
    "timeout": 'shift; exec "$@"',
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


def test_guard_treats_sglang_container_as_service(guard: Any) -> None:
    result, calls = guard(FAKE_DOCKER="sglang\\nn8n\\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "shutdown -r now" in calls


def _calls(log: str, prefix: str) -> list[int]:
    return [i for i, line in enumerate(log.splitlines()) if line.startswith(prefix)]


def test_guard_stops_an_active_enabled_engine_before_the_reboot(guard: Any) -> None:
    result, calls = guard(FAKE_ACTIVE="vllm", FAKE_ENABLED="vllm sglang")
    assert result.returncode == 0, result.stdout + result.stderr
    stop, reboot = _calls(calls, "systemctl stop vllm"), _calls(calls, "shutdown")
    assert stop and reboot and stop[0] < reboot[0], calls
    # Only the active engine is stopped.
    assert not _calls(calls, "systemctl stop sglang")


def test_guard_defers_when_active_engine_is_not_enabled(guard: Any) -> None:
    result, calls = guard(FAKE_ACTIVE="vllm", FAKE_ENABLED="")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "reboot deferred: vllm is active but not enabled; "
        "a reboot would not bring it back" in result.stdout
    )
    assert "systemctl stop" not in calls
    assert "shutdown" not in calls
    assert guard.marker.exists()


def test_guard_restarts_engine_and_defers_when_stop_fails(guard: Any) -> None:
    result, calls = guard(FAKE_ACTIVE="vllm", FAKE_ENABLED="vllm", FAKE_STOP_FAIL="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reboot deferred: could not stop vllm" in result.stdout
    assert _calls(calls, "systemctl stop vllm")
    assert _calls(calls, "systemctl start vllm")
    assert "shutdown" not in calls


def test_guard_restarts_stopped_engine_when_shutdown_fails(guard: Any) -> None:
    result, calls = guard(FAKE_ACTIVE="vllm", FAKE_ENABLED="vllm", FAKE_SHUTDOWN_RC="1")
    assert result.returncode == 1
    assert _calls(calls, "systemctl start vllm")


def test_guard_restarts_engine_once_when_killed_before_shutdown(guard: Any) -> None:
    # The fake shutdown TERMs the script, as a systemd TimeoutStartSec would.
    _, calls = guard(
        FAKE_ACTIVE="vllm",
        FAKE_ENABLED="vllm",
        FAKE_SHUTDOWN_KILL="1",
        FAKE_SHUTDOWN_RC="1",
    )
    assert len(_calls(calls, "systemctl start vllm")) == 1, calls


def test_guard_without_active_engine_reboots_without_stopping(guard: Any) -> None:
    result, calls = guard(FAKE_ACTIVE="", FAKE_ENABLED="vllm")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "systemctl stop" not in calls
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


# ── gr489996: dgx-dashboard-admin ──────────────────────────────────────────


def test_dgx_dashboard_admin_masked_only_where_the_unit_exists() -> None:
    tasks = [t for t in _tasks() if "dgx-dashboard" in t.get("tags", [])]
    assert len(tasks) == 2
    facts, mask = tasks
    assert "ansible.builtin.service_facts" in facts
    assert mask["ansible.builtin.systemd"] == {
        "name": "dgx-dashboard-admin.service",
        "state": "stopped",
        "enabled": False,
        "masked": True,
    }
    assert "'dgx-dashboard-admin.service' in ansible_facts.services" in mask["when"]
    for task in tasks:
        assert 'os_family == "linux"' in str(task["when"]), task["name"]
    # Only the privileged helper: the dashboard UI unit stays up.
    text = (_ROLE / "tasks" / "main.yml").read_text(encoding="utf-8")
    assert "name: dgx-dashboard.service" not in text


# ── task-level tags select a single concern ────────────────────────────────


def test_tag_map_selects_only_own_tasks() -> None:
    play = yaml.safe_load(
        (_REPO / "deploy" / "playbooks" / "10-hardening.yml").read_text(
            encoding="utf-8"
        )
    )
    assert len(play) == 1
    assert "tags" not in play[0]
    assert play[0]["roles"] == ["hardening"]  # a bare entry: no role-level tags

    tasks = _tasks()
    for task in tasks:
        assert "hardening" in task.get("tags", []), task["name"]

    expected = {
        "sshd": {
            "Configure sshd (macOS)",
            "Configure sshd (Linux)",
        },
        "dgx-dashboard": {
            "Read systemd units (DGX dashboard check)",
            "Stop, disable and mask dgx-dashboard-admin (Linux)",
        },
        "os-update-weekly": {
            "Remove the old daily update + reboot cron (Linux)",
            "Install host-busy + os-update-weekly guard (Linux)",
            "Render os-update-weekly service + timer (Linux)",
            "Enable or disable the os-update-weekly timer (Linux)",
            "Look for the retired nightly-reboot timer (Linux)",
            "Stop and disable the retired nightly-reboot timer (Linux)",
            "Remove the retired nightly-reboot units (Linux)",
            "Reload systemd after retiring nightly-reboot (Linux)",
        },
    }
    for tag, names in expected.items():
        assert {t["name"] for t in tasks if tag in t["tags"]} == names, tag
    selectors = set(expected)
    for task in tasks:
        assert len(selectors & set(task["tags"])) <= 1, task["name"]
    # The memory plan include hands its tags to the included tasks.
    include = next(t for t in tasks if "ansible.builtin.include_tasks" in t)
    applied = include["ansible.builtin.include_tasks"]["apply"]["tags"]
    assert {"hardening", "security", "oom-guard"} <= set(applied)
