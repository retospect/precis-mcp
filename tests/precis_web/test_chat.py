"""Ask chat preflight and background turns."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from precis import secrets
from precis.utils.llm import router
from precis_web import chat_turns


@pytest.fixture
def present(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    cfg = tmp_path / "chat_mcp.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setenv(chat_turns.MCP_CONFIG_ENV, str(cfg))
    monkeypatch.setattr(chat_turns.shutil, "which", lambda _n: "/usr/bin/claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("PRECIS_SECRETS_FILE_DIR", str(tmp_path))
    monkeypatch.setattr(secrets, "_STORE", None)
    return cfg


def test_preflight_all_present(present: Path) -> None:
    assert chat_turns.preflight() == []


def test_preflight_reports_missing_mcp_config(
    present: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(chat_turns.MCP_CONFIG_ENV)
    assert any(chat_turns.MCP_CONFIG_ENV in line for line in chat_turns.preflight())
    monkeypatch.setenv(chat_turns.MCP_CONFIG_ENV, str(present.parent / "gone.json"))
    assert any("does not exist" in line for line in chat_turns.preflight())


def test_preflight_reports_missing_api_key(
    present: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    lines = chat_turns.preflight()
    assert len(lines) == 1 and "ANTHROPIC_API_KEY" in lines[0]


def test_preflight_reports_invalid_utf8_api_key_file(
    present: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    (present.parent / "ANTHROPIC_API_KEY").write_bytes(b"\xff")

    assert chat_turns.preflight() == [
        "ANTHROPIC_API_KEY is not resolvable; store it on the /secrets page"
    ]


def test_preflight_reports_missing_claude(
    present: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chat_turns.shutil, "which", lambda _n: None)
    assert any("claude" in line for line in chat_turns.preflight())


@pytest.fixture
def runtime(present: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Mock]:
    runtime = Mock()
    runtime.store.get_ref.return_value = None
    runtime.dispatch_with_status.return_value = ("", False)
    append = Mock(return_value=SimpleNamespace(ref=SimpleNamespace(id=42)))
    runtime.append = append
    monkeypatch.setattr(chat_turns, "ConversationHandler", Mock(return_value=runtime))
    monkeypatch.setattr(runtime, "append_turn", append)
    monkeypatch.setattr(chat_turns.agentlog, "open_run", Mock(return_value=17))
    monkeypatch.setattr(chat_turns.agentlog, "close_run", Mock())
    monkeypatch.setattr(chat_turns, "_TURNS", {})
    yield runtime


def _result(**changes: Any) -> router.LlmResult:
    return router.LlmResult(
        **{
            "text": "Final answer",
            "cost_usd": 0.25,
            "turns_used": 2,
            "model": "opus",
            "tier": router.Tier.FRONTIER,
            **changes,
        }
    )


async def _event(req: router.LlmRequest, event: dict[str, Any]) -> None:
    assert req.on_event is not None
    await req.on_event(event)


async def _assistant(req: router.LlmRequest, *blocks: dict[str, Any]) -> None:
    await _event(req, {"type": "assistant", "message": {"content": list(blocks)}})


def _start(runtime: Mock, sid: str = "sid") -> chat_turns.ChatTurn:
    token = chat_turns.start_turn(sid, "a", "owner", "A question", runtime=runtime)
    turn = chat_turns.get_turn(token, "owner")
    assert turn is not None and turn.task is not None
    assert chat_turns.get_turn(token, "another") is None
    return turn


async def _wait(turn: chat_turns.ChatTurn) -> None:
    assert turn.task is not None
    await asyncio.wait_for(turn.task, timeout=5)


def test_turn_records_answer_with_cost_and_tool_chips(
    runtime: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run_turn() -> None:
        async def dispatch(req: router.LlmRequest) -> router.LlmResult:
            await _assistant(
                req,
                {
                    "type": "tool_use",
                    "name": "mcp__precis__get",
                    "input": {"id": "x" * 200},
                },
            )
            return _result()

        monkeypatch.setattr(chat_turns, "dispatch_async", dispatch)
        turn = _start(runtime)
        first = runtime.append.call_args.kwargs
        assert first["slug"] == "chat/sid/a"
        assert first["ref_meta"] == {
            "chat_session": "sid",
            "chat_lane": "a",
            "model": "opus",
            "max_usd": 10,
            "owner_abbrev": "owner",
            "title": "A question",
        }
        assert runtime.append.call_count == 1
        await _wait(turn)
        answer = runtime.append.call_args.kwargs
        assert runtime.append.call_count == 2 and answer["author"] == "asa"
        assert answer["body"] == "Final answer"
        assert answer["meta"]["status"] == "answer"
        assert answer["meta"]["cost_usd"] == 0.25
        assert answer["meta"]["turns"] == 2
        assert answer["meta"]["agentlog_id"] == 17
        assert answer["meta"]["tool_calls"] == 1
        assert turn.tool_chips[0]["name"] == "mcp__precis__get"
        assert len(turn.tool_chips[0]["arguments"]) == 120

    asyncio.run(run_turn())


def test_request_uses_api_key_and_denies_builtins_and_delete(
    runtime: Mock, present: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run_turn() -> None:
        from precis.utils import claude_agent

        launch = Mock()

        async def run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
            launch(argv, **kwargs)
            return SimpleNamespace(stdout="", stderr="")

        monkeypatch.setattr(claude_agent, "run_claude_async", run)
        monkeypatch.setattr(
            router, "_drop_unavailable_rungs", lambda ladder, model: (ladder, [])
        )
        monkeypatch.setattr(router, "_apply_cloud_throttle", lambda ladder: ladder)
        monkeypatch.setattr(
            router, "route", Mock(side_effect=AssertionError("unexpected fallback"))
        )
        monkeypatch.setattr(
            router, "resolve_chain", Mock(side_effect=AssertionError("unpinned chain"))
        )
        monkeypatch.setattr("precis.budget.breaker.gate_tier", lambda *a, **kw: None)
        monkeypatch.setattr(
            "precis.utils.llm.admit.check_dispatch", lambda *a, **kw: None
        )
        monkeypatch.setattr(
            "precis.utils.llm.local_serving.acquire", lambda model: None
        )
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake-OAuth")
        turn = _start(runtime)
        await _wait(turn)

        argv = launch.call_args.args[0]
        assert "--bare" in argv
        assert argv[argv.index("--tools") + 1] == ""
        assert argv[argv.index("--mcp-config") + 1] == str(present)
        assert argv[argv.index("--model") + 1] == "opus"
        assert argv[argv.index("--max-turns") + 1] == "40"
        assert float(argv[argv.index("--max-budget-usd") + 1]) == 10
        assert "You cannot delete." in argv[argv.index("--append-system-prompt") + 1]
        assert launch.call_args.kwargs["bare"] is True
        env = launch.call_args.kwargs["env"]
        assert env["ANTHROPIC_API_KEY"] == "sk-test"
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
        assert env["PRECIS_MCP_DENY_VERBS"] == "delete"
        assert env["PRECIS_ACTING_USER"] == "owner"
        assert env[chat_turns.agentlog.ENV_VAR] == "17"

    asyncio.run(run_turn())


def test_missing_api_key_is_an_error_not_a_fallback(
    runtime: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run_turn() -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake-OAuth")
        dispatch = Mock()
        monkeypatch.setattr(chat_turns, "dispatch_async", dispatch)
        turn = _start(runtime)
        await _wait(turn)
        dispatch.assert_not_called()
        assert turn.status == "error"
        assert "ANTHROPIC_API_KEY" in runtime.append.call_args.kwargs["body"]

    asyncio.run(run_turn())


def test_cap_hit_keeps_partial_and_never_defers(
    runtime: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run_turn() -> None:
        calls = Mock()

        async def dispatch(req: router.LlmRequest) -> router.LlmResult:
            calls()
            await _assistant(req, {"type": "text", "text": "Partial answer"})
            await _event(
                req,
                {
                    "type": "result",
                    "subtype": "error_max_budget_usd",
                    "total_cost_usd": 10,
                },
            )
            return _result(error="budget reached", cost_usd=None)

        monkeypatch.setattr(chat_turns, "dispatch_async", dispatch)
        turn = _start(runtime)
        await _wait(turn)
        calls.assert_called_once()
        assert turn.status == "capped"
        assert runtime.append.call_args.kwargs["body"] == "Partial answer"
        assert runtime.append.call_args.kwargs["meta"]["cost_usd"] == 10

    asyncio.run(run_turn())


def test_day_cap_refuses(runtime: Mock) -> None:
    async def run_turn() -> None:
        runtime.store.get_ref.return_value = SimpleNamespace(
            id=42, meta={"owner_abbrev": "owner"}
        )
        runtime.store.chunks.list_chunks_for_ref.return_value = [
            SimpleNamespace(
                meta={"author": "asa", "status": "capped", "cost_usd": 10},
                created_at=datetime.now(UTC),
            )
            for _ in range(3)
        ]
        with pytest.raises(RuntimeError, match="day cap reached"):
            _start(runtime)
        runtime.append.assert_not_called()

    asyncio.run(run_turn())


@pytest.fixture
def running(monkeypatch: pytest.MonkeyPatch) -> Callable[[int], asyncio.Event]:
    groups: dict[int, asyncio.Event] = {}

    async def dispatch(req: router.LlmRequest) -> router.LlmResult:
        pid = 100 + len(groups)
        killed = groups[pid] = asyncio.Event()
        assert req.on_process_group is not None
        await _assistant(req, {"type": "text", "text": "Partial answer"})
        req.on_process_group(pid)
        await killed.wait()
        return _result(error="killed")

    def kill(pid: int, sig: int) -> None:
        assert sig == chat_turns.signal.SIGKILL
        groups[pid].set()

    monkeypatch.setattr(chat_turns, "dispatch_async", dispatch)
    monkeypatch.setattr(chat_turns.os, "killpg", kill)
    return groups.__getitem__


async def _spawned(*turns: chat_turns.ChatTurn) -> None:
    async with asyncio.timeout(5):
        while any(turn.group_id is None for turn in turns):
            await asyncio.sleep(0.001)


def test_stop_kills_child(
    runtime: Mock, running: Callable[[int], asyncio.Event]
) -> None:
    async def run_turn() -> None:
        turn = _start(runtime)
        await _spawned(turn)
        chat_turns.stop_turn(turn.token)
        await _wait(turn)
        assert running(100).is_set()
        assert turn.status == "stopped"
        assert runtime.append.call_args.kwargs["body"] == "Partial answer"

    asyncio.run(run_turn())


def test_shutdown_kills_running_turns(
    runtime: Mock, running: Callable[[int], asyncio.Event]
) -> None:
    async def run_turn() -> None:
        first = _start(runtime)
        second = _start(runtime, "second")
        await _spawned(first, second)
        await asyncio.wait_for(chat_turns.shutdown(), timeout=5)
        assert running(100).is_set() and running(101).is_set()
        assert first.status == second.status == "stopped"

    asyncio.run(run_turn())


def test_denied_delete_is_recorded(
    runtime: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run_turn() -> None:
        deletion = {"kind": "memory", "id": "me123", "reason": "Duplicate"}

        async def dispatch(req: router.LlmRequest) -> router.LlmResult:
            await _assistant(
                req,
                {"type": "tool_use", "name": "mcp__precis__delete", "input": deletion},
            )
            return _result()

        monkeypatch.setattr(chat_turns, "dispatch_async", dispatch)
        await _wait(_start(runtime))
        assert runtime.append.call_args.kwargs["meta"]["desired_deletes"] == [deletion]

    asyncio.run(run_turn())


def test_stop_before_spawn_kills_group_and_admission_refuses_duplicate(
    runtime: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run_turn() -> None:
        spawning = asyncio.Event()
        allow_spawn = asyncio.Event()
        kill = Mock()
        monkeypatch.setattr(chat_turns.os, "killpg", kill)

        async def dispatch(req: router.LlmRequest) -> router.LlmResult:
            spawning.set()
            await allow_spawn.wait()
            assert req.on_process_group is not None
            req.on_process_group(101)
            return _result(error="killed")

        monkeypatch.setattr(chat_turns, "dispatch_async", dispatch)
        turn = _start(runtime)
        await asyncio.wait_for(spawning.wait(), timeout=5)
        with pytest.raises(RuntimeError, match="already running in this lane"):
            _start(runtime)
        chat_turns.stop_turn(turn.token)
        allow_spawn.set()
        await _wait(turn)
        kill.assert_called_once_with(101, chat_turns.signal.SIGKILL)
        assert turn.status == "stopped"
        assert runtime.append.call_count == 2

    asyncio.run(run_turn())


def test_streaming_child_has_its_own_process_group() -> None:
    import os
    import signal

    from precis.utils._claude_subprocess import ClaudeProcessError, run_claude_async

    def stop_group(pid: int) -> None:
        assert os.getpgid(pid) == pid
        os.killpg(pid, signal.SIGKILL)

    with pytest.raises(ClaudeProcessError) as exc:
        asyncio.run(
            run_claude_async(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                binary=sys.executable,
                label="chat test child",
                timeout_s=5,
                error_cls=ClaudeProcessError,
                on_process_group=stop_group,
                bare=True,
            )
        )
    assert exc.value.returncode == -signal.SIGKILL
