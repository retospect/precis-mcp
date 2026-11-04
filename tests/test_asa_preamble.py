"""Operator-preferences injection — the generic replacement for baking
deployment-specific user detail into the source.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock

import pytest

from asa_bot.config import PreambleConfig
from asa_bot.preamble import (
    _fetch_user_note,
    _render_conv_pointer,
    _render_inner_life,
    _render_operator_prefs,
    _render_user_note,
    _user_note_tag,
)
from asa_bot.precis_client import PrecisClient
from precis.store.types import Tag


def test_empty_by_default():
    assert _render_operator_prefs(PreambleConfig()) == ""


def test_inline_prefs_render_as_block():
    cfg = PreambleConfig(operator_prefs="Call me Sam. Terse answers.")
    out = _render_operator_prefs(cfg)
    assert out.startswith("## Operator preferences")
    assert "Call me Sam. Terse answers." in out


def test_path_wins_over_inline(tmp_path):
    p = tmp_path / "prefs.md"
    p.write_text("From the file.", encoding="utf-8")
    cfg = PreambleConfig(operator_prefs="inline", operator_prefs_path=str(p))
    out = _render_operator_prefs(cfg)
    assert "From the file." in out
    assert "inline" not in out


def test_unreadable_path_falls_back_to_inline(tmp_path):
    cfg = PreambleConfig(
        operator_prefs="inline fallback",
        operator_prefs_path=str(tmp_path / "does-not-exist.md"),
    )
    assert "inline fallback" in _render_operator_prefs(cfg)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="hardcoded POSIX '.asa/SOUL.md' suffix — default soul_path"
    " joins with os.sep, which is a backslash on Windows",
)
def test_generic_defaults_have_no_personal_paths():
    # Defaults must not hard-code any deployment's home/user.
    cfg = PreambleConfig()
    assert "/Users/hermes" not in cfg.soul_path
    assert cfg.soul_path.endswith(".asa/SOUL.md")


# ── platform param (asa_slack reuses build() unchanged; see conv pointer) ──


def test_platform_defaults_to_discord_unchanged():
    out = _render_conv_pointer(
        "discord/1/2/3",
        guild_name="g",
        channel_name="c",
        thread_name=None,
        author_handle="someone",
    )
    assert "replying in Discord" in out


def test_platform_slack():
    out = _render_conv_pointer(
        "slack/T1/C2/171000.0",
        guild_name="workshop",
        channel_name="general",
        thread_name=None,
        author_handle="Jane (<@U1>, human)",
        platform="Slack",
    )
    assert "replying in Slack" in out
    assert "replying in Discord" not in out


# ── gr553220: no decay-timer fiction in the inner-life instructions ──
# Re-tagging never wrote refreshed_at (touch_ref has no callers; tag
# writes never touch refs.*), so "re-tag to bump its decay timer" was
# an instruction to do nothing. The preamble must not teach it.


def test_inner_life_no_decay_timer_claims():
    out = _render_inner_life("state", "thoughts", "dreams")
    assert "decay timer" not in out
    assert "bump its decay" not in out
    assert "let it decay" not in out


@pytest.mark.parametrize(
    ("author", "tag"),
    [
        ("Reto Stamm (<@UP4JTH873>, human)", "user:reto-stamm-up4jth873-human"),
        ("Rocky (<@U0AKBJ0A0VA>, bot)", "user:rocky-u0akbj0a0va-bot"),
        ("Jane Doe", "user:jane-doe"),
        ("reto.stamm", "user:reto.stamm"),
        ("田中 太郎", "user:4c171aab9beb"),
        ("山田 花子", "user:0144d8ace5ee"),
    ],
)
def test_user_note_reads_writes_and_prompt_use_valid_label(
    author: str, tag: str
) -> None:
    precis = AsyncMock(spec=PrecisClient)
    precis.call_tool.side_effect = ["no memory entries", "created memory"]
    asyncio.run(_fetch_user_note(precis, author))
    search_call, put_call = precis.call_tool.await_args_list
    assert search_call.args == (
        "search",
        {"kind": "memory", "tags": [tag], "page_size": 10},
    )
    assert put_call.args[0] == "put"
    assert put_call.args[1]["tags"] == [tag]
    assert author in put_call.args[1]["text"]
    assert put_call.args[1]["args"] == {"auto_refresh_days": 180}
    assert str(Tag.parse_strict(tag, kind="memory")) == tag
    for blob in ("no memory entries", "existing profile note"):
        prompt = _render_user_note(blob, author)
        assert f"tags=[{tag!r}]" in prompt
        assert author in prompt


def test_user_note_error_is_not_an_empty_search() -> None:
    precis = AsyncMock(spec=PrecisClient)
    error = "[error:BadInput] tag value must not contain whitespace (no spaces)"
    precis.call_tool.return_value = error
    assert asyncio.run(_fetch_user_note(precis, "Jane Doe")) == error
    precis.call_tool.assert_awaited_once()


def test_user_note_empty_slugs_have_distinct_stable_tags() -> None:
    authors = ("田中 太郎", "山田 花子")
    tags = [_user_note_tag(author) for author in authors]
    assert len(set(tags)) == len(authors)
    for author, tag in zip(authors, tags, strict=True):
        assert tag.startswith("user:")
        assert tag.removeprefix("user:")
        assert _user_note_tag(author) == tag
        assert str(Tag.parse_strict(tag, kind="memory")) == tag


def test_user_note_with_no_inside_first_line_renders_content() -> None:
    blob = "Prefers no meetings before noon.\nWorks on robotics."
    precis = AsyncMock(spec=PrecisClient)
    precis.call_tool.return_value = blob
    assert asyncio.run(_fetch_user_note(precis, "Jane Doe")) == blob
    precis.call_tool.assert_awaited_once()
    prompt = _render_user_note(blob, "Jane Doe")
    assert blob in prompt
    assert "(no notes yet on this user)" not in prompt
