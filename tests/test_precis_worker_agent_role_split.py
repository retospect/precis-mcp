"""Regression pin — §L-a's precis_worker_agent role split.

``deploy/roles/precis_worker_agent/tasks/main.yml`` was split into
``provision.yml`` (everything 20b, the collapsed-worker playbook, wants to
reuse via ``tasks_from: provision``) and ``units.yml`` (the split unit's own
plist/service render+load, which 20b deliberately does NOT want). Nothing
enforces that split stays intact except convention — a later edit could
silently re-inline tasks into ``main.yml`` (reintroducing the unit render
20b doesn't want) or reorder the two imports without anyone noticing. Pin
the exact shape via a static YAML parse (no ansible needed).
"""

from __future__ import annotations

from pathlib import Path

import yaml

_ROLE_DIR = (
    Path(__file__).resolve().parent.parent
    / "deploy"
    / "roles"
    / "precis_worker_agent"
    / "tasks"
)


def test_main_yml_is_exactly_the_two_import_tasks_in_order() -> None:
    """``main.yml`` must be ONLY ``import_tasks: provision.yml`` then
    ``import_tasks: units.yml`` — provisioning before unit rendering, so a
    fresh run of playbook 37 (which imports this role whole) still lands
    every provisioning side-effect (colima, SOUL, mcp.json, anki, the
    quest_loop_reconcile seed) before the unit that depends on some of them
    (the SOUL-copy `notify: restart precis-worker-agent` handler included)."""
    main_yml = _ROLE_DIR / "main.yml"
    tasks = yaml.safe_load(main_yml.read_text(encoding="utf-8"))
    assert isinstance(tasks, list), "main.yml must be a plain task list"
    assert len(tasks) == 2, (
        "main.yml must contain exactly two entries (the two import_tasks) — "
        f"found {len(tasks)}: re-inlining a task here defeats the split "
        "20b relies on (`tasks_from: provision`)"
    )
    imported = [t.get("ansible.builtin.import_tasks") for t in tasks]
    assert imported == ["provision.yml", "units.yml"], (
        "main.yml must import provision.yml THEN units.yml, in that order "
        f"— found {imported}"
    )


def test_provision_and_units_files_exist_and_are_task_lists() -> None:
    """Both halves of the split must actually exist and parse as ansible
    task lists — a rename/typo in main.yml's import_tasks targets would
    otherwise only surface at ansible-playbook run time."""
    for name in ("provision.yml", "units.yml"):
        path = _ROLE_DIR / name
        assert path.is_file(), f"{name} missing from {_ROLE_DIR}"
        tasks = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(tasks, list) and tasks, (
            f"{name} must be a non-empty task list"
        )


def test_units_yml_owns_the_split_unit_render_not_provision_yml() -> None:
    """The one thing 20b must NOT get from ``tasks_from: provision``: the
    split unit's own plist/service render. Pins the boundary the split
    exists to enforce, not just that two files exist."""
    provision_text = (_ROLE_DIR / "provision.yml").read_text(encoding="utf-8")
    units_text = (_ROLE_DIR / "units.yml").read_text(encoding="utf-8")
    assert "precis-worker-agent.plist.j2" not in provision_text
    assert "precis-worker-agent.service.j2" not in provision_text
    assert "precis-worker-agent.plist.j2" in units_text
    assert "precis-worker-agent.service.j2" in units_text
