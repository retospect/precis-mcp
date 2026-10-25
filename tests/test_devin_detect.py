"""gr515490: ``scripts/hooks/_devin-detect.sh`` is the ONE hook-runtime
detector — hooks that need "am I under Devin" source it rather than
keeping their own copy of the marker list (a drifted copy is what
reddened the Devin panes twice, gr513748/gr514171). Pinned here:

- ``hook_is_devin`` answers from DEVIN_PROJECT_DIR (the reliable marker
  — Devin sets no AI_AGENT and pre-first-prompt payloads carry no
  prompt_id/turn_id, gr514171), with the payload ids and AI_AGENT as
  fallbacks;
- ``hook_project_dir`` resolves DEVIN_PROJECT_DIR before
  CLAUDE_PROJECT_DIR — the same order every hook must use; and
- both PreCompact hooks source the helper instead of reimplementing
  detection (the ``prompt_id``/``turn_id`` case-block lives only in the
  helper).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the helper is POSIX bash"
)

REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "scripts" / "hooks" / "_devin-detect.sh"
HOOKS = (
    REPO / "scripts" / "hooks" / "precompact-persist.sh",
    REPO / "scripts" / "hooks" / "compact-state-note.sh",
)


def _call(fn_call: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'. "{HELPER}"; {fn_call}'],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def _env(**extra: str) -> dict[str, str]:
    """A Claude-like env: no Devin markers, even when the suite itself
    runs inside a Devin session."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("DEVIN_") and k != "AI_AGENT"
    }
    env.pop("CLAUDE_PROJECT_DIR", None)
    env.update(extra)
    return env


def test_devin_project_dir_alone_is_devin() -> None:
    res = _call('hook_is_devin ""', _env(DEVIN_PROJECT_DIR="/x"))
    assert res.returncode == 0, res.stderr


def test_payload_ids_detect_devin_without_env() -> None:
    for payload in ('{"prompt_id": "p"}', '{"turn_id": "t"}'):
        res = _call(f"hook_is_devin '{payload}'", _env())
        assert res.returncode == 0, f"{payload}: {res.stderr}"


def test_ai_agent_fallback_detects_devin() -> None:
    res = _call('hook_is_devin ""', _env(AI_AGENT="devin_3000-11-205"))
    assert res.returncode == 0, res.stderr


def test_clean_claude_env_is_not_devin() -> None:
    res = _call('hook_is_devin "{\\"session_id\\": \\"s\\"}"', _env())
    assert res.returncode == 1, res.stderr


def test_project_dir_prefers_devin_then_claude() -> None:
    res = _call(
        "hook_project_dir",
        _env(DEVIN_PROJECT_DIR="/d", CLAUDE_PROJECT_DIR="/c"),
    )
    assert res.stdout == "/d"
    res = _call("hook_project_dir", _env(CLAUDE_PROJECT_DIR="/c"))
    assert res.stdout == "/c"
    res = _call("hook_project_dir", _env())
    assert res.stdout == ""


def test_hooks_source_the_shared_helper() -> None:
    """No reimplemented marker list: the prompt_id/turn_id case-block is
    the helper's alone, and each consumer resolves + sources it."""
    for hook in HOOKS:
        text = hook.read_text(encoding="utf-8")
        assert "_devin-detect.sh" in text, hook.name
        assert "hook_is_devin" in text, hook.name
        # the detection itself must not be restated
        assert "prompt_id" not in text, hook.name
        assert "turn_id" not in text, hook.name


def test_helper_reachable_for_a_copied_out_hook(tmp_path: Path) -> None:
    """The live compact-state-note.sh is ~/.claude/hooks/-resident — no
    helper beside it. With only the harness-exported project dir it must
    still source the repo's helper: DEVIN markers route the note to
    stderr, stdout stays empty (Devin's pre_compact contract)."""
    import json
    import shutil

    copy = tmp_path / "compact-state-note.sh"
    shutil.copy(HOOKS[1], copy)
    env = _env(DEVIN_PROJECT_DIR=str(REPO))
    res = subprocess.run(
        ["bash", str(copy)],
        input=json.dumps({"hook_event_name": "PreCompact"}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    assert res.returncode == 0
    assert res.stdout == ""
    assert res.stderr.startswith("[compact]")
