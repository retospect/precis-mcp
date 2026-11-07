"""scripts/hooks/kimi-adapt.py — the PreToolUse tier deny check (td576126).

Kimi reads no project permission file, so the adapter itself applies the
nearest ``.claude/settings.local.json`` ``permissions.deny`` list before
dispatching the tree's hooks. The tests run the adapter as a subprocess
with a Claude-shaped payload on stdin, in a throwaway tree whose
settings.local.json is a copy of the generated tier-R file — so the deny
list tracks the authority file. A missing or unreadable settings file
fails closed (``settings-unreadable``): the pane's own enforcement files
are denied regardless.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: the adapter is a uv script"
)

REPO = Path(__file__).resolve().parents[1]
ADAPT = REPO / "scripts" / "hooks" / "kimi-adapt.py"
TIER_R = REPO / ".claude" / "fleet" / "generated" / "claude-settings-R.json"


def _tree(tmp_path: Path, *, settings: str | None = "tier") -> Path:
    """A fake tree; ``settings`` is "tier" (copy the generated R file),
    raw file text, or None for no settings.local.json at all."""
    tree = tmp_path / "tree"
    (tree / ".claude").mkdir(parents=True)
    if settings == "tier":
        shutil.copyfile(TIER_R, tree / ".claude" / "settings.local.json")
    elif settings is not None:
        (tree / ".claude" / "settings.local.json").write_text(
            settings, encoding="utf-8"
        )
    return tree


def _run(
    cwd: Path, tool: str, tool_input: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
        "cwd": str(cwd),
    }
    return subprocess.run(
        [sys.executable, str(ADAPT), "PreToolUse"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )


def test_precis_write_tools_are_denied(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    for tool in (
        "mcp__precis__put",
        "mcp__precis__edit",
        "mcp__precis__delete",
        "mcp__precis__tag",
        "mcp__precis__link",
    ):
        proc = _run(tree, tool, {"kind": "todo"})
        assert proc.returncode == 2, tool
        assert tool in proc.stderr
        assert "settings.local.json" in proc.stderr
    assert _run(tree, "mcp__precis__get", {"kind": "todo"}).returncode == 0


def test_deny_rule_blocks_a_bash_prefix_and_env_words(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    proc = _run(tree, "Bash", {"command": "scripts/ship --quick"})
    assert proc.returncode == 2 and "Bash(scripts/ship:*)" in proc.stderr
    # Leading VAR=val words are stripped before the prefix is matched.
    proc = _run(tree, "Bash", {"command": "FOO=1 scripts/round cut"})
    assert proc.returncode == 2 and "Bash(scripts/round:*)" in proc.stderr
    assert _run(tree, "Bash", {"command": "git status"}).returncode == 0


def test_edit_glob_rules_and_write_cover(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    proc = _run(tree, "Edit", {"path": ".claude/fleet/authority.md"})
    assert proc.returncode == 2
    assert "Edit(.claude/fleet/authority.md)" in proc.stderr
    # An Edit rule also covers Kimi's Write tool.
    proc = _run(tree, "Write", {"path": ".claude/fleet/authority.md"})
    assert proc.returncode == 2
    assert _run(tree, "Edit", {"path": "src/x.py"}).returncode == 0


def test_cwd_in_a_subdirectory_still_finds_the_file(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    sub = tree / "src" / "pkg"
    sub.mkdir(parents=True)
    proc = _run(sub, "mcp__precis__put", {"kind": "todo"})
    assert proc.returncode == 2


def test_no_settings_file_fails_closed(tmp_path: Path) -> None:
    tree = _tree(tmp_path, settings=None)
    proc = _run(tree, "mcp__precis__put", {"kind": "todo"})
    assert proc.returncode == 2 and "settings-unreadable" in proc.stderr
    proc = _run(tree, "Bash", {"command": "scripts/ship --quick"})
    assert proc.returncode == 2 and "settings-unreadable" in proc.stderr
    # Reads and ordinary file edits still pass: closed means contained,
    # not bricked.
    assert _run(tree, "mcp__precis__get", {"kind": "todo"}).returncode == 0
    assert _run(tree, "Bash", {"command": "git status"}).returncode == 0
    assert _run(tree, "Edit", {"path": "src/x.py"}).returncode == 0


@pytest.mark.parametrize("bad", ["not json {", '{"permissions": {"deny": null}}'])
def test_malformed_settings_fails_closed(tmp_path: Path, bad: str) -> None:
    tree = _tree(tmp_path, settings=bad)
    for tool, tool_input in (
        ("mcp__precis__put", {"kind": "todo"}),
        ("Bash", {"command": "scripts/ship --quick"}),
    ):
        proc = _run(tree, tool, tool_input)
        assert proc.returncode == 2, (bad, tool)
        assert "settings-unreadable" in proc.stderr
    log = (tree / ".claude" / "kimi-adapt.log").read_text(encoding="utf-8")
    for ln in log.splitlines():
        assert ln.split("\t")[2:] == ["deny", "settings-unreadable"]


def test_enforcement_files_are_hard_denied(tmp_path: Path) -> None:
    """Independent of the settings file — a deny list that does not
    protect its own enforcement is a suggestion."""
    tree = _tree(tmp_path, settings=None)
    for fp in (
        ".claude/settings.local.json",
        ".claude/settings.json",
        ".claude/fleet/authority.md",
        "scripts/hooks/kimi-adapt.py",
        "~/.kimi-code/config.toml",
    ):
        for tool in ("Edit", "Write"):
            proc = _run(tree, tool, {"path": fp})
            assert proc.returncode == 2, (tool, fp)
    # The same files under a different tree root count too.
    proc = _run(tree, "Edit", {"path": "/other/tree/.claude/settings.local.json"})
    assert proc.returncode == 2
    # And under a real settings file — the tier list alone does not
    # cover settings.local.json.
    tree = _tree(tmp_path / "tier", settings="tier")
    assert _run(tree, "Edit", {"path": ".claude/settings.local.json"}).returncode == 2
    assert _run(tree, "Edit", {"path": "src/x.py"}).returncode == 0


def test_bash_writes_to_enforcement_files_are_denied(tmp_path: Path) -> None:
    tree = _tree(tmp_path, settings=None)
    for cmd in (
        "echo x > .claude/settings.local.json",
        "echo x >> .claude/settings.local.json",
        "tee .claude/settings.json",
        "sed -i s/a/b/ scripts/hooks/kimi-adapt.py",
        "cp /tmp/x .claude/fleet/authority.md",
        "mv .claude/settings.local.json /tmp/x",
        "cd .claude && echo x > settings.local.json",
        "echo x > ~/.kimi-code/config.toml",
        # A redirect needs no whitespace around the operator.
        "echo x>.claude/settings.json",
        "printf '%s' \"$j\">.claude/settings.local.json",
        "echo x>>scripts/hooks/kimi-adapt.py",
    ):
        proc = _run(tree, "Bash", {"command": cmd})
        assert proc.returncode == 2, cmd
    # Reads of the same paths stay allowed.
    assert (
        _run(tree, "Bash", {"command": "cat .claude/settings.local.json"}).returncode
        == 0
    )
    assert _run(tree, "Bash", {"command": "git status"}).returncode == 0


def test_bash_deny_normalises_wrappers_and_segments(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    for cmd in (
        "./scripts/ship --quick",
        "cd . && scripts/ship --quick",
        "cd /tmp && scripts/ship --quick",
        "bash scripts/ship --quick",
        "sh -c 'scripts/ship --quick'",
        "uv run scripts/ship --quick",
        "git status && scripts/ship --quick",
        "scripts/ship --quick || true",
        "scripts/ship --quick | cat",
        "FOO=1 cd . && BAR=2 scripts/ship --quick",
        # A quoted -c argument is split like the command it wraps.
        "bash -c 'cd . && scripts/ship --quick'",
        'sh -c "scripts/ship --quick; echo"',
        "bash -c 'echo ok; scripts/round cut'",
    ):
        proc = _run(tree, "Bash", {"command": cmd})
        assert proc.returncode == 2, cmd
    assert _run(tree, "Bash", {"command": "echo scripts/ship --quick"}).returncode == 0


def test_each_decision_writes_a_hook_log_line(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    assert _run(tree, "mcp__precis__get", {}).returncode == 0
    assert _run(tree, "mcp__precis__put", {}).returncode == 2
    log = (tree / ".claude" / "kimi-adapt.log").read_text(encoding="utf-8")
    lines = log.splitlines()
    assert len(lines) == 2
    fields = [ln.split("\t") for ln in lines]
    assert fields[0][1:] == ["mcp__precis__get", "allow", "-"]
    assert fields[1][1:] == ["mcp__precis__put", "deny", "mcp__precis__put"]
