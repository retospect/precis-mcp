"""Regression tests for ``scripts/hooks/precompact-persist.sh``.

The defect (2026-10-24, gr513748): Devin loads ``PreCompact`` hooks from
``.claude/settings.json`` and validates their stdout as ``pre_compact`` JSON.
The hook printed plain text — the shape Claude Code needs for the note to
reach the compaction context — so every Devin compaction logged "Hook
returned invalid pre_compact JSON output". The script now detects the
runtime: under Devin stdout stays empty and the advisory moves to stderr;
under Claude the plain-stdout note is preserved.
"""

from __future__ import annotations

import json
import os
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

CLAUDE_PAYLOAD = {
    "session_id": "s1",
    "transcript_path": "/tmp/t.jsonl",
    "hook_event_name": "PreCompact",
    "trigger": "auto",
    "custom_instructions": "",
}
# Devin's payload adds per-turn ids Claude never sends.
DEVIN_PAYLOAD = {**CLAUDE_PAYLOAD, "prompt_id": "p1", "turn_id": "t1"}


def _clean_env() -> dict[str, str]:
    """Env with no Devin markers, so the Claude path is exercised even when
    the test suite itself runs inside a Devin session."""
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("DEVIN_") and k != "AI_AGENT"
    }


def _run(payload: dict, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {**env, "CLAUDE_PROJECT_DIR": str(REPO)}
    return subprocess.run(
        ["bash", str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def test_claude_payload_prints_the_advisory_on_stdout() -> None:
    res = _run(CLAUDE_PAYLOAD, _clean_env())
    assert res.returncode == 0
    assert "PreCompact" in res.stdout


def test_devin_payload_leaves_stdout_empty() -> None:
    res = _run(DEVIN_PAYLOAD, _clean_env())
    assert res.returncode == 0
    assert res.stdout == ""
    assert "PreCompact" in res.stderr


def test_devin_env_marker_alone_selects_the_devin_path() -> None:
    env = {**_clean_env(), "AI_AGENT": "devin_3000-11-205_agent"}
    res = _run(CLAUDE_PAYLOAD, env)
    assert res.returncode == 0
    assert res.stdout == ""


def test_claude_env_marker_does_not_trigger_the_devin_path() -> None:
    env = {**_clean_env(), "AI_AGENT": "claude"}
    res = _run(CLAUDE_PAYLOAD, env)
    assert res.returncode == 0
    assert "PreCompact" in res.stdout
