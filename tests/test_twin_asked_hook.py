"""The twin-asked UserPromptSubmit hook (gr548277): prints the reminder
line for a matching prompt, silence otherwise."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: runs a bash hook"
)

HOOK = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "twin-asked.sh"


def _run(prompt: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(HOOK)],
        input=json.dumps({"prompt": prompt}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )


def test_matching_prompt_prints_the_line() -> None:
    r = _run("what is entropy?")
    assert r.returncode == 0
    assert "twin: Reto asked what 'entropy' is" in r.stdout
    assert "tags=['twin:asked']" in r.stdout


def test_remind_me_form_prints_the_term() -> None:
    r = _run("remind me what a perovskite is")
    assert "'perovskite'" in r.stdout


@pytest.mark.parametrize(
    "prompt",
    [
        "what is the status of X",
        "tell me what mitosis is",
        "can you explain gradient descent?",
        "",
    ],
)
def test_non_matching_prompts_are_silent(prompt: str) -> None:
    r = _run(prompt)
    assert r.returncode == 0
    assert r.stdout == ""
