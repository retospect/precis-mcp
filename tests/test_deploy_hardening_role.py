"""Pins for deploy/roles/hardening tasks that touch the Linux compute hosts.

- gr489603: the templated sshd_config carries an sftp Subsystem with the
  right sftp-server path per OS family, and both sshd tasks are tagged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jinja2
import yaml

_REPO = Path(__file__).resolve().parent.parent
_ROLE = _REPO / "deploy" / "roles" / "hardening"


def _tasks() -> list[dict[str, Any]]:
    tasks = yaml.safe_load((_ROLE / "tasks" / "main.yml").read_text(encoding="utf-8"))
    assert isinstance(tasks, list)
    return tasks


def _render_template(name: str, **context: Any) -> str:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(_ROLE / "templates"),
        undefined=jinja2.StrictUndefined,
    )
    return env.get_template(name).render(ansible_managed="managed", **context)


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
