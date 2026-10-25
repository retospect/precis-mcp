"""Regression tests for ``scripts/hooks/precompact-persist.sh``.

The defect (2026-10-24, gr513748): Devin loads ``PreCompact`` hooks from
``.claude/settings.json`` and validates their stdout as ``pre_compact`` JSON.
The hook printed plain text — the shape Claude Code needs for the note to
reach the compaction context — so every Devin compaction logged "Hook
returned invalid pre_compact JSON output". The script now detects the
runtime: under Devin stdout stays empty and the advisory moves to stderr;
under Claude the plain-stdout note is preserved.

gr514171 (same day): detection keyed only on the payload ids still leaked
plain text to stdout whenever a Devin pre_compact fired without them —
``prompt_id`` is a per-turn id absent for events before the first user
prompt (e.g. auto-compaction on a resumed session), and Devin does not set
``AI_AGENT``. Devin exports ``DEVIN_PROJECT_DIR`` on every hook spawn, so
that is now the primary marker.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the hook is a POSIX bash script"
)

HOOK = (
    Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "precompact-persist.sh"
)
REPO = Path(__file__).resolve().parents[1]
SETTINGS = REPO / ".claude" / "settings.json"


@pytest.fixture(scope="module")
def stub_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A fake project dir whose ``scripts/`` stubs out the hook's lint
    calls. The real ``scripts/memory-lint``/``scripts/backlog-lint`` cost
    ~6 s each inside the hook — multiplied across these runs they made the
    file ~43 s (gr515493). The stdout contract under test is the hook's
    own, so cheap stubs exercise it identically; memory-lint still emits a
    ``DUE`` line so the hook's filter path runs too."""
    proj = tmp_path_factory.mktemp("precompact-proj")
    sdir = proj / "scripts"
    sdir.mkdir(parents=True)
    mem = sdir / "memory-lint"
    mem.write_text("#!/bin/sh\necho '1 DUE item'\n", encoding="utf-8")
    mem.chmod(0o755)
    bl = sdir / "backlog-lint"
    bl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    bl.chmod(0o755)
    return proj


CLAUDE_PAYLOAD = {
    "session_id": "s1",
    "transcript_path": "/tmp/t.jsonl",
    "hook_event_name": "PreCompact",
    "trigger": "auto",
    "custom_instructions": "",
}
# Devin's payload adds per-turn ids Claude never sends.
DEVIN_PAYLOAD = {**CLAUDE_PAYLOAD, "prompt_id": "p1", "turn_id": "t1"}
# …but they are absent for events before the first user prompt, so a
# Devin-shaped payload without them must still take the Devin path.
DEVIN_PAYLOAD_NO_IDS = {**CLAUDE_PAYLOAD, "trigger": "auto"}


def _clean_env() -> dict[str, str]:
    """Env with no Devin markers, so the Claude path is exercised even when
    the test suite itself runs inside a Devin session."""
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("DEVIN_") and k != "AI_AGENT"
    }


def _run(
    payload: dict, env: dict[str, str], project: Path = REPO
) -> subprocess.CompletedProcess[str]:
    env = {**env, "CLAUDE_PROJECT_DIR": str(project)}
    return subprocess.run(
        ["bash", str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def test_claude_payload_prints_the_advisory_on_stdout(
    stub_project: Path,
) -> None:
    res = _run(CLAUDE_PAYLOAD, _clean_env(), stub_project)
    assert res.returncode == 0
    assert "PreCompact" in res.stdout


def test_devin_payload_leaves_stdout_empty(stub_project: Path) -> None:
    res = _run(DEVIN_PAYLOAD, _clean_env(), stub_project)
    assert res.returncode == 0
    assert res.stdout == ""
    assert "PreCompact" in res.stderr


def test_devin_env_marker_alone_selects_the_devin_path(stub_project: Path) -> None:
    env = {**_clean_env(), "AI_AGENT": "devin_3000-11-205_agent"}
    res = _run(CLAUDE_PAYLOAD, env, stub_project)
    assert res.returncode == 0
    assert res.stdout == ""


def test_claude_env_marker_does_not_trigger_the_devin_path(
    stub_project: Path,
) -> None:
    env = {**_clean_env(), "AI_AGENT": "claude"}
    res = _run(CLAUDE_PAYLOAD, env, stub_project)
    assert res.returncode == 0
    assert "PreCompact" in res.stdout


def test_devin_project_dir_alone_selects_the_devin_path(stub_project: Path) -> None:
    """gr514171: a Devin payload without prompt_id/turn_id (they are
    per-turn ids, absent before the first user prompt) and no AI_AGENT
    must still keep stdout empty — DEVIN_PROJECT_DIR is always set."""
    env = {**_clean_env(), "DEVIN_PROJECT_DIR": str(stub_project)}
    res = _run(DEVIN_PAYLOAD_NO_IDS, env)
    assert res.returncode == 0
    assert res.stdout == ""
    assert "PreCompact" in res.stderr


def _precompact_commands() -> list[str]:
    """Every ``command`` the project settings register for PreCompact."""
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    return [
        hook["command"]
        for entry in settings["hooks"].get("PreCompact", [])
        for hook in entry["hooks"]
        if hook.get("type") == "command"
    ]


def _repo_scripts(commands: list[str]) -> list[Path]:
    """Repo script paths a PreCompact command can run
    (``$CLAUDE_PROJECT_DIR/scripts/…`` and literal ``scripts/…``)."""
    scripts: list[Path] = []
    for cmd in commands:
        for match in re.findall(r"scripts/[A-Za-z0-9_./-]+", cmd):
            path = REPO / match
            if path.is_file():
                scripts.append(path)
    return scripts


def test_every_precompact_script_stdout_is_empty_or_json_under_devin(
    stub_project: Path,
) -> None:
    """Devin's pre_compact contract: stdout is EMPTY or valid JSON, for each
    repo script any PreCompact command in .claude/settings.json can run —
    including payloads that lack the per-turn ids."""
    scripts = _repo_scripts(_precompact_commands())
    assert scripts, "no repo PreCompact scripts found in .claude/settings.json"
    env = {**_clean_env(), "DEVIN_PROJECT_DIR": str(stub_project)}
    env["CLAUDE_PROJECT_DIR"] = str(stub_project)
    for script in scripts:
        res = subprocess.run(
            ["bash", str(script)],
            input=json.dumps(DEVIN_PAYLOAD_NO_IDS),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
        )
        if res.stdout:
            json.loads(res.stdout)  # raises unless it is valid JSON


def test_every_precompact_script_claude_behaviour_unchanged(
    stub_project: Path,
) -> None:
    """Under Claude the advisory still reaches the compaction context via
    plain stdout."""
    scripts = _repo_scripts(_precompact_commands())
    assert scripts
    env = {**_clean_env(), "CLAUDE_PROJECT_DIR": str(stub_project)}
    for script in scripts:
        res = subprocess.run(
            ["bash", str(script)],
            input=json.dumps(CLAUDE_PAYLOAD),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
        )
        assert res.returncode == 0
        assert res.stdout  # the note is still emitted for Claude
