"""A Discord turn never ends silently with only the working reaction (gr479812).

2026-10-12: asa reacted 🔄 to a message and never answered. The user-turn
capture hung inside ``PrecisClient.request`` holding the client lock; the
health ping timed out on that lock and ``restart()`` then blocked on the same
lock forever. Covers both halves: the bot's failure-visible path (exception,
timeout, error result) and the client no longer deadlocking behind a hung
subprocess.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import pytest

pytest.importorskip("discord")

from asa_bot import bot as bot_mod
from asa_bot.bot import AsaBot
from asa_bot.config import (
    CaptureConfig,
    Config,
    DiscordConfig,
    LLMConfig,
    PreambleConfig,
    PrecisConfig,
)
from asa_bot.precis_client import PrecisClient

_BOT_USER = object()


class _FakeChannel:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, content: str = "", **_: Any) -> None:
        self.sent.append(content)


class _FakeMessage:
    def __init__(self) -> None:
        self.id = 42
        self.thread = None
        self.channel = _FakeChannel()
        self.reactions: list[str] = ["🔄"]

    async def add_reaction(self, emoji: str) -> None:
        self.reactions.append(emoji)

    async def remove_reaction(self, emoji: str, member: Any) -> None:
        assert member is _BOT_USER
        self.reactions.remove(emoji)


def _bot(monkeypatch: pytest.MonkeyPatch, handle_one: Any) -> AsaBot:
    monkeypatch.setattr(AsaBot, "user", property(lambda self: _BOT_USER))
    bot = object.__new__(AsaBot)
    bot._cfg = Config(
        llm=LLMConfig(turn_timeout_seconds=0),
        discord=DiscordConfig(),
        precis=PrecisConfig(),
        preamble=PreambleConfig(),
        capture=CaptureConfig(),
    )
    monkeypatch.setattr(bot, "_handle_one", handle_one)
    return bot


def test_exception_swaps_reaction_and_posts_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(slug: str, message: Any) -> bool:
        raise ConnectionError("precis MCP closed stdout\nsecond line")

    msg: Any = _FakeMessage()
    asyncio.run(_bot(monkeypatch, boom)._run_turn("s", msg))

    assert msg.reactions == ["⚠️"]
    assert len(msg.channel.sent) == 1
    line = msg.channel.sent[0]
    assert "ConnectionError: precis MCP closed stdout second line" in line
    assert "\n" not in line


def test_hung_turn_times_out_visibly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bot_mod, "_TURN_DEADLINE_SLACK_S", 0.05)

    async def hang(slug: str, message: Any) -> bool:
        await asyncio.sleep(60)
        return True

    msg: Any = _FakeMessage()
    asyncio.run(_bot(monkeypatch, hang)._run_turn("s", msg))

    assert msg.reactions == ["⚠️"]
    assert len(msg.channel.sent) == 1
    assert "no reply within" in msg.channel.sent[0]


def test_error_result_marks_failed_without_second_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # _handle_one already posted "(no reply — …)" and returned False.
    async def errored(slug: str, message: Any) -> bool:
        return False

    msg: Any = _FakeMessage()
    asyncio.run(_bot(monkeypatch, errored)._run_turn("s", msg))

    assert msg.reactions == ["⚠️"]
    assert msg.channel.sent == []


def test_clean_reply_clears_working_reaction(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ok(slug: str, message: Any) -> bool:
        return True

    msg: Any = _FakeMessage()
    asyncio.run(_bot(monkeypatch, ok)._run_turn("s", msg))

    assert msg.reactions == []
    assert msg.channel.sent == []


# A precis "server" that reads requests and never answers: the hung leg.
_SILENT = [sys.executable, "-c", "import sys\nfor _ in sys.stdin: pass"]


def test_hung_precis_request_times_out_and_kills() -> None:
    async def run() -> None:
        client = PrecisClient(
            PrecisConfig(command=_SILENT, request_timeout_seconds=0.3)
        )
        await client._spawn()
        proc = client._proc
        with pytest.raises(TimeoutError, match="timed out"):
            await client.request("tools/list", {})
        assert proc is not None
        await asyncio.wait_for(proc.wait(), timeout=5)
        await client.stop()

    asyncio.run(run())


def test_restart_not_blocked_by_hung_request() -> None:
    async def run() -> None:
        client = PrecisClient(PrecisConfig(command=_SILENT, request_timeout_seconds=60))
        await client._spawn()
        old = client._proc
        stuck = asyncio.create_task(client.request("tools/call", {}))
        await asyncio.sleep(0.2)  # stuck now holds the lock, reading stdout
        await asyncio.wait_for(client.restart(), timeout=5)
        assert client._proc is not None and client._proc is not old
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(stuck, timeout=5)
        await client.stop()

    asyncio.run(run())


def test_leg_timeout_keeps_its_own_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bot_mod, "_TURN_DEADLINE_SLACK_S", 60)

    async def precis_timeout(slug: str, message: Any) -> bool:
        raise TimeoutError("precis MCP tools/call timed out after 120s")

    msg: Any = _FakeMessage()
    asyncio.run(_bot(monkeypatch, precis_timeout)._run_turn("s", msg))

    assert msg.reactions == ["⚠️"]
    assert "precis MCP tools/call timed out" in msg.channel.sent[0]


# Answers every request except method "hang".
_ANSWERS = [
    sys.executable,
    "-c",
    "import json, sys\n"
    "for line in sys.stdin:\n"
    "    m = json.loads(line)\n"
    "    if 'id' in m and m['method'] != 'hang':\n"
    "        print(json.dumps({'id': m['id'], 'result': {'ok': 1}}), flush=True)\n",
]


def test_request_after_timeout_kill_respawns() -> None:
    async def run() -> None:
        client = PrecisClient(
            PrecisConfig(command=_ANSWERS, request_timeout_seconds=0.5)
        )
        await client.start()
        old = client._proc
        with pytest.raises(TimeoutError):
            await client.request("hang", {})
        assert await client.request("tools/list", {}) == {"ok": 1}
        assert client._proc is not old
        await client.stop()

    asyncio.run(run())
