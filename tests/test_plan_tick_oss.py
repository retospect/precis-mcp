"""ADR 0046 unit-4b (②) — the ``/factory`` backend switch reaches the planner.

Under the default ANTHROPIC backend a tick spawns ``claude -p`` (covered by
``test_plan_tick_routing``); when ``PRECIS_LLM_BACKEND=openai`` (+ a base url)
the tick drives the precis verbs *in-process* over the OSS ``tools=`` loop,
binding its runtime context (parent todo / workspace / model / agentlog) via a
thread-isolated ContextVar instead of the subprocess env the OSS loop can't
carry. These tests cover: the context override + thread isolation, the
``stop_reason`` → ``PlanTickOutcome`` mapping, the executor honoring the
explicit ``resume_reason``, and ``run()`` taking the OSS path (no subprocess)
with the context bound around the loop.

DB-free: ``run()``'s planner-prompt / workspace / agentlog helpers are stubbed
and the OSS loop is a scripted fake, so no ``claude`` binary and no store.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from precis import agentlog
from precis.utils import inproc_context, workspace
from precis.utils.inproc_context import TickContext, tick_context
from precis.utils.llm.openai_tools import AgentLoopResult
from precis.workers.executors import claude_inproc as ci
from precis.workers.job_types import plan_tick as pt

# ── inproc_context: readers consult the ContextVar, env is the fallback ──


def test_current_is_none_without_a_tick() -> None:
    assert inproc_context.current() is None


def test_tick_context_overrides_env_readers(monkeypatch: pytest.MonkeyPatch) -> None:
    # Env carries DIFFERENT values; the in-process ctx must win.
    monkeypatch.setenv("PRECIS_CURRENT_TODO", "99")
    monkeypatch.setenv("PRECIS_WORKSPACE", "projects/env_one")
    monkeypatch.setenv("PRECIS_CURRENT_MODEL", "haiku")
    monkeypatch.setenv(agentlog.ENV_VAR, "99")
    ctx = TickContext(
        parent_todo=42,
        workspace="projects/ctx_one",
        model="sonnet",
        agentlog_id=7,
    )
    with tick_context(ctx):
        assert workspace.current_todo_from_env() == 42
        assert workspace.current_from_env() == "projects/ctx_one"
        assert workspace.current_model_from_env() == "sonnet"
        assert agentlog.current_from_env() == 7
    # After the block the binding is gone → env is read again.
    assert workspace.current_todo_from_env() == 99
    assert workspace.current_from_env() == "projects/env_one"
    assert workspace.current_model_from_env() == "haiku"
    assert agentlog.current_from_env() == 99


def test_env_fallback_when_no_ctx(monkeypatch: pytest.MonkeyPatch) -> None:
    # No ctx bound → readers behave exactly as before (byte-identical env path).
    monkeypatch.setenv("PRECIS_CURRENT_TODO", "5")
    monkeypatch.delenv("PRECIS_WORKSPACE", raising=False)
    assert inproc_context.current() is None
    assert workspace.current_todo_from_env() == 5
    assert workspace.current_from_env() is None


def test_partial_ctx_leaves_unset_fields_to_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A ctx with only parent_todo set must not mask the env for the others.
    monkeypatch.setenv("PRECIS_CURRENT_MODEL", "opus")
    with tick_context(TickContext(parent_todo=3)):
        assert workspace.current_todo_from_env() == 3
        assert workspace.current_model_from_env() == "opus"  # falls through to env


def test_context_is_thread_isolated() -> None:
    """Two threads bind different contexts concurrently; each reads its own —
    the property that makes the ContextVar safe under PRECIS_INPROC_CONCURRENCY."""
    seen: dict[str, int | None] = {}
    barrier = threading.Barrier(2)

    def worker(name: str, parent: int) -> None:
        with tick_context(TickContext(parent_todo=parent)):
            barrier.wait()  # both inside their own binding at once
            seen[name] = workspace.current_todo_from_env()

    t1 = threading.Thread(target=worker, args=("a", 11))
    t2 = threading.Thread(target=worker, args=("b", 22))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert seen == {"a": 11, "b": 22}


def test_non_positive_ctx_parent_is_ignored() -> None:
    with tick_context(TickContext(parent_todo=0)):
        assert workspace.current_todo_from_env() is None


# ── _oss_exit: stop_reason → (exit_code, resume_reason) ────────────────


def test_oss_exit_clean_answer() -> None:
    assert pt._oss_exit("stop") == (0, None)


def test_oss_exit_max_turns_is_resumable() -> None:
    assert pt._oss_exit("max_turns") == (1, "max_turns")


def test_oss_exit_error_is_hard_failure() -> None:
    assert pt._oss_exit("error") == (1, None)
    assert pt._oss_exit("something-else") == (1, None)


# ── executor honors the explicit resume_reason (OSS path) ──────────────


def test_resume_reason_honors_explicit_signal() -> None:
    # An OSS outcome sets resume_reason directly (no stream-json to parse).
    outcome = pt.PlanTickOutcome(
        exit_code=1,
        stdout="partial",
        stderr="",
        duration_s=1.0,
        resume_reason="max_turns",
    )
    assert ci._resume_reason(outcome, outcome.stdout) == "max_turns"


def test_resume_reason_clean_oss_outcome_is_none() -> None:
    outcome = pt.PlanTickOutcome(
        exit_code=0, stdout="done", stderr="", duration_s=1.0, resume_reason=None
    )
    assert ci._resume_reason(outcome, outcome.stdout) is None


def test_resume_reason_oss_error_bubbles() -> None:
    # exit 1, no resume_reason, plain text (not stream-json) → real failure.
    outcome = pt.PlanTickOutcome(
        exit_code=1, stdout="boom, not json", stderr="oops", duration_s=1.0
    )
    assert ci._resume_reason(outcome, outcome.stdout) is None


# ── run(): the OSS branch, context bound around the loop ───────────────


class _FakePrompts:
    system = "SYS"
    user = "USR"


class _FakeWorkspace:
    path = "projects/demo"


def _run_oss(
    monkeypatch: pytest.MonkeyPatch, *, stop_reason: str, with_workspace: bool = True
) -> tuple[pt.PlanTickOutcome, dict[str, Any]]:
    """Drive ``plan_tick.run`` under the OpenAI backend with the OSS loop
    scripted; returns the outcome plus what the loop observed."""
    import precis.workers.planner_prompt as planner_prompt
    from precis.utils.llm import router

    seen: dict[str, Any] = {}

    def fake_loop(**kw: Any) -> AgentLoopResult:
        # The context must be bound on THIS call — else children orphan.
        ctx = inproc_context.current()
        seen["ctx"] = ctx
        seen["model_id"] = kw["model"]
        return AgentLoopResult(
            final_text="tick output",
            turns_used=2,
            tool_calls_made=1,
            total_tokens=None,
            stop_reason=stop_reason,
        )

    def boom_run(*_a: Any, **_k: Any) -> Any:  # subprocess must NOT be reached
        raise AssertionError("OSS path must not spawn claude -p")

    monkeypatch.setenv("PRECIS_LLM_BACKEND", "openai")
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "https://oss.example/v1")
    for var in ("PRECIS_MODEL_OPUS", "PRECIS_MODEL_SONNET", "PRECIS_MODEL_HAIKU"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        planner_prompt, "build_planner_prompts", lambda *a, **k: _FakePrompts()
    )
    monkeypatch.setattr(
        pt,
        "_load_parent_workspace",
        lambda *a, **k: _FakeWorkspace() if with_workspace else None,
    )
    monkeypatch.setattr(pt, "_disable_prose_file_kind", lambda *a, **k: None)
    monkeypatch.setattr(agentlog, "open_log", lambda *a, **k: 55)
    monkeypatch.setattr(agentlog, "finalize_log", lambda *a, **k: None)
    monkeypatch.setattr(router, "run_oss_tool_loop", fake_loop)
    monkeypatch.setattr(pt.subprocess, "run", boom_run)

    outcome = pt.run(
        store=object(),
        job_ref_id=1,
        parent_ref_id=2,
        params={"model": "opus"},
    )
    return outcome, seen


def test_run_oss_binds_context_and_maps_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    outcome, seen = _run_oss(monkeypatch, stop_reason="stop")
    # Context was bound on the loop call, with the tick's identity.
    ctx = seen["ctx"]
    assert ctx is not None
    assert ctx.parent_todo == 2
    assert ctx.model == "opus"
    assert ctx.workspace == "projects/demo"
    assert ctx.agentlog_id == 55
    # Resolved OSS model id (cloud-super default) fed to the loop.
    assert seen["model_id"] == "claude-opus-4-8"
    # Clean answer → exit 0, no resume, final text captured.
    assert outcome.exit_code == 0
    assert outcome.stdout == "tick output"
    assert outcome.resume_reason is None
    # And the binding is torn down after the tick.
    assert inproc_context.current() is None


def test_run_oss_max_turns_maps_resumable(monkeypatch: pytest.MonkeyPatch) -> None:
    outcome, _ = _run_oss(monkeypatch, stop_reason="max_turns")
    assert outcome.exit_code == 1
    assert outcome.resume_reason == "max_turns"


def test_run_oss_no_workspace_leaves_ctx_workspace_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, seen = _run_oss(monkeypatch, stop_reason="stop", with_workspace=False)
    assert seen["ctx"].workspace is None
