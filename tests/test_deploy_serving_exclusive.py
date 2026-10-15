"""The sglang and vllm roles stop and disable each other's unit (gr485635).

Both serve on the same port and size against the same GB10 unified memory.
An enabled vllm unit came back at boot beside sglang on 2026-10-14, so
applying either role must leave the other's unit stopped AND disabled, and
must do so before its own unit starts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_ROLES = Path(__file__).resolve().parent.parent / "deploy" / "roles"
_PAIRS = [("sglang", "vllm"), ("vllm", "sglang")]


def _tasks(role: str) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = yaml.safe_load(
        (_ROLES / role / "tasks/main.yml").read_text(encoding="utf-8")
    )
    return tasks


def _defaults(role: str) -> dict[str, Any]:
    defaults: dict[str, Any] = yaml.safe_load(
        (_ROLES / role / "defaults/main.yml").read_text(encoding="utf-8")
    )
    return defaults


def _index(tasks: list[dict[str, Any]], pred: Any) -> int:
    hits = [i for i, task in enumerate(tasks) if pred(task)]
    assert len(hits) == 1, hits
    return hits[0]


@pytest.mark.parametrize(("role", "other"), _PAIRS)
def test_default_supersedes_the_other_role(role: str, other: str) -> None:
    defaults = _defaults(role)
    assert defaults[f"{role}_supersedes_service"] == other
    assert _defaults(other)[f"{other}_service_name"] == other


@pytest.mark.parametrize(("role", "other"), _PAIRS)
def test_role_stops_and_disables_the_other_unit_before_starting(
    role: str, other: str
) -> None:
    tasks = _tasks(role)
    var = f"{role}_supersedes_service"
    stop = _index(
        tasks,
        lambda t: (
            t.get("ansible.builtin.systemd", {}).get("name") == f"{{{{ {var} }}}}"
        ),
    )
    module = tasks[stop]["ansible.builtin.systemd"]
    assert module["state"] == "stopped"
    assert module["enabled"] is False
    assert tasks[stop]["become"] is True
    # A real failure (unit present but systemctl refuses) must surface.
    assert "failed_when" not in tasks[stop]
    assert "ignore_errors" not in tasks[stop]

    start = _index(
        tasks,
        lambda t: (
            t.get("ansible.builtin.systemd", {}).get("name")
            == f"{{{{ {role}_service_name }}}}"
            and t["ansible.builtin.systemd"].get("state") == "started"
        ),
    )
    deploy = _index(tasks, lambda t: "ansible.builtin.template" in t)
    assert stop < deploy < start


@pytest.mark.parametrize(("role", "other"), _PAIRS)
def test_absent_unit_is_skipped_not_failed(role: str, other: str) -> None:
    """Idempotent on a host that never had the other unit installed."""
    tasks = _tasks(role)
    var = f"{role}_supersedes_service"
    probe = _index(tasks, lambda t: "ansible.builtin.stat" in t and var in str(t))
    stat = tasks[probe]["ansible.builtin.stat"]
    assert stat["path"] == f"/etc/systemd/system/{{{{ {var} }}}}.service"
    registered = tasks[probe]["register"]

    stop = _index(
        tasks,
        lambda t: (
            t.get("ansible.builtin.systemd", {}).get("name") == f"{{{{ {var} }}}}"
        ),
    )
    assert probe < stop
    when = tasks[stop]["when"]
    assert f"{registered}.stat.exists" in when
    assert f"{var} | length > 0" in when
    # The other role installs its unit where the probe looks.
    other_deploy = _index(_tasks(other), lambda t: "ansible.builtin.template" in t)
    dest = _tasks(other)[other_deploy]["ansible.builtin.template"]["dest"]
    assert dest == f"/etc/systemd/system/{{{{ {other}_service_name }}}}.service"
