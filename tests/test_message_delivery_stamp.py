"""Delivery stamp on kind='message' (gr492860): put(id=, status=) and the
asa_bot post path that calls it."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

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


def test_stamp_expired(hub: Hub, store: Store) -> None:
    h, rid = _queue(hub, store)
    h.put(id=rid, status="expired")
    assert _meta(store, rid)["status"] == "expired"


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
from asa_bot.pg_listen import PgListener


class _Precis:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, args: dict[str, Any]) -> str:
        self.calls.append((name, args))
        return ""


class _Chan:
    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc
        self.posts: list[str] = []

    async def send(self, content: str) -> None:
        if self.exc:
            raise self.exc
        self.posts.append(content)


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


def test_recover_recent_messages_once_and_expire_stale() -> None:
    channel = _Chan()
    bot, precis = _bot(channel)
    listener = PgListener("")
    bot._listener = listener
    meta = {"target": "discord/1/2/3", "author": "asa"}
    cursor = SimpleNamespace(
        fetchall=AsyncMock(
            return_value=[(9, meta, True), (8, meta, False), (7, meta, False)]
        )
    )
    conn = AsyncMock()
    conn.execute.return_value = cursor

    async def run() -> None:
        recovered = await listener._recover_messages(conn)
        for ref_id in (9, 8, 7):
            data = listener.message_queue.get_nowait()
            assert data["ref_id"] == ref_id
            await bot._handle_outbound(data)
        # The same row can appear in the sweep and in a buffered NOTIFY,
        # even if delivery completed before the notification was drained.
        await listener._dispatch(
            SimpleNamespace(
                channel="precis.messages", payload=json.dumps({**meta, "ref_id": 8})
            ),
            recovered,
        )
        assert listener.message_queue.empty()

    asyncio.run(run())
    assert channel.posts == ["hi", "hi"]
    assert [(stamp["id"], stamp["status"]) for stamp in _stamps(precis)] == [
        (9, "expired"),
        (8, "sent"),
        (7, "sent"),
    ]


def test_stamp_non_numeric_id_names_the_put_form(hub: Hub, store: Store) -> None:
    """M3 burn-down (gr494831): a non-numeric id refuses with next=."""
    h, _rid = _queue(hub, store)
    with pytest.raises(BadInput) as exc:
        h.put(id="not-a-number", status="sent")
    assert exc.value.next is not None
    assert "put(kind='message', id=" in str(exc.value.next)
