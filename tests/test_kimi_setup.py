"""scripts/kimi-setup must survive Kimi Code rewriting config.toml.

Kimi re-serializes ~/.kimi-code/config.toml on its own saves (model pick,
settings) and drops comments. kimi-setup used to find its managed content
by `# >>> precis-mcp` comment markers, so the next run re-inserted
`extra_skill_dirs` and `[mcp]` a second time and the file stopped parsing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: shebang")

REPO = Path(__file__).resolve().parents[1]
SETUP = REPO / "scripts" / "kimi-setup"
MANAGED = "scripts/hooks/kimi-"
REASON = "mirrors the Claude Code allow list"


def run_setup(tmp_path: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    claude_json = tmp_path / "claude.json"
    claude_json.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "precis": {
                        "url": "https://mcp.invalid",
                        "bearerTokenEnvVar": "PRECIS_MCP_TOKEN",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"permissions": {"allow": ["Bash(git:*)", "mcp__precis__get"]}}),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["KIMI_CODE_HOME"] = str(tmp_path / "kimi-home")
    env["CLAUDE_JSON"] = str(claude_json)
    env["CLAUDE_SETTINGS"] = str(settings)
    return subprocess.run(
        [str(SETUP), *argv],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


def config(tmp_path: Path) -> Path:
    return tmp_path / "kimi-home" / "config.toml"


def kimi_rewrite(text: str) -> str:
    """What Kimi's own save does to the file: comments gone, a user table
    appended after the managed arrays."""
    body = "".join(ln for ln in text.splitlines(True) if not ln.startswith("#"))
    if "[secondary_model]" in body:
        return body
    return body + '\n[secondary_model]\ndefault_model = "kimi-code/k3-256k"\n'


def assert_single_managed(text: str) -> dict:
    data = tomllib.loads(text)
    assert data["extra_skill_dirs"] == [".claude/skills", ".claude/commands"]
    assert data["mcp"]["startup_timeout_ms"] == 30_000
    ours = [h for h in data["hooks"] if MANAGED in h["command"]]
    assert len(ours) == 4
    rules = [r for r in data["permission"]["rules"] if r.get("reason") == REASON]
    assert len(rules) == len({r["pattern"] for r in rules})
    assert ">>>" not in text and "<<<" not in text
    return data


def test_survives_kimi_rewrite(tmp_path: Path) -> None:
    home = tmp_path / "kimi-home"
    home.mkdir()
    config(tmp_path).write_text(
        'default_model = "kimi-code/k3.1-pro"\n\n[thinking]\nenabled = true\n',
        encoding="utf-8",
    )
    assert run_setup(tmp_path).returncode == 0
    assert_single_managed(config(tmp_path).read_text(encoding="utf-8"))

    for _ in range(2):
        rewritten = kimi_rewrite(config(tmp_path).read_text(encoding="utf-8"))
        config(tmp_path).write_text(rewritten, encoding="utf-8")
        proc = run_setup(tmp_path)
        assert proc.returncode == 0, proc.stderr
        data = assert_single_managed(config(tmp_path).read_text(encoding="utf-8"))
        assert data["default_model"] == "kimi-code/k3.1-pro"
        assert data["thinking"] == {"enabled": True}
        assert data["secondary_model"] == {"default_model": "kimi-code/k3-256k"}

    assert run_setup(tmp_path, "--check").returncode == 0


def test_repairs_legacy_half_marked_file(tmp_path: Path) -> None:
    """The shape found in the wild: Kimi already dropped the top-level and
    [mcp] markers, while the hooks+rules block still carries its markers
    and an unmarked copy of the hooks sits above it."""
    hook = (
        "[[hooks]]\n"
        'event = "PreToolUse"\n'
        'command = "f=scripts/hooks/kimi-adapt.py; exec \\"$f\\" PreToolUse"\n'
        "timeout = 60\n\n"
    )
    user_hook = '[[hooks]]\nevent = "Stop"\ncommand = "echo bye"\n\n'
    legacy = (
        'default_permission_mode = "ask"\n'
        'extra_skill_dirs = [".claude/skills", ".claude/commands"]\n\n'
        "[mcp]\nstartup_timeout_ms = 30000\n\n"
        + hook
        + user_hook
        + "# >>> precis-mcp kimi-setup: hooks + rules >>>\n"
        + hook
        + "# <<< precis-mcp kimi-setup: hooks + rules <<<\n"
    )
    (tmp_path / "kimi-home").mkdir()
    config(tmp_path).write_text(legacy, encoding="utf-8")
    proc = run_setup(tmp_path)
    assert proc.returncode == 0, proc.stderr
    data = assert_single_managed(config(tmp_path).read_text(encoding="utf-8"))
    assert data["default_permission_mode"] == "ask"
    assert {"event": "Stop", "command": "echo bye"} in data["hooks"]
