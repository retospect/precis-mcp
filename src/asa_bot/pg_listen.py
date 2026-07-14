"""LISTEN on precis.cron + precis.messages.

Dedicated psycopg connection that listens for NOTIFY payloads from
the precis cron-tick worker and the message dispatch path. Hands
each notification off to a queue the bot's dispatcher drains.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import psycopg

log = logging.getLogger(__name__)


class PgListener:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self.cron_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.message_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[Any] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    async def _loop(self) -> None:
        # psycopg's async connection supports LISTEN/NOTIFY in a clean
        # async loop. Auto-reconnect on connection failure.
        while not self._stop_event.is_set():
            try:
                await self._listen_once()
            except Exception:
                log.exception("pg listener crashed; restarting in 5s")
                await asyncio.sleep(5)

    async def _listen_once(self) -> None:
        async with await psycopg.AsyncConnection.connect(
            self._dsn, autocommit=True
        ) as conn:
            await conn.execute('LISTEN "precis.cron"')
            await conn.execute('LISTEN "precis.messages"')
            log.info("LISTENing on precis.cron + precis.messages")
            # Drain notifies while *holding* the connection. ``notifies(
            # timeout=)`` yields whatever arrived, then returns after the idle
            # window without tearing the connection down — the LISTEN stays
            # registered on a stable backend and nothing is missed between
            # windows. The old ``wait_for(gen.__anext__(), timeout=10)``
            # cancelled the notifies generator on every timeout, forcing a
            # reconnect each ~10s (repeated "LISTENing" logs) and dropping any
            # NOTIFY that landed in the gap.
            while not self._stop_event.is_set():
                async for notify in conn.notifies(timeout=10.0):
                    await self._dispatch(notify)
                    if self._stop_event.is_set():
                        break

    async def _dispatch(self, notify: Any) -> None:
        channel = getattr(notify, "channel", None) or str(notify)
        payload = getattr(notify, "payload", None) or ""
        try:
            data = json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            log.warning("notify on %s carried non-JSON payload: %r", channel, payload)
            data = {"raw": payload}
        if channel == "precis.cron":
            await self.cron_queue.put(data)
        elif channel == "precis.messages":
            await self.message_queue.put(data)
        else:
            log.warning("notify on unknown channel %r", channel)
