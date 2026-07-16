"""Unit 4b — the Layer-2 tex fixer selects its model through the ADR 0046
resolver (CLOUD_MID / sonnet), byte-identically to the legacy inline read.

DB-free and spawn-free: ``_run_chktex`` is stubbed to force the LLM path and
``subprocess.run`` is captured, so no ``claude`` binary runs.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.utils import tex_llm_fix as tlf


class _FakeCompleted:
    def __init__(self, stdout: str) -> None:
        self.returncode = 0
        self.stdout = stdout
        self.stderr = ""


def _capture_cmd(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **_kw: Any) -> _FakeCompleted:
        captured["cmd"] = cmd
        # A plausible corrected-file body so attempt_llm_fix returns 'hint'.
        return _FakeCompleted("\\section{x}\nfixed body\n")

    monkeypatch.setattr(tlf, "_run_chktex", lambda _t: ("Warning 1 something",))
    monkeypatch.setattr(tlf.subprocess, "run", fake_run)
    monkeypatch.setenv("PRECIS_CLAUDE_BIN", "claude-stub")
    res = tlf.attempt_llm_fix("\\section{x}\nsome body\n")
    assert res.verdict == "hint"
    return captured["cmd"]


def test_uses_resolved_sonnet_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env unset → CLOUD_MID default, byte-identical to the legacy default."""
    monkeypatch.delenv("PRECIS_MODEL_SONNET", raising=False)
    cmd = _capture_cmd(monkeypatch)
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-5"
    # Behavior preserved: still one-shot, permission default.
    assert cmd[cmd.index("--max-turns") + 1] == "1"
    assert cmd[cmd.index("--permission-mode") + 1] == "default"


def test_honours_sonnet_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_MODEL_SONNET", "claude-sonnet-pinned")
    cmd = _capture_cmd(monkeypatch)
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-pinned"
