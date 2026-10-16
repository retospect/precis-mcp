"""Regression gate for gr489397: managed nfs-keepalive timer on nfs_clients.

The NFS server's macOS 26.4 nfsd never sends FIN for a client-closed
transport; a Linux client that idle-drops (sunrpc ~5 min) wedges in
FIN-WAIT-2 and the hard shared mount hangs every access in D-state. The
interim fix was a TRANSIENT ``systemd-run`` timer — a reboot cleared it and
the client re-wedged in the gap. The durable fix is a managed
``nfs-keepalive.{service,timer}`` in the nfs_client role that statfs's the
shared mount every 60 s, well under the idle-drop window.

A transient unit of the same name may already be loaded on some hosts, so
the role must stop/reset-failed it BEFORE installing the file-backed units
(same-name transient units shadow files), then daemon-reload, install and
enable the timer. Pure yaml/jinja walk — no ansible needed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jinja2
import yaml

_REPO = Path(__file__).resolve().parent.parent
_ROLE = _REPO / "deploy" / "roles" / "nfs_client"
_PLAYBOOK = _REPO / "deploy" / "playbooks" / "01-nfs.yml"


def _tasks() -> list[dict[str, Any]]:
    tasks = yaml.safe_load((_ROLE / "tasks" / "main.yml").read_text(encoding="utf-8"))
    assert isinstance(tasks, list)
    return tasks


def _render(name: str, **context: Any) -> str:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(_ROLE / "templates"),
        undefined=jinja2.StrictUndefined,
    )
    return env.get_template(name).render(ansible_managed="managed", **context)


def _keepalive_tasks() -> list[dict[str, Any]]:
    return [t for t in _tasks() if "nfs-keepalive" in str(t.get("name", ""))]


def test_service_unit_stats_the_shared_mount() -> None:
    """Type=oneshot, 20 s timeout, and the mount path comes from the role's
    shared_mount variable — never a literal."""
    raw = (_ROLE / "templates" / "nfs-keepalive.service.j2").read_text(encoding="utf-8")
    assert "Type=oneshot" in raw
    assert "/usr/bin/timeout 20 /usr/bin/stat -f {{ shared_mount }}" in raw
    assert "/mnt/cluster" not in raw, "mount path must stay the shared_mount var"
    rendered = _render("nfs-keepalive.service.j2", shared_mount="/mnt/cluster")
    assert "ExecStart=/usr/bin/timeout 20 /usr/bin/stat -f /mnt/cluster" in rendered


def test_timer_unit_contents() -> None:
    """60 s from boot and 60 s after each run — under the sunrpc ~5 min
    idle-drop so the transport never closes."""
    rendered = _render("nfs-keepalive.timer.j2")
    assert "OnBootSec=60s" in rendered
    assert "OnUnitActiveSec=60s" in rendered
    assert "WantedBy=timers.target" in rendered


def test_transient_stop_precedes_install() -> None:
    """A same-name transient systemd-run unit shadows the managed files
    while loaded — it must be stopped before they land."""
    names = [str(t.get("name", "")) for t in _tasks()]

    def idx(frag: str) -> int:
        return next(i for i, n in enumerate(names) if frag in n)

    assert (
        idx("Stop any transient")
        < idx("Clear failed")
        < idx("Reload systemd")
        < idx("Install nfs-keepalive")
        < idx("Enable + start")
    )

    stop_task = _tasks()[idx("Stop any transient")]
    assert "systemctl stop nfs-keepalive.timer nfs-keepalive.service" in str(
        stop_task["ansible.builtin.command"]
    )
    # "not loaded" must not fail the run: the transient unit is absent on
    # hosts that were never patched by hand.
    assert "not loaded" in str(stop_task.get("failed_when", ""))


def test_timer_is_enabled_and_started() -> None:
    task = next(t for t in _tasks() if "Enable + start" in str(t.get("name", "")))
    unit = task["ansible.builtin.systemd"]
    assert unit["name"] == "nfs-keepalive.timer"
    assert unit["enabled"] is True
    assert unit["state"] == "started"
    assert unit.get("daemon_reload") is True


def test_keepalive_is_linux_only_on_nfs_clients() -> None:
    """Every keepalive task is Linux-gated, and the role only ever runs on
    the nfs_clients group (01-nfs.yml)."""
    tasks = _keepalive_tasks()
    assert tasks, "no nfs-keepalive tasks found in the nfs_client role"
    for task in tasks:
        assert 'os_family == "linux"' in str(task.get("when", "")), (
            f"keepalive task not Linux-gated: {task.get('name')}"
        )

    plays = yaml.safe_load(_PLAYBOOK.read_text(encoding="utf-8"))
    client_plays = [p for p in plays if "nfs_client" in p.get("roles", [])]
    assert client_plays and all(
        p.get("hosts") == "nfs_clients" for p in client_plays
    ), "nfs_client role must run only on the nfs_clients group"
