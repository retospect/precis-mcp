"""Core context compatibility and bounded neutral web context."""

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from asa_bot.config import PreambleConfig
from asa_bot.preamble import build as discord_build
from asa_bot.preamble import discord_profile
from precis.chat.context import WEB_CHAT, ContextProfile, build


class Caller:
    """Deterministic tool replies used to capture the pre-refactor golden."""

    def __init__(self, recent: str | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.recent = recent

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append((name, arguments))
        if name == "get":
            if arguments.get("view") == "last-meta":
                return (
                    '```json\n{"stop_reason":"max_tokens","cache_read_tokens":0,'
                    '"cache_creation_tokens":42}\n```'
                )
            if "recent" in arguments.get("args", {}):
                return self.recent or "cv123~4 [human]\nHello\n\ncv123~5 [asa]\nWelcome"
            return "~1 human: greeting"
        return "Memory: " + ", ".join(arguments["tags"])


def inputs() -> dict[str, Any]:
    return {
        "conv_slug": "discord/1/2/3",
        "guild_name": "Workshop",
        "channel_name": "general",
        "thread_name": "Research",
        "author_handle": "Jane Doe",
        "soul": "A steady soul.",
        "tool_hints": "ignored",
    }


def test_discord_profile_matches_legacy_output() -> None:
    """Golden captured by running asa_bot.preamble.build before moving it."""
    cfg = PreambleConfig(operator_prefs="Use concise answers.")
    golden = (Path(__file__).parent / "fixtures/chat/discord_preamble.txt").read_text(
        encoding="utf-8"
    )
    core = Caller()
    bridge = Caller()
    assert (
        asyncio.run(build(precis=core, profile=discord_profile(cfg), **inputs()))
        == golden
    )
    assert asyncio.run(discord_build(precis=bridge, cfg=cfg, **inputs())) == golden
    assert core.calls == bridge.calls


def test_web_profile_omits_inner_life_and_clamps_hot() -> None:
    recent = "cc4 [human]\nKeep this user text\n\n" + "\n\n".join(
        f"cc{index} [assistant]\n" + "long answer " * 2000 for index in range(5, 12)
    )
    caller = Caller(recent)
    profile = replace(WEB_CHAT, operator_prefs="Use concise answers.")
    out = asyncio.run(build(precis=caller, profile=profile, **inputs()))
    assert "A steady soul" not in out
    assert "Inner life" not in out
    assert "internal-state" not in out
    assert "internal-thought" not in out
    assert "dream" not in out.lower()
    assert "Use concise answers." in out
    assert "You're an assistant replying in Web" in out
    assert "Keep this user text" in out
    for index in range(5, 12):
        assert f"id='cc{index}'" in out
    assert len(out.encode("utf-8")) <= 12000 * 3
    assert "long answer " * 400 not in out
    searches = [args for name, args in caller.calls if name == "search"]
    assert not any(
        tag
        in {"internal-state", "internal-thought", "DREAM:speculative", "speculative"}
        for args in searches
        for tag in args["tags"]
    )
    assert (
        sum(
            args["page_size"]
            for args in searches
            if args["tags"][0].startswith("sticky:")
        )
        == 10
    )
    assert any(args.get("args") == {"recent": 8} for _, args in caller.calls)
    assert any(
        args.get("args") == {"digest": 20, "skip_recent": 8} for _, args in caller.calls
    )


def test_web_profile_bounds_whole_prompt() -> None:
    out = asyncio.run(
        build(
            precis=Caller(),
            profile=replace(WEB_CHAT, operator_prefs="界" * 50000),
            **inputs(),
        )
    )
    assert len(out.encode("utf-8")) <= 12000 * 3
    assert out.endswith("[Context truncated]\n")


@pytest.mark.parametrize("section", ["inner_state", "thoughts", "dreams"])
def test_inner_sections_switch_independently(section: str) -> None:
    profile = {
        "inner_state": ContextProfile(inner_state=False),
        "thoughts": ContextProfile(thoughts=False),
        "dreams": ContextProfile(dreams=False),
    }[section]
    caller = Caller()
    out = asyncio.run(build(precis=caller, profile=profile, **inputs()))
    label = {
        "inner_state": "Current state of self",
        "thoughts": "Recent thoughts",
        "dreams": "While I was away",
    }[section]
    assert label not in out
    assert "## Inner life" in out


def test_all_sections_can_be_disabled() -> None:
    caller = Caller()
    profile = ContextProfile(
        soul=False,
        operator_preferences=False,
        stickies=False,
        inner_state=False,
        thoughts=False,
        dreams=False,
        user_note=False,
        conv_pointer=False,
        last_turn_signal=False,
        recent_turns=0,
        digest_turns=0,
    )
    assert asyncio.run(build(precis=caller, profile=profile, **inputs())) == "\n"
    assert caller.calls == []


@pytest.mark.parametrize("author", ["asa", "asa · briefing", "assistant"])
def test_web_clamps_legacy_turn_selectors(author: str) -> None:
    caller = Caller(f"~5 [{author}]\n" + "answer " * 4000)
    out = asyncio.run(build(precis=caller, profile=WEB_CHAT, **inputs()))
    assert "id='discord/1/2/3~5'" in out
    assert "answer " * 600 not in out
