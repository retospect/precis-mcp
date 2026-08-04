"""Regression pin — §L-b's precis_worker role split (mirrors the
precis_worker_agent split pinned in test_precis_worker_agent_role_split.py).

``deploy/roles/precis_worker/tasks/main.yml`` was split into
``provision.yml`` (venv/deps/dirs/§L service_config seed — what 20b reuses
via ``tasks_from: provision``) and ``units.yml`` (the split
``--profile system`` unit render+load+status, playbook 20's rollback path).
Without this pin, re-inlining a task into main.yml would silently make 20b
double-render com.precis.worker split-then-collapsed on every deploy again.

Also pins the two 20b regressions found 2026-08-04: the playbook must reach
both roles via ``import_role`` with ``tasks_from: provision`` (in a
``roles:`` list entry, ``tasks_from`` silently degrades to a role var and
the FULL role runs — that bug resurrected the retired split agent unit),
and must not run either role's units.yml.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parent.parent
_ROLE_DIR = _REPO / "deploy" / "roles" / "precis_worker" / "tasks"
_20B = _REPO / "deploy" / "playbooks" / "20b-precis-worker-collapsed.yml"


def test_main_yml_is_exactly_the_two_import_tasks_in_order() -> None:
    tasks = yaml.safe_load((_ROLE_DIR / "main.yml").read_text(encoding="utf-8"))
    assert isinstance(tasks, list), "main.yml must be a plain task list"
    assert len(tasks) == 2, (
        "main.yml must contain exactly two entries (the two import_tasks) — "
        f"found {len(tasks)}"
    )
    imported = [t.get("ansible.builtin.import_tasks") for t in tasks]
    assert imported == ["provision.yml", "units.yml"], (
        f"main.yml must import provision.yml THEN units.yml — found {imported}"
    )


def test_provision_and_units_files_exist_and_are_task_lists() -> None:
    for name in ("provision.yml", "units.yml"):
        path = _ROLE_DIR / name
        assert path.is_file(), f"{name} missing from {_ROLE_DIR}"
        tasks = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(tasks, list) and tasks, (
            f"{name} must be a non-empty task list"
        )


def test_units_yml_owns_the_split_unit_render_not_provision_yml() -> None:
    provision_text = (_ROLE_DIR / "provision.yml").read_text(encoding="utf-8")
    units_text = (_ROLE_DIR / "units.yml").read_text(encoding="utf-8")
    assert "precis-worker.plist.j2" not in provision_text
    assert "precis-worker.service.j2" not in provision_text
    assert "precis-worker.plist.j2" in units_text
    assert "precis-worker.service.j2" in units_text


def test_20b_uses_import_role_tasks_from_provision_for_both_roles() -> None:
    """In a ``roles:`` list entry ``tasks_from`` is NOT honored (it becomes
    a plain role var and the full main.yml runs) — 20b must use task-level
    ``ansible.builtin.import_role`` with ``tasks_from: provision`` for both
    worker roles, and must not carry a ``roles:`` section at all."""
    plays = yaml.safe_load(_20B.read_text(encoding="utf-8"))
    assert isinstance(plays, list) and len(plays) == 1
    play = plays[0]
    assert "roles" not in play, (
        "20b must not use a roles: section — tasks_from silently degrades "
        "to a role var there (the 2026-08-04 split-unit resurrection bug)"
    )
    provision_imports = {
        t["ansible.builtin.import_role"]["name"]: t["ansible.builtin.import_role"]
        for t in play.get("tasks", [])
        if isinstance(t, dict)
        and isinstance(t.get("ansible.builtin.import_role"), dict)
        and t["ansible.builtin.import_role"].get("tasks_from") == "provision"
    }
    assert {"precis_worker", "precis_worker_agent"} <= set(provision_imports), (
        "20b must import BOTH worker roles with tasks_from: provision — "
        f"found only {sorted(provision_imports)}"
    )
