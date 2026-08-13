"""``asa_bot.briefing_buffer.BriefingBuffer`` — pure-asyncio, no discord import.

Covers gr51556: multi-part briefing NOTIFYs must post in ascending part
order once complete, and must not stall forever if a part's NOTIFY is
dropped (a listener reconnect between two parts, or any future consumer
that stops being strictly serial).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from asa_bot.briefing_buffer import BriefingBuffer


def _payload(
    part: int, total: int = 3, target: str = "discord/1/2/2"
) -> dict[str, Any]:
    return {
        "ref_id": 100 + part,
        "target": target,
        "author": "asa",
        "briefing_part": part,
        "briefing_parts": total,
        "briefing_date": "2026-08-13",
    }


class _FakePoster:
    """Records each flush as the ordered list of payloads it received."""

    def __init__(self) -> None:
        self.flushes: list[list[dict[str, Any]]] = []

    async def __call__(self, payloads: list[dict[str, Any]]) -> None:
        self.flushes.append(payloads)


def test_parts_arriving_in_order_post_in_order() -> None:
    poster = _FakePoster()
    buf = BriefingBuffer(poster=poster, timeout_seconds=30.0)

    async def _run() -> None:
        await buf.add(_payload(1))
        await buf.add(_payload(2))
        assert poster.flushes == []  # still waiting on part 3
        await buf.add(_payload(3))

    asyncio.run(_run())

    assert len(poster.flushes) == 1
    got_parts = [p["briefing_part"] for p in poster.flushes[0]]
    assert got_parts == [1, 2, 3]


def test_parts_arriving_out_of_order_post_in_ascending_order() -> None:
    poster = _FakePoster()
    buf = BriefingBuffer(poster=poster, timeout_seconds=30.0)

    async def _run() -> None:
        await buf.add(_payload(3))
        await buf.add(_payload(1))
        await buf.add(_payload(2))

    asyncio.run(_run())

    assert len(poster.flushes) == 1
    got_parts = [p["briefing_part"] for p in poster.flushes[0]]
    assert got_parts == [1, 2, 3]  # ascending, regardless of arrival order


def test_incomplete_set_flushes_on_timeout_in_order_and_warns(
    caplog: Any,
) -> None:
    poster = _FakePoster()
    buf = BriefingBuffer(poster=poster, timeout_seconds=0.05)

    async def _run() -> None:
        await buf.add(_payload(1))
        await buf.add(_payload(3))  # part 2's NOTIFY was "dropped"
        await asyncio.sleep(0.2)  # let the timeout fire

    with caplog.at_level(logging.WARNING, logger="asa_bot.briefing_buffer"):
        asyncio.run(_run())

    assert len(poster.flushes) == 1
    got_parts = [p["briefing_part"] for p in poster.flushes[0]]
    assert got_parts == [1, 3]  # whatever arrived, in ascending order
    assert any("part(s) [2]" in rec.message for rec in caplog.records)


def test_timeout_is_a_noop_once_the_set_already_flushed() -> None:
    """A completion-flush must beat a same-key timeout that fires later —
    the set posts exactly once, never twice."""
    poster = _FakePoster()
    buf = BriefingBuffer(poster=poster, timeout_seconds=0.05)

    async def _run() -> None:
        await buf.add(_payload(1))
        await buf.add(_payload(2))
        await buf.add(_payload(3))  # completes before the timeout fires
        await asyncio.sleep(0.2)  # give the (now-cancelled) timeout a chance

    asyncio.run(_run())

    assert len(poster.flushes) == 1  # not two


def test_independent_targets_buffer_separately() -> None:
    poster = _FakePoster()
    buf = BriefingBuffer(poster=poster, timeout_seconds=30.0)

    async def _run() -> None:
        await buf.add(_payload(1, target="discord/1/1/1"))
        await buf.add(_payload(1, target="discord/2/2/2"))
        await buf.add(_payload(2, target="discord/1/1/1"))
        await buf.add(_payload(3, target="discord/1/1/1"))
        await buf.add(_payload(2, target="discord/2/2/2"))
        await buf.add(_payload(3, target="discord/2/2/2"))

    asyncio.run(_run())

    assert len(poster.flushes) == 2
    targets = {flush[0]["target"] for flush in poster.flushes}
    assert targets == {"discord/1/1/1", "discord/2/2/2"}
