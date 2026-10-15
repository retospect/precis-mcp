"""gr489462: no playbook may pip-install precis-mcp from GitHub main by default.

Twice on 2026-10-15 a hand-run playbook installed ``main`` into
/opt/precis/venv (the roles' ``*_git_ref: main`` defaults) and the workers
crash-looped on "database is N migration(s) behind". The defaults are gone;
every install path now carries a ``fail`` task that refuses when its ref var
resolves empty, ahead of the pip/uv install task. These structure tests pin
both halves: no ``default("main")`` survives on a ref that feeds a
``precis-mcp@`` install, and each install-bearing tasks file fails fast with
the refusal message first.

``llamacpp_git_ref`` (still ``master``) is out of scope — it builds the
llama.cpp repo, not precis-mcp.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
_ROLES = _DEPLOY / "roles"

#: tasks file -> the ref var its precis-mcp install consumes.
_INSTALL_REFS = {
    _ROLES / "precis_worker/tasks/provision.yml": "precis_worker_git_ref",
    _ROLES / "precis_web/tasks/install.yml": "precis_web_git_ref",
    _ROLES / "precis_embedder/tasks/main.yml": "precis_embedder_git_ref",
    _ROLES / "autocatpath/tasks/main.yml": "precis_worker_git_ref",
    _ROLES / "asa_bot/tasks/main.yml": "asa_git_ref",
    _ROLES / "asa_slack/tasks/main.yml": "asa_git_ref",
}

_INSTALL_RE = re.compile(
    r"precis-mcp\[.*?\] @ git\+https://github\.com/retospect/precis-mcp@"
)


def _tasks(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [t for t in data if isinstance(t, dict)]


def _task_text(task: dict[str, Any]) -> str:
    return yaml.safe_dump(task)


def test_install_roles_have_no_git_ref_main_default() -> None:
    """No role may carry a ``*_git_ref`` default that resolves to main."""
    offenders: list[str] = []
    for defaults in _ROLES.glob("*/defaults/main.yml"):
        for line in defaults.read_text(encoding="utf-8").splitlines():
            if re.match(
                r"^\s*\w*git_ref\s*:\s*.*main\b", line
            ) and not line.lstrip().startswith("#"):
                offenders.append(f"{defaults.name}: {line.strip()}")
    assert not offenders, offenders


def test_install_sites_have_no_main_fallback() -> None:
    """A ``| default("main")`` on an install ref silently resurrects the bug."""
    for path in _INSTALL_REFS:
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            assert (
                'git_ref | default("main")' not in line
                and "git_ref | default('main')" not in line
            ), f"{path}:{i}"


def test_refuse_task_precedes_every_precis_mcp_install() -> None:
    raw_by_path = {p: p.read_text(encoding="utf-8") for p in _INSTALL_REFS}
    for path, var in _INSTALL_REFS.items():
        tasks = _tasks(path)
        refuse_at = None
        for i, t in enumerate(tasks):
            body = _task_text(t)
            if (
                "ansible.builtin.fail" in t
                and "Refuse an unpinned precis-mcp install" in t.get("name", "")
            ):
                assert var in body, f"{path}: refusal does not name {var}"
                # The when clause must fire on an UNSET/empty ref — check the
                # raw source (safe_dump reflows folded scalars).
                assert re.search(
                    rf"{var}\s*\|\s*default\(''\)\s*\)?\s*\|\s*length\s*==\s*0",
                    raw_by_path[path],
                ), f"{path}: refusal must fire on an unset/empty {var}"
                refuse_at = i
            if _INSTALL_RE.search(body):
                assert refuse_at is not None, (
                    f"{path}: precis-mcp install at task {i} has no refusal above it"
                )
        assert refuse_at is not None, f"{path}: no refusal task found"


def test_asa_code_refresh_refuses_standalone_without_a_pin() -> None:
    """Play 32 run by hand used to install main; now it needs the pin or -e."""
    doc = yaml.safe_load(
        (_DEPLOY / "playbooks" / "32-asa-code-refresh.yml").read_text(encoding="utf-8")
    )
    play = next(p for p in doc if isinstance(p, dict) and p.get("tasks"))
    asa_ref = play["vars"]["_asa_ref"]
    assert "default('main')" not in asa_ref and 'default("main")' not in asa_ref
    refuse = [
        t
        for t in play["tasks"]
        if "Refuse an unpinned precis-mcp install" in t.get("name", "")
    ]
    assert len(refuse) == 1
    assert "_asa_ref | length == 0" in yaml.safe_dump(refuse[0])


def test_redeploy_step0_still_resolves_main_deliberately() -> None:
    """The sanctioned path keeps its branch->sha resolver: scripts/deploy's
    REF=main relies on step-0's ls-remote default — that IS the pin, not the
    bug (the leaf roles refuse before they would install it unpinned)."""
    src = (_DEPLOY / "redeploy-precis.yml").read_text(encoding="utf-8")
    assert "git ls-remote https://github.com/retospect/precis-mcp" in src
    assert "precis_worker_git_ref | default('main')" in src
