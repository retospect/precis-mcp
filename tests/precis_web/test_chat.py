"""Ask chat preflight and background turns."""

from __future__ import annotations

import asyncio
import re
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from starlette.types import Receive, Scope, Send

from precis import secrets
from precis.users import WebUser
from precis.utils.llm import router
from precis_web import chat_turns, create_app
from precis_web.config import WebConfig
from tests.precis_web.conftest import FakeRuntime, FakeStore


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

    spawned: list[int] = []

    def on_spawn(pid: int) -> None:
        spawned.append(pid)
        if sys.platform != "win32":
            assert os.getpgid(pid) == pid
            os.killpg(pid, signal.SIGKILL)

    # Windows has no POSIX process groups. Verify the callback's PID against
    # the child's own report there, and exercise real group cancellation on POSIX.
    child = (
        "import os; print(os.getpid()); raise SystemExit(1)"
        if sys.platform == "win32"
        else "import time; time.sleep(5)"
    )

    with pytest.raises(ClaudeProcessError) as exc:
        asyncio.run(
            run_claude_async(
                [sys.executable, "-c", child],
                binary=sys.executable,
                label="chat test child",
                timeout_s=5,
                error_cls=ClaudeProcessError,
                on_process_group=on_spawn,
                bare=True,
            )
        )
    if sys.platform == "win32":
        assert exc.value.returncode == 1
        assert spawned == [int(exc.value.stdout.strip())]
    else:
        assert exc.value.returncode == -signal.SIGKILL


_SID = "20261126-1234-abcd"
_CHAT = f"/chat/{_SID}"
_HX = {"HX-Request": "true"}


def _ask_client(runtime: Mock | FakeRuntime, abbrev: str = "owner") -> TestClient:
    app = create_app(runtime=runtime, web_config=WebConfig(owner="owner"))

    async def with_user(scope: Scope, receive: Receive, send: Send) -> None:
        scope.setdefault("state", {})["web_user"] = WebUser(
            id=1,
            login=abbrev,
            abbrev=abbrev,
            full_name=None,
            email=None,
            disabled_at=None,
            last_login_at=None,
            created_at=None,
            updated_at=None,
        )
        await app(scope, receive, send)

    return TestClient(with_user)


def _chat_ref(sid: str = _SID, *, owner: str = "owner") -> SimpleNamespace:
    return SimpleNamespace(
        id=42,
        slug=f"chat/{sid}/a",
        title="First question",
        meta={"chat_session": sid, "owner_abbrev": owner, "model": "opus"},
    )


def _chunk(ord_: int, text: str, author: str = "owner", **meta: Any) -> SimpleNamespace:
    return SimpleNamespace(
        ord=ord_,
        text=text,
        meta={"author": author, **meta},
        created_at=datetime.now(UTC),
    )


@pytest.fixture
def pages(runtime: Mock) -> TestClient:
    ref = _chat_ref()
    runtime.store.get_ref.return_value = ref
    runtime.store.list_refs.return_value = [ref]
    runtime.store.chunks.list_chunks_for_ref.return_value = []
    return _ask_client(runtime)


def test_send_poll_pending_then_answer(
    pages: TestClient, runtime: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn = chat_turns.ChatTurn("tok", f"chat/{_SID}/a", "a", "owner", "opus", 10, 42)
    turn.text = "Partial <answer>"
    turn.tool_chips = [{"name": "mcp__precis__get", "arguments": '{"id": "me123"}'}]

    def start(sid: str, lane: str, user: str, text: str, **kwargs: Any) -> str:
        assert (sid, lane, user, text) == (_SID, "a", "owner", "A question")
        assert kwargs["runtime"] is runtime
        chat_turns._TURNS[turn.token] = turn
        runtime.store.chunks.list_chunks_for_ref.return_value = [_chunk(0, text)]
        return turn.token

    monkeypatch.setattr(chat_turns, "start_turn", start)
    pending = pages.post(f"{_CHAT}/send", data={"text": "A question"}, headers=_HX)
    assert pending.status_code == 200
    assert "A question" in pending.text
    assert 'hx-trigger="every 1.5s"' in pending.text
    assert "Partial &lt;answer&gt;" in pending.text
    assert "Interrupted" not in pages.get(_CHAT).text

    poll_url = f"{_CHAT}/turn/{turn.token}"
    assert "mcp__precis__get" in pages.get(poll_url).text
    assert f'action="{poll_url}/stop"' in pending.text
    stopping = pages.post(f"{poll_url}/stop", headers=_HX)
    assert stopping.status_code == 200 and "Stopping" in stopping.text
    assert turn.stopping

    turn.status = "stopped"
    runtime.store.chunks.list_chunks_for_ref.return_value.append(
        _chunk(
            1,
            "Final answer",
            "asa",
            status="stopped",
            msg_id=turn.token,
            model="sonnet",
            cost_usd=0.25,
            tool_calls=1,
            duration_s=2.0,
        )
    )
    done = pages.get(poll_url)
    assert done.status_code == 200
    assert "Final answer" in done.text and "Stopped" in done.text
    assert "sonnet · $0.2500" in done.text and "1 tool calls · 2.0 s" in done.text
    assert "hx-trigger" not in done.text
    chat_turns._TURNS.clear()
    assert "Final answer" in pages.get(poll_url).text


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/chat"),
        ("POST", "/chat/new"),
        ("GET", _CHAT),
        ("POST", f"{_CHAT}/send"),
        ("GET", f"{_CHAT}/turn/tok"),
        ("POST", f"{_CHAT}/turn/tok/stop"),
        ("POST", f"{_CHAT}/continue"),
        ("POST", f"{_CHAT}/retry"),
    ],
)
def test_non_owner_403(runtime: Mock, method: str, path: str) -> None:
    assert _ask_client(runtime, "other").request(method, path).status_code == 403
    runtime.store.get_ref.assert_not_called()


def test_nav_has_ask_for_owner() -> None:
    runtime = FakeRuntime(FakeStore())
    owner = _ask_client(runtime).get("/drive")
    other = _ask_client(runtime, "other").get("/drive")
    assert owner.status_code == other.status_code == 200
    assert 'href="/chat"' in owner.text
    assert 'href="/chat"' not in other.text


def test_orphan_human_turn_offers_retry(
    pages: TestClient, runtime: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime.store.chunks.list_chunks_for_ref.return_value = [
        _chunk(0, "Original prompt")
    ]
    page = pages.get(_CHAT)
    assert "Interrupted" in page.text
    assert f'action="{_CHAT}/retry"' in page.text
    start = Mock(return_value="tok")
    monkeypatch.setattr(chat_turns, "start_turn", start)
    assert pages.post(f"{_CHAT}/retry", headers=_HX).status_code == 200
    start.assert_called_once_with(
        _SID, "a", "owner", "Original prompt", runtime=runtime
    )


def test_capped_turn_offers_continue(
    pages: TestClient, runtime: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime.store.chunks.list_chunks_for_ref.return_value = [
        _chunk(0, "Question"),
        _chunk(1, "Partial answer", "asa", status="capped"),
    ]
    page = pages.get(_CHAT)
    assert "Capped" in page.text
    assert f'action="{_CHAT}/continue"' in page.text
    start = Mock(return_value="tok")
    monkeypatch.setattr(chat_turns, "start_turn", start)
    assert pages.post(f"{_CHAT}/continue", headers=_HX).status_code == 200
    start.assert_called_once_with(_SID, "a", "owner", "Continue.", runtime=runtime)


def test_day_cap_refusal_renders_message(pages: TestClient, runtime: Mock) -> None:
    runtime.store.chunks.list_chunks_for_ref.return_value = [
        _chunk(0, "Last answer", "asa", cost_usd=30)
    ]
    response = pages.post(f"{_CHAT}/send", data={"text": "Try again"}, headers=_HX)
    assert response.status_code == 200
    assert "day cap reached" in response.text and "Try again" in response.text
    runtime.append.assert_not_called()


def test_desired_deletes_listed(pages: TestClient, runtime: Mock) -> None:
    runtime.store.chunks.list_chunks_for_ref.return_value = [
        _chunk(0, "Question"),
        _chunk(
            1,
            "Answer",
            "asa",
            desired_deletes=[
                {"kind": "memory", "id": "me123", "reason": "Duplicate <record>"}
            ],
        ),
    ]
    page = pages.get(_CHAT)
    assert "Deletes the assistant asked for" in page.text
    assert 'href="/refs/memory/me123">me123</a>' in page.text
    assert "Duplicate &lt;record&gt;" in page.text
    assert 'href="#chat-turn-2">Turn 2</a>' in page.text


def test_new_chat_redirects_to_minted_sid(
    pages: TestClient, runtime: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime.store.get_ref.return_value = None
    start = Mock(return_value="tok")
    monkeypatch.setattr(chat_turns, "start_turn", start)
    created = pages.post(
        "/chat/new", data={"text": "First prompt"}, follow_redirects=False
    )
    assert created.status_code == 303
    assert re.fullmatch(r"/chat/\d{8}-\d{4}-[0-9a-f]{4}", created.headers["location"])
    start.assert_called_once_with(
        created.headers["location"].split("/")[-1],
        "a",
        "owner",
        "First prompt",
        runtime=runtime,
    )


def test_chat_index_groups_owned_chats_with_todays_cost(
    pages: TestClient, runtime: Mock
) -> None:
    ref = _chat_ref()
    sibling = SimpleNamespace(**vars(ref))
    sibling.id = 43
    sibling.slug = f"chat/{_SID}/b"
    other = _chat_ref("other", owner="other")
    standalone = SimpleNamespace(
        id=44, title="Other conversation", slug="ordinary", meta=ref.meta
    )
    old = _chunk(1, "Prior answer", "asa", cost_usd=2)
    old.created_at -= timedelta(days=1)
    runtime.store.list_refs.return_value = [ref, sibling, other, standalone]
    runtime.store.chunks.list_chunks_for_ref.side_effect = [
        [_chunk(0, "Answer", "asa", cost_usd=0.5), old],
        [_chunk(0, "Sibling answer", "asa", cost_usd=0.25)],
    ]

    response = pages.get("/chat")
    assert response.status_code == 200
    assert response.text.count(f'href="{_CHAT}"') == 1
    assert "$0.75 today" in response.text
    assert 'href="/chat/other"' not in response.text
    assert "Other conversation" not in response.text
    assert "New chat" in response.text


def test_preflight_replaces_composer(
    pages: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chat_turns, "preflight", lambda: ["Claude is unavailable."])
    page = pages.get(_CHAT)
    assert "Claude is unavailable." in page.text
    assert 'name="text"' not in page.text


def test_cross_origin_send_refused(pages: TestClient, runtime: Mock) -> None:
    response = pages.post(
        f"{_CHAT}/send",
        data={"text": "Hello"},
        headers={"Origin": "https://example.org"},
    )
    assert response.status_code == 403
    runtime.append.assert_not_called()


def test_turn_token_must_belong_to_chat(pages: TestClient) -> None:
    chat_turns._TURNS["tok"] = chat_turns.ChatTurn(
        "tok", "chat/other/a", "a", "owner", "opus", 10, 99
    )
    url = f"{_CHAT}/turn/tok"
    assert pages.get(url).status_code == 404
    assert pages.post(f"{url}/stop").status_code == 404
    assert not chat_turns._TURNS["tok"].stopping
