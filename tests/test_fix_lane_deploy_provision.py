"""Fix-lane deploy provisioning (td580789): the overlay, vault key, DSN file and
cluster ssh key reach the deploy user's checkout only behind
``precis_fix_lane_deploys``, and never into the play log.

Plain YAML parse + text checks; no ansible.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent
_ROLE = _ROOT / "deploy" / "roles" / "precis_worker_agent"
_SECRET_MARKERS = (".vault-pass", "PRECIS_DATABASE_URL", ".ssh/cluster")
_SWITCH = "precis_fix_lane_deploys"


def _tasks() -> list[dict]:
    data = yaml.safe_load(
        (_ROLE / "tasks" / "provision.yml").read_text(encoding="utf-8")
    )
    return [t for t in data if isinstance(t, dict)]


def _secret_tasks() -> list[dict]:
    out = []
    for task in _tasks():
        text = yaml.safe_dump({k: v for k, v in task.items() if k != "name"})
        if any(m in text for m in _SECRET_MARKERS) or any(
            m in str(task.get("name", "")) for m in _SECRET_MARKERS
        ):
            out.append(task)
    return out


def test_secret_tasks_exist() -> None:
    named = {m: False for m in _SECRET_MARKERS}
    for task in _secret_tasks():
        blob = yaml.safe_dump(task)
        for m in named:
            named[m] = named[m] or m in blob
    assert all(named.values()), named


def test_secret_tasks_are_no_log_and_gated() -> None:
    bad = []
    for task in _secret_tasks():
        name = task.get("name")
        when = task.get("when", [])
        when = " ".join(when) if isinstance(when, list) else str(when)
        if task.get("no_log") is not True:
            bad.append(f"{name}: no no_log")
        if _SWITCH not in when:
            bad.append(f"{name}: when lacks {_SWITCH}")
    assert not bad, bad


def test_overlay_copy_tasks_are_no_log_and_gated() -> None:
    tasks = [t for t in _tasks() if "{{ precis_overlay_dir }}" in yaml.safe_dump(t)]
    assert tasks, "no task consumes precis_overlay_dir"
    for task in tasks:
        when = task.get("when", [])
        when = " ".join(when) if isinstance(when, list) else str(when)
        assert task.get("no_log") is True, task.get("name")
        assert _SWITCH in when, task.get("name")


def test_file_lookups_keep_trailing_newline() -> None:
    """lookup('file') strips the trailing newline unless rstrip=False; OpenSSH
    then rejects the placed key ("Load key ...: invalid format" -> publickey
    denied on every node)."""
    pattern = re.compile(r"lookup\(\s*'file'\s*,(?P<args>[^}]*?)\)\s*\}\}")
    seen = 0
    bad = []
    for task in _tasks():
        for module in ("ansible.builtin.copy", "copy"):
            content = (task.get(module) or {}).get("content")
            if not isinstance(content, str):
                continue
            for m in pattern.finditer(content):
                seen += 1
                if "rstrip=False" not in m.group("args"):
                    bad.append(f"{task.get('name')}: {m.group(0)}")
    assert seen >= 3, f"expected the vault-pass, key and pub lookups, saw {seen}"
    assert not bad, (
        "lookup('file') without rstrip=False drops the trailing newline; "
        f"OpenSSH fails with 'invalid format' on the placed key: {bad}"
    )


def test_defaults_switch_is_off() -> None:
    defaults = yaml.safe_load(
        (_ROLE / "defaults" / "main.yml").read_text(encoding="utf-8")
    )
    assert defaults[_SWITCH] is False


def test_deploy_script_passes_overlay_extra_vars() -> None:
    text = (_ROOT / "scripts" / "deploy").read_text(encoding="utf-8")
    assert "-e precis_overlay_dir=" in text
    assert "-e precis_vault_pass_file=" in text
