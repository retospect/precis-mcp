"""Keep the Ollama purge scoped to Ollama and report the relevant filesystem."""

from pathlib import Path

import pytest
import yaml
from jinja2 import Template

_TASKS = (
    Path(__file__).resolve().parents[1] / "deploy/roles/ollama_purge/tasks/main.yml"
)


def test_homebrew_purge_disables_package_maintenance() -> None:
    tasks = yaml.safe_load(_TASKS.read_text(encoding="utf-8"))
    uninstall = next(task for task in tasks if "community.general.homebrew" in task)
    assert uninstall["community.general.homebrew"] == {
        "name": "ollama",
        "state": "absent",
        "update_homebrew": False,
        "upgrade_all": False,
    }
    assert uninstall["environment"]["HOMEBREW_NO_AUTO_UPDATE"] == "1"
    assert uninstall["environment"]["HOMEBREW_NO_AUTOREMOVE"] == "1"


@pytest.mark.parametrize(
    ("os_family", "mount"), [("darwin", "/System/Volumes/Data"), ("linux", "/")]
)
def test_disk_report_uses_data_volume(os_family: str, mount: str) -> None:
    tasks = yaml.safe_load(_TASKS.read_text(encoding="utf-8"))
    report = next(task for task in tasks if task.get("register") == "ollama_purge_df")
    argv = report["ansible.builtin.command"]["argv"]
    assert [Template(arg).render(os_family=os_family) for arg in argv] == [
        "df",
        "-h",
        mount,
    ]
