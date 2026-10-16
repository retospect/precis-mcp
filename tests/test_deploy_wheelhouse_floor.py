"""gr489600: the wheelhouse must carry autocatpath >= the catalyst floor
BEFORE the ``precis-mcp[catalyst]`` pip install runs.

20c (and every play that pulls ``precis_worker`` provision or
``precis_web`` install on an autocatpath_plugin host) installs with
``--no-sources --find-links /opt/precis/wheels``. A fresh compute host's
wheelhouse is empty, and the ``catalyst`` extra's floor
(``autocatpath>=0.29.0`` — not on PyPI, release-gated) then resolves
nothing or a too-old wheel. The fix's guard: find the wheels, refuse when
none satisfies the floor, name play 44's wheel push in the message.

Structure test only — ansible-side version semantics (`version` test)
were verified live during development.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_ROLES = Path(__file__).resolve().parent.parent / "deploy" / "roles"

_INSTALLERS = (
    _ROLES / "precis_worker" / "tasks" / "provision.yml",
    _ROLES / "precis_web" / "tasks" / "install.yml",
)


def _tasks(path: Path) -> list[dict[str, Any]]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _names(tasks: list[dict[str, Any]]) -> list[str]:
    return [t.get("name", "") for t in tasks]


def test_each_catalyst_installer_guards_the_wheelhouse_floor() -> None:
    for path in _INSTALLERS:
        tasks = _tasks(path)
        names = _names(tasks)
        find_i = next(i for i, n in enumerate(names) if "Find autocatpath wheels" in n)
        refuse_i = next(
            i
            for i, n in enumerate(names)
            if "wheelhouse cannot satisfy the catalyst floor" in n
        )
        # The refuse must sit immediately before the catalyst-bearing install:
        # provision's one install adds `,catalyst` on plugin hosts; the web's
        # is named for it.
        install_body = yaml.safe_dump(tasks[refuse_i + 1])
        assert "precis-mcp[" in install_body and "catalyst" in install_body, path.name
        assert find_i < refuse_i, path.name
        refuse = tasks[refuse_i]
        when = yaml.safe_dump(refuse["when"])
        assert "autocatpath_plugin" in when
        assert "precis_deploy_changed" in when
        assert "'version'" in when and "'>='" in when
        msg = refuse["ansible.builtin.fail"]["msg"]
        assert "44" in msg and "autocatpath_wheel" in msg


def test_floor_matches_the_pyproject_pin() -> None:
    """autocatpath_min_version must track pyproject's catalyst floor."""
    defaults = yaml.safe_load(
        (_ROLES / "autocatpath" / "defaults" / "main.yml").read_text(encoding="utf-8")
    )
    floor = defaults["autocatpath_min_version"]
    pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert f"autocatpath>={floor}" in pyproject, (
        "pyproject's catalyst floor and autocatpath_min_version drifted apart"
    )
