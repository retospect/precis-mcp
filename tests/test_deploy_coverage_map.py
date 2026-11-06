"""Deploy-lint: the slice lander finds a coverage map (gr567471 a).

`land_slice` launches `scripts/ship --checked` with the worker's own env
(factory_git.git_auth_env copies os.environ), so PRECIS_COVERAGE_DB_DIR in
the 20b/20g unit env reaches impacted-tests. The map reaches the dir via the
coverage_map task file; scripts/deploy refreshes the controller copy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REPO = Path(__file__).resolve().parent.parent
_DEPLOY = _REPO / "deploy"


def _tasks() -> list[dict[str, Any]]:
    path = _DEPLOY / "roles" / "precis_worker" / "tasks" / "coverage_map.yml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_copy_tasks_skip_when_the_controller_has_no_map() -> None:
    tasks = _tasks()
    copies = [t for t in tasks if "ansible.builtin.copy" in t]
    dirs = [t for t in tasks if "ansible.builtin.file" in t]
    assert len(copies) == 1 and len(dirs) == 1
    for t in (*copies, *dirs):
        assert "_coverage_src.results" in str(t["when"]), t["name"]
    assert copies[0]["ansible.builtin.copy"]["dest"].startswith(
        "{{ precis_coverage_db_dir }}/"
    )
    # Manifest after the map: a half-copy never pairs new manifest, old map.
    assert copies[0]["loop"] == [".coverage", ".coverage.precis.json"]


def test_stat_is_local_and_never_fails_the_play() -> None:
    stat = next(t for t in _tasks() if "ansible.builtin.stat" in t)
    assert stat["delegate_to"] == "localhost"
    assert stat["become"] is False


def test_20g_sets_env_and_imports_the_task_file() -> None:
    plays = yaml.safe_load(
        (_DEPLOY / "playbooks" / "20g-precis-worker-slices.yml").read_text(
            encoding="utf-8"
        )
    )
    play = plays[0]
    text = (_DEPLOY / "playbooks" / "20g-precis-worker-slices.yml").read_text(
        encoding="utf-8"
    )
    assert "'PRECIS_COVERAGE_DB_DIR': _slices_coverage_dir" in text
    imp = [
        t
        for t in play["tasks"]
        if t.get("ansible.builtin.import_role", {}).get("tasks_from") == "coverage_map"
    ]
    assert len(imp) == 1
    assert imp[0]["vars"]["precis_coverage_db_dir"] == "{{ _slices_coverage_dir }}"
    assert play["vars"]["_slices_coverage_dir"].startswith("/Users/deploy/")


def test_20b_unit_env_carries_the_dir_on_macos() -> None:
    text = (_DEPLOY / "playbooks" / "20b-precis-worker-collapsed.yml").read_text(
        encoding="utf-8"
    )
    assert (
        "combine({'PRECIS_COVERAGE_DB_DIR': precis_coverage_db_dir} "
        "if os_family == 'darwin' else {})"
    ) in text
    defaults = yaml.safe_load(
        (_DEPLOY / "roles" / "precis_worker" / "defaults" / "main.yml").read_text(
            encoding="utf-8"
        )
    )
    assert (
        defaults["precis_coverage_db_dir"]
        == (
            yaml.safe_load(
                (_DEPLOY / "playbooks" / "20g-precis-worker-slices.yml").read_text(
                    encoding="utf-8"
                )
            )[0]["vars"]["_slices_coverage_dir"]
        )
    )


def test_deploy_fetch_is_best_effort() -> None:
    text = (_REPO / "scripts" / "deploy").read_text(encoding="utf-8")
    i = text.index('"${REPO_ROOT}/scripts/impacted-tests" --fetch-db')
    # the failure arm continues the deploy (warn, no die/exit)
    arm = text[i : text.index("\nfi\n", i)]
    assert "|| say " in arm and "die" not in arm and "exit" not in arm
