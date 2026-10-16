"""gr489600: the wheelhouse must carry autocatpath >= the catalyst floor
BEFORE the ``precis-mcp[catalyst]`` pip install runs.

20c (and every play that pulls ``precis_worker`` provision or
``precis_web`` install on an autocatpath_plugin host) installs with
``--no-sources --find-links /opt/precis/wheels``. A fresh compute host's
wheelhouse is empty, and the ``catalyst`` extra's floor
(``autocatpath>=0.29.0`` — not on PyPI, release-gated) then resolves
nothing or a too-old wheel. The fix's guard: find the wheels, refuse when
none satisfies the floor, name the seed route in the message — play 44's
wheel push on the Linux compute node; on darwin plugin hosts play 44
can't reach them (the autocatpath role is linux-only), so the message
names the same play's ``wheelhouse_seed`` re-run with ``-e
autocatpath_wheel=``, which ``scripts/deploy`` drives (gr491948).

Structure test only — ansible-side version semantics (`version` test)
were verified live during development. The refusal ``msg`` is rendered
with plain jinja2 for the per-OS route assertions (it carries no
ansible-only filters).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jinja2
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


def _refuse_task(path: Path) -> dict[str, Any]:
    return next(
        t
        for t in _tasks(path)
        if "wheelhouse cannot satisfy the catalyst floor" in t.get("name", "")
    )


def _render_msg(task: dict[str, Any], os_family: str) -> str:
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    return env.from_string(task["ansible.builtin.fail"]["msg"]).render(
        os_family=os_family
    )


def test_refusal_message_names_the_darwin_seed_route() -> None:
    """gr491948: on darwin plugin hosts the play-44 hint is a dead end — the
    autocatpath role's first task asserts linux, so its wheel tasks never
    run there. The rendered darwin message must name the route that works:
    re-run the same play with ``-e autocatpath_wheel=`` (the wheelhouse_seed
    include lands it), which ``scripts/deploy`` fills from the catpath
    checkout."""
    for path in _INSTALLERS:
        rendered = _render_msg(_refuse_task(path), "darwin")
        assert "autocatpath_wheel=" in rendered, path.name
        assert "scripts/deploy" in rendered, path.name
        # No actionable play-44 push on darwin — that's the hint that
        # stranded melchior.
        assert "44-autocatpath.yml" not in rendered, path.name
        assert "--limit" not in rendered, path.name


def test_refusal_message_keeps_play44_on_linux() -> None:
    """provision.yml's refusal also fires on linux plugin hosts; there the
    play-44 wheel push remains the documented route."""
    provision = _ROLES / "precis_worker" / "tasks" / "provision.yml"
    rendered = _render_msg(_refuse_task(provision), "linux")
    assert "44-autocatpath.yml" in rendered
    assert "autocatpath_wheel=" in rendered


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
