"""Managed agent-start links must use committed primary-checkout code.

All repositories and homes are temporary. Tests neither launch agents nor
run a vendor installer, fetch from a remote, or touch the real user's links.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from jinja2 import Environment

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: symlinks")
REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "deploy/roles/agent_clis/files/agent_cli.py"
SCRIPT_PATHS = (
    "scripts/agent-start",
    "scripts/claude-tree",
    "scripts/codex-tree",
    "scripts/kimi-tree",
    "scripts/devin-tree",
    "scripts/lib/agent-tree.sh",
)

spec = importlib.util.spec_from_file_location("agent_launcher_update_tool", SOURCE)
assert spec is not None and spec.loader is not None
tool = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = tool
spec.loader.exec_module(tool)


@dataclass
class Installation:
    checkout: Path
    home: Path

    @property
    def destination(self) -> Path:
        return self.home / ".local/bin/agent-start"

    @property
    def target(self) -> Path:
        return self.checkout / "scripts/agent-start"

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=cwd or self.checkout,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            timeout=20,
        ).stdout.strip()

    def manage(self, mode: str, *, discover: bool = False) -> dict[str, Any]:
        settings = {} if discover else {"checkout": str(self.checkout)}
        return tool.manage_launcher(settings, mode, home=self.home)

    def link_wrapper(self, name: str, target: Path, *, relative: bool = False) -> Path:
        link = self.home / ".local/bin" / name
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(os.path.relpath(target, link.parent) if relative else target)
        return link


def init_source(path: Path, home: Path) -> Installation:
    path.mkdir()
    installation = Installation(path, home)
    installation.git("init", "-b", "main")
    installation.git("config", "user.name", "Launcher installer test")
    installation.git("config", "user.email", "installer@example.invalid")
    for relative in SCRIPT_PATHS:
        script = path / relative
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        if relative != "scripts/lib/agent-tree.sh":
            script.chmod(0o755)
    installation.git("add", ".")
    message = path.parent / f"{path.name}-commit-message"
    message.write_text("committed launcher fixture\n", encoding="utf-8")
    installation.git("commit", "-F", str(message))
    return installation


@pytest.fixture
def installation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Installation:
    tmp_path = tmp_path.resolve()
    home = tmp_path / "home"
    home.mkdir()
    for key in list(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    return init_source(tmp_path / "stable checkout", home)


def test_report_does_not_create_the_bin_directory(installation: Installation) -> None:
    row = installation.manage("report")
    assert row["cli"] == "agent-start"
    assert row["path"] == str(installation.destination)
    assert (row["version"], row["pin"], row["min"]) == (None, "", "")
    assert not (installation.home / ".local").exists()


def test_update_creates_link_and_second_update_is_a_noop(
    installation: Installation,
) -> None:
    row = installation.manage("update")
    assert row["ok"] and row["action"] == "updated", row
    assert installation.destination.is_symlink()
    assert installation.destination.resolve() == installation.target
    before = installation.destination.lstat()
    current = installation.manage("update")
    assert current["ok"] and current["action"] == "current", current
    after = installation.destination.lstat()
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)


@pytest.mark.parametrize("broken", [False, True])
def test_report_preserves_drift_then_update_repairs_symlink(
    installation: Installation, broken: bool
) -> None:
    other = installation.home / "old-launcher"
    if not broken:
        other.write_text("unrelated target contents\n", encoding="utf-8")
    installation.link_wrapper("agent-start", other)
    before = os.readlink(installation.destination)
    installation.manage("report")
    assert os.readlink(installation.destination) == before
    row = installation.manage("update")
    assert row["ok"] and row["action"] == "updated", row
    assert installation.destination.resolve() == installation.target
    if not broken:
        assert other.read_text(encoding="utf-8") == "unrelated target contents\n"


@pytest.mark.parametrize("mode", ["report", "update"])
@pytest.mark.parametrize("kind", ["file", "directory"])
def test_user_owned_destination_is_preserved(
    installation: Installation, mode: str, kind: str
) -> None:
    destination = installation.destination
    destination.parent.mkdir(parents=True)
    if kind == "file":
        destination.write_text("user's own launcher\n", encoding="utf-8")
    else:
        destination.mkdir()
    row = installation.manage(mode)
    assert row["ok"] is False, row
    assert not destination.is_symlink()
    if kind == "file":
        assert destination.read_text(encoding="utf-8") == "user's own launcher\n"
    else:
        assert destination.is_dir()


@pytest.mark.parametrize(
    ("wrapper", "script"),
    [
        ("kimi-wt", "kimi-tree"),
        ("devin-wt", "devin-tree"),
        ("agent-start", "agent-start"),
    ],
)
def test_relative_existing_wrapper_discovers_primary_source(
    installation: Installation, wrapper: str, script: str
) -> None:
    installation.link_wrapper(
        wrapper, installation.checkout / "scripts" / script, relative=True
    )
    row = installation.manage("update", discover=True)
    assert row["ok"], row
    assert installation.destination.resolve() == installation.target


def test_conflicting_wrapper_roots_need_explicit_selection(
    installation: Installation, tmp_path: Path
) -> None:
    other = init_source(tmp_path.resolve() / "other primary", installation.home)
    installation.link_wrapper("kimi-wt", installation.checkout / "scripts/kimi-tree")
    installation.link_wrapper("devin-wt", other.checkout / "scripts/devin-tree")
    refused = installation.manage("update", discover=True)
    assert refused["ok"] is False, refused
    assert not installation.destination.exists()
    selected = installation.manage("update")
    assert selected["ok"], selected
    assert installation.destination.resolve() == installation.target


@pytest.mark.parametrize("mode", ["report", "update"])
def test_missing_discovery_is_an_explicit_failure(
    installation: Installation, mode: str
) -> None:
    row = installation.manage(mode, discover=True)
    assert row["ok"] is False and row["detail"], row
    assert not (installation.home / ".local").exists()


@pytest.mark.parametrize("discover", [False, True])
def test_linked_task_checkout_is_not_an_installation_source(
    installation: Installation, tmp_path: Path, discover: bool
) -> None:
    task = tmp_path.resolve() / "task checkout"
    installation.git("worktree", "add", "-b", "worktree-task", str(task), "main")
    assert (task / ".git").is_file()
    if discover:
        installation.link_wrapper("kimi-wt", task / "scripts/kimi-tree")
    settings = {} if discover else {"checkout": str(task)}
    row = tool.manage_launcher(settings, "update", home=installation.home)
    assert row["ok"] is False, row
    assert not installation.destination.exists()


@pytest.mark.parametrize(
    "fault", ["dirty", "uncommitted", "missing", "non-executable", "no-git"]
)
def test_invalid_source_is_refused_without_creating_destination(
    installation: Installation, fault: str, tmp_path: Path
) -> None:
    settings = {"checkout": str(installation.checkout)}
    if fault == "dirty":
        helper = installation.checkout / "scripts/lib/agent-tree.sh"
        helper.write_text("# unshipped change\n", encoding="utf-8")
    elif fault == "uncommitted":
        installation.git("rm", "--cached", "scripts/agent-start")
        message = tmp_path / "remove-message"
        message.write_text("launcher no longer tracked\n", encoding="utf-8")
        installation.git("commit", "-F", str(message))
        assert installation.target.is_file()
    elif fault == "missing":
        (installation.checkout / "scripts/codex-tree").unlink()
    elif fault == "non-executable":
        (installation.checkout / "scripts/claude-tree").chmod(0o644)
    else:
        plain = tmp_path / "not a checkout"
        plain.mkdir()
        settings = {"checkout": str(plain)}
    row = tool.manage_launcher(settings, "update", home=installation.home)
    assert row["ok"] is False, row
    assert not (installation.home / ".local").exists()


def test_unrelated_checkout_changes_do_not_block_installation(
    installation: Installation,
) -> None:
    (installation.checkout / "notes.txt").write_text("other work\n", encoding="utf-8")
    row = installation.manage("update")
    assert row["ok"], row
    assert installation.destination.resolve() == installation.target


def test_committed_symlink_dependency_cannot_import_mutable_external_code(
    installation: Installation, tmp_path: Path
) -> None:
    outside = tmp_path / "uncommitted-helper.sh"
    outside.write_text("# mutable external code\n", encoding="utf-8")
    helper = installation.checkout / "scripts/lib/agent-tree.sh"
    helper.unlink()
    helper.symlink_to(outside)
    installation.git("add", "scripts/lib/agent-tree.sh")
    message = tmp_path / "symlink-message"
    message.write_text("tracked external symlink\n", encoding="utf-8")
    installation.git("commit", "-F", str(message))
    assert installation.git("diff", "--quiet", "HEAD", "--", *SCRIPT_PATHS) == ""
    row = installation.manage("update")
    assert row["ok"] is False, row
    assert not (installation.home / ".local").exists()


def test_vendor_only_legacy_config_does_not_install_launcher(
    installation: Installation, capsys: pytest.CaptureFixture[str]
) -> None:
    config = {
        "only": ["claude"],
        "clis": {"claude": {"bin": "claude", "pin": "1.0.0", "min": ""}},
    }

    def run(
        argv: list[str], timeout: float, *, stdout_only: bool = False
    ) -> tuple[int, str]:
        assert argv == ["/fake/claude", "--version"]
        return 0, "1.0.0"

    assert (
        tool.main(
            ["report", "--config-json", json.dumps(config)],
            run,
            lambda name: "/fake/claude",
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out.rsplit("AGENT_CLI_JSON ", 1)[1])
    assert [row["cli"] for row in payload["rows"]] == ["claude"]
    assert not (installation.home / ".local").exists()


def test_invalid_source_preflights_before_vendor_installers(
    installation: Installation, capsys: pytest.CaptureFixture[str]
) -> None:
    config = {
        "launcher": {"checkout": str(installation.checkout / "missing")},
        "only": ["claude"],
        "clis": {
            "claude": {
                "bin": "claude",
                "pin": "2.0.0",
                "min": "",
                "install": "must-not-run {version}",
            }
        },
    }
    calls = []

    def run(
        argv: list[str], timeout: float, *, stdout_only: bool = False
    ) -> tuple[int, str]:
        calls.append(argv)
        assert argv == ["/fake/claude", "--version"], "no installer may run"
        return 0, "1.0.0"

    assert (
        tool.main(
            ["update", "--config-json", json.dumps(config)],
            run,
            lambda name: "/fake/claude",
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out.rsplit("AGENT_CLI_JSON ", 1)[1])
    assert calls == [["/fake/claude", "--version"]]
    rows = {row["cli"]: row for row in payload["rows"]}
    assert rows["claude"]["status"] == "behind"
    assert rows["agent-start"]["ok"] is False
    assert not (installation.home / ".local").exists()


@pytest.mark.parametrize("mode", ["report", "update"])
def test_main_reports_launcher_outcome_in_json_and_strict_exit(
    installation: Installation, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    config = {
        "launcher": {"checkout": str(installation.checkout)},
        "only": ["claude"],
        "clis": {"claude": {"bin": "claude", "pin": "1.0.0", "min": ""}},
    }

    def run(
        argv: list[str], timeout: float, *, stdout_only: bool = False
    ) -> tuple[int, str]:
        assert argv == ["/fake/claude", "--version"]
        return 0, "1.0.0"

    result = tool.main(
        [mode, "--strict", "--config-json", json.dumps(config)],
        run,
        lambda name: "/fake/claude",
    )
    payload = json.loads(capsys.readouterr().out.rsplit("AGENT_CLI_JSON ", 1)[1])
    rows = {row["cli"]: row for row in payload["rows"]}
    assert set(rows) == {"claude", "agent-start"}
    assert rows["agent-start"]["path"] == str(installation.destination)
    if mode == "report":
        assert result == 1
        assert rows["agent-start"]["action"] == "needs-update"
        assert not (installation.home / ".local").exists()
    else:
        assert result == 0
        assert rows["agent-start"]["action"] == "updated"
        assert installation.destination.resolve() == installation.target


@pytest.mark.parametrize("check_mode", [False, True])
def test_role_check_mode_reports_and_only_updates_count_as_changed(
    check_mode: bool,
) -> None:
    tasks = yaml.safe_load(
        (SOURCE.parents[1] / "tasks/main.yml").read_text(encoding="utf-8")
    )
    facts = next(
        task["ansible.builtin.set_fact"]
        for task in tasks
        if "ansible.builtin.set_fact" in task
    )
    operation = next(task for task in tasks if "ansible.builtin.script" in task)
    environment = Environment()
    environment.filters.update(bool=bool, to_json=json.dumps, quote=shlex.quote)
    environment.tests["search"] = lambda value, pattern: bool(re.search(pattern, value))
    effective = environment.from_string(facts["agent_cli_effective_mode"]).render(
        ansible_check_mode=check_mode, agent_cli_mode="update"
    )
    assert effective == ("report" if check_mode else "update")
    assert (
        environment.from_string(
            facts["agent_cli_config"]["launcher"]["checkout"]
        ).render(agent_cli_launcher_checkout="/configured/stable")
        == "/configured/stable"
    )
    command = environment.from_string(
        operation["ansible.builtin.script"]["cmd"]
    ).render(
        agent_cli_effective_mode=effective, agent_cli_strict=False, agent_cli_config={}
    )
    assert shlex.split(command)[:2] == ["agent_cli.py", effective]
    changed = environment.compile_expression(operation["changed_when"])(
        agent_cli_effective_mode=effective,
        agent_cli_out={"stdout": "agent-start ok · updated"},
    )
    assert changed is (not check_mode)
