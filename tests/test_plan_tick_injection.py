"""§12 injection lockdown — the planner subprocess owns its whole system
prompt (ADR 0051 §12, slice A3).

No ``claude`` binary spawns: ``subprocess.run`` is stubbed and the argv +
kwargs it would receive are captured, so these assert the *wiring* (neutral
cwd, ambient-CLAUDE.md detection) without a live model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import precis.workers.job_types.plan_tick as pt


class _FakeCompleted:
    returncode = 0
    stdout = ""
    stderr = ""


class _FakePrompts:
    system = "SYSTEM-ASSEMBLED-BYTES"
    user = "USER"


def _run_capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import precis.agentlog as agentlog
    import precis.workers.planner_prompt as planner_prompt

    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kw: Any) -> _FakeCompleted:
        captured["cmd"] = cmd
        captured["kwargs"] = kw
        return _FakeCompleted()

    monkeypatch.setattr(
        planner_prompt, "build_planner_prompts", lambda *a, **k: _FakePrompts()
    )
    monkeypatch.setattr(pt, "_load_parent_workspace", lambda *a, **k: None)
    monkeypatch.setattr(pt, "_disable_prose_file_kind", lambda *a, **k: None)
    monkeypatch.setattr(agentlog, "open_log", lambda *a, **k: 1)
    monkeypatch.setattr(agentlog, "finalize_log", lambda *a, **k: None)
    monkeypatch.setattr(pt.subprocess, "run", fake_run)
    monkeypatch.setenv("PRECIS_CLAUDE_BIN", "claude-stub")
    monkeypatch.delenv("PRECIS_MCP_CONFIG", raising=False)

    pt.run(store=object(), job_ref_id=1, parent_ref_id=2, params={"model": "opus"})
    return captured


def test_subprocess_runs_from_a_claude_md_free_neutral_cwd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _run_capture(monkeypatch)
    cwd = captured["kwargs"]["cwd"]
    assert cwd and Path(cwd).is_dir()
    # The neutral cwd itself carries no project CLAUDE.md.
    assert not (Path(cwd) / "CLAUDE.md").exists()
    # The assembled system prompt is passed verbatim (owned end-to-end).
    cmd = captured["cmd"]
    assert "--append-system-prompt" in cmd
    assert cmd[cmd.index("--append-system-prompt") + 1] == "SYSTEM-ASSEMBLED-BYTES"


def test_neutral_cwd_is_stable_and_empty() -> None:
    a = pt._neutral_cwd()
    b = pt._neutral_cwd()
    assert a == b  # reused across ticks, no per-tick churn
    assert not (Path(a) / "CLAUDE.md").exists()


def test_ambient_scan_finds_a_project_claude_md(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("# rogue persona\n")
    sub = tmp_path / "work"
    sub.mkdir()
    found = pt._ambient_claude_md_paths(str(sub))
    assert str(tmp_path / "CLAUDE.md") in found


def test_ambient_scan_clean_dir_is_empty_or_home_only(tmp_path: Path) -> None:
    """A dir with no CLAUDE.md up its own tree yields no *project* hits; the
    only possible entry is the user's ~/.claude/CLAUDE.md (env-dependent)."""
    found = pt._ambient_claude_md_paths(str(tmp_path))
    home_md = str(Path.home() / ".claude" / "CLAUDE.md")
    assert all(p == home_md for p in found)
