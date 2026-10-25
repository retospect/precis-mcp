"""Drift guard + behavior tests for ``scripts/hooks/compact-state-note.sh``.

gr514969: the live copy of the compaction state-note hook is user-level —
``~/.claude/hooks/compact-state-note.sh`` — and ``~/.claude/settings.json``
(SessionStart ``compact`` and PreCompact) runs it when the project copy
differs or is absent (``cmp -s``). The repo copy is the dev source; the
two must stay byte-identical or the shim silently keeps serving the stale
global. The drift test compares them and skips only when the host has no
global copy to guard against — the ``scripts/test`` container has no
``~/.claude``, which is why the guard previously never ran there.

The script itself carries the DEVIN_PROJECT_DIR fix (gr514171): a Devin
``pre_compact`` payload must see empty/JSON stdout, so the note goes to
stderr under Devin and stays on stdout under Claude.
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

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "scripts" / "hooks" / "compact-state-note.sh"
GLOBAL = Path.home() / ".claude" / "hooks" / "compact-state-note.sh"


def test_repo_copy_is_byte_identical_to_the_live_global() -> None:
    """The ~/.claude/settings.json shim cmp's the two — drift silently
    serves the stale global copy."""
    if not GLOBAL.is_file():
        pytest.skip(
            "no live global at ~/.claude/hooks/compact-state-note.sh "
            "(container runs have no ~/.claude) — nothing to drift-guard"
        )
    assert HOOK.read_bytes() == GLOBAL.read_bytes(), (
        "scripts/hooks/compact-state-note.sh drifted from "
        "~/.claude/hooks/compact-state-note.sh — the PreCompact shim runs "
        "the global when they differ, so a stale global keeps serving"
    )


def _run(
    payload: dict, env: dict[str, str], argv: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(HOOK), *argv],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def _claude_env() -> dict[str, str]:
    """Env with no Devin markers, so the Claude path is exercised even when
    the suite itself runs inside a Devin session."""
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("DEVIN_") and k != "AI_AGENT"
    }


def test_claude_payload_prints_the_note_on_stdout() -> None:
    res = _run({"hook_event_name": "PreCompact"}, _claude_env())
    assert res.returncode == 0
    assert res.stdout.startswith("[compact]")
    assert res.stderr == ""


def test_devin_project_dir_routes_the_note_to_stderr() -> None:
    """gr514171: Devin's pre_compact wants stdout empty/JSON; the note goes
    to stderr. DEVIN_PROJECT_DIR is always set for Devin hook spawns."""
    env = {**_claude_env(), "DEVIN_PROJECT_DIR": "/x"}
    res = _run({"hook_event_name": "PreCompact"}, env)
    assert res.returncode == 0
    assert res.stdout == ""
    assert res.stderr.startswith("[compact]")


def test_resume_arg_prints_the_resume_note() -> None:
    res = _run({"hook_event_name": "SessionStart"}, _claude_env(), ("--resume",))
    assert res.returncode == 0
    assert res.stdout.startswith("[compact] Resume from")
