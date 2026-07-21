"""plan_tick's in-process OSS ``tools=`` loop branch (the OpenAI backend).

Under a tools-capable OSS backend (``PRECIS_LLM_BACKEND=openai``) the tick runs
in-process over the OSS ``tools=`` loop rather than as ``claude -p`` (the
ANTHROPIC-backend branch, covered by ``test_plan_tick_claude``).
The tick goes *through* ``router.dispatch`` (so it gains the breaker gate +
route-log), which runs the OSS loop synchronously in-thread; the tick binds its
runtime context (parent todo / workspace / model / agentlog / the draft
prose-file kind-gate) via a thread-isolated ContextVar around the dispatch call.
These tests cover: the context override + thread isolation, the per-tick
kind-gate honored by the runtime, the ``LlmResult`` → ``PlanTickOutcome``
mapping (pause / stop / max_turns / error), the executor honoring the explicit
``resume_reason``, and ``run()`` binding the context around the loop.
The backend is set to ``openai`` here only to pin the tag's cloud tier so the
resolved model id is deterministic.

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
    monkeypatch: pytest.MonkeyPatch,
    *,
    stop_reason: str,
    with_workspace: bool = True,
    draft: tuple[str, str, str] | None = None,
) -> tuple[pt.PlanTickOutcome, dict[str, Any]]:
    """Drive ``plan_tick.run`` under the OpenAI backend with the OSS loop
    scripted; returns the outcome plus what the loop observed. ``dispatch`` is
    NOT stubbed — the real router runs, calling the stubbed ``run_oss_tool_loop``
    — so the breaker/admission/slot path is exercised (dark without a store)."""
    import precis.workers.planner_prompt as planner_prompt
    from precis.utils.llm import router

    seen: dict[str, Any] = {}

    def fake_loop(**kw: Any) -> AgentLoopResult:
        # The context must be bound on THIS call — else children orphan.
        seen["ctx"] = inproc_context.current()
        seen["model_id"] = kw["model"]
        seen["tool_less"] = kw.get("tool_less")
        return AgentLoopResult(
            final_text="tick output",
            turns_used=2,
            tool_calls_made=1,
            total_tokens=None,
            stop_reason=stop_reason,
        )

    monkeypatch.setenv("PRECIS_LLM_BACKEND", "openai")
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "https://oss.example/v1")
    # Non-empty so the tick advertises precis tools (tool_less=False).
    monkeypatch.setenv("PRECIS_MCP_CONFIG", "mcp.json")
    for var in ("PRECIS_MODEL_OPUS", "PRECIS_MODEL_SONNET", "PRECIS_MODEL_HAIKU"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        planner_prompt, "build_planner_prompts", lambda *a, **k: _FakePrompts()
    )
    monkeypatch.setattr(planner_prompt, "bound_draft", lambda store, rid: draft)
    monkeypatch.setattr(
        pt,
        "_load_parent_workspace",
        lambda *a, **k: _FakeWorkspace() if with_workspace else None,
    )
    monkeypatch.setattr(agentlog, "open_log", lambda *a, **k: 55)
    monkeypatch.setattr(agentlog, "finalize_log", lambda *a, **k: None)
    monkeypatch.setattr(router, "run_oss_tool_loop", fake_loop)

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
    # PRECIS_MCP_CONFIG set → precis tools advertised (not a bare completion).
    assert seen["tool_less"] is False
    # No draft bound → no per-tick kind prohibition.
    assert ctx.disabled_kinds == ()
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


def test_run_oss_bound_draft_gates_prose_kind_on_ctx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A tex-format draft bound to the tick → the ctx prohibits the `tex` file
    # kind for the tick, with the what-to-do-instead hint (the OSS twin of the
    # claude path's PRECIS_KINDS_DISABLED entry).
    _, seen = _run_oss(
        monkeypatch, stop_reason="stop", draft=("frypat", "Title", "tex")
    )
    disabled = dict(seen["ctx"].disabled_kinds)
    assert "tex" in disabled
    assert "frypat" in disabled["tex"]


def test_run_oss_breaker_pause_is_resumable(monkeypatch: pytest.MonkeyPatch) -> None:
    # A router-level pause (breaker / all-slots-busy) folds into a resumable
    # `paused` outcome rather than a hard failure — the executor backs off and
    # re-mints when the window clears. Stub `dispatch` itself to return paused.
    import precis.workers.planner_prompt as planner_prompt
    from precis.utils.llm import router
    from precis.utils.llm.router import LlmResult, Tier

    monkeypatch.setenv("PRECIS_LLM_BACKEND", "openai")
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "https://oss.example/v1")
    monkeypatch.setattr(
        planner_prompt, "build_planner_prompts", lambda *a, **k: _FakePrompts()
    )
    monkeypatch.setattr(planner_prompt, "bound_draft", lambda store, rid: None)
    monkeypatch.setattr(pt, "_load_parent_workspace", lambda *a, **k: _FakeWorkspace())
    monkeypatch.setattr(agentlog, "open_log", lambda *a, **k: 55)
    monkeypatch.setattr(agentlog, "finalize_log", lambda *a, **k: None)
    monkeypatch.setattr(
        router,
        "dispatch",
        lambda req: LlmResult(
            text="",
            cost_usd=None,
            turns_used=None,
            model="m",
            tier=Tier.CLOUD_SUPER,
            error="daily dollar cap reached",
            paused=True,
        ),
    )
    outcome = pt.run(
        store=object(), job_ref_id=1, parent_ref_id=2, params={"model": "opus"}
    )
    assert outcome.exit_code == 1
    assert outcome.resume_reason == "paused"


# ── _oss_exit_from_result: LlmResult → (exit_code, resume_reason) ──────


def _oss_result(**kw: Any) -> Any:
    from precis.utils.llm.router import LlmResult, Tier

    base: dict[str, Any] = {
        "text": "",
        "cost_usd": None,
        "turns_used": None,
        "model": "m",
        "tier": Tier.CLOUD_SUPER,
    }
    base.update(kw)
    return LlmResult(**base)


def test_oss_exit_from_result_pause() -> None:
    assert pt._oss_exit_from_result(_oss_result(error="capped", paused=True)) == (
        1,
        "paused",
    )


def test_oss_exit_from_result_error() -> None:
    assert pt._oss_exit_from_result(_oss_result(error="boom", stop_reason="error")) == (
        1,
        None,
    )


def test_oss_exit_from_result_stop() -> None:
    assert pt._oss_exit_from_result(_oss_result(stop_reason="stop")) == (0, None)


def test_oss_exit_from_result_max_turns() -> None:
    assert pt._oss_exit_from_result(_oss_result(stop_reason="max_turns")) == (
        1,
        "max_turns",
    )


# ── the per-tick kind-gate, honored by the runtime (in-process) ──────


def test_inproc_gate_rejects_disabled_kind(runtime: Any) -> None:
    hint = "write prose into the bound draft instead"
    with tick_context(TickContext(disabled_kinds=(("tex", hint),))):
        out = runtime.dispatch("get", {"kind": "tex", "id": "x"})
    assert "disabled for this tick" in out
    assert hint in out


def test_inproc_gate_is_noop_without_a_tick(runtime: Any) -> None:
    # No tick bound → the ContextVar gate is inert; whatever tex's normal
    # resolution says, it is NOT the tick-gate message.
    out = runtime.dispatch("get", {"kind": "tex", "id": "x"})
    assert "disabled for this tick" not in out


def test_inproc_gate_other_kinds_pass(runtime: Any) -> None:
    # A tick that gates `tex` must not gate a different kind.
    with tick_context(TickContext(disabled_kinds=(("tex", "h"),))):
        out = runtime.dispatch("get", {"kind": "markdown", "id": "x"})
    assert "disabled for this tick" not in out
