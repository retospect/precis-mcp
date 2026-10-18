"""scripts/fleet-authority apply: merge tier permissions into a worktree."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "fleet_authority", REPO / "scripts" / "lib" / "fleet_authority.py"
)
assert _spec is not None and _spec.loader is not None
fa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fa)


def _git(cwd: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", *argv], cwd=cwd, check=True, capture_output=True, text=True,
        encoding="utf-8",
    ).stdout  # fmt: skip


@pytest.fixture
def primary(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / ".gitignore").write_text(".claude/*\n.devin/*\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "init")
    return repo.resolve()


def _tree(primary: Path, name: str = "b1") -> Path:
    tree = primary / ".claude" / "worktrees" / name
    _git(primary, "worktree", "add", "-q", "-b", f"worktree-{name}", str(tree))
    return tree


def test_apply_keeps_other_keys_replaces_permissions(primary: Path) -> None:
    tree = _tree(primary)
    local = tree / ".claude" / "settings.local.json"
    local.parent.mkdir(parents=True)
    old = {"model": "opus", "env": {"X": "1"}, "permissions": {"allow": ["Bash(rm:*)"]}}
    local.write_text(json.dumps(old), encoding="utf-8")

    assert fa.apply("W", tree, yes=False) == 0
    assert json.loads(local.read_text(encoding="utf-8")) == old
    assert not (tree / ".devin" / "config.json").exists()

    assert fa.apply("W", tree, yes=True) == 0
    new = json.loads(local.read_text(encoding="utf-8"))
    want = json.loads(
        (fa.GENERATED / "claude-settings-W.json").read_text(encoding="utf-8")
    )
    assert new == {
        "model": "opus",
        "env": {"X": "1"},
        "permissions": want["permissions"],
    }
    bak = local.with_name("settings.local.json.bak")
    assert json.loads(bak.read_text(encoding="utf-8")) == old
    devin = json.loads((tree / ".devin" / "config.json").read_text(encoding="utf-8"))
    assert "Exec(scripts/release)" in devin["permissions"]["deny"]
    assert not (tree / ".devin" / "config.json.bak").exists()
    # Both files are gitignored: nothing for scripts/ship to commit.
    assert _git(tree, "status", "--porcelain") == ""


def test_apply_prints_codex_flags(
    primary: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert fa.apply("R", _tree(primary), yes=False) == 0
    assert "codex-flags: --sandbox read-only" in capsys.readouterr().out


@pytest.mark.parametrize("where", ["primary", "plain", "subdir", "outside"])
def test_apply_refuses_non_builder_worktree(
    primary: Path, tmp_path: Path, where: str
) -> None:
    tree = _tree(primary)
    (tree / "sub").mkdir()
    plain = tmp_path / "plain"
    plain.mkdir()
    elsewhere = tmp_path / "elsewhere"
    _git(primary, "worktree", "add", "-q", "-b", "other", str(elsewhere))
    target = {"primary": primary, "plain": plain, "subdir": tree / "sub",
              "outside": elsewhere}[where]  # fmt: skip
    assert fa.apply("W", target, yes=True) == 2
    assert not (target / ".claude" / "settings.local.json").exists()


def test_apply_updates_existing_settings_and_is_idempotent(
    primary: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tree = _tree(primary)
    local = tree / ".claude" / "settings.local.json"
    local.parent.mkdir(parents=True)
    local.write_text(json.dumps({"permissions": {"allow": ["old"]}}), encoding="utf-8")
    assert fa.apply("W", tree, yes=True) == 0
    allow = json.loads(local.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert "old" not in allow
    capsys.readouterr()
    assert fa.apply("W", tree, yes=True) == 0
    assert f"unchanged {local}" in capsys.readouterr().out


def test_role_unions_allow_exec_over_tier() -> None:
    data = fa.load()
    tier_w = fa.resolve(data, "W")["allow_exec"]
    got = fa.resolve(data, "W", "coordinator")["allow_exec"]
    assert got == [*tier_w, "scripts/fleet-wave launch"]
    # a role without allow_exec behaves exactly like its tier
    assert fa.resolve(data, "R", "host_desk") == fa.resolve(data, "R")
    # the tier alone is unchanged by roles existing
    assert "scripts/fleet-wave launch" not in tier_w
    # an entry already in the tier is not duplicated
    data["roles"]["dup"] = {"tier": "W", "allow_exec": [tier_w[0], "x y"]}
    assert fa.resolve(data, "W", "dup")["allow_exec"] == [*tier_w, "x y"]


def test_apply_role_writes_role_permissions(primary: Path) -> None:
    tree = _tree(primary)
    assert fa.apply("", tree, yes=True, role="coordinator") == 0
    local = tree / ".claude" / "settings.local.json"
    cfg = json.loads(local.read_text(encoding="utf-8"))
    assert "Bash(scripts/fleet-wave launch:*)" in cfg["permissions"]["allow"]
    devin = json.loads((tree / ".devin" / "config.json").read_text(encoding="utf-8"))
    assert "Exec(scripts/fleet-wave launch)" in devin["permissions"]["allow"]


def test_apply_unknown_role_refuses_naming_valid_roles(
    primary: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tree = _tree(primary)
    assert fa.apply("", tree, yes=True, role="nope") == 2
    err = capsys.readouterr().err
    assert "next=" in err
    assert "coordinator" in err
    assert "host_desk" in err
    assert not (tree / ".claude" / "settings.local.json").exists()


def test_apply_refuses_a_file_git_would_track(primary: Path) -> None:
    (primary / ".gitignore").write_text(".claude/*\n", encoding="utf-8")
    _git(primary, "commit", "-q", "-am", "track .devin")
    tree = _tree(primary)
    assert fa.apply("W", tree, yes=True) == 2
    assert not (tree / ".claude" / "settings.local.json").exists()
