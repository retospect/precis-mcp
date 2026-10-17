"""Every apt task under deploy/ retries transient mirror/lock failures (gr494680).

A single Canonical round-robin IP timing out fails ``apt update`` outright;
retries have always succeeded, so each apt task carries until/retries/delay
and a dpkg ``lock_timeout``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
APT = {"ansible.builtin.apt", "apt"}
# package/apt_repository reach apt too but take no lock_timeout argument.
VIA_APT = APT | {
    "ansible.builtin.package",
    "package",
    "ansible.builtin.apt_repository",
    "apt_repository",
}


def _tasks(node):
    if isinstance(node, list):
        for item in node:
            yield from _tasks(item)
    elif isinstance(node, dict):
        yield node
        for key in (
            "block",
            "rescue",
            "always",
            "tasks",
            "pre_tasks",
            "post_tasks",
            "handlers",
        ):
            if key in node:
                yield from _tasks(node[key])


def _apt_tasks():
    out = []
    for path in sorted(DEPLOY.rglob("*.yml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue  # Jinja-templated files are covered by other tests
        for task in _tasks(doc):
            if VIA_APT & task.keys():
                out.append(
                    pytest.param(
                        task, id=f"{path.relative_to(DEPLOY)}::{task.get('name')}"
                    )
                )
    return out


def test_apt_tasks_found():
    assert len(_apt_tasks()) >= 14


@pytest.mark.parametrize("task", _apt_tasks())
def test_apt_task_retries(task):
    assert task.get("retries", 0) >= 3, "apt task needs retries: 3"
    assert task.get("delay", 0) >= 10, "apt task needs delay: 10"
    register = task.get("register")
    assert register, "apt task needs register: for until"
    assert str(task.get("until")).split()[0] == register, (
        "until must test the registered result"
    )
    module = next(iter(APT & task.keys()), None)
    if module:
        assert task[module].get("lock_timeout", 0) >= 60, "apt task needs lock_timeout"
