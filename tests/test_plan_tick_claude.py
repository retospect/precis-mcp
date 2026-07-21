"""plan_tick's ``claude -p`` transport, restored as a router-selectable option.

Under the default ANTHROPIC backend ``select_transport`` routes the tick to
``CLAUDE_AGENT`` — the real Claude Code agent (MCP tools, OAuth) — and plan_tick
drives it *through* ``router.dispatch`` (:func:`plan_tick._run_claude_tick`)
rather than hand-building a ``claude`` command. These tests cover: the request
the tick binds (tier / prompt / stream-json / neutral cwd / env-overlay context),
the ``LlmResult`` → ``PlanTickOutcome`` mapping (clean / max_turns / budget /
timeout / breaker-pause / hard error), and the re-added claude-only helpers
(env overlay, draft kind-gate, neutral cwd, ambient CLAUDE.md scan).

DB-free: ``run()``'s planner-prompt / workspace / agentlog helpers are stubbed
and ``dispatch`` is a scripted fake, so no ``claude`` binary and no store.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from precis import agentlog
from precis.utils.llm import router
from precis.utils.llm.router import LlmResult, Tier
from precis.workers.job_types import plan_tick as pt


class _FakePrompts:
    system = "SYS"
    user = "USR"


class _FakeWorkspace:
    path = "projects/demo"
    doc_type = "paper"  # not 'patent' → the claims-digest refresh no-ops


def _clean_result() -> LlmResult:
    return LlmResult(
        text="answer",
        cost_usd=0.4,
        turns_used=3,
        model="claude-opus-4-8",
        tier=Tier.CLOUD_SUPER,
        raw_text="<stream-json>",
        terminal_reason=None,
    )


def _run_claude(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: LlmResult,
    with_workspace: bool = True,
    mcp_config: str | None = "mcp.json",
) -> tuple[pt.PlanTickOutcome, dict[str, Any]]:
    """Drive ``plan_tick.run`` on the ANTHROPIC backend with ``dispatch``
    scripted; returns the outcome plus the captured ``LlmRequest``."""
    import precis.workers.planner_prompt as planner_prompt

    seen: dict[str, Any] = {}

    def fake_dispatch(req: Any) -> LlmResult:
        seen["req"] = req
        return result

    # ANTHROPIC backend → CLAUDE_AGENT transport (the default; be explicit so a
    # stray env in the runner can't flip us onto the OSS branch).
    monkeypatch.delenv("PRECIS_LLM_BACKEND", raising=False)
    if mcp_config is None:
        monkeypatch.delenv("PRECIS_MCP_CONFIG", raising=False)
    else:
        monkeypatch.setenv("PRECIS_MCP_CONFIG", mcp_config)
    monkeypatch.setattr(
        planner_prompt, "build_planner_prompts", lambda *a, **k: _FakePrompts()
    )
    monkeypatch.setattr(
        pt,
        "_load_parent_workspace",
        lambda *a, **k: _FakeWorkspace() if with_workspace else None,
    )
    monkeypatch.setattr(agentlog, "open_log", lambda *a, **k: 55)
    monkeypatch.setattr(agentlog, "finalize_log", lambda *a, **k: None)
    monkeypatch.setattr(router, "dispatch", fake_dispatch)

    outcome = pt.run(
        store=object(),
        job_ref_id=1,
        parent_ref_id=2,
        params={"model": "opus"},
    )
    return outcome, seen


# ── run(): the claude branch binds the request + maps the outcome ──────


def test_run_claude_binds_request_and_maps_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome, seen = _run_claude(monkeypatch, result=_clean_result())
    req = seen["req"]
    # Tier is the tag's cloud tier; tools + stream-json + neutral cwd + source.
    assert req.tier is Tier.CLOUD_SUPER
    assert req.tools_needed is True
    assert req.prompt == "USR"
    assert req.system_prompt == "SYS"
    assert req.output_format == "stream-json"
    assert "--verbose" in req.extra_args
    assert req.source == "plan_tick"
    assert req.ref_id == 2
    assert req.cwd and Path(req.cwd).is_dir()
    assert not (Path(req.cwd) / "CLAUDE.md").exists()
    # mcp_config is absolutized so the neutral cwd can't strand a relative path.
    assert req.mcp_config == str(Path("mcp.json").resolve())
    # env-overlay carries the tick's runtime context for the spawned MCP server.
    assert req.env_overlay["PRECIS_CURRENT_TODO"] == "2"
    assert req.env_overlay["PRECIS_CURRENT_MODEL"] == "opus"
    assert req.env_overlay["PRECIS_WORKSPACE"] == "projects/demo"
    assert req.env_overlay[agentlog.ENV_VAR] == "55"
    # Clean run → exit 0, raw stream-json captured as stdout, no resume.
    assert outcome.exit_code == 0
    assert outcome.stdout == "<stream-json>"
    assert outcome.resume_reason is None


def test_run_claude_no_workspace_omits_workspace_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, seen = _run_claude(monkeypatch, result=_clean_result(), with_workspace=False)
    assert "PRECIS_WORKSPACE" not in seen["req"].env_overlay


def test_run_claude_missing_mcp_config_leaves_it_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, seen = _run_claude(monkeypatch, result=_clean_result(), mcp_config=None)
    assert seen["req"].mcp_config is None


@pytest.mark.parametrize(
    ("terminal_reason", "expected_resume"),
    [
        ("max_turns", "max_turns"),
        ("error_max_budget_usd", "budget"),
        ("completed", None),
    ],
)
def test_run_claude_terminal_reason_maps_resume(
    terminal_reason: str,
    expected_resume: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    res = LlmResult(
        text="partial",
        cost_usd=1.0,
        turns_used=60,
        model="claude-opus-4-8",
        tier=Tier.CLOUD_SUPER,
        raw_text="<stream>",
        terminal_reason=terminal_reason,
    )
    outcome, _ = _run_claude(monkeypatch, result=res)
    if expected_resume is None:
        # 'completed' is a process-teardown exit after a finished turn → clean.
        assert outcome.exit_code == 0
        assert outcome.resume_reason is None
    else:
        assert outcome.exit_code == 1
        assert outcome.resume_reason == expected_resume


def test_run_claude_hard_error_bubbles(monkeypatch: pytest.MonkeyPatch) -> None:
    res = LlmResult(
        text="",
        cost_usd=None,
        turns_used=None,
        model="claude-opus-4-8",
        tier=Tier.CLOUD_SUPER,
        error="claude -p (agent) exited 1: boom",
    )
    outcome, _ = _run_claude(monkeypatch, result=res)
    assert outcome.exit_code == 1
    assert outcome.resume_reason is None
    assert "boom" in outcome.stderr


def test_run_claude_timeout_is_resumable(monkeypatch: pytest.MonkeyPatch) -> None:
    res = LlmResult(
        text="",
        cost_usd=None,
        turns_used=None,
        model="claude-opus-4-8",
        tier=Tier.CLOUD_SUPER,
        error="claude -p (agent) timed out after 1800s",
    )
    outcome, _ = _run_claude(monkeypatch, result=res)
    assert outcome.exit_code == 1
    assert outcome.resume_reason == "timeout"


def test_run_claude_breaker_pause_is_resumable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    res = LlmResult(
        text="",
        cost_usd=None,
        turns_used=None,
        model="claude-opus-4-8",
        tier=Tier.CLOUD_SUPER,
        error="daily dollar cap reached",
        paused=True,
    )
    outcome, _ = _run_claude(monkeypatch, result=res)
    assert outcome.exit_code == 1
    assert outcome.resume_reason == "paused"


# ── _claude_exit: the outcome mapping in isolation ─────────────────────


def _res(**kw: Any) -> LlmResult:
    base: dict[str, Any] = {
        "text": "",
        "cost_usd": None,
        "turns_used": None,
        "model": "m",
        "tier": Tier.CLOUD_SUPER,
    }
    base.update(kw)
    return LlmResult(**base)


def test_claude_exit_clean() -> None:
    assert pt._claude_exit(_res(terminal_reason=None)) == (0, None)


def test_claude_exit_completed_is_clean() -> None:
    assert pt._claude_exit(_res(terminal_reason="completed")) == (0, None)


def test_claude_exit_max_turns() -> None:
    assert pt._claude_exit(_res(terminal_reason="max_turns")) == (1, "max_turns")


def test_claude_exit_budget() -> None:
    assert pt._claude_exit(_res(terminal_reason="error_max_budget_usd")) == (
        1,
        "budget",
    )


def test_claude_exit_paused_wins_over_error() -> None:
    assert pt._claude_exit(_res(error="capped", paused=True)) == (1, "paused")


def test_claude_exit_other_error_subtype_fails() -> None:
    assert pt._claude_exit(_res(terminal_reason="error_during_execution")) == (1, None)


# ── env overlay + draft kind-gate ──────────────────────────────────────


def test_tick_env_overlay_core_context() -> None:
    overlay = pt._tick_env_overlay(
        store=object(),
        parent_ref_id=42,
        model="sonnet",
        agentlog_id=7,
        workspace=_FakeWorkspace(),
    )
    assert overlay["PRECIS_CURRENT_TODO"] == "42"
    assert overlay["PRECIS_CURRENT_MODEL"] == "sonnet"
    assert overlay["PRECIS_WORKSPACE"] == "projects/demo"
    assert overlay[agentlog.ENV_VAR] == "7"


def test_disable_prose_file_kind_gates_tex(monkeypatch: pytest.MonkeyPatch) -> None:
    import precis.workers.planner_prompt as planner_prompt

    monkeypatch.setattr(
        planner_prompt, "bound_draft", lambda store, rid: ("frypat", "Title", "tex")
    )
    monkeypatch.delenv("PRECIS_KINDS_DISABLED", raising=False)
    overlay: dict[str, str] = {}
    pt._disable_prose_file_kind(object(), 1, overlay)
    assert overlay["PRECIS_KINDS_DISABLED"].startswith("tex:")
    assert "frypat" in overlay["PRECIS_KINDS_DISABLED"]


def test_disable_prose_file_kind_merges_operator_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import precis.workers.planner_prompt as planner_prompt

    monkeypatch.setattr(
        planner_prompt, "bound_draft", lambda store, rid: ("book", "T", "md")
    )
    monkeypatch.setenv("PRECIS_KINDS_DISABLED", "web")
    overlay: dict[str, str] = {}
    pt._disable_prose_file_kind(object(), 1, overlay)
    val = overlay["PRECIS_KINDS_DISABLED"]
    assert val.startswith("web,markdown:")


def test_disable_prose_file_kind_no_draft_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import precis.workers.planner_prompt as planner_prompt

    monkeypatch.setattr(planner_prompt, "bound_draft", lambda store, rid: None)
    overlay: dict[str, str] = {}
    pt._disable_prose_file_kind(object(), 1, overlay)
    assert overlay == {}


# ── neutral cwd + ambient CLAUDE.md scan (ADR 0051 §12) ────────────────


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
