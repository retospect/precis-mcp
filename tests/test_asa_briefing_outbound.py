"""``AsaBot._handle_outbound`` routing between immediate post and
``BriefingBuffer`` buffering (gr51556).

Builds an ``AsaBot`` instance via ``object.__new__`` — bypassing
``discord.Client.__init__`` (which wants a real gateway/event-loop setup
this repo has no fixture for) — since only the outbound-routing method and
the two attributes it touches (``_briefing_buffer``, ``_post_message_ref``)
are under test. Mirrors the ``pytest.importorskip("discord")`` pattern
``tests/test_asa_split_for_discord.py`` already uses for the ``[asa]``
extra.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("discord")

from asa_bot.bot import AsaBot
from asa_bot.briefing_buffer import BriefingBuffer


def _bare_bot(*, timeout_seconds: float = 30.0) -> AsaBot:
    bot = object.__new__(AsaBot)
    posted: list[dict[str, Any]] = []

    async def fake_post(data: dict[str, Any]) -> None:
        posted.append(data)

    bot._post_message_ref = fake_post  # type: ignore[method-assign]
    bot._briefing_buffer = BriefingBuffer(
        poster=bot._post_briefing_parts, timeout_seconds=timeout_seconds
    )
    bot.posted = posted  # type: ignore[attr-defined]
    return bot


def _part(part: int, total: int = 2, ref_id: int = 1) -> dict[str, Any]:
    return {
        "ref_id": ref_id,
        "target": "discord/1/2/2",
        "author": "asa",
        "briefing_part": part,
        "briefing_parts": total,
        "briefing_date": "2026-08-13",
    }


def test_non_briefing_payload_posts_immediately() -> None:
    bot = _bare_bot()
    data = {"ref_id": 1, "target": "discord/1/2/2", "author": "asa"}

    asyncio.run(bot._handle_outbound(data))

    assert bot.posted == [data]  # type: ignore[attr-defined]


def test_multi_part_payload_buffers_until_complete_then_posts_in_order() -> None:
    bot = _bare_bot()
    p1, p2 = _part(1, ref_id=101), _part(2, ref_id=102)

    async def _run() -> None:
        await bot._handle_outbound(p2)  # out of order on the wire
        assert bot.posted == []  # type: ignore[attr-defined]
        await bot._handle_outbound(p1)

    asyncio.run(_run())

    assert [d["ref_id"] for d in bot.posted] == [101, 102]  # type: ignore[attr-defined]


def test_incomplete_multi_part_set_posts_what_arrived_after_timeout() -> None:
    bot = _bare_bot(timeout_seconds=0.05)
    p1 = _part(1, total=3, ref_id=201)
    p3 = _part(3, total=3, ref_id=203)

    async def _run() -> None:
        await bot._handle_outbound(p1)
        await bot._handle_outbound(p3)  # part 2's notify never arrives
        await asyncio.sleep(0.2)

    asyncio.run(_run())

    assert [d["ref_id"] for d in bot.posted] == [201, 203]  # type: ignore[attr-defined]
