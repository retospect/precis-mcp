"""gr465126: keep /mnt visible to node_exporter's filesystem collector.

Ubuntu's prometheus-node-exporter package excludes /mnt via a default
mount-points-exclude regex, and the role left /etc/default/
prometheus-node-exporter at ARGS="". /mnt/cluster then produces no
node_filesystem_* series: NFSMountAbsent fires on every Linux host and
NFSMountHung / NFSMountFullOrEmpty can never fire. The role renders the
defaults file with the packaged exclude list minus `mnt`; the rendered ARGS
is checked against real mount paths, not just for the flag's presence.
"""

from __future__ import annotations

import re
from pathlib import Path

import jinja2
import yaml

_ROLE = Path(__file__).resolve().parent.parent / "deploy" / "roles" / "node_exporter"


def _render(**over: object) -> str:
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    src = (_ROLE / "templates" / "prometheus-node-exporter.default.j2").read_text(
        encoding="utf-8"
    )
    defaults = yaml.safe_load(
        (_ROLE / "defaults" / "main.yml").read_text(encoding="utf-8")
    )
    return env.from_string(src).render(**{**defaults, "ansible_managed": "x", **over})


def _args(rendered: str) -> str:
    m = re.search(r'^ARGS="(.*)"$', rendered, re.MULTILINE)
    assert m, "no ARGS line rendered"
    return m.group(1)


def _exclude(rendered: str) -> str:
    m = re.search(
        r"--collector\.filesystem\.mount-points-exclude=(\S+)", _args(rendered)
    )
    assert m, "no mount-points-exclude flag in ARGS"
    return m.group(1)


def test_exclude_regex_keeps_mnt_and_still_drops_proc_and_sys() -> None:
    excl = _exclude(_render())
    assert re.match(excl, "/mnt/cluster") is None
    assert re.match(excl, "/proc") is not None
    assert re.match(excl, "/sys") is not None


def test_extra_args_are_appended_not_replaced() -> None:
    args = _args(_render(node_exporter_extra_args=["--collector.systemd"]))
    assert "--collector.systemd" in args
    assert "--collector.filesystem.mount-points-exclude=" in args


def test_linux_tasks_render_the_defaults_file_and_restart() -> None:
    tasks = (_ROLE / "tasks" / "main.yml").read_text(encoding="utf-8")
    yaml.safe_load(tasks)
    assert "src: prometheus-node-exporter.default.j2" in tasks
    assert "dest: /etc/default/prometheus-node-exporter" in tasks
    assert "notify: restart node_exporter" in tasks

    handlers = yaml.safe_load(
        (_ROLE / "handlers" / "main.yml").read_text(encoding="utf-8")
    )
    linux = [
        h
        for h in handlers
        if h.get("listen") == "restart node_exporter"
        and h.get("when") == 'os_family == "linux"'
    ]
    assert linux, "no Linux restart handler for the changed defaults file"
    unit = linux[0]["ansible.builtin.systemd"]
    assert unit["name"] == "prometheus-node-exporter"
    assert unit["state"] == "restarted"
