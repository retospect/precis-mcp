"""Buffers multi-part briefing NOTIFY payloads until complete, then flushes.

``briefing.py::_deliver`` queues a long morning brief as N ``message`` refs,
each with its own ``pg_notify('precis.messages', …)`` in one tx. Postgres
delivering one tx's notifies in send order to a serial listener made parts
post in order *today*, but that was never a guarantee: NOTIFY isn't
durable (a listener reconnect between two parts silently drops one), and
nothing pins the consumer (``bot.py::_consume_messages``) to stay serial
forever (gr51556).

``BriefingBuffer`` makes ordering and completeness explicit instead of
incidental. Multi-part payloads carry ``briefing_part``/``briefing_parts``/
``briefing_date``; the buffer holds them keyed by ``(target,
briefing_date)`` until all parts have arrived, then posts 1..N in
ascending order via the injected ``poster`` callable. A per-set timeout
(started on the first part's arrival) posts whatever arrived — in order,
with the missing part numbers logged — if the set is still incomplete
when it fires, so a dropped NOTIFY can't stall delivery forever.

Pure-asyncio (no Discord import), so it's importable and unit-testable
without the ``[asa]`` extra installed. A flush (whether triggered by
completion or by timeout) is idempotent — a set posts exactly once,
guarded by popping it out of ``_sets`` before the ``poster`` call — so
correctness doesn't depend on the consumer staying single-threaded.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

#: How long to wait for the rest of a briefing's parts before posting
#: whatever arrived. ~30s comfortably covers a listener reconnect blip
#: without holding a complete-but-slow delivery back noticeably.
DEFAULT_TIMEOUT_SECONDS = 30.0


@dataclasses.dataclass
class _PartSet:
    total: int
    parts: dict[int, dict[str, Any]] = dataclasses.field(default_factory=dict)


class BriefingBuffer:
    """Buffers briefing parts by ``(target, briefing_date)``; posts in order.

    ``poster`` is awaited with the ordered list of part payloads once a set
    is ready (complete, or timed out with at least one part). Each payload
    is exactly what ``add()`` received — untouched — so the caller decides
    what "posting a payload" means (``bot.py`` reuses its normal single-
    message posting logic per part).
    """

    def __init__(
        self,
        poster: Callable[[list[dict[str, Any]]], Awaitable[None]],
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._poster = poster
        self._timeout_seconds = timeout_seconds
        self._sets: dict[tuple[Any, Any], _PartSet] = {}
        self._timeout_tasks: dict[tuple[Any, Any], asyncio.Task[Any]] = {}

    async def add(self, payload: dict[str, Any]) -> None:
        """Buffer one part's payload; flushes the set once complete.

        Schedules the per-set timeout on the first part of a new
        ``(target, briefing_date)`` key.
        """
        key = (payload.get("target"), payload.get("briefing_date"))
        total: int = payload["briefing_parts"]
        part: int = payload["briefing_part"]
        entry = self._sets.get(key)
        if entry is None:
            entry = _PartSet(total=total or 0)
            self._sets[key] = entry
            self._timeout_tasks[key] = asyncio.create_task(self._timeout_flush(key))
        entry.parts[part] = payload
        if entry.total and len(entry.parts) >= entry.total:
            await self._flush(key)

    async def _timeout_flush(self, key: tuple[Any, Any]) -> None:
        await asyncio.sleep(self._timeout_seconds)
        entry = self._sets.get(key)
        if entry is None:
            return  # already flushed on completion
        missing = [i for i in range(1, entry.total + 1) if i not in entry.parts]
        if missing:
            log.warning(
                "briefing %r: timed out after %.0fs waiting for part(s) %s — "
                "posting %d/%d received",
                key,
                self._timeout_seconds,
                missing,
                len(entry.parts),
                entry.total,
            )
        await self._flush(key)

    async def _flush(self, key: tuple[Any, Any]) -> None:
        # Pop before awaiting the poster so a completion-flush racing a
        # timeout-flush (both land on the event loop) can't post twice —
        # whichever runs first claims the set.
        entry = self._sets.pop(key, None)
        task = self._timeout_tasks.pop(key, None)
        if entry is None:
            return
        # Cancel the pending timeout task on a completion flush — but never
        # self-cancel when this IS the timeout task's own call chain
        # (cancelling ourselves mid-await would raise CancelledError out
        # from under the ``poster`` call below).
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
        ordered = [entry.parts[i] for i in sorted(entry.parts)]
        await self._poster(ordered)


__all__ = ["DEFAULT_TIMEOUT_SECONDS", "BriefingBuffer"]
