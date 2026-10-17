"""Delivery stamp on kind='message' (gr492860): put(id=, status=) and the
asa_bot post path that calls it."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.message import MessageHandler
from precis.store import Store


def _queue(hub: Hub, store: Store) -> tuple[MessageHandler, int]:
    h = MessageHandler(hub=hub)
    h.put(text="hello", target="discord/1/2/3")
    return h, _message_id(store)


def _message_id(store: Store) -> int:
    with store.pool.connection() as conn:
        rows = conn.execute("SELECT ref_id FROM refs WHERE kind='message'").fetchall()
    return int(rows[0][0])


def _meta(store: Store, rid: int) -> dict[str, Any]:
    ref = store.get_ref(kind="message", id=rid)
    assert ref is not None
    return dict(ref.meta or {})


def test_stamp_sent_then_failed_records_reason(hub: Hub, store: Store) -> None:
    h, rid = _queue(hub, store)
    assert _meta(store, rid)["status"] == "queued"
    h.put(id=rid, status="failed", error="Forbidden: 403")
    m = _meta(store, rid)
    assert m["status"] == "failed" and m["error"] == "Forbidden: 403"
    assert m["target"] == "discord/1/2/3"
    h.put(id=rid, status="sent")
    m = _meta(store, rid)
    assert m["status"] == "sent" and not m["error"]


def test_stamp_rejects_bad_status_and_unknown_id(hub: Hub, store: Store) -> None:
    h, rid = _queue(hub, store)
    with pytest.raises(BadInput):
        h.put(id=rid, status="queued")
    with pytest.raises(BadInput):
        h.put(id=rid + 999, status="sent")
    with pytest.raises(BadInput):
        h.put(id=rid, text="edit")


def test_stamp_reaches_handler_through_tool_dispatch(
    runtime_with_store: Any, store: Store
) -> None:
    runtime_with_store.dispatch(
        "put", {"kind": "message", "text": "hi", "target": "discord/1/2/3"}
    )
    rid = _message_id(store)
    runtime_with_store.dispatch(
        "put", {"kind": "message", "id": rid, "status": "failed", "error": "x"}
    )
    m = _meta(store, rid)
    assert m["status"] == "failed" and m["error"] == "x"


pytest.importorskip("discord")

from asa_bot.bot import AsaBot


class _Precis:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, args: dict[str, Any]) -> str:
        self.calls.append((name, args))
        return ""


class _Chan:
    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc

    async def send(self, content: str) -> None:
        if self.exc:
            raise self.exc


def _bot(chan: Any, text: str = "hi") -> tuple[AsaBot, _Precis]:
    bot: Any = object.__new__(AsaBot)
    p = _Precis()
    bot._precis = p

    async def body(_r: Any) -> str:
        return text

    async def chan_for(_s: str) -> Any:
        return chan

    class _D:
        max_message_chars = 1900

    class _C:
        discord = _D()

    bot._cfg = _C()
    bot._fetch_message_body = body
    bot._channel_from_slug = chan_for
    return bot, p


def _stamps(p: _Precis) -> list[dict[str, Any]]:
    return [a for n, a in p.calls if a.get("kind") == "message"]


def test_bot_stamps_sent() -> None:
    bot, p = _bot(_Chan())
    asyncio.run(bot._post_message_ref({"ref_id": 7, "target": "discord/1/2/3"}))
    assert _stamps(p) == [{"kind": "message", "id": 7, "status": "sent"}]


def test_bot_stamps_failed_on_send_error_and_reraises() -> None:
    bot, p = _bot(_Chan(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        asyncio.run(bot._post_message_ref({"ref_id": 8, "target": "discord/1/2/3"}))
    (s,) = _stamps(p)
    assert s["status"] == "failed" and "boom" in s["error"]


def test_bot_stamps_failed_when_no_channel() -> None:
    bot, p = _bot(None)
    asyncio.run(bot._post_message_ref({"ref_id": 9, "target": "discord/1/2/3"}))
    (s,) = _stamps(p)
    assert s["status"] == "failed" and "no channel" in s["error"]
